---
name: enrich-context
description: Capture the business knowledge that is not in the schema -- definitions, default filters, units, canonical tables -- as reviewable rules and examples. Use when answers are plausible but wrong, or when the same correction keeps being made.
allowed-tools: Bash(vanna:*)
---

# Enriching context

The schema says a column is called `status` and holds text. It does not say that
`CANCELLED` orders are excluded from every revenue figure. That gap is where
confidently wrong answers come from, and closing it is what this skill does.

## Two modes. Pick one and say which.

**Grill** — you ask the user one question at a time and write down the answers.
Slower, and the only way to capture knowledge that exists solely in someone's
head.

**Auto-pilot** — you read the project, propose findings in a batch, and the user
approves or rejects. Faster, and limited to what is inferable from data and
code.

State the mode before starting. Switching halfway means the user does not know
whether they are being asked or told.

## Universal rules

**Only add.** Never edit or delete an existing rule. Someone wrote it for a
reason you cannot see.

**Every change is validated.** After each MDL edit, `vanna project validate`.
On failure, revert that single change — not the batch.

**Pre-draft everything.** Show the exact text you propose to write, before
writing it. "Should I add a rule about currency?" is not reviewable; the rule is.

**Say how confident you are, and why.** "High — stated by the user." "Medium —
probed 30 distinct values, meaning inferred." "Low — guessed from the name."

## Finding the gaps

### Structural

- Enum-shaped columns with no stated meaning
- Columns that are nullable where a business rule would forbid it
- `_cents` / `_bps` / `_usd` suffixes with no unit recorded
- Two tables that look like the same thing (`orders`, `orders_v2`)
- Timestamp columns with no stated timezone

### From the data

Probe before asserting. For a candidate enum column:

```sql
SELECT DISTINCT status FROM orders LIMIT 30
```

30 distinct values returned → cardinality is too high, it is not an enum, stop.
Fewer → you have the real values, but *not* their meaning. Ask, or mark low
confidence.

Ask once for permission to run probes, then treat it as granted for the session.

### From what went wrong

Query history is the best source available: a question that was rated negative
is a gap somebody already found for you.

## Where each finding goes

| Finding | Sink |
|---|---|
| What a column means | `description` on the model column |
| A value's meaning | `description`, listing the values |
| A default filter everyone forgets | `knowledge/rules/*.md` |
| A metric definition | a cube measure |
| A question-and-SQL pair worth reusing | `knowledge/sql/<slug>.md` |
| Which of two similar tables is canonical | `knowledge/rules/*.md` |

Rules are markdown with front matter, in the format the instruction store
already reads:

```markdown
---
title: Default filters
scope: global
priority: 20
---

- Exclude `status = 'CANCELLED'` from every revenue figure.
- Exclude accounts where `is_internal` is true from customer counts.
```

Keep rules few and unconditionally true. A rule that is only sometimes right
teaches the model to weigh rules as suggestions.

## Grill mode: questions worth asking

Ask about what you found, not in the abstract. "I see `orders.status` has values
PENDING, SHIPPED, DELIVERED, CANCELLED — which count as revenue?" gets an
answer. "Tell me about your business rules" does not.

1. Which of these tables is the one people actually query?
2. What does this column mean, and what do its values mean?
3. What filter do you always apply and would be annoyed to have to say?
4. Which of these two similar-looking metrics is the real one?
5. What has someone got wrong here before?

Stop when answers start repeating. Ten good rules beat forty hedged ones.

## Finishing

```bash
vanna project validate --level warning
vanna project build
```

Then re-ask a question that was previously wrong, and check that it is now
right. An enrichment session with no before-and-after is a session nobody can
evaluate.
