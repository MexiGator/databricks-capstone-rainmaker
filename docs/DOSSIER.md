# Rainmaker — Build Dossier

Every significant decision in this project, why it was made, what it cost, and
when I'd revisit it. Organised by question asked rather than by system
component, because that's how these come up in conversation.

Searchable HTML version: `docs/dossier.html`

---

# 1 · Problem and product

## What problem does Rainmaker solve?

A hailstorm crosses Tarrant County. Roofers find out the same way everyone else
does — a customer calls. The company that books the work is the one that
reached out first.

Today that outreach is manual: someone with a laptop, a radar app, and a
spreadsheet, working the phones until the storm's news cycle passes. It is slow,
it misses most of the affected list, and it competes against door-knockers who
showed up in person within hours.

Rainmaker turns that scramble into a ranked queue and a one-click loop.

**Tags:** business · pitch

## Who is it for?

Regional home-services companies — roofing, plumbing, HVAC, water restoration —
with an existing customer list and a small office staff. The user is an office
manager or sales lead, not a data engineer. Six synthetic tenants model this in
the demo, each with a real hazard footprint.

**Tags:** business · pitch

## What does it actually do, end to end?

1. Polls live National Weather Service alerts for the states where the company
   has customers.
2. Matches each storm footprint against the CRM — customers *and* prospects.
3. Scores every pair for exposure: how bad, how close, does this hazard drive
   this service line, is this customer worth calling first.
4. Ranks them into a queue.
5. On request, drafts outreach grounded in the past campaign that actually
   booked jobs for this hazard.
6. The analyst approves and sends.
7. When the customer replies, the agent classifies the reply and books the
   inspection.
8. Every status transition is captured and rolled up into a funnel.

**Tags:** business · pitch · engineering

## Why weather?

Three properties most demand signals don't have together:

- **It's causal, not correlative.** Hail damages roofs. There's no inference gap
  to defend.
- **It's free, real, and continuous.** api.weather.gov, no key, no rate-limit
  negotiation, always something happening somewhere.
- **It's time-boxed.** Insurance claim windows typically close about a year
  after the storm date, and door-knockers arrive within days. Being first is
  worth money, which is what makes the product worth paying for.

**Tags:** business · pitch

## Isn't this just a mail merge with extra steps?

A mail merge sends the same message to a list you already chose. Three things
here aren't that:

- **The list is computed, not chosen.** Exposure scoring ranks 61 customers
  against every live alert and returns the handful worth calling. Nobody picked
  them.
- **The copy is retrieved, not templated.** The agent searches past campaigns
  for the one that actually booked for this hazard and service line, then
  grounds the draft on it. A template doesn't get better because a different
  campaign worked last month; this does.
- **The loop closes.** A mail merge ends at send. This reads the reply,
  classifies it, and writes a booking.

The honest version: strip the retrieval and the reply handling and you'd have a
smart mail merge. Those two pieces are the product.

**Tags:** business · pitch · engineering

## Is texting homeowners right after a storm predatory?

Worth having a real answer, because it's the sharpest question you'll get.

What makes storm-chasing predatory is usually one of: claiming damage that
hasn't been inspected, manufacturing urgency, targeting people who never opted
in, or making it hard to say no. The system is built against all four:

- **The draft prompt forbids claiming damage occurred.** It says the property is
  worth checking. No invented statistics, prices, or dates — that's an explicit
  rule in the prompt.
- **The urgency is real and external.** Insurance claim windows and the
  physics of unaddressed water intrusion aren't manufactured by the seller.
- **The list is the company's own customers and prospects**, not a purchased
  one.
- **Opt-out is a hard stop before any scoring.** "Sounds great, but stop texting
  me" is classified as a decline with confidence 1.0. Five tests cover that path
  alone.

The unresolved tension: a homeowner who didn't ask to be contacted still gets a
text. That's a real cost, and the honest framing is that it's the same cost as
any outbound sales motion — reduced, not eliminated, by targeting people who
plausibly need the service and making the exit trivial.

**Tags:** business · ethics · pitch

---

# 2 · Architecture

## What's the overall architecture?

