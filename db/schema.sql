-- =====================================================================
-- RAINMAKER — Lakebase (managed Postgres) schema
-- Proactive Demand & Engagement Engine for weather-driven home services
--
-- Idempotent: safe to run repeatedly. Every object uses IF NOT EXISTS.
-- Run via: python -c "import lakebase; lakebase.ensure_schema()"
--   or paste into the Lakebase SQL editor.
--
-- NOTE: this is the OPERATIONAL store the app reads/writes.
-- The analytics side (gold_results) lives in Delta with Change Data Feed
-- enabled; opportunities/outreach mirror into Delta for requirement #6.
-- =====================================================================

-- PREREQUISITE: pgvector must already be enabled on the instance
-- ("Enable Lakebase Search"). ensure_schema() creates the extension in its
-- own transaction first -- kept out of this file because a failed
-- CREATE EXTENSION would abort every statement below it.


-- ---------------------------------------------------------------------
-- REFERENCE: which NWS event types create demand for which service line
-- Many-to-many on purpose: a hurricane drives BOTH roofing and restoration.
-- urgency_weight feeds the exposure score (0..1).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_service_map (
    event_type      TEXT         NOT NULL,
    service_type    TEXT         NOT NULL,
    urgency_weight  NUMERIC(3,2) NOT NULL DEFAULT 0.50
                    CHECK (urgency_weight >= 0 AND urgency_weight <= 1),
    damage_mode     TEXT,
    PRIMARY KEY (event_type, service_type)
);


