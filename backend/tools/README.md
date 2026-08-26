# `backend/tools/`

Standalone scripts. Nothing in `vanna/` or `vanna_app/` imports them, and they are
excluded from the backend image — they are run by hand, or by a `make` target.

Two groups, and the difference matters.

**They talk to the database directly.** These import `vanna_app` (they put
`backend/` on `sys.path`), so they need `backend/requirements.txt` installed and
`VANNA_APP_DATABASE_URL` set.

| Script | What it does | `make` |
|---|---|---|
| `seed_database.py` | Migrate, import the configuration, then re-export `database/`. One command for a fresh control plane. | `seed-config` |
| `export_sql_schema.py` | Writes `database/sql_schema.sql` and `database/seed/configuration.sql` back out from the control plane. **These two files are generated — regenerate them after adding a migration.** | `export-schema` |
| `import_config_files.py` | Loads the YAML under `backend/` (projects, instructions, domains) into the `config_files` catalog. Checksum-gated. | |

**They drive the running app over HTTP.** Nothing is inserted behind the app's
back, so every limit, permission check and audit entry applies exactly as it does
to a person. They need a running stack and an account, not a database URL.

| Script | What it does | `make` |
|---|---|---|
| `seed_demo_data.py` | Fills a workspace with realistic content, so no screen is empty. | `seed` |
| `seed_dashboards.py` | Builds dashboards per workspace, from each workspace's own catalog. | |
| `seed_reports.py` | Fifty parameterised reports, six or seven per seeded workspace. | |
| `ask_demo_questions.py` | Asks real questions through the chat, so the history screen has real Q&A. | `seed-questions`, `seed-writes` |
| `prune_chinook_seed.py` | Removes ad-hoc instructions and starter questions that drift from `backend/domains/domains.yml`. | |
| `enable_domain_packs.py` | Opts each workspace into the starter-library pack that suits its domain. | `enable-packs`, `revoke-packs` |
| `retire_wrong_rules.py` | Switches off workspace rules naming columns the database does not have. | `retire-wrong-rules` |
| `grant_demo_writes.py` | Grants write access on a few small tables, so the write flow can be exercised. | `grant-writes`, `revoke-writes` |
| `provision_role_accounts.py` | Creates the four QA accounts the role screenshot run signs in as. | `role-accounts` |
| `export_qa.py` | Exports question/answer/query/database history as `qa.json`. | `qa-json` |
| `record_demo.py` | Records a walkthrough of the running app as video, into `artifacts/video/`. | `demo-video` |
| `load_probe.py` | Concurrent load against a running stack, with the observed connection count beside it. | |

## Front-end work without a stack

`mock_component_backend.py` is a standalone FastAPI mock that streams every rich
component — no database, no LLM key, no control plane. It serves
`test-comprehensive.html` at `/` and mounts `frontend/dist/assets` at `/static`,
which is the path that page asks for, so build the front end first:

```
cd frontend && npm run build
pip install -r backend/tools/requirements.txt
python backend/tools/mock_component_backend.py
```

## `plotly_export_bundle/`

The esbuild inputs for the custom Plotly build that exported dashboards embed —
`backend/vanna/dashboards/vendor/plotly-export.min.{js,css}`. Rebuilt by
`make plotly-bundle`, which also re-copies `tile-figure.js` from
`frontend/public/assets/shared/` into the same `vendor/` directory. The backend
image carries no Node, which is why both are committed rather than built at
image-build time.