```
api.weather.gov ──poll──▶ weather_events ─┐
synthetic CRM ───seed──▶ customers ───────┼─▶ Match & Score (Spark)
past campaigns ──seed──▶ outreach_templates┘         │
                             │                       ▼
                             │                 opportunities
                        embed corpus                 │
                             ▼                       ▼
                      pgvector index ──────▶ Agent (3 tools)
                                                     │
                                                     ▼
                                      Storm Response console (Flask)
                                                     │
                                       Delta + CDF ──▶ gold_results
```

Six components: a poller, a seeder, a Spark scoring job, an embedding job, a
three-tool agent, and a two-tab Flask console. Analytics runs off the Delta
change feed.

**Tags:** engineering · architecture

## Why both Lakebase (Postgres) and Delta? Isn't one enough?

They do different jobs and neither does the other's well.

- **Lakebase is the operational store.** The console needs single-row reads and
  writes at click speed. A user changes a status and expects it reflected
  immediately. That's OLTP.
- **Delta is the analytics store.** Change Data Feed, versioned history,
  columnar scans over the whole table. That's OLAP.

Running the console off Delta would make every click slow. Running analytics off
Postgres would mean no change feed. The round trip between them is the honest
architecture, not a workaround — it's the same split Zach drew on the Day 1
architecture slide.

**Tags:** engineering · architecture · databricks

## Why does Spark write to Delta and then sync to Postgres, rather than writing to Postgres directly?

Serverless Databricks can't do a Spark JDBC write to Lakebase — that came up in
the Day 2 lecture. So Spark owns the computation and Delta, and a small psycopg2
upsert moves the result into the operational store.

This turned out better than the direct write would have been: Delta becomes the
lineage record of what the pipeline computed, and Postgres holds what the
humans then did to it. Two different facts in two appropriate places.

**Tags:** engineering · architecture · databricks

## Why one app with two tabs instead of two apps?

Two reasons, one practical and one design.

Practical: Databricks Free Edition caps you at three apps. HW1 has one, so
spending two on Rainmaker leaves no headroom.

Design: it mirrors how real ops tools work. An operate view and a reporting view
over the same data, with shared navigation. Splitting them would mean two
deployments, two secret bindings, and a user who has to remember two URLs.

**Tags:** engineering · architecture · product

## Why Flask rather than FastAPI or Streamlit?

Continuity. The Day 1 ticketing app is Flask, the deploy pattern is proven, and
the secret binding is identical. With a hard deadline, reusing a path you've
already debugged beats a marginally nicer framework.

Streamlit was the real alternative and I'd have lost the custom UI — the
exposure meter, the bulletin strip, the grounding panel. Those are what make the
console look like an instrument rather than a form.

**Tags:** engineering · architecture

---

# 3 · Data model

## What are the tables?

**Reference (seeded, static):**
- `event_service_map` — which NWS alert drives which service line, with an
  urgency weight
- `outreach_templates` — seven past campaigns with measured booked rates; this
  is the RAG corpus
- `customers` — 61 customers and prospects across six tenants

**Live (written by the pipeline):**
- `weather_events` — real NWS alerts, including narrative text
- `opportunities` — the scored queue, one row per (event, customer)
- `corpus_embeddings` — 384-dim vectors over both text corpora

**Written by the agent and the app:**
- `outreach` — drafts and sends
- `inbound_replies` — customer responses
- `bookings` — scheduled inspections
- `opportunity_status_history` — appended by trigger on every transition

**Analytics (written by the rollup):**
- `gold_results`, `cdf_audit`, `cdf_checkpoint`

**Tags:** engineering · data-model

## Why is `opportunity_id` a hash instead of a serial?

`sha1(event_id + '|' + customer_id)`, truncated.

Match & Score re-runs every time the weather poller finds new alerts. With a
serial key, each run would insert duplicate opportunities for the same
storm-customer pair. With a deterministic hash, the same pair always produces
the same id, so the insert becomes an upsert and re-running is free.

The delimiter matters: without the `|`, `id("ab", "c")` and `id("a", "bc")`
would collide. There's a test for exactly that.

**Tags:** engineering · data-model · gotcha

## Why is there an `est_job_value` column when `contract_value` exists?

Prospects have no purchase history — `contract_value` is 0. Without a separate
column, the value factor in the exposure score would return 0 for every prospect
and sink the entire prospect list to the bottom of the queue.

`est_job_value` is the expected value of the *next* job: for customers roughly
their typical ticket, for prospects the regional median. It's what scoring
actually reads.

