#!/usr/bin/env python3
"""Container smoke test (CI): the app from compose.yaml -- python:3.13-slim,
host network, user 568 -- running against tools/fake_wll.py. Checks that the
harvester records polls and summaries, and that the web service serves them.

    python tests/smoke_container.py --data-dir DIR --web http://127.0.0.1:8580
"""

import argparse
import csv
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

FAKE_DID = "001D0A7FFFFF"  # tools/fake_wll.py


def get(url: str) -> tuple[int, str]:
    """(HTTP status, body); status 0 if the server could not be reached."""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read().decode()
    except OSError as e:
        return 0, str(e)


def rows(url: str) -> list[dict[str, str]]:
    code, body = get(url)
    return list(csv.DictReader(io.StringIO(body))) if code == 200 else []


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--web", required=True, help="base URL of the web service")
    p.add_argument("--timeout", type=float, default=120)
    a = p.parse_args()
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
    summary_url = f"{a.web}/csv?table=summary_5s&start={since}"

    st: dict[str, Any] | None = None
    summary: list[dict[str, str]] = []
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        try:
            st = json.loads((a.data_dir / "status.json").read_text())
        except (OSError, ValueError):
            st = None
        summary = rows(summary_url)
        if st and st["http"]["ok"] >= 10 and len(summary) >= 2:
            break
        time.sleep(2)
    else:
        print(f"FAIL: no data within {a.timeout:.0f} s; status={st}, summary rows={len(summary)}")
        return 1

    iss = rows(f"{a.web}/csv?table=iss_si&start={since}")
    health = get(f"{a.web}/health")
    page = get(f"{a.web}/")
    script = get(f"{a.web}/static/app.js")
    latest = json.loads(get(f"{a.web}/api/latest")[1] or "{}")
    now = time.time()
    series = json.loads(get(f"{a.web}/api/series?t0={now - 120}&t1={now}&step=5")[1] or "{}")
    raw = sorted(a.data_dir.glob("raw/*/wll_http_*.jsonl*"))
    checks = [
        ("harvester talks to the simulated WLL", st["did"] == FAKE_DID),
        ("UDP is off, as compose.yaml says", st["udp"]["enabled"] is False),
        ("no failed polls", st["http"]["fail"] == 0),
        ("raw archive written", bool(raw)),
        ("summaries have statistics", all(r["temp_c_mean"] and int(r["n_polls"]) >= 1 for r in summary)),
        ("10 s data served", len(iss) >= 10 and all(r["temp_c"] for r in iss)),
        ("health endpoint OK", health == (200, "OK\n")),
        ("dashboard page served", page[0] == 200 and "uPlot" in page[1]),
        ("dashboard script served", script[0] == 200 and "api/series" in script[1]),
        ("latest readings in SI", (latest.get("iss") or {}).get("temp_c") is not None),
        ("chart series with data", any(v is not None for v in series.get("temp_c_mean", []))),
    ]
    for what, ok in checks:
        print(f"{'ok  ' if ok else 'FAIL'} {what}")
    print(f"{st['http']['ok']} polls, {len(summary)} summary rows, {len(iss)} rows of 10 s data")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
