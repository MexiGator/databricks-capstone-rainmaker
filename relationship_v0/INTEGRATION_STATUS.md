# Rainmaker v0.1 — Relationship Layer: integration status

Isolated, additive, **dark by default**. With `ENABLE_RELATIONSHIP_V0` unset the
app is byte-for-byte the graded build — the only edit to any existing file is a
15-line, flag-gated blueprint registration appended to `app/app.py`.

## Build status (done + verified locally)

| Brief step | What was wired | Verified |
|---|---|---|
| 1. Land package | `relationship_v0/` in repo; `contact_id` reconciled `BIGINT→TEXT` to soft-link `customers.customer_id` | ✅ |
| 2. `db._connect` | → `lakebase.connect()` (repo helper), cwd-safe sys.path bootstrap | ✅ import |
| 3. Seed + embed | `pipeline/embed_care_content.py` (clone of `embed_corpus.py`; same MiniLM/HF-cache) | ✅ compile + `guide_text` |
| 4. `_load_contacts` + flag-gate | loads `customers` + relationship overlay; blueprint gated in `app.py` | ✅ Flask test client |
| 5. 4th agent tool | `agent/care_tool.py::send_proactive_care_tip` + `record_care_reply` handoff | ✅ orchestration paths |
| 6. Care console UI | warmth badges + Proactive Care panel at `GET /care` | ✅ browser screenshot |
| 7. CDF + care-lift | `pipeline/care_rollup.py` (Delta CDF mirror + booking-lift) | ✅ funnel + lift logic in local Spark |

**Tests:** `pytest relationship_v0/tests -q` → **71 passed** (41 original unit
tests + 30 new care-message eval cases: retrieval hit-rate, grounding, refusal).
Graded suite `pytest tests -q` → **184 passed**, unchanged.

## To light it up (these run in the Databricks workspace)

These need a live Lakebase/Databricks and can't be run off-workspace:

1. **Apply the schema** (needs *Enable Lakebase Search* / pgvector on the instance):
   ```bash
   psql "$LAKEBASE_URL" -f relationship_v0/schema_relationship.sql
   ```
2. **Seed + embed the care corpus** (downloads MiniLM to `/tmp/hf`):
   ```python
   import os; os.environ["LAKEBASE_URL"] = "..."   # or run inside the app env
   import embed_care_content; embed_care_content.run()
   ```
3. **Deploy the app with the flag on**: set `ENABLE_RELATIONSHIP_V0=1` in the
   app env (keep the Secret resource key `lakebase-url`). Open **`/care`** for the
   Proactive Care console; the JSON endpoints are `/care/forecast-scan`,
   `/care/queue`, `/care/approve-send`, `/contacts/<id>/relationship`,
   `/relationship/recompute`.
4. **Register the 4th Agent Bricks tool** — see `agent/AGENT_BRICKS_TOOL.md`
   (`send_proactive_care_tip`, schema in `agent.care_tool.TOOL_SPEC`).
5. **After a care demo pass**, compute the funnel + headline metric:
   ```python
   import care_rollup; care_rollup.run()
   ```
   Reads `care_gold_funnel` (queued→…→booked) and `care_gold_lift`
   (care-touched vs non-care-touched booking rate + lift).

## Rollback
Unset `ENABLE_RELATIONSHIP_V0` (instant). Optionally `DROP TABLE`
`contact_relationship, care_content, care_sends, care_cdf_checkpoint,
care_cdf_audit, care_gold_funnel, care_gold_lift` — the graded app is untouched.

## Isolation guarantee
- Only existing-file edit: `app/app.py` (+15 lines, flag-gated). `git diff`
  shows nothing else changed.
- New tables only; **no `ALTER`** on graded tables. The rollup uses its **own**
  `care_cdf_checkpoint` / `care_cdf_audit` (never the graded `cdf_*` tables) and
  reads `opportunities`/`customers` without writing them.
