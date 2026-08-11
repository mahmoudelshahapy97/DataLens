---
name: onboarding
description: Set up Vanna against a database and answer a first question. Use when someone asks to connect Vanna to their data, configure a new project, or get started.
allowed-tools: Bash(vanna:*)
---

# Onboarding

Take a user from nothing to an answered question.

## Hard rules

**One step per exchange.** Run a command, read its output, report, then take the
next step. Batching commands means a failure at step 2 is discovered after step
5 has already written files.

**Never ask for a password in the conversation.** Credentials go in a `.env`
file the user edits themselves. Profiles reference them as `${VAR}`. You should
never see the value, and `vanna profile add` refuses a literal credential.

**Never invent a setting name.** If unsure what a dialect needs, ask the user
rather than guessing a key that will silently do nothing.

**Do not query before the catalog is scanned.** Without it the model has no
schema and will invent table names.

## Steps

### 1. Check the install

```bash
vanna --version
```

Absent → `pip install vanna`. A dialect needs its driver:
`pip install "vanna[postgres]"`.

### 2. Decide: their database, or the demo

Ask which. The demo is a bundled SQLite database — good for showing the shape of
the thing in two minutes, useless for their actual questions.

### 3. Create the project

```bash
vanna project init <name> --dialect <dialect>
cd <name>
```

Add `--empty` when you are going to populate `models/` yourself: the scaffolded
`example` model is something you would otherwise carry into a real project.

### 4. Set up credentials

Tell the user to put secrets in `.env` at the project root (already gitignored):

```
PGPASSWORD=...
```

Then create the profile referencing them:

```bash
vanna profile add <name> --dialect postgres \
  --set 'dsn=postgresql://user:${PGPASSWORD}@host:5432/dbname' \
  --activate
```

Confirm it resolves — this prints provenance, never values:

```bash
vanna profile debug <name>
```

### 5. Scan the schema

The catalog is what the model is shown. It records real column values for
low-cardinality columns, which is what stops the agent guessing filter literals.

### 6. Ask something

Start with a question whose answer the user can check by eye. A first answer
that is plausible but unverifiable teaches nobody anything.

## Where to go next

| They want | Skill |
|---|---|
| Business names, metrics, joins that compile | `generate-mdl` |
| To capture rules and definitions they keep repeating | `enrich-context` |
| To know whether answers are actually right | `evaluate` |

## When something fails

- **Cannot connect** → `vanna profile debug`. It names the missing variable.
- **Agent invents table names** → the catalog is empty. Scan first.
- **Answers use the wrong column** → the schema is ambiguous. `enrich-context`.
