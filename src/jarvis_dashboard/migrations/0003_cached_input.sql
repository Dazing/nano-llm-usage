-- 0003: cache-aware API-equivalent cost estimate (cost plan D2).
--
-- Adds the durable raw and delta columns for the optional `cached_input`
-- counter (radiance:linear_cached_tokens_total). hourly_totals, latest_interval
-- and gap_spans are already keyed by counter key / scope and need no change;
-- series_state.optional_json stores one entry per optional key.
--
-- Both columns are nullable: rows written before cache tracking existed stay
-- null, which is exactly "not observed", so they are never priced. Cost itself
-- is computed at read time and is never stored.

ALTER TABLE samples ADD COLUMN cached_input INTEGER;
ALTER TABLE samples ADD COLUMN delta_cached_input INTEGER;
