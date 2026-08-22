"""The parts of a dbt project that are not schema.

``dbt.py`` imports dbt's *structure* -- tables, columns, descriptions, join
paths from ``relationships`` tests, enumerations from ``accepted_values``. That
covers the catalog. Three further things a dbt project already knows are useful
to an agent and were being left on the floor:

* **The connection.** ``profiles.yml`` names the warehouse dbt itself runs
  against. Re-typing those details into a connection profile is both tedious and
  a chance to point at the wrong database.
* **Which tests failed.** ``run_results.json`` says whether the constraints in
  the project are actually holding. A ``not_null`` test that is currently failing
  means the column really does contain nulls, and an agent told the column is
  never null will write SQL that quietly drops rows.
* **The model graph.** Which models exist, and which are downstream of many
  others, is a decent first guess at what people ask about -- enough to seed a
  set of starter questions rather than facing a new user with an empty box.

Nothing here writes to the catalog; each function returns plain data for the
caller to store, so importing one part does not commit you to the others.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# profiles.yml -> connection details
# ----------------------------------------------------------------------

#: dbt's adapter names, mapped onto this project's engine names. Only the
#: adapters with a runner here appear; anything else is reported rather than
#: guessed at, since a wrong mapping produces a connection that fails obscurely.
ADAPTERS = {
    "postgres": "postgres",
    "redshift": "postgres",   # wire-compatible enough for a read-only connection
    "mysql": "mysql",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "duckdb": "duckdb",
    "sqlserver": "mssql",
    "synapse": "mssql",
    "oracle": "oracle",
    "clickhouse": "clickhouse",
    "trino": "presto",
    "presto": "presto",
    "spark": "hive",
}


def read_profiles(
    profiles_path: Path, *, profile: Optional[str] = None, target: Optional[str] = None
) -> Tuple[Dict[str, Any], List[str]]:
    """Read connection fields out of a dbt ``profiles.yml``.

    Args:
        profiles_path: Path to ``profiles.yml``.
        profile: Which profile to read. Defaults to the only one, or errors.
        target: Which target within it. Defaults to the profile's own ``target``.

    Returns:
        ``(fields, warnings)`` where *fields* is shaped for
        ``vanna.core.datasource.Engine.url`` -- so it can be handed straight to a
        connection profile.

    **Passwords are not copied.** dbt profiles routinely use
    ``{{ env_var('DBT_PASSWORD') }}``, and a literal password sitting in the file
    is a credential we should not duplicate into a second file. The field is left
    empty and named in the warnings.
    """
    warnings: List[str] = []
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise ImportError(
            "Reading profiles.yml needs PyYAML: pip install PyYAML"
        ) from exc

    document = yaml.safe_load(Path(profiles_path).read_text(encoding="utf-8")) or {}
    document.pop("config", None)   # dbt's own settings, not a profile

    if not document:
        return {}, ["profiles.yml is empty."]

    if profile is None:
        names = list(document)
        if len(names) != 1:
            return {}, [
                f"profiles.yml defines {len(names)} profiles ({', '.join(names)}); "
                "name one with --profile."
            ]
        profile = names[0]

    block = document.get(profile)
    if not isinstance(block, dict):
        return {}, [f"No profile named {profile!r} in profiles.yml."]

    outputs = block.get("outputs") or {}
    target = target or block.get("target")
    if target not in outputs:
        return {}, [
            f"Profile {profile!r} has no target {target!r}. "
            f"Available: {', '.join(outputs) or 'none'}."
        ]

    output = outputs[target] or {}
    adapter = str(output.get("type") or "").lower()
    engine = ADAPTERS.get(adapter)
    if engine is None:
        return {}, [
            f"dbt adapter {adapter!r} has no equivalent engine here. "
            "Configure the connection by hand."
        ]

    fields: Dict[str, Any] = {"engine": engine}

    def take(source_key: str, target_key: str) -> None:
        value = output.get(source_key)
        if value in (None, ""):
            return
        text = str(value)
        # A Jinja template is dbt's, not ours; copying it verbatim would produce
        # a connection string containing "{{ env_var(...) }}".
        if "{{" in text:
            warnings.append(
                f"{source_key} is a dbt template ({text.strip()}); "
                "set it yourself."
            )
            return
        fields[target_key] = text

    take("host", "host")
    take("server", "host")
    take("port", "port")
    take("dbname", "database")
    take("database", "database")
    take("user", "username")
    take("schema", "schema")
    take("account", "account")
    take("warehouse", "warehouse")
    take("role", "role")
    take("project", "project")
    take("dataset", "dataset")
    take("path", "path")
    take("catalog", "catalog")

    if output.get("password"):
        warnings.append(
            "The password was not copied. Set it on the profile, ideally as an "
            "environment variable rather than a literal."
        )

    logger.info(
        "Read dbt profile %s/%s (%s -> %s)", profile, target, adapter, engine
    )
    return fields, warnings


# ----------------------------------------------------------------------
# run_results.json -> instructions
# ----------------------------------------------------------------------


def instructions_from_run_results(
    run_results_path: Path, manifest_path: Optional[Path] = None
) -> List[str]:
    """Turn failing dbt tests into instructions the agent should be told.

    A passing test is not worth saying -- it only confirms what the schema
    already implies. A *failing* one contradicts the schema, and that is exactly
    what an agent needs warning about: told a column is unique when it is not, it
    will happily write a join that multiplies rows.
    """
    path = Path(run_results_path)
    if not path.exists():
        return []

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not parse %s: %s", path, exc)
        return []

    names: Dict[str, str] = {}
    if manifest_path and Path(manifest_path).exists():
        try:
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            for unique_id, node in (manifest.get("nodes") or {}).items():
                if isinstance(node, dict):
                    names[unique_id] = str(node.get("name") or unique_id)
        except Exception:  # noqa: BLE001 - names are a nicety
            pass

    instructions: List[str] = []
    for result in document.get("results") or []:
        if not isinstance(result, dict):
            continue
        status = str(result.get("status") or "").lower()
        if status in ("pass", "success", "skipped"):
            continue
        unique_id = str(result.get("unique_id") or "")
        if ".test." not in unique_id and not unique_id.startswith("test."):
            continue

        name = names.get(unique_id, unique_id.split(".")[-1])
        failures = result.get("failures")
        count = f" ({failures} row(s))" if failures else ""
        instructions.append(
            f"The dbt test `{name}` is currently failing{count}. Do not rely on "
            "the constraint it checks; verify it in the data before assuming it."
        )

    if instructions:
        logger.info("%s failing dbt test(s) became instructions", len(instructions))
    return instructions


# ----------------------------------------------------------------------
# manifest.json -> seed questions
# ----------------------------------------------------------------------


def starter_questions(manifest_path: Path, limit: int = 8) -> List[str]:
    """Draft starter questions from the dbt model graph.

    Ranked by how many other models depend on a model: the most-referenced ones
    are the project's shared facts and dimensions, which is a far better guess at
    what people will ask about than alphabetical order.

    These are **drafts**. They are phrased from model and column names, so they
    are only as natural as those are -- the intent is that someone edits them,
    not that they ship as written.
    """
    path = Path(manifest_path)
    if not path.exists():
        return []

    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not parse %s: %s", path, exc)
        return []

    nodes = {
        unique_id: node
        for unique_id, node in (manifest.get("nodes") or {}).items()
        if isinstance(node, dict) and node.get("resource_type") == "model"
    }

    references: Dict[str, int] = dict.fromkeys(nodes, 0)
    for node in nodes.values():
        for upstream in (node.get("depends_on") or {}).get("nodes") or []:
            if upstream in references:
                references[upstream] += 1

    ranked = sorted(nodes, key=lambda uid: (-references[uid], uid))

    questions: List[str] = []
    for unique_id in ranked:
        node = nodes[unique_id]
        name = str(node.get("name") or "")
        if not name:
            continue
        label = name.replace("_", " ")

        columns = node.get("columns") or {}
        # A date-ish column turns "how many" into a far more useful question.
        date_column = next(
            (
                c
                for c in columns
                if any(k in c.lower() for k in ("date", "_at", "day", "month"))
            ),
            None,
        )
        category = next(
            (
                c
                for c in columns
                if any(
                    k in c.lower()
                    for k in ("status", "type", "category", "region", "country", "tier")
                )
            ),
            None,
        )

        if date_column:
            questions.append(f"How has {label} changed over the last 12 months?")
        elif category:
            questions.append(f"Break down {label} by {category.replace('_', ' ')}.")
        else:
            questions.append(f"How many rows are in {label}, and what does it contain?")

        if len(questions) >= limit:
            break

    return questions
