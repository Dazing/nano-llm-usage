# Accounting and operational acceptance scenarios

Normative companion to `specification.md`. These are requirements and synthetic inputs, not executed results. Unless stated otherwise, use one source `jarvis`, labels `model_name="fixture-model",engine="0"`, increasing UTC observation times 15 seconds apart and compatible process-start evidence. Pairs below mean `(computed_input, generated_output)`. All counts are integers; API values are decimal strings.

## A. Normal accounting and identity

1. **Opening balance:** observe `(1000,100)`. Save opening balances. Observed deltas are not yet available: cards show collecting/baseline guidance, not 1100 lifetime tokens. The baseline contributes no observed interval to hourly summaries.
2. **Positive deltas:** next observe `(1600,140)`, then `(1900,160)`. Accepted increments are `(600,40)` and `(300,20)`. Observed lifetime becomes input 900, output 60, processed 960. Summary and history agree.
3. **Idle is real zero:** another `(1900,160)` 15 seconds later contributes `(0,0)` with a valid observed interval; its rates are zero. A new hour containing only valid unchanged observations shows zero, not null.
4. **Identity normalization:** reorder labels and use equivalent escaped label values. Preserve the series and its baseline. An engine `1` observation is a different series; it must not inflate engine `0` cards or borrow its uptime.
5. **Optional request accounting:** at baseline expose request counters `(2000,100)`, then `(3000,140)`. Request increments `(1000,40)` remain separate from engine increments `(600,40)`. Processed engine total remains 640 for that interval, not 1680. Missing request counters display N/A or a flagged partial historical sum, never substitute engine values.
6. **Optional reset independence:** request_input decreases while both engine counters increase normally. Preserve normal engine increments; baseline only request_input anew. request_output continues if valid. Do not erase its prior observed total.

## B. Restart and missing data

7. **Primary reset:** after `(1900,160)`, observe `(50,5)` and then `(90,8)`. Start a new epoch at `(50,5)`, add nothing for that baseline, then add `(40,3)`. Prior observed 960 becomes 1003. Mark the reset boundary; work since the previous scrape before the restart is unknown.
8. **Only one primary decreases:** `(1900,160)` → `(50,170)`. Rebaseline both primary counters; do not credit output 10 while treating its companion as an unrelated session.
9. **Restart hidden by increasing counters:** at UTC 12:00:00 uptime is 100s; at 12:05:00 uptime is 200s. First process-start estimate is around 11:58:20, second around 12:01:40: intervals do not overlap even though uptime increased. Counters may also have increased. Establish a new epoch with no credited delta. Use the HTTP timing/2s tolerance rules, not a naked uptime comparison.
10. **Missing stats only:** primary counters increase but `/stats` times out. Accept nonnegative deltas with continuity_unverified; service is degraded rather than offline. Do not claim exact detection of an unseen restart.
11. **Recoverable metrics outage:** baseline `(100,10)` at 10:00:00; polls fail until a valid `(400,40)` at 10:03:00 with compatible process-start evidence. Recover `(300,30)` once. Rate is null because the interval spans a gap; quality marks collection_gap and time_attribution_approximate. Do not insert zero-token observations during the outage.
12. **Outage with a restart:** same initial baseline, but recovery has disjoint process-start evidence. Credit nothing on recovery; retain a gap and reset event. Next valid contiguous reading can contribute normally.
13. **Series disappears/reappears:** a scrape without the selected series marks it stale/missing. Retain its baseline. Return with compatible evidence uses the gap rule; a different model creates a new identity without resetting old history.
14. **Collector downtime:** no failure rows exist while the container is stopped. Detect a gap from elapsed time exceeding two poll intervals when it resumes. Compatible continuity recovers a delta once; restart evidence rebaselines. History remains readable throughout subsequent Jarvis outages.
15. **Invalid primary pair:** one engine counter missing, malformed, negative, NaN, Infinity or fractional. Reject the primary pair and retain its previous baseline. A later valid pair can recover across the resulting gap. Optional errors must not invalidate otherwise usable engine counters.
16. **Clock regression:** an observation time is equal to or earlier than its prior time. No rate or negative-duration interval is produced. Start a conservative new epoch and record clock_discontinuity; keep existing observed totals unchanged.

## C. Time boundaries, history and precision