This is the kind of bug that would have been invisible in a demo — the queue
would look fine, it would just silently never show a prospect.

**Tags:** engineering · data-model · gotcha

## Why is `event_service_map` many-to-many?

A hurricane drives roofing *and* restoration demand. A winter storm drives
plumbing at urgency 0.80 and roofing at 0.55 — burst pipes are more urgent than
ice dams, but both are real.

A one-to-one mapping would force a choice and lose half the opportunities.

Secondary benefit: the weather poller derives its API filter from this table, so
adding a new hazard is a data change rather than a code change.

**Tags:** engineering · data-model

## What's the opportunity lifecycle?

```
identified → drafted → sent → responded → booked → quoted → won → completed
                                    └──────────────────────────────▶ lost
```

Who writes each transition:
- `identified` — Match & Score (Spark)
- `drafted` — Agent Tool 1
- `sent` — Agent Tool 2
- `responded` — Agent Tool 3, when the reply isn't a confident yes
- `booked` — Agent Tool 3, on a confident yes
- `won` / `lost` — **human only**

The agent stops at `booked`. Whether a job sells is decided on site by a person
with a ladder, and the system has no way to observe it.

**Tags:** engineering · data-model · agent

## Why doesn't re-running the scoring job reset an opportunity's status?

The upsert in `match_and_score.sync_to_lakebase` deliberately updates
`exposure_score`, `priority`, `distance_km`, and `est_value` — and does **not**
touch `status`.

Without that exclusion, re-polling the weather mid-demo would reset every sent
and booked opportunity back to `identified`, wiping the funnel on screen. It's
a one-line omission that would be very hard to diagnose live.

**Tags:** engineering · gotcha · data-model

---

# 4 · Scoring

## How does exposure scoring work?

```
exposure = severity × proximity × urgency × value    →  0..1
```

- **severity** — from the NWS severity field. Extreme 1.00, Severe 0.85,
  Moderate 0.60, Minor 0.35.
- **proximity** — 1.0 at the centre of the alert footprint, 0.5 at its edge,
  decaying to 0 over the next 40 km.
- **urgency** — from `event_service_map`. Does this hazard actually drive this
  service line.
- **value** — job value normalised against a $30k ceiling, plus a loyalty bonus
  (platinum +0.15, gold +0.07).

Worked example — a platinum roofing customer 2 km from the centre of an Extreme
tornado warning, $28k job:

```
1.00 × 0.98 × 1.00 × (0.93 + 0.15 → capped 1.00) = 0.98  → critical
```

Same customer 800 km away:

```
1.00 × 0.00 × 1.00 × 1.00 = 0.00  → filtered out
```

**Tags:** engineering · scoring

## Why multiplicative instead of additive?

Because any factor at zero should kill the row.

With an additive score, a platinum customer with a $30k job value 800 km from
the storm would still accumulate enough points from value and severity to rank
near the top of the queue. They are not an opportunity. Multiplication lets
proximity veto value.

