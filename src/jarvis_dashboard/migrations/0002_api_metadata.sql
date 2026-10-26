-- 0002: durable prerequisites for the API (Delivery 2 plan Step 2).
--
-- Adds per-counter contribution coverage (first/last contributing observation,
-- metadata completeness), series presence + stable enumeration key, primary
-- collection success timestamps, compact latest-accepted-interval diagnostics
-- for rates, and indexes for the bounded API queries. Legacy rows are filled by
-- a bounded, resumable backfill in storage.backfill_api_metadata(): rows whose
-- raw evidence has already been purged keep metadata_complete = 0 with null
-- bounds rather than inventing clean coverage.

ALTER TABLE hourly_totals ADD COLUMN first_observed_at TEXT;
ALTER TABLE hourly_totals ADD COLUMN last_observed_at TEXT;
ALTER TABLE hourly_totals ADD COLUMN metadata_complete INTEGER NOT NULL DEFAULT 0;
ALTER TABLE series ADD COLUMN seq INTEGER;
ALTER TABLE series ADD COLUMN first_valid_at TEXT;
ALTER TABLE series ADD COLUMN last_valid_at TEXT;
ALTER TABLE sources ADD COLUMN first_success_at TEXT;
ALTER TABLE sources ADD COLUMN last_success_at TEXT;

-- Latest accepted interval per counter: the only source for instantaneous
-- rates, kept so rates stay answerable after raw samples are retained away.
CREATE TABLE latest_interval (
    source_id        TEXT NOT NULL,
    identity         TEXT NOT NULL,
    counter_key      TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    tokens           INTEGER,
    interval_seconds REAL,
    eligible         INTEGER NOT NULL DEFAULT 0,
    quality_json     TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (source_id, identity, counter_key)
);

CREATE INDEX idx_series_enum ON series(source_id, seq);
CREATE INDEX idx_events_series_order ON events(source_id, occurred_at, event_id);
CREATE INDEX idx_gap_scope ON gap_spans(source_id, scope, ended_at, started_at);
CREATE INDEX idx_epochs_series_order ON epochs(source_id, identity, started_at, epoch_id);
