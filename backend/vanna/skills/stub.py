"""The discovery stub installed into an agent client.

Deliberately tiny and deliberately empty of content. It carries only the
*trigger phrases* that should route a request to Vanna, and the two commands
that fetch everything else. All real guidance stays in the wheel, so upgrading
the CLI upgrades the instructions -- there is no cached copy to go stale, and no
version of the guide that disagrees with the version of the tool.
"""

from __future__ import annotations

_STUB = """---
name: vanna
description: >-
  Query a database in natural language, and manage the semantic layer that makes
  the answers trustworthy. Use for: asking questions of a database; writing or
  debugging SQL against a warehouse; "how many", "what was revenue",
  "show me the top"; connecting a new database; defining metrics, models or
  business rules; restricting which rows or columns a user can see; checking
  whether generated SQL is correct.
allowed-tools: Bash(vanna:*)
---

# Vanna

A database, queried in natural language, with a reviewable semantic layer
underneath.

## Find the right workflow first

```bash
vanna skills list              # what guides exist
vanna skills get <name>        # the guide itself
vanna skills get <name> --full # ...with its reference material
```

Do this before improvising. The guides encode failure modes -- fan-out on
one-to-many joins, fail-closed access rules, credentials that must never reach
the conversation -- that are not obvious from the command help.

## Everyday commands

```bash
vanna ask "<question>"         # shape a question with the project's context
vanna ask "<question>" --run   # ...and answer it in-process
vanna project show             # what this project declares
vanna project validate         # check the semantic layer
vanna profile debug            # why is it connecting there?
```

## Rules

- Never ask for a password in conversation. Credentials live in `.env`, and
  profiles reference them as `${VAR}`.
- Never invent a column or model name. Check with `vanna project show`.
- Run `vanna project validate` after editing anything under `models/`.
"""


def render_discovery_stub() -> str:
    """The stub file's contents, for writing into an agent client."""
    return _STUB
