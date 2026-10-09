# Agent upgrade — evidence from the running stack

**19 September 2026.** Six new tools, an explicit turn graph, a self-check
stage, and four bugs found by pointing the whole thing at a live deployment.

Everything below was captured from `http://localhost:3000` against Postgres,
MySQL and Oracle containers, with a real model answering real questions. The
screenshots are the browser, not mock-ups.

Reproduce with the stack up:

```bash
python backend/tools/capture_upgrade_evidence.py
```

---

## The headline

**The chat could not answer a single question before this work.** Not
degraded — zero. Every request died on:

```
openai.BadRequestError: Unsupported parameter: 'max_tokens' is not
supported with this model. Use 'max_completion_tokens' instead.
```

The default model is `gpt-5`, which renamed that parameter. `AgentConfig.max_tokens`
defaults to 4096 and is forwarded on every request, so there was no question
that could get through. It surfaced in the UI as "Something went wrong while
answering that."

---

## What the screenshots show

| File | What it proves |
|---|---|
| `01-workspace.png` | Signed in, chat ready. |
| `02-multi-hop-join.png` | `suggest_joins` → `profile_column` ×2 → `validate_sql` → `run_sql`, 6/6 tasks. Iron Maiden $138.60, U2 $105.93, Metallica $90.09. |
| `03-clarification.png` | `request_clarification`: four clickable options, "Waiting for your reply", composer reading "Choose an option, or rephrase your question…". |
| `04-trend.png` | `analyze_timeseries` over 60 months: +8.4%, biggest moves, hedged seasonality. |
| `05-datasources.png` | Four databases across three engines, all **OK**. |
| `06-databases.png` | Two workspaces on two different databases. |

### The multi-hop case

`artist` and `invoice_line` share no key. The path is
`invoice_line → track → album → artist`, and no single table's schema shows it —
this is the guess that produces a query that runs and a number that is wrong.

`suggest_joins` found the path; `profile_column` then profiled `unit_price` and
`quantity` — the two columns about to be multiplied — before any SQL was written.

Verified against ground truth:

```sql
SELECT ar.name, ROUND(SUM(il.unit_price*il.quantity)::numeric,2) AS revenue
FROM chinook.invoice_line il
JOIN chinook.track  t  ON t.track_id  = il.track_id
JOIN chinook.album  al ON al.album_id = t.album_id
JOIN chinook.artist ar ON ar.artist_id = al.artist_id
GROUP BY ar.name ORDER BY revenue DESC LIMIT 3;

 Iron Maiden | 138.60
 U2          | 105.93
 Metallica   |  90.09
```

The agent's answer matches to the cent.

### The clarification case

"Show me our best customers" is ambiguous in a way that changes the answer.
Rather than guessing, the agent offered four self-contained questions, each
carrying its full text as the button's action. The long one truncates in the
label and keeps the whole question in the action — as designed.

The turn ends there. No further model call is made, because
`ToolResult.metadata[END_TURN]` stops the loop; otherwise the model receives
"I asked the user X" and, told never to end a turn silently, answers its own
question.

Clicking through gave the correct specific answer (Helena Holý, $49.62).

---

## Test matrix

Nine questions, four databases, two workspaces. Raw data in `test-results.json`.

| Case | Engine | Tools | Result |
|---|---|---|---|
| How many albums? | PG chinook | `validate_sql`, `run_sql` | 347 ✅ |
| Top 3 artists by revenue | PG chinook | `suggest_joins`, `profile_column`, `validate_sql`, `run_sql` | verified exact ✅ |
| Revenue trend? | PG chinook | `profile_column`, `validate_sql`, `analyze_timeseries` | +8.4% over 60 months ✅ |
| "Best customers" | PG chinook | `request_clarification` | asked, did not guess ✅ |
| How many films? | MySQL sakila | `validate_sql`, `run_sql` | 1,000 ✅ |
| Top 3 actors | MySQL sakila | `suggest_joins`, `profile_column`, `validate_sql`, `run_sql` | answered ✅ |
| Orders + customers | PG northwind | `validate_sql`, `run_sql` | 830 / 91 ✅ |
| What tables? | Oracle XEPDB1 | `search_tables` | listed ✅ |
| How many films? | acme / pagila | `run_sql` | 1,000, separate workspace ✅ |

All six new tools fired in production.

---

## Bugs found and fixed

### 1. Total outage on `gpt-5` — `max_tokens`

`_token_limit_parameter()` in `vanna/integrations/openai/llm.py` now picks the
spelling by model family (`gpt-5`, `o1`, `o3`, `o4` → `max_completion_tokens`;
everything else unchanged). An unknown model keeps the old name, so a wrong
guess degrades rather than breaks. 18 tests in
`tests/test_openai_token_parameter.py`.

### 2. Oracle could not be registered at all — `SELECT 1`

The connection probe hardcoded `SELECT 1` in three places. Valid in every engine
here **except** Oracle, which answers `ORA-00923: FROM keyword not found`. So the
console rejected every Oracle database, reachable ones included.

The database was always fine — `SELECT 1 FROM DUAL` worked first try. Added
`BaseSqlRunner.health_check_sql`, overridden to `SELECT 1 FROM DUAL` in
`OracleRunner`, and used at all three call sites. `05-datasources.png` shows
Oracle **OK**. 8 tests in `tests/test_health_check_sql.py`, one of which fails
if a fourth hardcoded `SELECT 1` ever appears.

### 3. SQL Server's error sent operators the wrong way

`pyodbc` 5.3.0 **is** installed; `import pyodbc` fails on `libodbc.so.2`. The old
message said "Install with: pip install pyodbc" — advice already followed. It now
names the real cause: the unixODBC system library, which pip cannot supply.

**Still not working.** Making it work means adding `unixodbc` and Microsoft's
`msodbcsql18` to the image, and `requirements.in:49` says that was excluded
deliberately — while `requirements.txt` pins `pyodbc` anyway. Those two files
disagree, and reconciling them is a call for whoever owns the image.

### 4. `VANNA_MAX_TENANT_RUNTIMES` documented as `32`, actually `2`

A 16× error on the setting that governs exactly the multi-database case. With
2 workspaces × 4 databases, **4 runtime evictions across 9 questions** — roughly
half paid a full rebuild: reconnect, rebuild the semantic layer, re-read the
catalog. README corrected, with the sizing guidance that was missing.

### Also noticed, not fixed

- `backend/tests/e2e/` drives `#si-email` / `#si-password` / `#si-go`. The app
  renders `#login-email` / `#login-password` and a bare submit button. Those e2e
  tests cannot pass against the current frontend.
- `CardComponentRenderer` interpolates `title` and `content` into `innerHTML`
  unescaped. Model-generated text — which can carry warehouse data — reaches the
  DOM as markup. Pre-existing, worth a look.

---

## Caveats

- **The container is hot-patched.** Fixes went in via `docker cp`. They are in
  the source tree and tested, but the *image* still has the broken code —
  `docker compose build backend` before any rebuild or restart.
- **The critic is on and has never rejected anything.** It approved every answer
  in this run, which is correct: every answer was right. Its value against wrong
  answers is unmeasured, and measuring it needs an A/B over `qa.json` with real
  spend.
- **`acme` workspace and four data sources were created for this test** and are
  still registered.