17. **Hour boundary:** baseline `(100,10)` at 10:59:50, next `(130,14)` at 11:00:05. Attribute `(30,4)` entirely to the 11:00 UTC bucket; mark time_attribution_approximate. Do not prorate integer tokens or silently attribute all work to the earlier hour.
18. **Gap across midnight:** last success 23:59:50, recovery 00:02:00 with compatible uptime. Attribute its recovered delta to the new day's 00:00 hour. Split gap duration by UTC hour; both affected buckets expose the gap, even if the earlier bucket receives no tokens. A gap-only bucket has null token values, not zero.
19. **No data vs zero:** an untouched day returns null counts; a day of valid unchanged samples returns zero strings. A day with some valid samples and gaps returns observed sums plus gap metadata, never a completeness assertion.
20. **Range contract:** hourly 31-day and daily 366-day aligned requests work; larger/misaligned/reversed requests fail with 400. Unknown identity fails with 404. Half-open bounds exclude a contribution exactly at `to`. The current UTC hour/day is visibly incomplete in the UI.
21. **Parser boundaries:** legal scientific notation `1e3` represents 1000; escaped quote/backslash/newline label values are decoded correctly. Duplicate selected metric with identical labels is invalid. Unrelated metric families do not become counters or series.
22. **Precision and overflow:** accept integer `9007199254740991` as an upstream counter if all other validation passes; reject a larger value with an explicit precision error. Stored sums/API labels can exceed JavaScript Number's safe range and remain exact decimal strings/BigInt. Reject a delta addition that would overflow signed SQLite int64 without advancing any state in that transaction.
23. **Long-term retention:** after raw samples/events expire, the same UTC hourly/daily and lifetime totals and durable quality flags remain. Detailed event history may be unavailable and says so. No hourly total, epoch or baseline is deleted by raw retention.

## D. Transactions and recovery

24. **Crash before commit:** kill between pending sample insertion and transaction commit. On reopen, sample, hourly increments and baseline either all exist or none do. The next reading recovers from the last committed baseline.
25. **Crash after commit:** kill before the collector updates any transient in-memory state. Reopen and read durable state; no replayed credit. Reprocessing the same observation ID is a no-op.
26. **Repeated value, new observation ID:** unchanged counters produce a valid zero increment, not duplicate previous usage.
27. **Second writer:** launch a second application against the same volume. It refuses to start a collector with an actionable lock error. It must not depend only on SQLite's brief transaction locks.
28. **Storage failures:** simulate disk-full, locked and corrupt database paths. Baselines never advance on failed writes. Surface storage failure via readiness/logs even if an event cannot be persisted. Do not initialize an empty replacement over corruption.
29. **Backup consistency:** create an online backup while collection continues. Integrity check passes and all summary totals match one committed snapshot, not a mix of epochs/transactions. Do not require matching later live totals after the backup snapshot.
30. **Restore and migration:** restore to a fresh stopped volume and compare summary values at the backup snapshot. Supported migration preserves them; newer schema version is refused. A deliberate endpoint change under the same SOURCE_ID preserves old totals but starts a fresh baseline.

## E. Application and resource proof

31. **Real production surface:** run the built container against an HTTP fixture and open the served UI in a real browser. Confirm scenario A2 totals appear, select UTC ranges, switch theme, use keyboard controls, inspect diagnostics, disconnect the fixture and restore it. Capture 390px and 1440px views, including stale and empty states. No console errors or direct browser requests to Radiance/CDNs.
32. **Frontend lifetime:** repeatedly change range/series and hide/show the tab. No multiplying polling timers, overlapping stale responses overwriting a new selection, or accumulating Chart.js instances. History remains visible during network/API failures with a stale timestamp.
33. **Resource qualification:** execute specification D9 for at least 30 minutes using the production image, synthetic year of summaries, two tabs (one history timer and one live timer each), a long query and backup, with the live poller running against representative-size `/stats` and `/metrics` payloads. Record measurements; no fabricated success or use of a dev server instead. No OOM under 512 MiB; distinguish preferred RSS target from measured total cgroup usage.
34. **Unraid operator gate:** verify volume survives container recreation, logs rotate, the process is non-root, and private LAN binding works. Observe actual Radiance counter changes after a controlled request. Synthetic fixture results do not prove the live gate. Lack of NAS access must be listed explicitly, not bypassed with invented observations.

## F. Live usage panel (specification D10)

