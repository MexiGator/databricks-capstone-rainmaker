# Rainmaker

**A weather-driven demand engine for home-services companies.**

When a hailstorm crosses Tarrant County, roofers find out the same way everyone
else does — a customer calls. The companies that book the work are the ones that
reached out first. Today that means someone with a laptop, a radar app, and a
spreadsheet, working the phones until the storm's news cycle passes.

Rainmaker watches live National Weather Service alerts, matches each storm
footprint against a CRM of customers and prospects, ranks who is most likely to
have damage worth fixing, drafts outreach grounded in the campaigns that
actually booked jobs before, and — when someone replies yes — books the
inspection.

Built for the Databricks *Rise of the AI Data Engineer* boot camp capstone.

---

## Architecture

```
  api.weather.gov                     ┌──────────────────────┐
  (live NWS alerts) ──── poll ───────▶│  weather_events      │
                                      │  (Lakebase/Postgres) │
  synthetic CRM ──── seed ───────────▶│  customers           │
  past campaigns ─── seed ───────────▶│  outreach_templates  │
                                      └──────────┬───────────┘
                                                 │
                          ┌──────────────────────┴────────────────┐
                          │                                       │
                   ┌──────▼───────┐                    ┌──────────▼─────────┐
                   │ Match & Score│                    │  Embed corpus      │
                   │   (Spark)    │                    │  MiniLM → pgvector │
                   └──────┬───────┘                    └──────────┬─────────┘
                          │                                       │
                   ┌──────▼────────────────────────────┐          │
                   │  opportunities  (ranked queue)    │          │
                   └──────┬────────────────────────────┘          │
                          │                                       │
        ┌─────────────────▼─────────────────┐                     │
        │  Agent — 3 tools                  │◀── retrieval ───────┘
        │  1 draft_outreach       (RAG)     │
        │  2 send_and_create_lead (action)  │
        │  3 handle_reply_and_book (action) │
        └─────────────────┬─────────────────┘
                          │
        ┌─────────────────▼─────────────────┐    ┌────────────────────────┐
        │  Storm Response console (Flask)   │    │  Delta + Change Data   │
        │  queue · drafts · Ask · Results   │◀───│  Feed → gold_results   │
        └───────────────────────────────────┘    └────────────────────────┘
```

## Requirements coverage

| # | Requirement | Where it lives |
|---|---|---|
| 1 | Spark data pipeline | `pipeline/match_and_score.py` — joins weather × service map × customers, scores exposure, writes Delta |
| 2 | Real third-party API | `pipeline/weather_client.py` — live NWS active alerts. Load-bearing: no alerts, no product |
| 3 | Unstructured data | `pipeline/embed_corpus.py` — NWS narrative prose + campaign copy, chunked and embedded to pgvector |
| 4 | Interactive app | `app/` — two tabs, agent actions, Ask bar with a grounding panel |
| 5 | Agent, ≥2 action tools | `agent/tools.py` — four tools, all with real database side-effects |
| 6 | Change data capture → Delta | `pipeline/gold_rollup.py` — reads `readChangeFeed`, rolls up funnel and revenue |

## Layout

```
db/          schema.sql · lakebase.py · seed.py
pipeline/    weather_client · poll_weather · scoring · match_and_score
             embed_corpus · gold_rollup
agent/       retrieval · classify · tools · simulate · llm
app/         app.py · templates/index.html · app.yaml
tests/       91 tests, no network required
```

## Setup

**1. Lakebase.** Create an instance, enable native password auth, add a role,
copy the connection URL. Turn on **Enable Lakebase Search** — without pgvector
nothing retrieval-related works, and the failure is silent.

**2. Secret.** Store the full URL in scope `database`, key `lakebase-url`.

**3. Schema and data.** In a notebook:

```python
import os; os.environ["LAKEBASE_URL"] = "postgresql://..."
import lakebase, seed
lakebase.ensure_schema()      # idempotent
seed.run()                    # 61 customers, 17 event mappings, 7 campaigns

import poll_weather;    poll_weather.run()      # live NWS alerts
import match_and_score; match_and_score.run()   # Spark scoring
import embed_corpus;    embed_corpus.run()      # vector index
```

**4. App.** Deploy `app/` as a Databricks App. Add a Secret resource with
resource key **exactly** `lakebase-url` — it must match `valueFrom` in
`app.yaml`, or the app boots with `LAKEBASE_URL is not set`.

**5. After a demo pass**, run `gold_rollup.run()` to compute the Results tab
from the change feed.

## Safety first — and it's a gate, not a slogan

The first message Rainmaker ever sends anyone is a **storm safety notice**:
official NWS guidance for the alert they're in, verbatim, with no ask attached.
Commercial outreach is blocked until it has gone.

