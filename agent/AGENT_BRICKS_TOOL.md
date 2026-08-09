# 4th Agent Bricks tool — `send_proactive_care_tip`

v0.1 relationship layer. Registers a **fourth** real-action tool on the graded
Rainmaker agent. Dark unless `ENABLE_RELATIONSHIP_V0=1`.

## What it does
Given a contact and a **forecast** event, it: (1) loads the contact's
relationship warmth, (2) runs the tested policy to decide send/suppress + CTA
strength, (3) grounds the message in an approved care guide, (4) writes a
`care_sends` row as `status='queued'` (human-in-the-loop). The analyst approves
& sends from the Proactive Care panel.

## Callable
`agent/care_tool.py::send_proactive_care_tip(contact_id, event, trigger="forecast", auto_send=False)`

The tool JSON schema is `agent.care_tool.TOOL_SPEC`.

## Register in Agent Bricks
1. Add a Python tool whose entrypoint imports and calls
   `agent.care_tool.send_proactive_care_tip`.
2. Use `TOOL_SPEC["name"]`, `["description"]`, and `["parameters"]` as the tool
   name / description / input schema.
3. Ensure the app env has `ENABLE_RELATIONSHIP_V0=1` and `LAKEBASE_URL` (Secret
   resource key `lakebase-url`), and that `schema_relationship.sql` has been run.

## Example call
```json
{ "contact_id": "cust_0001",
  "event": { "event_type": "Hard Freeze Watch", "headline": "Hard Freeze Watch",
             "area": "Dallas, TX", "event_id": "e-123" },
  "trigger": "forecast" }
```
Returns `{ "queued": true, "care_send_id": 42, "tier": "warm",
"cta_strength": "soft", "guide_id": "care_freeze_pipes", "message": "..." }`,
or `{ "queued": false, "reason": "opted_out" | "frequency_cap" |
"no_matching_care_guide", ... }` when the policy holds it back.

## Reply → booking hand-off
`agent/care_tool.py::record_care_reply(care_send_id, reply_text, reply_intent)`
advances the care send and, when `reply_intent="interested"`, returns an
`opportunity_seed` for the **existing** booking flow. v0.1 never writes the
graded `opportunities`/`bookings` tables itself — the analyst pushes the seed in
via Tool 3.
