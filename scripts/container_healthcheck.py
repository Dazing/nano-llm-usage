#!/usr/bin/env python3
"""Container readiness probe: standard library only, no curl, no wget.

Exit 0 only when ``/health/ready`` answers 200 with ``status: "ready"``.
The liveness endpoint is deliberately not used here: a container whose
collector thread died is alive but must stop receiving traffic.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

PORT = os.environ.get("PORT", "8080")
URL = f"http://127.0.0.1:{PORT}/health/ready"


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=4) as response:
            status = response.status
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"readiness probe failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    ready = status == 200 and body.get("status") == "ready"
    if not ready:
        print(
            f"readiness probe: status={status} state={body.get('status')} "
            f"storage={body.get('storage_code')}",
            file=sys.stderr,
        )
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
