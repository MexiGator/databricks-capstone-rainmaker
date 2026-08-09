# Rainmaker v0.1 — Relationship Engagement module

Additive, isolated feature layer on top of the submittable Rainmaker. Two
capabilities: a **relationship_score** (warmth of the bond) and **forecast-
triggered Proactive Care** (help before the hazard, not chase after it). Goal:
use relationship engagement to put inspections on the calendar without spending
trust we can't get back.

## Isolation guarantee (why this can't break your submission)
- Every file is **new** and lives under `relationship_v0/`. No existing file is edited.
- The schema is **new tables only** (`contact_relationship`, `care_content`, `care_sends`) — no `ALTER` on graded tables.
- The Flask routes are a **blueprint registered behind `ENABLE_RELATIONSHIP_V0=1`** — dark by default.
- Rollback = don't set the flag; optionally `DROP TABLE` the three new tables.

## What's already done and verified (pure logic, 41 passing tests)
| File | Role |
|---|---|
| `scoring.py` | `relationship_score` (0–100) + tier, from CRM/engagement signals. Tunable `WEIGHTS`. |
| `policy.py` | warmth × trigger × consent/frequency → next best action + CTA strength. |
| `care_content.py` | 6 seed care guides + deterministic selection + message composer. |
| `forecast_scan.py` | proactive-event filter + contact matching (pure core; NWS IO isolated). |
| `pipeline.py` | ties it together → the ranked care queue (`build_care_queue`). |

Run it: `python run_local_demo.py` · Test it: `pytest relationship_v0/tests -q`

## What Claude Code wires into the repo
| File | TODO |
|---|---|
| `db.py` | swap `_connect()` for the repo's `lakebase.get_conn()`; provide the CRM×engagement join for `recompute_all`. |
| `care_agent_tool.py` | register `run_care_tool` as the 4th Agent Bricks tool; default the injected callbacks to `db.*`. |
| `routes.py` | fill `_load_contacts` (customers table) and reuse `weather_client.harvest` for forecast events. |
| `schema_relationship.sql` | run once against Lakebase; then seed + embed `care_content`. |

See `CLAUDE_CODE_BUILD_BRIEF.md` for the full spec, integration steps, acceptance
criteria, build order, and the paste-ready kickoff prompt.
