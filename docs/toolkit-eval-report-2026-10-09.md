# vanna scored with IBM text2sql-eval-toolkit

**Date:** 2026-10-09 · **Branch:** `mlit-hop-join` (uncommitted eval changes, listed under [Files](#files)) · **vanna model:** `gpt-5-2025-08-07` · **Judge:** `gpt-5.4-mini`

## Summary

All 96 suite questions went through the live app from WSL and were scored by the toolkit, alongside a zero-shot baseline (the toolkit's own pipeline, using the same `gpt-5` model).

| | vanna (agent) | gpt-5 zero-shot |
|---|---:|---:|
| Correct (our relaxed scorer) | **83 / 96 (86%)** | **93 / 96 (97%)** |
| Correct (LLM judge) | 81 | 90 |
| Toolkit `subset_non_empty_execution_accuracy` | 65 | 76 |
| No SQL (asked a clarifying question) | 9 | 0 |
| Mean time per question | 70.6 s | 14.2 s |
| Mean prompt / completion tokens | 25.3k / 2.8k | 1.5k / 0.9k |

What the run shows:

- **On this suite the agent is behind a single prompt to the same model: 10 points lower, 5× slower, 16× more prompt tokens.** The baseline sees the whole schema with its foreign keys in a 1.5k-token prompt. Every suite database is small enough for that, so this suite doesn't exercise what vanna adds (search over large schemas, business rules, grants). Even so, the gap needs explaining before the agent is called a net gain. [BIRD](#next-steps) is the fairer test.
- **Clarifications account for 9 of vanna's 13 failures. The same failure was in the October 9 report and hasn't been fixed.** Six of the nine are in `ecommerce`, mostly "should cancelled orders count?", even though each question defines the measure. Re-asked with the first option picked, 8 of the 9 are correct. Asking is also inconsistent: on re-asking, 3 of the 9 got a straight answer with no question.
- **vanna wrote only 2 wrong queries in 96.**
  - **Misleading view:** it trusted `employees.current_dept_emp`, which also returns former employees. This was also in the October 9 report.
  - **Wrong denominator:** on a northwind late-shipment percentage it counted only orders that have both a shipped and a required date. The judge caught it; our row comparison passed it.
- **Two of the 13 failures are the agent answering correctly and then running a check query.** On ecommerce-9 and complex_pagila-3, the first statement was correct. The agent then ran a `COUNT` / `MIN(date), MAX(date)` sanity check, and both scorers read the last statement. If the UI shows the last result, users see the check too.
- **Northwind has no declared foreign keys, and the baseline still scored 15/15 from table and column names.** The inferred joins didn't give vanna a measurable advantage on this suite.
- **The toolkit needed six fixes to give trustworthy numbers.** The worst: it re-renders every query through sqlglot before running it, which broke 3 vanna queries and 3 baseline queries ([details](#toolkit-issues-found)). All six are worked around in the bridge, and the numbers above come from re-scoring with the fixes in place.

## What was tested

| | |
|---|---|
| Instance | Local Docker stack, reached from WSL (`localhost:3000` app, `localhost:5432` Postgres 16). Backend defaults: full schema under 30,000 characters, critic on |
| Questions | `backend/evals/datasets/sql_accuracy/suite.json`: 11 sets, 96 questions, 8 databases. All 96 reference queries were checked first (`--check-gold`); all run and return rows |
| Workspaces | `demo` (chinook, northwind, ecommerce, healthcare, booking, employees, world) and `acme` (pagila) |
| vanna run | `backend/evals/toolkit_bridge.py suite --pipeline-id vanna-gpt5 --workers 3`, as asked. A clarifying question counts as a miss |
| Baseline | Toolkit `LLMSQLGenerationPipeline`, `openai:gpt-5-2025-08-07`, one zero-shot prompt per question with the schema exported from the live databases (`export --target-host`) |
| Judge | Toolkit's calibrated judge prompt (`backend/evals/toolkit_judge_openai.yaml`) on `gpt-5.4-mini`, a different model from the one being judged. It only runs when results don't already match |
| Toolkit | IBM text2sql-eval-toolkit 1.6.0, Python 3.13 venv in WSL (`~/.venvs/t2s-eval`) |

**The scorers.** "Correct" in the summary uses our `sql_accuracy.compare_results`. Each answer was re-run and compared after the toolkit run:

- extra or reordered columns are accepted
- numbers are compared at two decimals
- zero-filled extra rows are tolerated

The toolkit's own metrics are stricter:

- `execution_accuracy` requires the same columns.
- `subset_non_empty_execution_accuracy` allows extra columns, but has no numeric tolerance.
- `logic_execution_accuracy` reruns the query with the reference SELECT list swapped in.
- `bird_execution_accuracy` is BIRD's set-of-tuples match.

## Results

### Per database

| Database | n | vanna ours | vanna judge | vanna subset-EX | vanna no-SQL | baseline ours | baseline judge | vanna mean time | vanna prompt tokens |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| booking | 10 | 9 | 9 | 4 | 1 | 10 | 9 | 64 s | 19.9k |
| chinook | 14 | 14 | 14 | 12 | 0 | 14 | 14 | 66 s | 23.0k |
| ecommerce | 13 | **7** | 7 | 7 | **5** | 13 | 13 | 88 s | 21.1k |
| employees | 9 | 8 | 8 | 5 | 0 | 9 | 9 | 112 s | 28.5k |
| healthcare | 11 | 10 | 10 | 8 | 1 | 11 | 10 | 71 s | 26.3k |
| northwind | 15 | 14 | 14 | 10 | 0 | 15 | 15 | 63 s | 29.9k |
| pagila | 14 | 12 | 12 | 12 | 1 | 11 | 10 | 55 s | 32.4k |
| world | 10 | 9 | 7 | 7 | 1 | 10 | 10 | 58 s | 18.2k |
| **Total** | **96** | **83** | **81** | **65** | **9** | **93** | **90** | **71 s** | **25.3k** |

### By difficulty

| Difficulty | n | vanna ours | baseline ours |
|---|---:|---:|---:|
| easy | 10 | 9 | 10 |
| medium | 33 | 27 | 32 |
| hard | 53 | 47 | 51 |

### Every toolkit metric

| Metric (sum over 96) | vanna | baseline |
|---|---:|---:|
| `execution_accuracy` | 54 | 59 |
| `subset_non_empty_execution_accuracy` | 65 | 76 |
| `bird_execution_accuracy` | 55 | 60 |
| `logic_execution_accuracy` | 72 | 83 |
| `llm_score` | 81 | 90 |
| `is_sqlglot_parsable` | 87 (of 87 with SQL) | 96 |
| Our relaxed match | 83 | 93 |

The toolkit's strict metrics run 18 points (vanna) and 17 points (baseline) below our relaxed match. 11 of those 18 vanna questions use `ROUND()`: vanna rounds percentages and averages to one or two decimals, and the toolkit has no numeric tolerance. The other 7 are extra zero-filled rows ("each continent" including continents with none) and column-shape differences. **Read the toolkit's EX numbers as a lower bound for this suite. Use our relaxed match plus the judge.**

### Skills where vanna dropped questions

`multi_hop` 44/51 (baseline 49), `ratio` 6/8 (8), `having` 5/7 (6), `null_handling` 2/4 (4), `date_arithmetic` 2/3 (3), `many_to_many` 2/3 (3), `subquery_comparison` 2/3 (3), `relational_division` 3/4 (3), `top_n_per_group` 5/6 (5), `temporal_current_row` 5/6 (6), `value_filter` 5/6 (6). Nearly every miss here is a clarification, not a SQL mistake. Skills with no misses for either pipeline include `anti_join` 6/6, `fan_trap` 5/5, `window` 10/11, `time_series` 5/5 and `self_join` 2/2.

## Every vanna failure

| Question | Why it failed | Baseline | Correct with first option picked? |
|---|---|:---:|:---:|
| booking-6 revenue per room type | Asked how to split a multi-room reservation's total | ✓ | ✓ (didn't ask the second time) |
| ecommerce-0 net revenue per category | Asked paid vs delivered vs all orders | ✓ | ✓ |
| ecommerce-2 top product per category | Asked about order status and base product vs variant | ✓ | ✓ |
| ecommerce-7 % revenue from discounted lines | Asked about order status | ✓ | ✗: first option excludes cancelled orders, which narrows the question |
| ecommerce-11 avg order value by country | Asked about order status | ✓ | ✓ (didn't ask the second time) |
| ecommerce-12 variants with stock below 2008 sales | Asked what counts as "sold" | ✓ | ✓ |
| healthcare-1 no-show % per visit type | Asked which appointments the denominator includes | ✓ | ✓ |
| multi_hop_pagila-0 revenue per category | Asked whether to split revenue across a film's categories | ✓ | ✓ (didn't ask the second time) |
| world-7 avg life expectancy per continent | Asked simple vs population-weighted average | ✓ | Logic correct, but rounded to 1 decimal, so 2-decimal compare fails |
| ecommerce-9 users ordering in 2006, 2007 and 2008 | **Answer was correct**, then the agent ran `COUNT(DISTINCT user_id)` as a check; scored on the check | ✓ | n/a |
| complex_pagila-3 avg rental duration per category | **Answer was correct**, then the agent ran `MIN/MAX(rental_date)`; scored on the check | ✓ | n/a |
| employees-7 current employees by gender and department | **Wrong SQL:** used `current_dept_emp`, which includes former employees | ✓ | n/a |
| complex_northwind-2 late-shipment percentage | **Wrong denominator** per the judge: counts only orders with both dates set. Our row compare passed it, so it is not in the 13 counted above | ✓ | n/a |

The baseline's 3 failures:
- **multi_hop_pagila-6:** filtered `'Mary' 'Smith'`, but the data stores `'MARY' 'SMITH'`. It never looks at the data; vanna's value-resolving step handles this.
- **complex_pagila-1:** `UNION ALL` of `payment` and its partitions, which double-counts. vanna handles Pagila's partitions.
- **complex_pagila-2:** compared against `COUNT(*) FROM store`, and there are more than 2 stores.

These are exactly what vanna's profiling and catalog are for, and they are the only places in this suite where vanna beat the baseline.

## vanna issues found

1. **It asks clarifying questions when the question already defines the measure (9/96, 6 in ecommerce).** The October 9 report recommended tightening the clarify step in `vanna/core/system_prompt/analyst.py`; that hasn't landed. Fixing it is worth about 8 points on this suite. The decision to ask also varies between runs (3 of 9 didn't ask a second time), so a single run's clarification rate has a lot of variance.
2. **It runs check queries after answering, and the last statement isn't the answer.** In 9 turns the agent ran more than one statement. In 2 of them a check came last; multi_hop_northwind-4 ran 8 statements. Either keep the agent from querying after its answer, or record which statement is the answer (a flag on the generation row) so the UI and both scorers can use it.
3. **The `employees.current_dept_emp` problem is still there.** It needs a table annotation or a grant-level note saying that view includes former employees.
4. **Generation history is missing its retrieval columns.** `RecordingRunSqlTool._record` (`backend/vanna_app/platform.py:166`) never sets `retrieved_table_names`, `retrieval_strategy` or `retrieved_example_ids`. They are empty for all 87 statements in this run. `vanna_app/lineage.py` reads `retrieved_table_names`, and `sql_accuracy.py` reports `strategies` from `retrieval_strategy`, so both report nothing. It also stops triage from separating retrieval misses from reasoning errors.
5. **Cost and latency.** 70.6 s and 28k tokens per question, against 14 s and 2.5k for one prompt to the same model. Two requests ran past 300 s with 3 concurrent workers (both returned in 105–168 s when retried alone). 79 of 87 queries carry inline `--` comments, which adds completion tokens. Rounding to 1–2 decimals reads well but costs points on any exact-match benchmark.

## Toolkit issues found

All six are handled in `backend/evals/toolkit_bridge.py`, except the shared-memory one, which needs a database setting.

| Issue | Effect | Handling |
|---|---|---|
| Postgres predictions are re-rendered by sqlglot (`quote_mixed_case_columns`) and the result overwrites `predicted_sql` | `100.0 * a / b` became `CAST(… AS DOUBLE PRECISION)`, and Postgres has no `ROUND(double precision, int)`. That broke 3 vanna and 3 baseline queries; 183 of 187 stored queries had been rewritten | Turned off for `vanna_*` benchmarks (lower-case schemas); BIRD keeps it. Run restored from `vanna_app.generations` and re-scored |
| Judge summary contains numpy `int64` | `json.dump` crashed after every verdict had already been paid for | numpy-safe `json` swapped into the evaluation module |
| matplotlib picks the Tk backend under WSLg; charts are drawn from worker threads | Process killed ("Illegal instruction") while scoring the last database | `MPLBACKEND=Agg` |
| Markdown reports written in the locale encoding | Crash on Windows (cp1252 can't encode the emoji) | Bridge re-runs itself with `-X utf8` on Windows |
| Baseline pipeline doesn't create its `results/` directory | `FileNotFoundError` after generating | Create it first |
| Toolkit runs queries 8 at a time against the Docker Postgres | `could not resize shared memory segment … No space left on device` (64 MB `/dev/shm`), so a reference query failed | Scored with `--db-threads 2`; give `db_postgres` a larger `shm_size` |

**The judge disagrees with our scorer on 4 of 22 vanna mismatches and 3 of 20 baseline mismatches.**
- It caught one real error that row comparison missed (complex_northwind-2).
- It also penalised correct answers:
  - for unqualified table names that `search_path` resolves (baseline booking-8)
  - for listing zero rows for continents with no speakers (world-3)
- On world-6 it claims a different set of countries, while our compare says the results match. That one needs a human look.

Treat judge "No" verdicts as prompts for review, not ground truth.

## Test suite

Run on the same branch after the eval changes:

| Suite | Result |
|---|---|
| Unit (`pytest -m "not integration"`) | **1609 passed**, 205 skipped, 0 failed |
| Integration (`pytest -m integration`, `VANNA_TEST_DATABASE_URL` = Docker Postgres via the WSL IP) | **408 passed**, 1 skipped, 3 failed |
| Gold check (`sql_accuracy.py --check-gold --suite`) | 96 / 96 reference queries run and return rows |

The 3 integration failures:

- `test_datasource_health.py::TestTheResultIsRemembered::test_a_successful_check_is_recorded` and `::test_recovery_clears_the_error`: **environmental.** The tests hard-code `postgresql://postgres:postgres123@127.0.0.1:5432`. On this Windows machine `127.0.0.1:5432` is a native Postgres 17 with a different password, not the Docker one, so the "working" source fails its health check.
- `test_chat_multitenancy.py::TestChatCrossWorkspaceAccess::test_a_forged_workspace_header_does_not_grant_a_chat_in_it[chat_poll]`: **a real behaviour bug** (not caused by this branch's eval changes). A forged `x-tenant-id` sent to `chat_poll` returns **HTTP 500**. `TenantDispatchChatHandler._delegate` (`vanna_app/wiring.py:485`) raises the `PermissionError` from `resolve_user` before `Agent.send_message` can turn it into an in-band error, and the route logs it as an unhandled failure. Access is still denied, so nothing leaks, and the same test passes for `chat_sse`. It should return 403, or the in-band error the test expects.

## Next steps

1. Fix the clarify step and re-run the suite: `make eval-toolkit` with a new `EVAL_PIPELINE` id. The toolkit dashboard (`TEXT2SQL_DATA_ROOT=.evals/toolkit text2sql-eval-dashboard --mode full`) shows question by question what changed between the two runs.
2. Populate `retrieved_table_names` / `retrieval_strategy` in `RecordingRunSqlTool._record`, then triage retrieval misses.
3. Record which statement is the answer (or keep the agent from running checks after answering), and score that statement.
4. Run **BIRD mini-dev (Postgres)**: load the dump into `db_postgres`, register a `bird` workspace, then `predict` and `score` `bird_mini_dev_postgres_test_50` with and without `--with-evidence`. Its 11 databases in one schema push vanna into search mode, which this suite never does. That is where the agent should beat a one-prompt baseline.
5. Give `db_postgres` `shm_size: 256mb` in the sandbox compose file.
6. Map `PermissionError` from tenant resolution in the chat dispatcher to a 403, or to the in-band error the other chat transports return.

## Reproducing

From WSL, with the toolkit venv (`uv venv --python 3.13 ~/.venvs/t2s-eval && uv pip install -e ../text2sql-eval-toolkit httpx`):

```bash
H=postgresql://postgres:postgres123@localhost:5432
python backend/evals/toolkit_bridge.py export --target-host $H --toolkit ../text2sql-eval-toolkit
python backend/evals/toolkit_bridge.py suite --pipeline-id vanna-gpt5 --target-host $H \
  --token "$VANNA_API_TOKEN" --app-db $H/vanna_app --db-threads 2 \
  --judge backend/evals/toolkit_judge_openai.yaml \
  --workspace chinook=demo:postgresql://db_postgres/chinook ... --workspace pagila=acme:postgresql://db_postgres/pagila
```

Quota: the run needs about 100 questions on `demo`. `VANNA_DAILY_QUOTA` is 200 per workspace per day, so it was raised to 1000 for the run and set back afterwards.

Outputs, in `.evals/` (git-ignored):
- `toolkit/results/vanna_<db>-predictions_eval{.json,_summary.md,_errors.md}`
- `toolkit/analysis.json`: every question, both pipelines, every metric, the judge's explanation and our verdict
- `toolkit/clarified_followup.json`
- `toolkit/logs/`

## Files

- `backend/evals/toolkit_bridge.py` (new): export, predict, score and suite for the toolkit, plus the workarounds above
- `backend/evals/sql_accuracy.py`: shared `generate()` / `Generation` used by both scorers; also reads `error` and `retrieved_table_names`
- `backend/evals/toolkit_judge.yaml` (Claude) and `backend/evals/toolkit_judge_openai.yaml` (OpenAI) (new)
- `backend/tests/test_toolkit_bridge.py` (new, 16 tests)
- `Makefile`: `eval-toolkit-export` and `eval-toolkit` targets
- `.gitignore`: `.evals/`
