-- 0001: initial durable schema (specification D5 tables + gap/cursor storage).
PRAGMA foreign_keys;

CREATE TABLE sources (
    source_id           TEXT PRIMARY KEY,
    endpoint_fingerprint TEXT NOT NULL,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    last_attempt_at     TEXT,
    last_error_code     TEXT
);

CREATE TABLE series (
    source_id     TEXT NOT NULL REFERENCES sources(source_id),
    identity      TEXT NOT NULL,          -- canonical sorted label-set JSON
    labels_json   TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    stale_since   TEXT,
    PRIMARY KEY (source_id, identity)
);

-- Durable committed accounting state per series (single writer).
CREATE TABLE series_state (
    source_id           TEXT NOT NULL,
    identity            TEXT NOT NULL,
    epoch_id            INTEGER NOT NULL REFERENCES epochs(epoch_id),
    primary_computed_input     INTEGER,
    primary_generated_output   INTEGER,
    primary_observed_at TEXT,
    continuity_verified INTEGER NOT NULL DEFAULT 0,
    uptime_lo           TEXT,
    uptime_hi           TEXT,
    optional_json       TEXT NOT NULL DEFAULT '{}',
    open_gaps_json      TEXT NOT NULL DEFAULT '{}',
    last_accepted_at    TEXT,
    stale_since         TEXT,
    PRIMARY KEY (source_id, identity)
);

CREATE TABLE epochs (
    epoch_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id           TEXT NOT NULL,
    identity            TEXT NOT NULL,
    reason              TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    opening_computed_input     INTEGER,
    opening_generated_output   INTEGER
);
CREATE INDEX idx_epochs_series ON epochs(source_id, identity, started_at);

CREATE TABLE samples (
    sample_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL,
    identity        TEXT NOT NULL,
    applied_key     TEXT NOT NULL,        -- per-poll application key (replay guard)
    observed_at     TEXT NOT NULL,
    computed_input  INTEGER,
    generated_output INTEGER,
    request_input   INTEGER,
    request_output  INTEGER,
    delta_computed_input    INTEGER,
    delta_generated_output  INTEGER,
    delta_request_input     INTEGER,
    delta_request_output    INTEGER,
    quality_json    TEXT NOT NULL DEFAULT '[]',
    rate_eligible   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_id, identity, applied_key)
);
CREATE INDEX idx_samples_time ON samples(source_id, observed_at);
CREATE INDEX idx_samples_series ON samples(source_id, identity, observed_at);

CREATE TABLE hourly_totals (
    source_id   TEXT NOT NULL,
    identity    TEXT NOT NULL,
    hour        TEXT NOT NULL,            -- canonical 'YYYY-MM-DDTHHZ'
    counter_key TEXT NOT NULL,
    tokens      INTEGER,                  -- NULL = no trustworthy total (gap-only)
    observed_intervals INTEGER NOT NULL DEFAULT 0,
    quality_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (source_id, identity, hour, counter_key)
);
CREATE INDEX idx_hourly_series ON hourly_totals(source_id, identity, hour);
CREATE INDEX idx_hourly_hour ON hourly_totals(hour);

CREATE TABLE gap_spans (
    gap_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id  TEXT NOT NULL,
    identity   TEXT NOT NULL,
    scope      TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT                       -- NULL = still open
);
CREATE INDEX idx_gap_open ON gap_spans(source_id, identity, scope, ended_at);

CREATE TABLE events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id  TEXT,
    identity   TEXT,
    kind       TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_events_time ON events(occurred_at);

-- Key/value runtime state (storage/collector status for later readiness use).
CREATE TABLE collector_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE maintenance_cursor (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