**Safety content makes zero LLM calls.** An LLM inventing storm safety advice is
one hallucination away from telling someone to go outside during a tornado
warning. The notice is composed from the NWS `instruction` field verbatim —
public domain, so quoting it in full is both legal and correct — plus a curated
property checklist keyed by service line and hazard. It also means the notice
still sends when the model endpoint is down.

The gate (`safety.commercial_allowed`) has two rules: a safety notice must have
gone first, and no selling during an *active* Extreme-severity event. Safety
notices deliberately do **not** advance the opportunity status, so they never
inflate the funnel.

## How scoring works

```
exposure = severity × proximity × urgency × value        →  0..1
```

Multiplicative, not additive, so any factor at zero kills the row. A platinum
customer 800 km from the hail is not an opportunity, and an additive score
would still rank them near the top.

- **severity** — the NWS severity field (Extreme → Minor)
- **proximity** — 1.0 at the footprint centre, 0.5 at its edge, decaying to 0
  over the next 40 km. Not a hard cutoff: a house 5 km outside a hail polygon
  is far likelier to have damage than one 200 km away
- **urgency** — from `event_service_map`; a freeze warning drives plumbing at
  0.95 and roofing at 0.55
- **value** — normalised job value plus a loyalty bonus

Constants live at the top of `pipeline/scoring.py` and are covered by tests, so
tuning is a data change rather than a code change.

## Retrieval

One vector index over both corpora — NWS narratives and campaign copy — because
the headline question ("given this week's storms, who do I prioritise and what
should I say?") needs to retrieve across both in a single search.

Two things make this real RAG rather than decoration:

1. **The corpus answers questions the model cannot.** "What wording books hail
   jobs?" is answerable only from *your* campaigns and *your* booked rates.
   Delete the corpus and the answer gets worse — that is the test.
2. **Refusal happens before the model is called.** If nothing clears the
   similarity floor, `ask()` returns a refusal without invoking the LLM.
   Handing a model empty context and hoping it declines is not a guardrail.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q          # 91 passed
```

No test touches the network — the NWS tests run against fixture GeoJSON. A
suite that needs the internet is a suite that fails during a demo.

What's covered, and why those things:

- **Scoring** — that proximity vetoes value, that priority bands are total,
  that `opportunity_id` is stable so re-running never duplicates a row
- **Reply classification** — this function decides whether a booking row gets
  written. Opt-out is a hard stop before any scoring, because *"sounds great,
  but stop texting me"* reads enthusiastic to a naive scorer and texting them
  again is a compliance problem, not a missed sale
- **The simulator** — 300 seeded replies are run through the real classifier to
  assert the seeded intent matches. Without it the demo's booked count could
  quietly be a lie
- **The NWS client** — malformed alerts are skipped, never raised; statewide
  polygons get a capped radius so one alert cannot sweep in the whole customer
  list

## What is real and what is simulated

Stated plainly, because the distinction matters more than the demo looking good.

**Real:** NWS alerts and their narrative text. The Spark scoring. The vector
index and every retrieval. All three agent tools and their database writes. The
Delta change feed and the rollup computed from it.

**Synthetic:** the CRM. 61 customers and prospects across six regional tenants,
placed where their hazard actually occurs. Real customer data cannot be used.

**Simulated:** inbound customer replies. Production would use Twilio inbound
webhooks; `agent/simulate.py` stands in. It is seeded rather than random, so the
third take of the demo behaves exactly like the first, and the mix (~60%
interested, 25% question, 15% not now) is roughly what a well-targeted
post-storm campaign returns. Everything downstream of the reply — the
classification, the booking, the status write, the change record — is real.

## Known limits

- **Bounding-circle geometry.** Alert polygons are reduced to a centroid and
  radius. Point-in-polygon would be more precise, but the alert footprint is
  already an approximation of where damage occurred, and the score treats
  distance as a gradient rather than a boundary.
- **Zone-only alerts.** Some NWS alerts carry no polygon. They score at neutral
  proximity rather than being dropped.
- **Scoring runs in UDFs, not Spark column expressions.** Slower, and
  deliberately so — logic that only exists inside a Spark expression cannot be
  unit-tested. At this data volume it costs nothing.
- **Won revenue is analyst-marked.** The agent books inspections. Whether a job
  sells is decided on site, and the system has no way to observe that. Dollars
  before a win are labelled *estimated pipeline*, never revenue.

## Roadmap

Named, not built: appointment confirmations and reminders, post-job review
requests, seasonal re-engagement for customers no storm has touched, email as a
second channel, and prospect enrichment from permit and property records.