There's a test asserting exactly this — `test_distant_customer_is_filtered_out_
despite_high_value`.

**Tags:** engineering · scoring

## Why does proximity decay past the footprint edge instead of cutting off?

A house 5 km outside a hail polygon is far more likely to have damage than one
200 km away. A binary in/out test would rank them identically.

The alert footprint is itself an approximation of where damage occurred — NWS
polygons are drawn fast, under pressure, from radar. Treating that boundary as
a hard fact would be trusting it more than the people who drew it do.

**Tags:** engineering · scoring

## Why is scoring pure Python in UDFs rather than Spark column expressions?

Spark column expressions would be faster. But logic that only exists inside a
Spark expression can't be unit-tested — you'd need a Spark session, fixture
DataFrames, and a collect() just to check that a distant customer scores zero.

The whole scoring layer lives in `pipeline/scoring.py` as plain functions with
no Spark import, and `match_and_score.py` wraps them in UDFs. That's what makes
20 scoring tests possible.

At 61 customers × a few dozen alerts, the performance cost is unmeasurable. At
100,000 customers I'd port the hot path to column expressions and keep the pure
functions as the test oracle.

**Tags:** engineering · scoring · testing · tradeoff

## How were the constants chosen?

Judgment, not fitting — there's no labelled outcome data to fit against.

Severity weights track the NWS severity ladder. The 40 km decay distance is
roughly the radius of a typical severe-thunderstorm polygon. The $30k value
ceiling is near the top of a residential roof replacement, so a $200k commercial
job doesn't dominate the queue. The 0.25 queue cutoff was set so the demo queue
holds a workable number of rows rather than everything.

All of them are module constants at the top of `scoring.py`, referenced by name
in the tests. Tuning is a data change, not a code change — and the first real
deployment should refit them against actual booked outcomes.

**Tags:** engineering · scoring · limits

---

# 5 · Retrieval and RAG

## What makes this real RAG rather than decoration?

One test: **if you deleted the corpus, would the output get worse?**

The question the agent needs answered is *"what wording actually gets a
homeowner to book after hail?"* That answer doesn't exist in the model's
weights. It exists only in this company's past campaigns and their measured
booked rates. The model cannot produce it without retrieving.

Compare that to the common capstone pattern — vector search over documents the
model could summarise from general knowledge anyway. There, retrieval is
decorative.

**Tags:** engineering · rag · pitch

## What's in the corpus?

Two text sources, one index:

- **NWS narrative text** — the `description` and `instruction` blocks from live
  alerts. Real meteorological prose about hazard and safety.
- **Campaign copy** — seven past campaigns, each 5–7 sentences of real outreach
  writing with a measured booked rate and send count.

The booked rate is written *into the chunk text*, not just the metadata. An
analyst asking "what messaging works best" is asking about that number, so it
has to be inside what gets embedded and retrieved.

**Tags:** engineering · rag · data-model

## Why one unified index instead of separate weather and template indexes?

The headline question is *"given this week's storms, who should I prioritise and
what should I say?"* That needs to retrieve across both corpora in a single
search.

Two indexes would mean two searches and an arbitrary rule for merging the
results — how do you compare a 0.71 from the weather index against a 0.68 from
the template index when they were ranked separately? One index makes the
similarity scores directly comparable.

`source_type` still allows filtering to one corpus when that's what you want,
which is how `best_template` works.

**Tags:** engineering · rag · architecture

## Why does `best_template` use semantic search instead of a WHERE clause?

You could write `WHERE event_type = 'Excessive Heat Warning'`. It would work
today and return nothing the day NWS renames the event — which they did in 2025,
replacing "Excessive Heat Warning" with "Extreme Heat Warning."

Semantic search degrades gracefully where equality doesn't. A query for the new
name still retrieves the old campaign, because the text is about heat and air
conditioning regardless of the header.

Among semantically close campaigns, the final pick weights similarity 60% and
past booked rate 40% — closeness alone would ignore which message actually
worked.

**Tags:** engineering · rag · tradeoff

## What happens when retrieval finds nothing relevant?

`ask()` returns a refusal **without calling the model at all**.

That ordering is the point. Handing an LLM an empty context block and hoping it
declines is not a guardrail — models are helpful by default and will produce a
plausible answer from their own weights. The only reliable refusal is one that
happens before generation.

The floor is 0.35 cosine similarity, tuned for all-MiniLM-L6-v2 where unrelated
text typically lands under 0.25.

Tool 1 degrades differently: no strong campaign match means it writes from the
weather event alone and records `template_id = NULL`, so the grounding panel
shows nothing rather than something false.

**Tags:** engineering · rag · reliability

## Why all-MiniLM-L6-v2 and 384 dimensions?

Mainly because Homework 2 used it, and mixing embedding models silently destroys
similarity — vectors from different models aren't comparable, and the failure
mode is plausible-looking scores that mean nothing.

On its merits it's a reasonable pick: small enough to load on serverless in
seconds, good enough for short-document retrieval, and it runs locally so there's
no per-query embedding cost or API dependency.

One deployment gotcha: serverless Databricks has a read-only HOME, so the model
download fails with a permissions error that looks nothing like the real cause.
`embed_corpus.py` sets `HF_HOME=/tmp/hf` at import.

**Tags:** engineering · rag · databricks · gotcha

---

# 6 · The agent

## What are the three tools?

| Tool | Does | Writes |
|---|---|---|
| `draft_outreach` | Retrieves the best past campaign, drafts a personalised message | `outreach` row, status → drafted |
| `send_and_create_lead` | Approves and sends, registers the lead | `outreach` → sent, status → sent |
| `handle_reply_and_book` | Classifies the reply, books the inspection | `bookings` row, status → booked |

**Tags:** engineering · agent

## Why do the tools need real side-effects?

The requirement says "action-taking tools," and the common failure is three
tools that all generate text and hand it back. That's one tool called three
times.

Each of these writes rows a human would otherwise have to write. Tool 3 in
particular creates a booking — a scheduled appointment with a rep assigned and a
value attached. That's the difference between an agent and a chatbot.

**Tags:** engineering · agent · pitch

## Why are replies classified with rules rather than an LLM?

This function decides whether a booking row gets written. It needs to be:

- **Deterministic** — the same reply must always produce the same outcome
- **Testable** — 30+ cases run in milliseconds with no mocking
- **Offline** — no network round trip that can fail mid-demo
- **Auditable** — you can point at the exact pattern that fired

An LLM call there would be a worse engineering decision dressed up as a more
impressive one. The reply vocabulary here is genuinely narrow — yes, no, how
much, who is this — and rules cover it well.

Where I'd revisit: multilingual replies, or a company whose customers write long
discursive messages. `classify_with_llm` is left as a documented fallback seam.

**Tags:** engineering · agent · tradeoff

## Why is opt-out a hard stop before any scoring?

"Sounds great, but stop texting me" reads enthusiastic to a naive scorer —
"sounds great" is a strong positive pattern. Texting that person again is a TCPA
compliance problem, not a missed sale.

`HARD_OPT_OUT` is checked before the scoring pass and returns `not_now` with
confidence 1.0 regardless of anything else in the message. Five tests cover it,
including two deliberately written to look positive.

**Tags:** engineering · agent · ethics · reliability

## When does the agent book automatically, and when does it defer?

Only a **confident** `interested` books itself — intent must be `interested` and
confidence at or above 0.45.

Confidence is the winning category's share of total signal, so a message that
hits one category cleanly scores high and a mixed message scores low. Mixed
messages are exactly when a human should look.

One deliberate exception: "Yes please, but how much will it cost?" books. It's
interested *and* a question, and routing it to a human loses the moment — the
rep answers pricing on the call. Getting this right required a fix, not a
loosened assertion; see the testing section.

**Tags:** engineering · agent

## What can the agent not do?

- Mark a job **won**. That's decided on site after a quote.
- Send without approval. Tool 2 runs on an analyst click.
- Book on an ambiguous reply. Below the confidence floor it flags for a human.
- Invent prices, dates, or damage claims — explicitly forbidden in the prompt.

Naming the boundaries is part of the pitch. An agent with no stated limits reads
as an agent nobody has thought carefully about.

**Tags:** engineering · agent · pitch

---

# 7 · Change data capture

## How is Change Data Feed actually used?

Not decoratively. The Results tab renders numbers computed from the feed:

1. `gold_rollup.py` reads current opportunity state from Lakebase.
2. MERGEs it into the Delta table — the merge generates change records.
3. Reads `readChangeFeed` from a stored checkpoint version.
4. Rolls up funnel counts and revenue at two grains: overall and by storm type.
5. Pushes the rollup *and* the raw change rows back to Lakebase.

The Results tab labels its own source: "Computed from the Delta change feed"
when the rollup has run, "Live query" when it hasn't. Those are different claims
and the UI shouldn't blur them.

**Tags:** engineering · cdc · databricks

## Why MERGE instead of overwrite?

Overwrite rewrites every row, so the change feed reports the entire table as
changed. The three status transitions you actually care about drown in 200 rows
of noise, and the audit trail becomes useless.

MERGE with `WHEN MATCHED AND t.status <> s.status` produces change records only
for rows that genuinely moved.

**Tags:** engineering · cdc · gotcha

## Why is there a Postgres status-history trigger *and* Delta CDF?

Different jobs, and the app is honest about which it's showing.

The trigger is the operational log — it fires the instant a status changes, so
the trail is visible immediately without waiting for a batch job. Delta CDF is
the analytics engine, and it's what the requirement asks for.

The `/api/cdf-trail` endpoint prefers the CDF-derived rows and falls back to the
trigger history, reporting `source` either way. During a first demo the rollup
may not have run yet, and a blank tab is worse than a labelled fallback.

**Tags:** engineering · cdc · architecture

## Why "estimated pipeline" and not "revenue"?

Because the first booking from proactive outreach is an *inspection*, not a sold
job.

Calling booked-but-unsold dollars "revenue captured" is the single most common
way a demo like this becomes non-credible to anyone who has run a services
business. The Results tab shows both — pipeline created (est.) and jobs won
(actual) — and the gap between them is the close rate, which is a real number a
buyer would want to see.

`won_value` on `bookings` is only populated when status reaches `won`.

**Tags:** business · engineering · pitch · ethics

---

# 8 · Real, synthetic, simulated

## What's real?

- **NWS alerts and their narrative text.** Live, from api.weather.gov. Load
  bearing: no alerts, no product.
- **The Spark scoring pipeline.**
- **The vector index and every retrieval.**
- **All three agent tools and their database writes.**
- **The Delta change feed and the rollup computed from it.**

**Tags:** credibility · pitch

## What's synthetic?

**The CRM.** 61 customers and prospects across six regional tenants, placed in
cities where their hazard actually occurs — hail alley for roofing, upper
midwest for plumbing, sunbelt for HVAC.

Real customer data can't be used, and this is standard for a capstone. The
placement isn't arbitrary: it's what makes the demo never empty, because some
NWS alert is always live over someone's footprint.

The seven outreach campaigns are also written rather than harvested, but they're
real prose with plausible booked rates — thin templates would have made the RAG
requirement cosmetic.

**Tags:** credibility · pitch

## What's simulated, and why?

**Inbound customer replies.** Production would use Twilio inbound webhooks;
`agent/simulate.py` stands in.

Why: a 90-second demo can't wait for real humans to text back. The alternative —
pre-seeding a static set of replies — would be less honest, because it would
hide the fact that nothing live is happening.

What's *not* simulated: everything downstream. The classification, the booking,
the status write, the change record are all real code doing real work on a
message that happens to have been generated rather than received.

**Tags:** credibility · pitch · engineering

## Why is the simulator seeded rather than random?

So take three of the demo behaves exactly like take one.

The intent for a given opportunity is derived from a hash of its id, so the same
opportunity always produces the same reply. Random replies would mean a recorded
demo you can't reproduce and a funnel that shifts every time you refresh.

The mix is ~60% interested / 25% question / 15% not now — roughly what a
well-targeted post-storm campaign returns. There's a test asserting the
distribution holds across 1,000 draws, because a demo where nobody says yes
isn't a demo, and one where everyone says yes isn't credible.

**Tags:** engineering · testing · credibility

---

# 9 · Testing

## What's the test philosophy?

91 tests, none of which touch the network. Test the decisions that change
outcomes, not the plumbing.

The four suites:
- **`test_scoring.py`** — that proximity vetoes value, that priority bands are
  total, that opportunity ids are stable
- **`test_classify.py`** — the auto-book decision, mixed signals, opt-out
- **`test_weather_client.py`** — malformed alerts skipped not raised, polygon
  geometry, capped radius
- **`test_simulate.py`** — determinism, distribution, and simulator/classifier
  agreement

**Tags:** engineering · testing

## Why does no test touch the network?

A suite that needs the internet is a suite that fails while you're recording a
demo, or while a grader runs it.

Every NWS test runs against fixture GeoJSON shaped like a real response, and the
fetch layer is tested through a fake session object. The tradeoff is that a real
API contract change wouldn't be caught — which is why `normalize_alert` is
written to skip malformed features rather than raise.

**Tags:** engineering · testing · tradeoff

## What bug did the tests actually catch?

The best one: **"Yes please, but how much will it cost?"** classified as a
`question` and got routed to a human instead of booking.

That's a customer who explicitly said yes, lost to a scoring quirk — the pricing
question scored 4.5 against the affirmative's 3.0. In production it would look
like nothing at all; the booking simply wouldn't happen.

The fix was to make an explicit affirmative carry a bonus, as a *bonus* rather
than a hard override, so genuinely mixed replies still fall below the auto-book
floor. The alternative — loosening the assertion — would have shipped the bug.

Two others worth naming: the `sys.path` inserts were relative to the working
directory and would have worked locally then failed on deploy; and `simulate.py`
imported psycopg2 at module load, so pure reply logic couldn't be tested without
a database driver.

**Tags:** engineering · testing · gotcha

## What's the strongest single test?

`test_simulated_replies_classify_to_their_seeded_intent`. It runs 300 simulated
replies through the real classifier and asserts the seeded intent matches.

Without it, the simulator could seed a reply as "interested" while the classifier
reads it as a question — and the demo's booked count would quietly be a lie.
It's the only test that checks two components agree rather than checking one
component works.

**Tags:** engineering · testing

---

# 10 · Limits and roadmap

## Known limits

- **Bounding-circle geometry.** Alert polygons are reduced to a centroid and
  radius. Point-in-polygon would be more precise; the footprint is already an
  approximation, and the score treats distance as a gradient.
- **Zone-only alerts.** Some NWS alerts carry no polygon. They score at neutral
  proximity (0.5) rather than being dropped — better than inventing a location.
- **Scoring runs in UDFs.** Slower than column expressions, deliberately, for
  testability.
- **Won revenue is analyst-marked.** No integration with the field.
- **Constants are judgment-set**, not fitted to outcome data.
- **Single-region assumption.** Distance is great-circle; no drive-time or
  crew-capacity modelling.

**Tags:** engineering · limits

## What breaks at 10,000 customers?

- **The cross join.** Every active alert against every service-matched customer
  is fine at 61 and not at 10,000 × 200 alerts. Fix: spatial partitioning —
  bucket customers by geohash prefix and only join alerts whose footprint
  touches the bucket.
- **The UDFs.** Port the hot path to Spark column expressions, keep the pure
  functions as the test oracle.
- **The pandas hop.** `pd.read_sql` into `spark.createDataFrame` doesn't survive
  that volume. Fix: Delta mirrors of the CRM, refreshed on a schedule.
- **HNSW recall.** Fine at thousands of chunks; needs tuning at millions.

None of these are rewrites. That's a reasonable answer to give an investor.

**Tags:** engineering · limits · pitch

## Couldn't a competitor rebuild this in a weekend?

The mechanism, roughly yes. The pipeline is a weather API, a join, a score, and
an LLM call.

What isn't a weekend: the campaign corpus with real booked rates. That's the
asset that makes retrieval worth doing, and it compounds — every campaign sent
through the system adds a labelled outcome. A competitor starting today has an
empty corpus and no way to know which message works.

The honest version: the moat is data accumulation and the customer relationship,
not the code. Which is true of most software and worth saying plainly rather
than claiming technical defensibility that isn't there.

**Tags:** business · pitch

## What would you build next?

Named as roadmap, deliberately not built:

- **Appointment confirmations and reminders** — the highest-value next tool,
  because no-shows are where booked inspections leak.
- **Post-job review requests** — timed off the completed status.
- **Seasonal re-engagement** — customers no storm has touched still need
  maintenance.
- **Email as a second channel** — SMS is right for urgency, wrong for detail.
- **Prospect enrichment** from permit and property records — roof age is the
  single best predictor of hail claim success.

**Tags:** business · roadmap · pitch

---

# 11 · Demo

## The 90-second run

1. **Open on the bulletin strip.** Real NWS alerts, live, in teletype. "This is
   what's happening right now."
2. **The queue.** Ranked by exposure. Point at the meter — severity, distance,
   service match, customer value, in one glance.
3. **Draft.** Click a top row → Draft outreach. The grounding panel shows which
   past campaign it retrieved and the similarity score.
4. **Send.** The loop runs: sent → reply appears → classified → inspection
   booked. Roughly two seconds.
5. **Results tab.** Funnel fills, pipeline ticks up. Open the change feed —
   "every one of those is a captured transition."
6. **Ask.** "What messaging has worked best for hail?" Grounded answer with
   sources and similarity scores.

Money line: *"From one hailstorm — 80 opportunities, 30 reached, 7 inspections
booked, $42k of estimated pipeline. From a storm that used to be a scramble."*

**Tags:** pitch · demo

## What to do if the demo breaks

- **No active alerts.** Genuinely possible on a calm day. Widen `event_types` to
  include Heat Advisory — almost always live somewhere in the sunbelt footprint.
  The poller says so plainly rather than looking broken.
- **Model endpoint slow.** Draft a message before recording; the draft persists
  in `outreach` and renders from the database.
- **Empty Results tab.** Run `gold_rollup.run()` first; the tab falls back to a
  live query and labels itself, but the CDF story is stronger.
- **Anything else.** The `/healthz` endpoint reports database connectivity
  directly.

**Tags:** demo · reliability
