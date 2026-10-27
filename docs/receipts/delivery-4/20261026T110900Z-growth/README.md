# AC7 growth math on the verified one-year volume

```
.venv/bin/python scripts/qualify_resources.py --growth-only \
  --db /tmp/d4/full2/dashboard.sqlite3 --window-days 367.0 \
  --out /tmp/d4/receipts/growth2          # exit 0, 3 checks, 0 failures
```

`VACUUM INTO` a scratch copy of the 987,324,416-byte volume (the live volume is
never opened for writing), then per-table bytes from `dbstat` **including every
index of the table** (`PRAGMA index_list`), row counts, per-row bytes and the
stated extrapolation assumptions. `growth.json` is the machine-readable result;
`run.log` is the harness output. This is the size half of AC7 only — the
measured runtime half of AC6/AC7 (≥30-minute workload) needs a container runtime
and is recorded NOT EXECUTED in `docs/delivery-4-status.md` section 4.