-- ---------------------------------------------------------------------
-- CRM: customers AND prospects (is_prospect flags the difference).
-- lat/lon + service_type are the join keys for Match & Score.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customers (
    customer_id     TEXT PRIMARY KEY,
    tenant          TEXT NOT NULL,
    name            TEXT NOT NULL,
    email           TEXT,
    phone           TEXT,
    city            TEXT NOT NULL,
    state           CHAR(2) NOT NULL,
    zip             TEXT,
    lat             DOUBLE PRECISION NOT NULL,
    lon             DOUBLE PRECISION NOT NULL,
    service_type    TEXT NOT NULL
                    CHECK (service_type IN ('roofing','plumbing','hvac','restoration')),
    contract_value  NUMERIC(12,2) NOT NULL DEFAULT 0,
    lifetime_value  NUMERIC(12,2) NOT NULL DEFAULT 0,
    -- expected value of the NEXT job. For customers ~ their typical ticket;
    -- for prospects ~ the regional median. Match & Score reads this, so
    -- prospects (contract_value = 0) still price correctly.
    est_job_value   NUMERIC(12,2) NOT NULL DEFAULT 0,
    tenure_start    DATE,
    tier            TEXT NOT NULL DEFAULT 'standard'
                    CHECK (tier IN ('platinum','gold','standard')),
    assigned_rep    TEXT,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','churned','lead')),
    is_prospect     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_customers_geo     ON customers (lat, lon);
CREATE INDEX IF NOT EXISTS idx_customers_service ON customers (service_type);
CREATE INDEX IF NOT EXISTS idx_customers_tenant  ON customers (tenant);


-- ---------------------------------------------------------------------
-- WEATHER EVENTS: real NWS active alerts (populated by the poller).
-- event_id is the NWS alert id -> stable dedup key, re-polling is safe.
-- narrative_text is the unstructured payload we embed (requirement #3).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS weather_events (
    event_id        TEXT PRIMARY KEY,
    event_type      TEXT NOT NULL,
    severity        TEXT,           -- Extreme | Severe | Moderate | Minor
    certainty       TEXT,
    urgency         TEXT,
    headline        TEXT,
    area_desc       TEXT,
    state           CHAR(2),
    lat             DOUBLE PRECISION,
    lon             DOUBLE PRECISION,
    radius_km       DOUBLE PRECISION NOT NULL DEFAULT 60,
    effective_at    TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,
    narrative_text  TEXT,
    payload         JSONB,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_weather_type   ON weather_events (event_type);
CREATE INDEX IF NOT EXISTS idx_weather_active ON weather_events (expires_at);


-- ---------------------------------------------------------------------
-- OPPORTUNITIES: the output of Match & Score. One row per (event, customer).
-- opportunity_id is deterministic (sha1 of event_id + customer_id) so
-- re-running the scoring job UPSERTs instead of duplicating.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS opportunities (
    opportunity_id    TEXT PRIMARY KEY,
    weather_event_id  TEXT NOT NULL REFERENCES weather_events(event_id) ON DELETE CASCADE,
    customer_id       TEXT NOT NULL REFERENCES customers(customer_id)   ON DELETE CASCADE,
    tenant            TEXT NOT NULL,
    service_needed    TEXT NOT NULL,
    distance_km       DOUBLE PRECISION,
    exposure_score    NUMERIC(4,3) NOT NULL
                      CHECK (exposure_score >= 0 AND exposure_score <= 1),
    priority          TEXT NOT NULL
                      CHECK (priority IN ('critical','high','medium','low')),
    est_value         NUMERIC(12,2) NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'identified'
                      CHECK (status IN ('identified','drafted','sent','responded',
                                        'booked','quoted','won','completed','lost')),
    assigned_rep      TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (weather_event_id, customer_id)
);

CREATE INDEX IF NOT EXISTS idx_opp_queue  ON opportunities (status, exposure_score DESC);
CREATE INDEX IF NOT EXISTS idx_opp_tenant ON opportunities (tenant);


-- ---------------------------------------------------------------------
-- OUTREACH: what the agent drafted/sent (Agent Tool 1 + Tool 2 write here).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outreach (
    outreach_id     BIGSERIAL PRIMARY KEY,
    opportunity_id  TEXT NOT NULL REFERENCES opportunities(opportunity_id) ON DELETE CASCADE,
    -- 'safety' notices go out FIRST and are never generated (see agent/safety.py).
    -- 'commercial' is the RAG-drafted pitch, gated on a safety notice existing.
    kind            TEXT NOT NULL DEFAULT 'commercial'
                    CHECK (kind IN ('safety','commercial')),
    template_id     TEXT,            -- which RAG-retrieved template grounded it
    similarity      NUMERIC(4,3),    -- retrieval score, shown in the grounding panel
    message_text    TEXT NOT NULL,
    channel         TEXT NOT NULL DEFAULT 'sms' CHECK (channel IN ('sms','email')),
    status          TEXT NOT NULL DEFAULT 'drafted'
                    CHECK (status IN ('drafted','approved','sent','failed')),
    approved_by     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ,
    -- The urgent-tier safety notice says "I'll follow up once it's clear".
    -- An unkept promise is worse than no promise, so it is tracked here and
    -- surfaced in the console rather than trusted to memory.
    follow_up_due   TIMESTAMPTZ,
    follow_up_done  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_outreach_opp  ON outreach (opportunity_id);
CREATE INDEX IF NOT EXISTS idx_outreach_kind ON outreach (opportunity_id, kind);


-- ---------------------------------------------------------------------
-- INBOUND REPLIES: customer responses. Real Twilio inbound in production;
-- simulate_responses() stands in for the demo (seeded, deterministic).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inbound_replies (
    reply_id        BIGSERIAL PRIMARY KEY,
    opportunity_id  TEXT NOT NULL REFERENCES opportunities(opportunity_id) ON DELETE CASCADE,
    reply_text      TEXT NOT NULL,
    intent          TEXT CHECK (intent IN ('interested','question','not_now')),
    intent_conf     NUMERIC(4,3),
    is_simulated    BOOLEAN NOT NULL DEFAULT FALSE,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_replies_opp ON inbound_replies (opportunity_id);


-- ---------------------------------------------------------------------
-- BOOKINGS: an APPOINTMENT / INSPECTION -- not the sold job.
-- est_value feeds "estimated pipeline". Revenue is only recognised when
-- status reaches 'won'. (See rainmaker-design.md, revenue recognition.)
-- Agent Tool 3 writes here.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bookings (
    booking_id      BIGSERIAL PRIMARY KEY,
    opportunity_id  TEXT NOT NULL REFERENCES opportunities(opportunity_id) ON DELETE CASCADE,
    customer_id     TEXT NOT NULL REFERENCES customers(customer_id)        ON DELETE CASCADE,
    service_type    TEXT NOT NULL,
    proposed_slot   TIMESTAMPTZ NOT NULL,
    est_value       NUMERIC(12,2) NOT NULL DEFAULT 0,
    won_value       NUMERIC(12,2),   -- actual $, set only when status='won'
    assigned_rep    TEXT,
    status          TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled','completed','quoted','won','lost')),
    booked_by       TEXT NOT NULL DEFAULT 'agent',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings (status);


-- ---------------------------------------------------------------------
-- OUTREACH TEMPLATES: the RAG corpus. Past campaigns with real copy and
-- a measured booked-rate, embedded so the agent retrieves the best match.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outreach_templates (
    template_id      TEXT PRIMARY KEY,
    campaign_name    TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    service_type     TEXT NOT NULL,
    message_text     TEXT NOT NULL,
    past_booked_rate NUMERIC(4,3) NOT NULL DEFAULT 0
                     CHECK (past_booked_rate >= 0 AND past_booked_rate <= 1),
    sends            INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- CORPUS EMBEDDINGS: ONE index over BOTH text corpora.
--   source_type='weather'  -> weather_events.narrative_text  (NWS prose)
--   source_type='template' -> outreach_templates.message_text (campaign copy)
--
-- Unified on purpose: the headline Ask query ("given this week's storms,
-- who do I prioritise and what do I say?") has to retrieve across both in
-- a single search. Two separate indexes would mean two searches and an
-- arbitrary merge rule.
--
-- 384 dims = all-MiniLM-L6-v2, the SAME model as Homework 2.
-- Mixing embedding models silently destroys similarity.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corpus_embeddings (
    embedding_id  BIGSERIAL PRIMARY KEY,
    source_type   TEXT NOT NULL CHECK (source_type IN ('weather','template')),
    source_id     TEXT NOT NULL,
    chunk_index   INTEGER NOT NULL DEFAULT 0,
    chunk_text    TEXT NOT NULL,
    title         TEXT,
    metadata      JSONB,
    embedding     vector(384) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_type, source_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_corpus_hnsw
    ON corpus_embeddings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_corpus_source
    ON corpus_embeddings (source_type, source_id);


-- ---------------------------------------------------------------------
-- STATUS HISTORY: every opportunity transition, appended by trigger.
-- This is the operational audit trail the Results tab renders, and it
-- mirrors what Delta CDF captures on the analytics side.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS opportunity_status_history (
    history_id      BIGSERIAL PRIMARY KEY,
    opportunity_id  TEXT NOT NULL,
    old_status      TEXT,
    new_status      TEXT NOT NULL,
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_history_opp ON opportunity_status_history (opportunity_id, changed_at);

CREATE OR REPLACE FUNCTION log_opportunity_status_change()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := now();
    IF (TG_OP = 'UPDATE' AND NEW.status IS DISTINCT FROM OLD.status) THEN
        INSERT INTO opportunity_status_history (opportunity_id, old_status, new_status)
        VALUES (NEW.opportunity_id, OLD.status, NEW.status);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_opportunity_status ON opportunities;
CREATE TRIGGER trg_opportunity_status
    BEFORE UPDATE ON opportunities
    FOR EACH ROW EXECUTE FUNCTION log_opportunity_status_change();


-- ---------------------------------------------------------------------
-- CONVENIENCE VIEW: the Storm Response queue, pre-joined for the app.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_opportunity_queue AS
SELECT  o.opportunity_id,
        o.tenant,
        c.name,
        c.city,
        c.state,
        c.phone,
        c.tier,
        c.is_prospect,
        o.service_needed,
        o.exposure_score,
        o.priority,
        o.est_value,
        o.status,
        o.assigned_rep,
        w.event_type,
        w.headline,
        w.severity,
        o.distance_km,
        o.updated_at
FROM opportunities o
JOIN customers      c ON c.customer_id = o.customer_id
JOIN weather_events w ON w.event_id    = o.weather_event_id
ORDER BY o.exposure_score DESC, o.est_value DESC;


-- ---------------------------------------------------------------------
-- ANALYTICS LANDING (requirement #6).
--
-- These two tables are written by the gold rollup job, which reads the
-- DELTA CHANGE DATA FEED -- not by the app and not by a trigger. The
-- Results tab renders them, so what you see on that tab is genuinely
-- computed from captured changes rather than a live query dressed up.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gold_results (
    grain         TEXT NOT NULL,      -- 'overall' | 'event_type' | 'template'
    grain_value   TEXT NOT NULL DEFAULT '',
    identified    INTEGER NOT NULL DEFAULT 0,
    sent          INTEGER NOT NULL DEFAULT 0,
    responded     INTEGER NOT NULL DEFAULT 0,
    booked        INTEGER NOT NULL DEFAULT 0,
    won           INTEGER NOT NULL DEFAULT 0,
    pipeline_est  NUMERIC(14,2) NOT NULL DEFAULT 0,
    revenue_won   NUMERIC(14,2) NOT NULL DEFAULT 0,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (grain, grain_value)
);

-- Raw change-feed rows, surfaced so the proof is visible in the UI.
CREATE TABLE IF NOT EXISTS cdf_audit (
    audit_id        BIGSERIAL PRIMARY KEY,
    opportunity_id  TEXT NOT NULL,
    change_type     TEXT NOT NULL,     -- insert | update_preimage | update_postimage
    old_status      TEXT,
    new_status      TEXT,
    commit_version  BIGINT,
    commit_ts       TIMESTAMPTZ,
    UNIQUE (opportunity_id, commit_version, change_type)
);

CREATE INDEX IF NOT EXISTS idx_cdf_audit_ts ON cdf_audit (commit_ts DESC);

-- Bookmark so each rollup reads only NEW changes.
CREATE TABLE IF NOT EXISTS cdf_checkpoint (
    table_name    TEXT PRIMARY KEY,
    last_version  BIGINT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
