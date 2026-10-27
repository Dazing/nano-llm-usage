# Final-tree gate chain (Step 9 verification)

Run in one chain after every Delivery 4 change was on disk, in this order:

```
.venv/bin/python -m pytest -q                # exit 0 → pytest.log  (243 passed)
.venv/bin/python scripts/smoke.py            # exit 0 → smoke.log   (61 [ok ], SMOKE PASS,
                                             #   "restore refuses a running target: 3")
.venv/bin/python scripts/smoke_frontend.py   # exit 0 → frontend.log (14 phases, 65 [ok ],
                                             #   0 FAIL, FRONTEND SMOKE PASSED)
```

`exits.txt` records the three exit codes. `shots/` copies the 9 screenshots the
browser harness wrote for this run (its own output directory is
`docs/receipts/delivery-3/`). The image build/inspect, the nine image cases and
the ≥30-minute workload are **not** part of this chain: they require a container
runtime and are recorded NOT EXECUTED in `docs/delivery-4-status.md` §4.