35. **Live current values:** with the fixture reporting `running=3`, `waiting=1`, `prefill_tps=1240.4`, `decode_tps=180.2`, the row shows `3 / 1`, `1,240 tok/s` and `180 tok/s · 60/req` within 2 s. With `running=0` the per-request slot shows `-` while the total rate stays.
36. **Live windows:** draft counters advancing 7 accepted per 10 drafted each second show `70%` in the 5s slot after ≥5 s; the 1m slot stays `-` until ≥60 s of continuous samples and the 30m slot until ≥1800 s (proven with an injected clock). Lifetime is total accepted ÷ total drafted from the first sample. MoE GPU hit behaves the same over `experts.routed_res` / `experts.routed` with no lifetime, and a missing `experts` block makes every MoE slot `-`.
37. **Idle windows:** a window whose denominator did not advance shows `-`, never `0%` and never a carried-over value.
38. **Live run reset:** a `uptime_s` decrease, any live counter decrease, or a gap over 3 s between a source's successful samples clears that source's windows; they refill from `-` and MTP lifetime restarts. The other source's tiles are untouched.
39. **Live staleness:** a source with no successful sample for over 3 s renders `-`; with both sources stale the row is marked stale. Durable collection, `/health/ready` and `/api/v1/status` are unaffected by live failures, and `/api/v1/live` keeps answering while the read-admission cap is saturated.
40. **Live persistence and timers:** no live value reaches SQLite and the durable collector's request volume is unchanged. The browser keeps exactly one 1 s live timer and one 15 s history timer per tab; both pause while hidden and resume when visible; the browser never contacts Radiance directly.

## G. API-equivalent cost estimate (specification D11)

Rates for these scenarios are `COST_RATE_INPUT_PER_MTOK=3`, `COST_RATE_CACHED_INPUT_PER_MTOK=0.30`, `COST_RATE_OUTPUT_PER_MTOK=15`. `Triples` below mean `(request_input, request_output, cached_input)` totals accepted in one hour, and every hour named has at least one observed interval for all three counters unless stated.

41. **Priced hour:** one priced hour totals request input 1,000,000, request output 100,000 and cached input 600,000. `api_equivalent.amount` is exactly `"2.880000"` (`400000×3 + 600000×0.30 + 100000×15` per million), `tokens` are `{"400000","600000","100000"}`, `cache_hit_ratio` is `0.6`, `priced_hours` equals `expected_hours`, `partial` is false and `flags` is empty. The card reads `≈ $2.88` and `Cache hits 60% of input`.
42. **Cache tracking starts late:** a 31-day month where `cached_input` is only observed from day 14. Only the hours with all three counters are priced: days 1–13 contribute nothing to the amount and nothing to `cache_hit_ratio` — request tokens are never priced as if uncached when the cache counter is unknown. `priced_hours` is about 18 days of hours, `expected_hours` the whole month, `partial` true, and the card states the coverage in words (`partial · covers 18.0 of 31.0 days`). A fully tracked month shows no coverage note at all.
43. **Cache counter ahead of request counter:** over the priced hours cached input 1,200,000 exceeds request input 1,000,000. The uncached remainder prices as zero, `flags` contains `cache_exceeds_input`, the amount stays visible and is labelled approximate in the UI (`approximate — cache count ahead of request count`). Never hide the flag by clamping the displayed share.
44. **No rates / partial rates:** with no `COST_RATE_*` set, `api_equivalent.amount` is `null` while `cache_hit_ratio`, `priced_hours` and `expected_hours` remain populated and the card hides the money line. Setting only one or two of the three variables is a startup configuration error that names every missing variable; the process does not start.
45. **Source never publishes the cached counter:** a scrape without `radiance:linear_cached_tokens_total` leaves engine and request counters exactly as before, prices nothing (`amount` null, `priced_hours` 0, coverage `0.0` of the range) and lists a `Cached input` scope in diagnostics. Nothing is ever priced from the two request counters alone.
46. **Money is never stored:** changing the three rates and restarting reprices the same database. No table, column, migration or backup artifact contains an amount, and an older image refuses a schema-3 database rather than guessing at the new columns.

## Suggested raw exposition fixture

Use a local fixture HTTP server; do not send test traffic to Jarvis by default. Change values under test control and expose a compatible `/stats` document containing `model` and `uptime_s`. Keep request-counting behavior distinct from per-step engine counters.

```text
# TYPE radiance:prefill_tokens_total counter
radiance:prefill_tokens_total{model_name="fixture-model",engine="0"} 1000
# TYPE radiance:decode_tokens_total counter
radiance:decode_tokens_total{model_name="fixture-model",engine="0"} 100
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{model_name="fixture-model",engine="0"} 2000
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{model_name="fixture-model",engine="0"} 100
# TYPE radiance:linear_cached_tokens_total counter
radiance:linear_cached_tokens_total{model_name="fixture-model",engine="0"} 1200
```
