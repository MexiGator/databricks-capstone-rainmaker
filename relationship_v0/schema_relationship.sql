-- ============================================================================
-- Rainmaker v0.1 — Relationship Engagement schema (Lakebase / Postgres)
-- ADDITIVE ONLY. Creates NEW tables; never alters an existing one. Safe to run
-- against the graded database — if v0.1 is abandoned, `DROP TABLE` these three
-- and the submittable app is untouched. Mirrors the pgvector conventions from
-- the Day 2 weather service (vector(384), HNSW, cosine <=>).
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;   -- same "Enable Lakebase Search" toggle as HW2

-- ----------------------------------------------------------------------------
-- 1) contact_relationship — the relationship_score object (one row per contact)
--    Warmth/health of the bond, distinct from a storm's exposure/priority.
--    `components` stores the per-signal breakdown so the app can explain a score.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contact_relationship (
    contact_id          TEXT PRIMARY KEY,            -- = customers.customer_id (TEXT, e.g. 'cust_0001'); soft link, no FK to stay decoupled
    tenant              TEXT,
    relationship_score  NUMERIC(5,1) NOT NULL DEFAULT 0,   -- 0..100
    tier                TEXT NOT NULL DEFAULT 'cold',       -- hot|warm|cool|cold
    components          JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {recency, engagement, ...}
    consent_ok          BOOLEAN NOT NULL DEFAULT TRUE,
    opted_out           BOOLEAN NOT NULL DEFAULT FALSE,
    recent_care_touches INTEGER NOT NULL DEFAULT 0,          -- rolling FREQUENCY_WINDOW_DAYS
    last_touch_at       TIMESTAMPTZ,
    last_scored_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_contact_relationship_tier  ON contact_relationship (tier);
CREATE INDEX IF NOT EXISTS ix_contact_relationship_score ON contact_relationship (relationship_score DESC);

-- ----------------------------------------------------------------------------
-- 2) care_content — the Proactive Care RAG corpus (parallel to outreach_templates)
--    Seeded from relationship_v0.care_content.CARE_GUIDES; embedded for the Ask
--    bar + a semantic fallback when the deterministic lookup misses.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS care_content (
    id            TEXT PRIMARY KEY,          -- = CARE_GUIDES[i]["id"]
    service_type  TEXT NOT NULL,
    event_types   TEXT[] NOT NULL,           -- substrings matched against live NWS names
    title         TEXT NOT NULL,
    tips          JSONB NOT NULL,            -- ["tip", "tip", ...]
    guide_url     TEXT NOT NULL,
    soft_cta      TEXT NOT NULL,
    embedding     vector(384),               -- all-MiniLM-L6-v2, same model as HW2
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_care_content_service ON care_content (service_type);
-- HNSW cosine index for the semantic fallback / Ask bar (build after seeding+embedding)
CREATE INDEX IF NOT EXISTS ix_care_content_embedding
    ON care_content USING hnsw (embedding vector_cosine_ops);

-- ----------------------------------------------------------------------------
-- 3) care_sends — the engagement log (feeds relationship_score AND attribution)
--    Every proactive touch + its outcome. `status` is the CDF-tracked column for
--    the Results tab; a positive reply hands off to the existing booking flow.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS care_sends (
    care_send_id   BIGSERIAL PRIMARY KEY,
    contact_id     TEXT NOT NULL,            -- = customers.customer_id (TEXT); soft link
    tenant         TEXT,
    event_id       TEXT,                     -- the forecast event that triggered it
    event_type     TEXT,
    service_type   TEXT,
    guide_id       TEXT,                     -- = care_content.id (grounding)
    template_kind  TEXT NOT NULL,            -- care_tip | damage_check | reengage
    cta_strength   TEXT NOT NULL,            -- none|soft|medium|strong
    channel        TEXT NOT NULL DEFAULT 'sms',
    message_text   TEXT NOT NULL,
    -- status lifecycle: queued -> approved -> sent -> opened -> clicked
    --                                     -> replied -> booked (hand-off) | opted_out
    status         TEXT NOT NULL DEFAULT 'queued',
    reply_text     TEXT,
    reply_intent   TEXT,                     -- interested | question | not_now
    opportunity_id TEXT,                     -- = opportunities.opportunity_id (TEXT); set when a positive reply becomes a booking
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_care_sends_contact ON care_sends (contact_id);
CREATE INDEX IF NOT EXISTS ix_care_sends_status  ON care_sends (status);
CREATE INDEX IF NOT EXISTS ix_care_sends_event   ON care_sends (event_id);

-- ----------------------------------------------------------------------------
-- CDF / analytics note (Results tab): Lakebase is Postgres, so Change Data Feed
-- lives on the DELTA side. For the relationship funnel, mirror care_sends to a
-- Delta table and `ALTER TABLE ... SET TBLPROPERTIES (delta.enableChangeDataFeed=true)`,
-- exactly as step-6-analytics-cdf.md does for `opportunities`. The gold rollup
-- then reads the change feed for: care sent -> opened -> replied -> booked, plus
-- the money metric — booking rate of care-touched vs. non-care-touched contacts
-- (the proof that relationship engagement drives inspections).
-- ----------------------------------------------------------------------------

-- 4) analytics sink tables for pipeline/care_rollup.py (mirrors gold_rollup.py).
--    Additive, v0.1-only: its OWN checkpoint + audit so it never touches the
--    graded cdf_checkpoint / cdf_audit tables.
CREATE TABLE IF NOT EXISTS care_cdf_checkpoint (
    table_name    TEXT PRIMARY KEY,
    last_version  BIGINT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS care_cdf_audit (
    care_send_id    BIGINT NOT NULL,
    change_type     TEXT NOT NULL,
    new_status      TEXT,
    commit_version  BIGINT NOT NULL,
    commit_ts       TIMESTAMPTZ,
    PRIMARY KEY (care_send_id, commit_version, change_type)
);

-- Care funnel by grain ('overall' or an event_type), cumulative stages.
CREATE TABLE IF NOT EXISTS care_gold_funnel (
    grain_value  TEXT PRIMARY KEY,   -- 'overall' | event_type
    queued       INTEGER NOT NULL DEFAULT 0,
    approved     INTEGER NOT NULL DEFAULT 0,
    sent         INTEGER NOT NULL DEFAULT 0,
    opened       INTEGER NOT NULL DEFAULT 0,
    clicked      INTEGER NOT NULL DEFAULT 0,
    replied      INTEGER NOT NULL DEFAULT 0,
    booked       INTEGER NOT NULL DEFAULT 0,
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The headline metric: booking rate of care-touched vs. non-care-touched.
CREATE TABLE IF NOT EXISTS care_gold_lift (
    id                INTEGER PRIMARY KEY DEFAULT 1,   -- single-row table
    care_contacts     INTEGER NOT NULL DEFAULT 0,
    care_booked       INTEGER NOT NULL DEFAULT 0,
    care_rate         NUMERIC(6,4) NOT NULL DEFAULT 0,
    noncare_contacts  INTEGER NOT NULL DEFAULT 0,
    noncare_booked    INTEGER NOT NULL DEFAULT 0,
    noncare_rate      NUMERIC(6,4) NOT NULL DEFAULT 0,
    lift              NUMERIC(6,4) NOT NULL DEFAULT 0,   -- care_rate - noncare_rate
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (id = 1)
);
