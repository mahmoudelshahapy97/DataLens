# SQL accuracy evaluation: schema graph and expanded eval suite

**Date:** 2026-10-09 · **Branch:** `mlit-hop-join` (last commit `dbe1c0b`, plus the uncommitted eval changes listed under [Files](#files)) · **Model:** `gpt-5-2025-08-07` (provider `auto` chose OpenAI)

## Summary

The agent answered **82 of 96 questions correctly as asked (85%)**. When its clarifying questions were answered, it got **92 of 96 (96%)**.

- **What costs accuracy is the clarify step, not SQL.** The agent stopped to ask a clarifying question on 11 of 96 questions. Six of those were in `ecommerce`, mostly asking whether cancelled or pending orders should count, even when the question defined the measure exactly. Once a reading was picked, nearly all of them passed.
- **Wrong SQL was rare.** Of the four questions that still fail, only one is a genuine SQL error. The agent trusted a view whose name misleads: `employees.current_dept_emp` returns former employees too.
- **Hard questions do well.** Hard questions scored 48 of 53 as asked. These include anti-joins, top-N per group, fan traps, relational division, and tables that reference each other. Medium questions scored lower (25 of 33) because most of the clarifications fell on them.
- **Inferred joins work.** Northwind declares no foreign keys. Every question about it was answered correctly (15 of 15) using only joins the scanner inferred.
- **Bridge tables don't change accuracy.** A separate experiment added bridge tables to the prompt when only part of the schema fits. Accuracy was the same with and without them, because the agent finds missing tables with its own tools. Bridges cut prompt tokens by about 10%.

## What was tested

| | |
|---|---|
| Instance | Local Docker stack: `vanna-backend` rebuilt from this branch, Postgres 16 control plane and warehouses |
| Scorer | `backend/evals/sql_accuracy.py --suite` |
| Datasets | 11 sets, 96 questions, 8 databases, listed in `backend/evals/datasets/sql_accuracy/suite.json` |
| Settings | Backend defaults: full schema when under 30,000 characters, search limit 15, up to 5 bridge tables, critic on |
| Workspaces | `demo` (chinook, northwind, plus the five eval databases registered for this run) and `acme` (pagila) |

**How answers are scored.** Each question is sent to the running app through `chat_poll`. The SQL the agent actually ran is read from `vanna_app.generations`. That SQL and the reference SQL are both run against the warehouse, and their results are compared:

- **Exact match:** same columns, same rows, row order ignored.
- **Relaxed match** (the headline number): it also accepts extra or reordered columns, and extra rows that only show a zero or empty value, such as "each store" listing stores with 0 rentals.

Numbers are compared at two decimals using SQL-style rounding.

**How clarifications are handled.** `request_clarification` ends the turn with several complete questions to choose from. "As asked" counts that as a miss. "Clarifications answered" re-ran those questions with `--answer-clarifications`, which picks the first option and continues the same conversation.

## Results

### Per dataset

| Dataset | Questions | As asked | Clarifications answered | Clarified | Mean prompt tokens | Mean time |
|---|---:|---:|---:|---:|---:|---:|
| multi_hop_chinook | 8 | 7 | 8 | 1 | 22.5k | 52 s |
| complex_chinook | 6 | 6 | 6 | 0 | 19.4k | 40 s |
| multi_hop_northwind | 9 | 9 | 9 | 0 | 25.7k | 48 s |
| complex_northwind | 6 | 6 | 6 | 0 | 23.8k | 50 s |
| multi_hop_pagila | 8 | 7 | 8 | 0 | 28.9k | 51 s |
| complex_pagila | 6 | 5 | 6 | 1 | 21.9k | 50 s |
| ecommerce | 13 | 7 | 10 | 5 | 18.0k | 42 s |
| healthcare | 11 | 10 | 11 | 1 | 18.4k | 45 s |
| booking | 10 | 8 | 10 | 2 | 18.1k | 34 s |
| employees | 9 | 8 | 8 | 0 | 25.8k | 81 s |
| world | 10 | 9 | 10 | 1 | 16.0k | 43 s |
| **Total** | **96** | **82 (85%)** | **92 (96%)** | **11** | **21.3k** | **48 s** |

### By difficulty

| Difficulty | Questions | As asked | Clarifications answered |
|---|---:|---:|---:|
| easy | 10 | 9 | 10 |
| medium | 33 | 25 | 32 |
| hard | 53 | 48 | 50 |

### By skill (skills with at least 3 questions)

| Skill | Questions | As asked | Clarifications answered |
|---|---:|---:|---:|
| multi_hop | 51 | 44 | 48 |
| window | 11 | 10 | 11 |
| ratio | 8 | 5 | 7 |
| having | 7 | 5 | 6 |
| anti_join | 6 | 6 | 6 |
| top_n_per_group | 6 | 6 | 6 |
| value_filter | 6 | 5 | 6 |
| temporal_current_row | 6 | 5 | 5 |
| fan_trap | 5 | 5 | 5 |
| time_series | 5 | 4 | 5 |
| relational_division | 4 | 2 | 3 |
| null_handling | 4 | 3 | 4 |
| date_arithmetic | 3 | 2 | 3 |
| subquery_comparison | 3 | 2 | 2 |
| circular_reference | 3 | 3 | 3 |
| many_to_many | 3 | 2 | 3 |
| temporal_history | 3 | 3 | 3 |

## Failure analysis

### Unnecessary clarifications (11 questions)

The agent ended the turn to ask instead of answering. Typical cases:

- **ecommerce (5).** Asked whether to count only paid or delivered orders. The questions defined the measure ("sum of order line totals") and said nothing about status.
- **chinook.** "Customers based in Brazil": customer country or billing country. That's a fair question, since both columns exist.
- **booking.** "Revenue per room type": how to split a reservation that covers several rooms. Every reservation in this data has exactly one room.
- **healthcare, world, pagila (one each).** Asked about which statuses to include, time windows, or which stores counted.

Once a reading was picked, 9 of these 11 passed. In the other two (ecommerce "discounted share" and "stock below 2008 sales"), the first option the agent offered narrowed the question to delivered or paid orders. They then answered a different question, which is an artefact of always picking the first option.

### Still failing with clarifications answered (4 questions)

| Question | Cause | Category |
|---|---|---|
| employees: current headcount by gender and department | Used the view `employees.current_dept_emp`. Despite its name, it returns every employee's latest department, including people who have left (300,024 rows against 240,124 actually current). | **Genuine SQL error** (a trap in the data) |
| ecommerce: share of revenue from discounted lines | The first clarification option limited it to delivered or paid orders | Caused by picking the first option |
| ecommerce: variants with stock below 2008 sales | Same: the first option limited it to delivered orders | Caused by picking the first option |
| ecommerce: users who ordered in every year 2006–2008 | Paged its 60-row answer with `LIMIT 30 OFFSET 30`, so the final statement was page 2. An earlier statement in the turn had the full answer. | Scorer reads the last statement |

## Earlier experiments in this session

### Bridge tables in search mode

Search mode was forced (`VANNA_SCHEMA_FULL_TEXT_THRESHOLD=0`) with a window of 4 tables, and bridge tables were turned off or on, on the 25 multi-hop questions:

| Mode | Correct (relaxed) | Mean prompt tokens (chinook / northwind / pagila) |
|---|---:|---|
| Full schema | 24/25 | 24.0k / 26.3k / 52.4k* |
| Search window 4, bridges off | 22/25 | 30.4k / 28.3k / 39.2k |
| Search window 4, bridges on | 21/25 | 27.3k / 25.6k / 37.5k |

\* Before the partition fix below.

Offline, on the live catalogs, bridges increased how many of the tables each reference query needs actually reached the prompt: Pagila 13→18 of 33, Northwind 18→23 of 28, Chinook 20→25 of 30. They didn't change accuracy, because the agent uses `search_tables`, `get_table_schema` and `suggest_joins` to find what the prompt leaves out. So bridges save tokens; they don't add accuracy.

### Inferred joins (Northwind, no declared foreign keys)

The scanner inferred 10 joins, and none were wrong. Nine were confirmed against a sample of the data with zero orphan rows (score 0.95). The tenth comes from an empty table, so it keeps its name-based score of 0.85, which is above the threshold for use. It misses three joins whose column names don't correspond: `employeeterritory.employeeid → employee.empid`, `employee.mgrid` (references its own table), and `customercustomerdemographic.customerid → customer.custid`.

### Pagila partitions

The 54 partitions of `payment` were scanned as separate tables, and the parent table had no join edges. After the fix (below), Pagila's catalog went from 78 tables to 23 and its full-schema prompt from **52.4k to 32.4k tokens** per question, with the same accuracy (7/8).

## Bugs found and fixed while testing

1. **Read grants didn't apply to the schema prompt.** `GrantFilteredCatalog.get_context` ran against the unfiltered catalog, so tables and columns a role hadn't been granted were described to the model. (Execution was still blocked by the SQL policy.) Fixed in `vanna_app/read_guard.py`.
2. **The Rescan button wrote to the wrong place.** `POST /schema/rescan` filed every scan under data source `default`, so the catalog the agent reads never changed. Fixed in `vanna_app/routes/workspace.py`.
3. **Postgres partitions were scanned as tables** (see above). Fixed in `schema_catalog/scanner.py`.
4. **Relationships to tables that no longer exist were still rendered**, for example 36 foreign keys of dropped partitions. Fixed in `schema_catalog/base.py`.
5. **The review panel showed only the workspace's default database.** It now follows the database selected on screen. Fixed in `vanna_app/routes/catalog.py`.
6. **Inferred joins carried the "inferred" caveat twice** in the prompt. Fixed in `schema_catalog/inference.py`.
7. **Scorer problems:**
   - rounding didn't match SQL `ROUND`;
   - it scored a trailing sanity-check query, which the new `relaxed_any_statement` metric now exposes;
   - zero-filled groups were counted as wrong answers;
   - clarifications showed up as "agent ran no SQL". They are now detected, reported as `clarification_rate`, and can be answered.
8. **Gold-data problem:** 34 of 40 reference queries in `qa.json` were broken by a one-line export that kept `--` comments. `tools/export_qa.py` is fixed, but `qa.json` must be re-exported.

Each fix has a regression test. The backend suite passes: 1619 passed, 587 skipped (Postgres integration tests run separately: 1885 passed).

## Recommendations

1. **Make the clarify step stricter** (largest gain, roughly 10 points). In `vanna/core/system_prompt/analyst.py`, have the agent answer with a stated assumption unless the question genuinely can't be answered. Status filters and time windows the question doesn't mention should default to "all".
2. **Describe `employees.current_dept_emp`** on the Schema page. For example: "Latest department per employee, including former employees; for current staff use `dept_emp.to_date = '9999-01-01'`." This is the one real SQL error, and an annotation is the intended fix.
3. **Keep bridge tables on for large schemas** for the token savings. Don't expect them to raise accuracy while the agent has schema tools.
4. **Re-export `qa.json`** (`make qa-json`) and have someone review its 40 reference queries. They're the agent's own past answers, not verified ground truth.
5. **Re-run the suite after any prompt or retrieval change.** It's the baseline for regressions, about 40 minutes with three workers.

## Known issues not addressed

- Three Postgres integration tests fail, and failed before this work too (checked on `da7714c`):
  - `test_chat_multitenancy::...forged_workspace_header...` returns 500 instead of a refusal;
  - two `test_datasource_health` tests.
- `database/sql_schema.sql` is out of date (it lacks migrations 0018 and 0019). Regenerate it with `tools/export_sql_schema.py`.
- On this machine a native Windows PostgreSQL 17 service holds `localhost:5432`. Tools running on Windows must reach the Docker Postgres through the WSL IP.

## Reproducing

Check the reference answers (no agent, no model calls):

```bash
python backend/evals/sql_accuracy.py --suite backend/evals/datasets/sql_accuracy/suite.json \
    --target-host postgresql://USER:PASS@HOST:5432 --check-gold
```

Score the agent (three databases shown; add one `--workspace` per database):

```bash
python backend/evals/sql_accuracy.py --suite backend/evals/datasets/sql_accuracy/suite.json \
    --target-host postgresql://USER:PASS@HOST:5432 \
    --token "$VANNA_API_TOKEN" --app-db "$VANNA_APP_DATABASE_URL" \
    --workspace chinook=demo:postgresql://db_postgres/chinook \
    --workspace northwind=demo:postgresql://db_postgres/northwind \
    --workspace pagila=acme:postgresql://db_postgres/pagila \
    --label baseline            # add --answer-clarifications to score past the clarify step
```

Reports are written to `backend/evals/results/`, which git ignores. Each run produces one JSON file per dataset plus a suite total, with per-difficulty and per-skill breakdowns.

## Environment state after the run

- The five eval databases (`ecommerce`, `healthcare`, `booking`, `employees`, `world`) are still registered under the `demo` workspace, labelled `Eval …`, and have been scanned. Remove them from the console if they shouldn't stay.
- The eval API tokens are revoked. The backend runs with default settings.
- The run used about 110 of the day's question quota across `demo` and `acme`.

## Files

Uncommitted at the time of writing:
- `backend/evals/sql_accuracy.py`
- `backend/evals/datasets/sql_accuracy/` (eight new datasets plus `suite.json`)
- `backend/tests/test_sql_accuracy.py`, `test_relationship_inference.py`, `test_schema_context_graph.py`
- the scanner, catalog and retrieval changes listed above
- `docker-compose.yml`, `.env.example`
