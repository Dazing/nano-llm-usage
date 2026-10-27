# Step 4 / D5 — one-year seed through the real collector, verified through the real API

Executed 2026-10-26 on the qualification host (see `../20261026T100600Z-preflight/environment.json`).

```
.venv/bin/python scripts/seed_history.py \
  --db /tmp/d4/full2/dashboard.sqlite3 \
  --base-url http://127.0.0.1:0 \
  --source-id qual-jarvis --seed 20260426 \
  --manifest /tmp/d4/full2/seed-manifest.json          # exit 0, seed-receipts.txt
.venv/bin/python scripts/seed_history.py \
  --db /tmp/d4/full2/dashboard.sqlite3 \
  --manifest /tmp/d4/full2/seed-manifest.json --verify # exit 0, verify.log (1518 checks, 0 failures)
```

`seed-manifest.json` here is the receipt copy: `hourly_cases` is trimmed to the
4 hours the verifier asserts (`hourly_cases_total_hours` = 8769, note and the
full file's SHA-256 inside the file); `daily` keeps all 368 days.
`verify-trimmed-manifest.log` re-runs the same verification against this trimmed
copy — exit 0, `checks=1518 failures=0` — so the receipt is self-sufficient.

The 987 MB volume stays in the harness working directory `/tmp/d4/full2/`
(`*.sqlite3*` is git-ignored); it is the input for the growth math in
`../20261026T110900Z-growth/` and for image case 4 once a container runtime exists.
