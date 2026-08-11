"""``vanna project`` -- scaffolding and inspecting a project directory."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from ..config import ProfileStore
from ..project import Project, created_paths, init_project
from ..semantic import (
    Severity,
    build_manifest,
    has_errors,
    load_manifest_from_project,
    validate_manifest,
)


@click.group()
def project() -> None:
    """Create and inspect Vanna projects."""


@project.command("init")
@click.argument("name")
@click.option(
    "--path",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory to create the project in (default: ./<name>).",
)
@click.option("--dialect", default="sqlite", help="SQL dialect models compile to.")
@click.option("--profile", "profile_name", default=None, help="Connection profile to pin.")
@click.option(
    "--empty",
    is_flag=True,
    help=(
        "Skip the example model and rule. Use this when an agent will populate "
        "the project: scaffolded placeholders get carried into real projects."
    ),
)
@click.option("--force", is_flag=True, help="Overwrite an existing project file.")
def init(
    name: str,
    path: Optional[Path],
    dialect: str,
    profile_name: Optional[str],
    empty: bool,
    force: bool,
) -> None:
    """Create a project directory."""
    root = path or Path.cwd() / name

    if profile_name:
        # Fail now rather than at the first query: a project pinned to a
        # profile that does not exist is a confusing thing to discover later.
        ProfileStore().get(profile_name)

    created = init_project(
        root, name, dialect=dialect, profile=profile_name, empty=empty, force=force
    )

    click.secho(f"Created project {name!r} in {created.root}", fg="green")
    for relative in created_paths(created):
        click.echo(f"  {relative}")

    click.echo("\nNext:")
    if not profile_name:
        click.echo("  vanna profile add <name> --dialect <dialect> --set ...")
    click.echo("  vanna project show")


@project.command("show")
@click.option(
    "--path", type=click.Path(path_type=Path), default=None, help="Project directory."
)
def show(path: Optional[Path]) -> None:
    """Summarise the project found here or above."""
    found = Project.load(path)
    config, paths = found.config, found.paths

    click.echo(f"{click.style(config.name, bold=True)}")
    click.echo(f"  root          {paths.root}")
    click.echo(f"  dialect       {config.dialect}")
    click.echo(f"  profile       {config.profile or '(none pinned)'}")
    click.echo(f"  fanout guard  {config.fanout_guard}")

    models = paths.model_dirs()
    click.echo(f"  models        {len(models)}")
    for model_dir in models[:10]:
        click.echo(f"      {model_dir.name}")
    if len(models) > 10:
        click.echo(f"      ... {len(models) - 10} more")

    click.echo(f"  views         {len(paths.view_files())}")
    click.echo(f"  cubes         {len(paths.cube_files())}")

    knowledge = paths.knowledge_dir
    examples = list((knowledge / "sql").glob("*.md")) if knowledge.is_dir() else []
    rules = list((knowledge / "rules").glob("*.md")) if knowledge.is_dir() else []
    click.echo(f"  knowledge     {len(examples)} examples, {len(rules)} rules")

    manifest = paths.manifest_file
    click.echo(
        f"  built         {'yes -- ' + str(manifest) if manifest.is_file() else 'no'}"
    )


@project.command("validate")
@click.option("--path", type=click.Path(path_type=Path), default=None)
@click.option(
    "--level",
    type=click.Choice([Severity.ERROR, Severity.WARNING, Severity.STRICT]),
    default=Severity.WARNING,
    help="Lowest severity to report. Errors are always shown.",
)
def validate(path: Optional[Path], level: str) -> None:
    """Check the semantic layer for problems.

    Everything reported here would otherwise surface as a compiler error several
    steps removed from the line that caused it.
    """
    found = Project.load(path)
    manifest = load_manifest_from_project(found.paths, dialect=found.config.dialect)
    issues = validate_manifest(manifest, level=level)

    if not issues:
        click.secho(
            f"OK: {len(manifest.models)} models, "
            f"{len(manifest.relationships)} relationships, "
            f"{len(manifest.cubes)} cubes.",
            fg="green",
        )
        return

    colours = {Severity.ERROR: "red", Severity.WARNING: "yellow", Severity.STRICT: "cyan"}
    for issue in issues:
        click.secho(f"{issue.severity:<8} {issue.message}", fg=colours.get(issue.severity))
        if issue.where:
            click.echo(f"         in {issue.where}")
        if issue.hint:
            click.echo(f"         -> {issue.hint}")

    errors = sum(1 for i in issues if i.severity == Severity.ERROR)
    click.echo()
    click.secho(
        f"{errors} error(s), {len(issues) - errors} other finding(s).",
        fg="red" if errors else "yellow",
    )
    if errors:
        raise SystemExit(1)


@project.command("build")
@click.option("--path", type=click.Path(path_type=Path), default=None)
@click.option(
    "--skip-validation",
    is_flag=True,
    help="Write the manifest even if it has errors. For debugging only.",
)
def build(path: Optional[Path], skip_validation: bool) -> None:
    """Compile the YAML tree to target/mdl.json."""
    found = Project.load(path)
    manifest = load_manifest_from_project(found.paths, dialect=found.config.dialect)

    issues = validate_manifest(manifest, level=Severity.ERROR)
    if has_errors(issues) and not skip_validation:
        # Writing a manifest known to be broken means the next command fails
        # somewhere less obvious, against a file that looks freshly built.
        for issue in issues:
            click.secho(f"error    {issue.message}", fg="red")
            if issue.where:
                click.echo(f"         in {issue.where}")
        click.secho("\nNot written. Fix the errors, or pass --skip-validation.", fg="red")
        raise SystemExit(1)

    target = build_manifest(found.paths, dialect=found.config.dialect)
    click.secho(
        f"Built {target} "
        f"({len(manifest.models)} models, {len(manifest.cubes)} cubes).",
        fg="green",
    )


@project.command("from-catalog")
@click.option("--path", type=click.Path(path_type=Path), default=None)
@click.option(
    "--catalog",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help="Scanned catalog JSON (default: the project's target/catalog.json).",
)
@click.option("--overwrite", is_flag=True, help="Replace models that already exist.")
def from_catalog(path: Optional[Path], catalog: Optional[Path], overwrite: bool) -> None:
    """Draft models and relationships from a scanned catalog.

    The output is exactly as smart as the database schema and no smarter. Its
    value is that the next step is renaming and describing, rather than learning
    a file format from scratch.
    """
    import json

    from ..capabilities.schema_catalog.models import (
        RelationshipMetadata,
        TableMetadata,
    )
    from ..semantic import manifest_from_catalog, write_model_yaml, write_relationships_yaml

    found = Project.load(path)
    source = catalog or found.paths.catalog_file
    if not source.is_file():
        raise click.ClickException(
            f"No catalog at {source}. Scan the database first "
            "(POST /api/vanna/v2/schema/rescan, or SchemaScanner in code)."
        )

    raw = json.loads(source.read_text(encoding="utf-8"))
    # LocalSchemaCatalog stores {"tables": [...], "relationships": [...]}, but a
    # bare list of tables is a reasonable thing for someone to hand us.
    tables_raw = raw.get("tables", raw) if isinstance(raw, dict) else raw
    rels_raw = raw.get("relationships", []) if isinstance(raw, dict) else []

    tables = [TableMetadata.model_validate(t) for t in tables_raw]
    relationships = [RelationshipMetadata.model_validate(r) for r in rels_raw]

    manifest = manifest_from_catalog(tables, relationships)

    existing = {d.name.lower() for d in found.paths.model_dirs()}
    written, skipped = 0, 0
    for model in manifest.models:
        if model.name.lower() in existing and not overwrite:
            skipped += 1
            continue
        write_model_yaml(found.paths, model)
        written += 1

    if manifest.relationships and (overwrite or not found.paths.relationships_file.exists()
                                   or _relationships_are_empty(found.paths)):
        write_relationships_yaml(found.paths, manifest.relationships)

    click.secho(f"Wrote {written} model(s).", fg="green")
    if skipped:
        click.echo(f"Skipped {skipped} that already exist (use --overwrite).")
    click.echo("\nNext: edit models/, then `vanna project validate && vanna project build`.")


def _relationships_are_empty(paths) -> bool:
    """Whether relationships.yml is still the scaffolded placeholder."""
    import yaml

    try:
        raw = yaml.safe_load(paths.relationships_file.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    return not (raw.get("relationships") or [])
