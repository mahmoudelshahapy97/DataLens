"""``vanna ask`` -- wrap a question in the context needed to answer it.

The default mode makes **no LLM call**. It prints a prompt: the question, plus
the schema, rules and examples the project actually has. That output pipes into
whatever agent the user already runs, which means Vanna does not need to be the
thing holding an API key, and the same command works for Claude Code, Cursor, a
shell script, or a person reading it.

``--run`` answers in-process, because Vanna has the whole agent locally and
withholding that would be artificial.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import click

from ..project import Project
from ..semantic import describe_manifest, load_built_manifest

_TEMPLATE = """\
You are answering a question against a database using the Vanna CLI.

Follow this sequence:
  1. Read the context below. Do not query anything not named in it.
  2. Write SQL against the {noun} names shown -- not the physical tables{physical_note}.
  3. Check non-trivial SQL: vanna project validate
  4. Run it: vanna query --sql '...'
  5. Answer in plain language, stating any caveat the tool reported.

Rules:
  - Never invent a column. If it is not below, it does not exist.
  - Never call NOW() or CURRENT_DATE; pin literal dates so the query is
    reproducible tomorrow.
  - If a fan-out warning appears, say so in the answer -- the total may be
    inflated by a one-to-many join.

{context}

Question: {question}
"""


def _gather_context(project: Optional[Project]) -> tuple:
    """The schema description and whether it is semantic."""
    if project is None:
        return "(No project found. Run `vanna project init`.)", False

    manifest = load_built_manifest(project.paths)
    if manifest is not None and manifest.models:
        return describe_manifest(manifest), True

    return (
        "(No semantic layer built. Run `vanna project build`, or scan the "
        "database so the catalog can be shown here.)",
        False,
    )


def _rules(project: Optional[Project]) -> str:
    """Business rules from the project's knowledge directory."""
    if project is None:
        return ""
    rules_dir = project.paths.knowledge_dir / "rules"
    if not rules_dir.is_dir():
        return ""

    blocks = []
    for path in sorted(rules_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        if text:
            blocks.append(text)
    return "\n\n".join(blocks)


def _examples(project: Optional[Project], limit: int = 5) -> str:
    """A few question/SQL pairs, as few-shot material."""
    if project is None:
        return ""
    sql_dir = project.paths.knowledge_dir / "sql"
    if not sql_dir.is_dir():
        return ""

    blocks = []
    for path in sorted(sql_dir.glob("*.md"))[:limit]:
        blocks.append(path.read_text(encoding="utf-8").strip())
    return "\n\n".join(blocks)


@click.command()
@click.argument("question")
@click.option(
    "--path", type=click.Path(path_type=Path), default=None, help="Project directory."
)
@click.option(
    "--run",
    is_flag=True,
    help="Answer in-process instead of printing a prompt. Needs an LLM API key.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable output.")
def ask(question: str, path: Optional[Path], run: bool, as_json: bool) -> None:
    """Wrap a question in this project's context.

    By default this only *shapes* the question -- no LLM is called, nothing is
    executed. Pipe it into your agent:

        vanna ask "revenue by region last quarter" | claude
    """
    project = Project.find(path)
    schema, is_semantic = _gather_context(project)

    sections = [f"## Available data\n\n{schema}"]

    rules = _rules(project)
    if rules:
        sections.append(f"## Business rules (authoritative)\n\n{rules}")

    examples = _examples(project)
    if examples:
        sections.append(f"## Worked examples\n\n{examples}")

    prompt = _TEMPLATE.format(
        noun="model" if is_semantic else "table",
        physical_note=(
            " (the physical tables are deliberately hidden)" if is_semantic else ""
        ),
        context="\n\n".join(sections),
        question=question,
    )

    if run:
        raise click.ClickException(
            "--run needs an assembled agent, which this project does not build "
            "from the CLI yet. Pipe the prompt into your agent instead:\n"
            f"  vanna ask {question!r} | <your agent>"
        )

    if as_json:
        click.echo(
            json.dumps(
                {
                    "question": question,
                    "prompt": prompt,
                    "semantic": is_semantic,
                    "project": str(project.root) if project else None,
                },
                indent=2,
            )
        )
        return

    click.echo(prompt)
