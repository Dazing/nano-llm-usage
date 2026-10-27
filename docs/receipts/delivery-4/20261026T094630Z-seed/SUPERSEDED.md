# SUPERSEDED — do not use these numbers

Seeded with `scripts/seed_history.py` as it stood at 09:46Z, whose traffic model
dropped every per-poll step below 1 token. At the 15 s cadence used for the last
30 days that froze all four counters, so the last 30 days of this volume carry
zero traffic and the recovered-hour / previous-midnight designed hours were
credited `0`. The 12-day mechanics run that "verified" it agreed with the
manifest because the expectation ledger is derived from the same model, so the
defect was invisible to `--verify`.

Fixed in `scripts/seed_history.py` (`TOKEN_FLOOR` 1.6 -> 240 tokens/minute at
weight 1.0, plus fractional accumulation in `TrafficModel.advance`). The
replacing run is the `-seed` directory with the newest timestamp; see
`docs/delivery-4-status.md` section 5.
