#!/usr/bin/env python3
"""wllharvest -- local data harvester for a Davis WeatherLink Live (WLL).

The WLL serves a local API (https://weatherlink.github.io/weatherlink-live-local-api/):

  GET /v1/current_conditions    Latest record for every sensor the WLL tracks
                                (ISS, leaf/soil, its own barometer and inside
                                T/RH). Polled every POLL_INTERVAL s; the
                                documented minimum interval is 10 s.
  GET /v1/real_time?duration=N  Asks the WLL to broadcast ISS wind and rain as
                                UDP on port 22222 every 2.5 s for N s (max
                                86400). Broadcasts do not cross routers, so
                                this only works on the WLL's own subnet.

Everything received is written twice under DATA_DIR:

  raw/YYYY/wll_{http,udp}_YYYYMMDD.jsonl[.gz]
      The record of truth: one JSON line per response, packet or failure, with
      the device's JSON spliced in verbatim. UTC days, gzipped once finished.
  wll.sqlite
      Parsed tables in the WLL's native units (degF, mph, inHg, rain counts),
      *_si views in SI units, and summary_10min: min/mean/median/max, rain and
      wind direction per 10 minutes. Derived from raw/: `rebuild` regenerates it.

`serve` offers CSV extracts of the summaries and 10 s data over HTTP.
Standard library only (Python >= 3.13), so it runs on a stock python image.
"""

import argparse
import collections
import contextlib
import csv
import datetime as dt
import gzip
import http.client
import http.server
import io
import json
import logging
import math
import os
import re
import select
import shutil
import signal
import socket
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from types import FrameType
from typing import Any, TextIO, cast

Json = dict[str, Any]

VERSION = "1.3.0"
MIN_POLL_INTERVAL = 10.0  # documented limit for /v1/current_conditions
MAX_RT_DURATION = 86400  # documented limit for /v1/real_time
UDP_STALL = 30.0  # s of UDP silence before re-requesting broadcasts
UDP_STALL_MAX = 600.0  # cap on the doubling stall wait
HOUSEKEEPING = 60.0  # s between fsync / status / summary checks

log = logging.getLogger("wllharvest")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Documented record fields (API v1) per table. Tables get these columns up
# front so the SI views always compile; fields a later firmware adds become
# new columns automatically when first seen.
FIELDS = {
    "iss": """txid temp hum dew_point wet_bulb heat_index wind_chill thw_index
        thsw_index wind_speed_last wind_dir_last wind_speed_avg_last_1_min
        wind_dir_scalar_avg_last_1_min wind_speed_avg_last_2_min
        wind_dir_scalar_avg_last_2_min wind_speed_hi_last_2_min
        wind_dir_at_hi_speed_last_2_min wind_speed_avg_last_10_min
        wind_dir_scalar_avg_last_10_min wind_speed_hi_last_10_min
        wind_dir_at_hi_speed_last_10_min rain_size rain_rate_last rain_rate_hi
        rainfall_last_15_min rain_rate_hi_last_15_min rainfall_last_60_min
        rainfall_last_24_hr rain_storm rain_storm_start_at solar_rad uv_index
        rx_state trans_battery_flag rainfall_daily rainfall_monthly
        rainfall_year rain_storm_last rain_storm_last_start_at
        rain_storm_last_end_at""".split(),
    "soil_leaf": """txid temp_1 temp_2 temp_3 temp_4 moist_soil_1 moist_soil_2
        moist_soil_3 moist_soil_4 wet_leaf_1 wet_leaf_2 rx_state
        trans_battery_flag""".split(),
    "baro": "bar_sea_level bar_trend bar_absolute".split(),
    "inside": "temp_in hum_in dew_point_in heat_index_in".split(),
    "rt_iss": """txid wind_speed_last wind_dir_last rain_size rain_rate_last
        rain_15_min rain_60_min rain_24_hr rain_storm rain_storm_start_at
        rainfall_daily rainfall_monthly rainfall_year wind_speed_hi_last_10_min
        wind_dir_at_hi_speed_last_10_min""".split(),
}
# (stream, data_structure_type) -> table; anything else goes to <stream>_type<N>
TABLE_FOR: dict[tuple[str, Any], str] = {
    ("http", 1): "iss",
    ("http", 2): "soil_leaf",
    ("http", 3): "baro",
    ("http", 4): "inside",
    ("udp", 1): "rt_iss",
}
BASE_COLS = ("recv_time", "ts", "did", "lsid")

# mm per rain count, by the record's rain_size code (0.01 in, 0.2 mm, 0.1 mm, 0.001 in)
RAIN_MM = "(CASE rain_size WHEN 1 THEN 0.254 WHEN 2 THEN 0.2 WHEN 3 THEN 0.1 WHEN 4 THEN 0.0254 END)"
CONVERT = {  # native unit -> (column suffix in *_si view, SQL expression)
    "degF": ("_c", "({c} - 32.0) / 1.8"),
    "mph": ("_ms", "{c} * 0.44704"),
    "inHg": ("_hpa", "{c} * 33.8638866667"),
    "count": ("_mm", "{c} * " + RAIN_MM),
    "count/h": ("_mmh", "{c} * " + RAIN_MM),
}
DEGF = {
    "temp",
    "dew_point",
    "wet_bulb",
    "heat_index",
    "wind_chill",
    "thw_index",
    "thsw_index",
    "temp_in",
    "dew_point_in",
    "heat_index_in",
    "temp_1",
    "temp_2",
    "temp_3",
    "temp_4",
}
RAIN_COUNTS = {"rain_storm", "rain_storm_last", "rain_15_min", "rain_60_min", "rain_24_hr"}


def unit_of(field: str) -> str | None:
    """Native unit of a documented field, if the SI view must convert it."""
    if field in DEGF:
        return "degF"
    if field.startswith("wind_speed"):
        return "mph"
    if field.startswith("bar_"):
        return "inHg"
    if field.startswith("rain_rate"):
        return "count/h"
    if field.startswith("rainfall") or field in RAIN_COUNTS:
        return "count"
    return None  # %RH, degrees, W/m2, UV index, cb, codes, epoch seconds


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def column_name(key: object) -> str:
    col = re.sub(r"\W", "_", str(key), flags=re.ASCII) or "_"
    if col[0].isdigit():
        col = "_" + col
    return "rec_" + col if col in ("recv_time", "ts", "did") else col


def si_view_sql(table: str) -> str:
    # time_utc is the host's (NTP) arrival time: the WLL's integer ts runs
    # ~0.3 s slow and truncates, so it reads 0.3-1.3 s early (see README).
    cols = ["recv_time", "ts", "strftime('%Y-%m-%d %H:%M:%f', recv_time, 'unixepoch') AS time_utc", "did", "lsid"]
    for f in FIELDS[table]:
        kind = unit_of(f)
        if kind is None:
            cols.append(q(f))
        else:
            suffix, expr = CONVERT[kind]
            cols.append(f"{expr.format(c=q(f))} AS {q(f + suffix)}")
    return f"CREATE VIEW {q(table + '_si')} AS SELECT {', '.join(cols)} FROM {q(table)}"


# Period summaries, derived from the 10 s records (recomputable at any time).
# Fields reduced to min/mean/median/max per period, in SI units:
#   summary column prefix -> (*_si view, column of that view)
SUMMARY_STATS = {
    "temp_c": ("iss_si", "temp_c"),
    "rh": ("iss_si", "hum"),
    "dew_point_c": ("iss_si", "dew_point_c"),
    "solar_wm2": ("iss_si", "solar_rad"),
    "uv_index": ("iss_si", "uv_index"),
    "wind_ms": ("iss_si", "wind_speed_last_ms"),
    "rain_rate_mmh": ("iss_si", "rain_rate_last_mmh"),
    "slp_hpa": ("baro_si", "bar_sea_level_hpa"),
    "station_p_hpa": ("baro_si", "bar_absolute_hpa"),
    "temp_in_c": ("inside_si", "temp_in_c"),
    "rh_in": ("inside_si", "hum_in"),
}
STAT_NAMES = ("min", "mean", "median", "max")
ISS_EXTRA = [
    "wind_dir_last",
    "rx_state",
    "trans_battery_flag",
    "rainfall_daily_mm",
    "wind_speed_avg_last_10_min_ms",
    "wind_dir_scalar_avg_last_10_min",
    "wind_speed_hi_last_10_min_ms",
    "wind_dir_at_hi_speed_last_10_min",
]
SUMMARY_COLS = (
    ["time_utc", "n_polls", "n_failed", "rx_ok_frac", "battery_low", "rain_mm"]
    + [f"{p}_{s}" for p in SUMMARY_STATS for s in STAT_NAMES]
    + [
        "wind_dir_mean_deg",
        "wind_dir_steadiness",
        "n_calm",
        "wind_vec_ms",
        "wind_vec_dir_deg",
        "wll_wind_avg_10min_ms",
        "wll_wind_dir_10min_deg",
        "wll_gust_10min_ms",
        "wll_gust_dir_10min_deg",
    ]
)
RAIN_GAP = 60.0  # s; rain increments count only between samples this close
RESET_MAX_MM = 1.0  # a counter drop to at most this is the WLL's midnight reset
GLITCH_SAMPLES = 3  # a lower reading seen this many times in a row becomes the baseline
RETRY_DELAY = 1.5  # s before retrying a poll the WLL dropped (it serves one client at a time)
SUMMARY_PERIOD = int(os.environ.get("WLL_SUMMARY_PERIOD") or 600)  # s


class RainCounter:
    """Turns the cumulative rainfall_daily counter (mm) into increments.

    A drop to near zero is the WLL's reset at local midnight, and the new
    value is rain since then. Any other drop is treated as a glitch and
    ignored, keeping the higher reading as the reference so nothing is counted
    twice when it comes back; if the lower reading persists, it becomes the new
    baseline without being counted. Samples more than RAIN_GAP apart re-anchor
    the counter without crediting rain, since its timing is unknown."""

    def __init__(self) -> None:
        self.t: float | None = None
        self.ref = 0.0
        self.low = 0

    def step(self, t: float, mm: float) -> float | None:
        """Rain since the previous sample, or None if it cannot be attributed."""
        if self.t is None or t - self.t > RAIN_GAP:
            self.t, self.ref, self.low = t, mm, 0
            return None
        self.t = t
        if mm >= self.ref:
            inc, self.ref, self.low = mm - self.ref, mm, 0
            return inc
        if mm <= RESET_MAX_MM:
            self.ref, self.low = mm, 0
            return mm
        self.low += 1
        if self.low >= GLITCH_SAMPLES:
            self.ref, self.low = mm, 0
        return 0.0


def summary_table(period: int) -> str:
    return f"summary_{period // 60}min" if period % 60 == 0 else f"summary_{period}s"


def vector_mean(dirs: Sequence[float], weights: Sequence[float] | None = None) -> tuple[float | None, float | None]:
    """(direction deg, magnitude) of the mean of compass directions as vectors
    of the given lengths (unit vectors by default); (None, None) if empty."""
    if not dirs:
        return None, None
    weights = weights or [1.0] * len(dirs)
    x = math.fsum(w * math.sin(math.radians(d)) for d, w in zip(dirs, weights, strict=True)) / len(dirs)
    y = math.fsum(w * math.cos(math.radians(d)) for d, w in zip(dirs, weights, strict=True)) / len(dirs)
    mag = math.hypot(x, y)
    return (round(math.degrees(math.atan2(x, y)), 3) % 360 if mag > 1e-12 else None), mag


class Store:
    """SQLite database of parsed records. Ingest is idempotent, so replaying
    raw lines (rebuild) never duplicates rows."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=60)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.cols: dict[str, set[str]] = {}
        with self.transaction():
            self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS polls (recv_time REAL NOT NULL UNIQUE, ts INTEGER,"
                " did TEXT, ok INTEGER NOT NULL, status INTEGER, latency REAL, error TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS rt_requests (recv_time REAL NOT NULL UNIQUE,"
                " ok INTEGER NOT NULL, status INTEGER, latency REAL, duration INTEGER,"
                " broadcast_port INTEGER, error TEXT)"
            )
            for table, fields in FIELDS.items():
                self._ensure(table, fields)
                self.db.execute(f"DROP VIEW IF EXISTS {q(table + '_si')}")
                self.db.execute(si_view_sql(table))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('created_utc', ?)", (utc_iso(time.time()),))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('harvester_version', ?)", (VERSION,))

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            self.cols.clear()  # an ALTER TABLE may have been rolled back
            raise
        self.db.execute("COMMIT")

    def close(self) -> None:
        self.db.close()

    def _ensure(self, table: str, fields: Iterable[str]) -> None:
        have = self.cols.get(table)
        if have is None:
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({q(table)})")}
            if not have:
                self.db.execute(
                    f"CREATE TABLE {q(table)} (recv_time REAL NOT NULL, ts INTEGER,"
                    " did TEXT, lsid INTEGER, UNIQUE (ts, lsid, did))"
                )
                have = set(BASE_COLS)
                if table not in FIELDS:
                    log.warning("new record type: created table %s", table)
            self.db.execute(f"CREATE INDEX IF NOT EXISTS {q(table + '_recv_time')} ON {q(table)} (recv_time)")
            self.cols[table] = have
        for f in fields:
            if f not in have:
                self.db.execute(f"ALTER TABLE {q(table)} ADD COLUMN {q(f)}")
                have.add(f)
                if f not in FIELDS.get(table, ()):
                    log.warning("new field: added column %s.%s", table, f)

    def _insert(self, table: str, recv: float, ts: Any, did: Any, rec: Json) -> None:
        row: Json = {"recv_time": recv, "ts": ts, "did": did}
        for k, v in rec.items():
            if k == "data_structure_type":  # implied by the table
                continue
            if isinstance(v, (dict, list)):
                row[column_name(k)] = json.dumps(v, separators=(",", ":"))
            elif isinstance(v, int) and not -(2**63) <= v < 2**63:
                row[column_name(k)] = str(v)
            else:
                row[column_name(k)] = v
        self._ensure(table, [c for c in row if c not in BASE_COLS])
        self.db.execute(
            f"INSERT OR IGNORE INTO {q(table)} ({', '.join(map(q, row))}) VALUES ({', '.join('?' * len(row))})",
            list(row.values()),
        )

    def _conditions(self, stream: str, recv: float, data: Json) -> None:
        ts, did = data.get("ts"), data.get("did")
        for c in data.get("conditions") or []:
            if isinstance(c, dict):
                dst = c.get("data_structure_type")
                table = TABLE_FOR.get((stream, dst)) or f"{stream}_type" + re.sub(r"\W", "_", str(dst), flags=re.ASCII)
                self._insert(table, recv, ts, did, c)

    def ingest(self, rec: Json) -> tuple[str | None, Json | None]:
        """Parse one raw-archive line (decoded) into the tables.

        Returns (error or None, payload) where payload is the WLL's `data`
        object for HTTP lines and the packet itself for UDP lines."""
        kind, recv, body = rec.get("kind"), rec.get("recv"), rec.get("body")
        if not isinstance(recv, (int, float)):
            return "raw line has no recv time", None
        if kind == "udp":
            if not isinstance(body, dict):
                return "unparseable UDP packet", None
            self._conditions("udp", recv, body)
            return None, body
        status, err, data = rec.get("status"), rec.get("error"), None
        if err is None:
            if not isinstance(body, dict):
                err = f"HTTP {status}: body is not JSON"
            elif body.get("error") is not None:
                err = f"HTTP {status}: device error {json.dumps(body['error'])}"
            elif status != 200:
                err = f"HTTP {status}"
            elif not isinstance(body.get("data"), dict):
                err = f"HTTP {status}: no data object"
            else:
                data = body["data"]
        d = data or {}
        if kind == "cc":
            self.db.execute(
                "INSERT OR IGNORE INTO polls VALUES (?,?,?,?,?,?,?)",
                (recv, d.get("ts"), d.get("did"), int(err is None), status, rec.get("latency"), err),
            )
            if data:
                self._conditions("http", recv, data)
        elif kind == "rt":
            self.db.execute(
                "INSERT OR IGNORE INTO rt_requests VALUES (?,?,?,?,?,?,?)",
                (recv, int(err is None), status, rec.get("latency"), d.get("duration"), d.get("broadcast_port"), err),
            )
        else:
            err = f"unknown raw record kind {kind!r}"
        return err, data

    # -- period summaries ---------------------------------------------------

    def last_summary(self, period: int) -> int | None:
        """Start of the newest summary row for this period, or None."""
        table = summary_table(period)
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone():
            return None
        return self.db.execute(f"SELECT max(t_start) FROM {q(table)}").fetchone()[0]

    def summarize(self, period: int, t0: float, t1: float) -> int:
        """(Re)compute the summary row of every period starting in [t0, t1)
        from the stored 10 s records, a day per transaction; returns the
        number of rows written. A period still in progress gets a partial
        row (n_polls short of period/interval), replaced when redone."""
        table = summary_table(period)
        start, n = math.floor(t0 / period) * period, 0
        stop = math.ceil(t1 / period) * period
        chunk = max(1, 86400 // period) * period
        while start < stop:
            end = min(start + chunk, stop)
            with self.transaction():
                self._ensure_summary(table)
                n += self._summarize_chunk(table, period, start, end)
            start = end
        return n

    def _ensure_summary(self, table: str) -> None:
        if table in self.cols:
            return
        self.db.execute(f"CREATE TABLE IF NOT EXISTS {q(table)} (t_start INTEGER PRIMARY KEY)")
        have = {r[1] for r in self.db.execute(f"PRAGMA table_info({q(table)})")}
        for c in SUMMARY_COLS:
            if c not in have:
                self.db.execute(f"ALTER TABLE {q(table)} ADD COLUMN {q(c)}")
        self.cols[table] = have | set(SUMMARY_COLS)

    def _summarize_chunk(self, table: str, period: int, start: int, end: int) -> int:
        bins: collections.defaultdict[int, collections.defaultdict[str, list[Any]]] = collections.defaultdict(
            lambda: collections.defaultdict(list)
        )

        def key(t: float) -> int:
            return int(t // period) * period

        views: collections.defaultdict[str, list[tuple[str, str]]] = collections.defaultdict(list)
        for prefix, (view, col) in SUMMARY_STATS.items():
            views[view].append((prefix, col))
        for view, pcs in views.items():
            cols = list(dict.fromkeys([c for _, c in pcs] + (ISS_EXTRA if view == "iss_si" else [])))
            lead = RAIN_GAP if view == "iss_si" else 0.0  # previous sample, for rain increments
            rain_counter = RainCounter()
            for r in self.db.execute(
                f"SELECT recv_time, {', '.join(map(q, cols))} FROM {q(view)}"
                " WHERE recv_time >= ? AND recv_time < ? ORDER BY recv_time",
                (start - lead, end),
            ):
                t, vals = r[0], dict(zip(cols, r[1:], strict=True))
                if view == "iss_si" and vals["rainfall_daily_mm"] is not None:
                    inc = rain_counter.step(t, vals["rainfall_daily_mm"])
                    if inc is not None and t >= start:
                        bins[key(t)]["rain_mm"].append(inc)
                if t < start:
                    continue
                b = bins[key(t)]
                # While the ISS link is lost (rx_state 2) the WLL repeats its last
                # values, so those samples count for rx_ok_frac but not the statistics.
                lost = view == "iss_si" and vals["rx_state"] == 2
                for prefix, col in pcs:
                    if vals[col] is not None and not lost:
                        b[prefix].append(vals[col])
                if view == "iss_si":
                    b["iss"].append(vals)
        for t, ok in self.db.execute(
            "SELECT recv_time, ok FROM polls WHERE recv_time >= ? AND recv_time < ?", (start, end)
        ):
            bins[key(t)]["ok" if ok else "failed"].append(t)

        for b0, v in bins.items():
            row: Json = {
                "t_start": b0,
                "time_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(b0)),
                "n_polls": len(v["ok"]),
                "n_failed": len(v["failed"]),
            }
            iss = v["iss"]
            rx = [s["rx_state"] for s in iss if s["rx_state"] is not None]
            row["rx_ok_frac"] = sum(x == 0 for x in rx) / len(rx) if rx else None
            fresh = [s for s in iss if s["rx_state"] != 2]
            bat = [s["trans_battery_flag"] for s in fresh if s["trans_battery_flag"] is not None]
            row["battery_low"] = max(bat) if bat else None
            row["rain_mm"] = math.fsum(v["rain_mm"]) if v["rain_mm"] else None
            for prefix in SUMMARY_STATS:
                xs = v[prefix]
                stats = (min(xs), statistics.fmean(xs), statistics.median(xs), max(xs)) if xs else (None,) * 4
                row.update(zip((f"{prefix}_{s}" for s in STAT_NAMES), stats, strict=True))
            wind = [
                (s["wind_speed_last_ms"], s["wind_dir_last"])
                for s in fresh
                if s["wind_speed_last_ms"] is not None and s["wind_dir_last"] is not None
            ]
            moving = [d for sp, d in wind if sp > 0]
            row["n_calm"] = len(wind) - len(moving) if wind else None
            # Compass directions are averaged as unit vectors (calms excluded);
            # steadiness is the resultant length, 1 = constant direction.
            row["wind_dir_mean_deg"], row["wind_dir_steadiness"] = vector_mean(moving)
            row["wind_vec_dir_deg"], row["wind_vec_ms"] = vector_mean([d for _, d in wind], [sp for sp, _ in wind])
            last = fresh[-1] if fresh else {}
            row["wll_wind_avg_10min_ms"] = last.get("wind_speed_avg_last_10_min_ms")
            row["wll_wind_dir_10min_deg"] = last.get("wind_dir_scalar_avg_last_10_min")
            row["wll_gust_10min_ms"] = last.get("wind_speed_hi_last_10_min_ms")
            row["wll_gust_dir_10min_deg"] = last.get("wind_dir_at_hi_speed_last_10_min")
            self.db.execute(
                f"INSERT OR REPLACE INTO {q(table)} ({', '.join(map(q, row))}) VALUES ({', '.join('?' * len(row))})",
                list(row.values()),
            )
        return len(bins)


# ---------------------------------------------------------------------------
# Raw archive
# ---------------------------------------------------------------------------


def raw_line(meta: Json, text: str | None) -> str:
    """One archive line: `meta` as JSON with the device's reply spliced in
    verbatim as "body" (or kept as a "text" string if it is not JSON)."""
    if text is None:
        return json.dumps(meta, separators=(",", ":"))
    body = text.strip()
    try:
        json.loads(body)
    except ValueError:
        return json.dumps(dict(meta, text=text), separators=(",", ":"))
    # Valid JSON cannot hold a raw newline inside a string, so this is safe.
    body = body.replace("\r", " ").replace("\n", " ")
    return json.dumps(meta, separators=(",", ":"))[:-1] + ',"body":' + body + "}"


def gzip_file(path: Path) -> None:
    """Compress a finished day file to <name>.gz, appending a new gzip member
    if the .gz already exists (clock stepped back across midnight)."""
    gz = path.with_name(path.name + ".gz")
    tmp = gz.with_name(gz.name + ".tmp")
    if gz.exists():
        shutil.copyfile(gz, tmp)
    with open(path, "rb") as src, open(tmp, "ab") as dst:
        with gzip.GzipFile(filename=path.name, mode="wb", fileobj=dst, mtime=0) as z:
            shutil.copyfileobj(src, z)
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(tmp, gz)
    path.unlink()
    log.info("compressed %s", gz)


class RawArchive:
    """Append-only daily (UTC) JSON-lines files for one stream."""

    def __init__(self, root: str | Path, stream: str) -> None:
        self.root, self.stream = Path(root), stream
        self.day: str | None = None
        self.fh: TextIO | None = None

    def write(self, recv: float, line: str) -> None:
        day = time.strftime("%Y%m%d", time.gmtime(recv))
        if day != self.day or self.fh is None:
            self.close()
            path = self.root / day[:4] / f"wll_{self.stream}_{day}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            # Kept open across writes; closed at the day rollover or close().
            self.fh, self.day = open(path, "a", encoding="utf-8"), day  # noqa: SIM115
            self.compress_finished(day)
        assert self.fh is not None
        self.fh.write(line + "\n")
        self.fh.flush()

    def compress_finished(self, today: str) -> None:
        for p in sorted(self.root.glob(f"*/wll_{self.stream}_*.jsonl")):
            if p.name != f"wll_{self.stream}_{today}.jsonl":
                try:
                    gzip_file(p)
                except OSError as e:  # retried at the next rollover
                    log.error("could not compress %s: %s", p, e)

    def sync(self) -> None:
        if self.fh:
            os.fsync(self.fh.fileno())

    def close(self) -> None:
        if self.fh:
            self.fh.flush()
            os.fsync(self.fh.fileno())
            self.fh.close()
        self.day = self.fh = None


def read_raw(paths: Iterable[Path]) -> Iterator[tuple[Path, int, Any]]:
    """Yield (path, line number, decoded record) from raw archive files,
    skipping (and logging) undecodable lines such as a crash-truncated tail."""
    for p in paths:
        with (
            gzip.open(p, "rt", encoding="utf-8", errors="replace")
            if p.suffix == ".gz"
            else open(p, encoding="utf-8", errors="replace")
        ) as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    yield p, n, json.loads(line)
                except ValueError:
                    log.warning("%s:%d: undecodable line skipped", p, n)


# ---------------------------------------------------------------------------
# Harvester
# ---------------------------------------------------------------------------


def utc_iso(t: float | None) -> str | None:
    return None if t is None else dt.datetime.fromtimestamp(t, dt.UTC).isoformat(timespec="seconds")


def transient(err: str) -> bool:
    """True for connection-level failures worth one quick retry (not timeouts,
    HTTP errors or bad payloads)."""
    return any(
        s in err
        for s in ("RemoteDisconnected", "Connection refused", "Connection reset", "IncompleteRead", "BadStatusLine")
    )


def noteworthy(n: int) -> bool:
    """Log the 1st, 3rd, 10th, 30th, ... and every 360th repeat of a failure."""
    return n in (1, 3, 10, 30, 100) or n % 360 == 0


class Harvester:
    def __init__(self, cfg: argparse.Namespace) -> None:
        self.cfg = cfg
        self.base = f"http://{cfg.host}" + ("" if cfg.port == 80 else f":{cfg.port}")
        data = Path(cfg.data_dir)
        data.mkdir(parents=True, exist_ok=True)
        self.store = Store(data / "wll.sqlite")
        self.raw: dict[str, RawArchive] = {s: RawArchive(data / "raw", s) for s in ("http", "udp")} if cfg.raw else {}
        self.status_path = data / "status.json"
        self.stopping = False
        self.sock: socket.socket | None = None
        self.did: str | None = None
        self.started = time.time()
        # First summary period not yet written; run() catches up from the database.
        self.sum_from = math.floor(self.started / cfg.summary_period) * cfg.summary_period
        # HTTP polling
        self.http_ok = self.http_fail = self.http_consec = self.http_retries = 0
        self.http_last_ok: float | None = None
        self.http_fail_since: float | None = None
        self.http_last_error: str | None = None
        self.http_last_error_time: float | None = None
        self.offsets: list[float] = []  # recv_time - device ts, this hour
        self.last_offset: float | None = None
        self.clock_warned = 0.0
        # UDP real-time
        self.udp_note: str | None = "disabled" if not cfg.udp else None
        self.udp_packets = self.udp_bad = self.udp_dups = 0
        self.udp_last: float | None = None
        self.udp_last_mono: float | None = None
        self.udp_prev: bytes | None = None
        self.udp_prev_time = 0.0
        self.rt_ok = self.rt_fail = self.rt_consec = 0
        self.rt_ok_mono: float | None = None
        self.rt_next = 0.0
        self.rt_last_error: str | None = None
        self.stall_wait, self.stalls = UDP_STALL, 0
        self.hour = self.hour_polls = self.hour_ok = self.hour_udp = 0

    # -- plumbing -----------------------------------------------------------

    def record(self, stream: str, meta: Json, text: str | None) -> tuple[str | None, Json | None]:
        """Archive one response/packet/failure and ingest it into the DB."""
        line = raw_line(meta, text)
        if stream in self.raw:
            try:
                self.raw[stream].write(meta["recv"], line)
            except OSError as e:
                log.error("raw archive write failed: %s", e)
        try:
            with self.store.transaction():
                return self.store.ingest(json.loads(line))
        except sqlite3.Error as e:
            log.error("database write failed (data kept in raw archive): %s", e)
            return f"database: {e}", None

    def http_get(self, path: str) -> tuple[float, float, int | None, str | None, str | None]:
        m0 = time.monotonic()
        t0 = time.time()
        status: int | None = None
        body: bytes | None = None
        err: str | None = None
        req = urllib.request.Request(self.base + path, headers={"User-Agent": f"wllharvest/{VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as r:
                status, body = r.status, r.read()
        except urllib.error.HTTPError as e:  # the WLL explains errors in a JSON body
            status = e.code
            with contextlib.suppress(OSError, http.client.HTTPException):
                body = e.read()
        except (OSError, http.client.HTTPException, ValueError) as e:
            err = f"{type(e).__name__}: {e}"
        latency = time.monotonic() - m0
        text = None if body is None else body.decode("utf-8", "replace")
        return t0 + latency, latency, status, text, err

    @staticmethod
    def meta(
        recv: float, kind: str, status: int | None = None, latency: float | None = None, error: str | None = None
    ) -> Json:
        m: Json = {"recv": round(recv, 3), "kind": kind}
        if status is not None:
            m["status"] = status
        if latency is not None:
            m["latency"] = round(latency, 3)
        if error is not None:
            m["error"] = error
        return m

    # -- HTTP current conditions --------------------------------------------

    def poll(self) -> None:
        for attempt in (1, 2):
            recv, latency, status, text, err = self.http_get("/v1/current_conditions")
            err, data = self.record("http", self.meta(recv, "cc", status, latency, err), text)
            self.hour_polls += 1
            # The WLL drops or refuses a request while serving another client;
            # one quick retry recovers that poll. Both attempts are recorded.
            if attempt == 1 and err and transient(err) and not self.stopping:
                self.http_retries += 1
                log.debug("current_conditions dropped (%s); retrying in %g s", err, RETRY_DELAY)
                time.sleep(RETRY_DELAY)
                continue
            break
        if err or data is None:
            err = err or "no data"
            self.http_fail += 1
            self.http_consec += 1
            self.http_fail_since = self.http_fail_since or recv
            self.http_last_error, self.http_last_error_time = err, recv
            if noteworthy(self.http_consec):
                log.warning("current_conditions failed (%d in a row): %s", self.http_consec, err)
            return
        if self.http_consec:
            log.info(
                "current_conditions recovered after %d failures (%.0f s)",
                self.http_consec,
                recv - (self.http_fail_since or recv),
            )
        self.http_ok += 1
        self.hour_ok += 1
        self.http_consec, self.http_fail_since, self.http_last_ok = 0, None, recv
        if data.get("did") and data["did"] != self.did:
            if self.did:
                log.warning("device id changed %s -> %s", self.did, data["did"])
            else:
                log.info("WLL device id %s", data["did"])
            self.did = data["did"]
        if isinstance(data.get("ts"), (int, float)):
            off = recv - data["ts"]
            self.offsets.append(off)
            self.last_offset = off
            # ts is the integer second the WLL handled the request, so synced
            # clocks give 0 <= off < 1 + latency (measured: WLL ~0.3 s slow).
            if not -2.0 <= off <= 3.0 + latency and recv - self.clock_warned > 3600:
                self.clock_warned = recv
                log.warning("clock mismatch: host - WLL = %.1f s (is NTP running on the host?)", off)

    # -- UDP real-time ------------------------------------------------------

    def open_udp(self) -> None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):  # share the port with other WLL clients
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            s.bind(("", self.cfg.udp_port))
            s.setblocking(False)
            self.sock = s
            log.info("listening for real-time UDP on port %d", self.cfg.udp_port)
        except OSError as e:
            self.udp_note = f"UDP disabled: cannot bind port {self.cfg.udp_port}: {e}"
            log.error(self.udp_note)

    def request_rt(self) -> None:
        dur = self.cfg.rt_duration
        recv, latency, status, text, err = self.http_get(f"/v1/real_time?duration={dur}")
        err, data = self.record("http", self.meta(recv, "rt", status, latency, err), text)
        mono = time.monotonic()
        if err or data is None:
            err = err or "no data"
            self.rt_fail += 1
            self.rt_consec += 1
            self.rt_last_error = err
            if status == 409:  # documented: no ISS configured on this WLL
                self.rt_next = mono + 3600
                log.warning("real-time broadcast unavailable (no ISS configured?): %s", err)
            else:
                self.rt_next = mono + min(60 * 2 ** (self.rt_consec - 1), 900)
                if noteworthy(self.rt_consec):
                    log.warning("real-time request failed (%d in a row): %s", self.rt_consec, err)
            return
        self.rt_ok += 1
        self.rt_consec, self.rt_ok_mono = 0, mono
        self.rt_next = mono + dur / 2
        port = data.get("broadcast_port")
        if port is not None and port != self.cfg.udp_port and self.rt_ok == 1:
            log.warning("WLL broadcasts on port %s but we listen on %d", port, self.cfg.udp_port)
        log.debug("real-time broadcast requested for %d s (WLL says %s)", dur, data.get("duration"))

    def read_udp(self) -> None:
        sock = self.sock
        if sock is None:
            return
        while True:
            try:
                pkt, (ip, _port) = sock.recvfrom(65535)
            except BlockingIOError:
                return
            except OSError as e:
                log.error("UDP receive failed: %s", e)
                return
            recv = time.time()
            # A host with two interfaces on the WLL's subnet hears each subnet
            # broadcast twice. Real packets always differ (each carries a new
            # ts, 2.5 s apart), so a byte-identical repeat within 1 s is a copy.
            if pkt == self.udp_prev and recv - self.udp_prev_time < 1.0:
                self.udp_dups += 1
                if self.udp_dups == 1:
                    log.info("dropping duplicate UDP copies (host has >1 interface on the WLL's subnet?)")
                continue
            self.udp_prev, self.udp_prev_time = pkt, recv
            m = self.meta(recv, "udp")
            m["from"] = ip
            err, _ = self.record("udp", m, pkt.decode("utf-8", "replace"))
            if err:
                self.udp_bad += 1
                if noteworthy(self.udp_bad):
                    log.warning("bad UDP packet from %s (%d so far): %s", ip, self.udp_bad, err)
                continue
            if self.udp_packets == 0:
                log.info("first real-time UDP packet received from %s", ip)
            self.udp_packets += 1
            self.hour_udp += 1
            self.udp_last, self.udp_last_mono = recv, time.monotonic()
            self.stall_wait, self.stalls = UDP_STALL, 0

    def check_udp_stall(self, mono: float) -> None:
        """Re-request broadcasts if the WLL went quiet (e.g. it rebooted)."""
        if not self.sock or self.rt_ok_mono is None or self.rt_consec:
            return
        silent = mono - max(self.udp_last_mono or 0.0, self.rt_ok_mono)
        if silent <= self.stall_wait:
            return
        self.stalls += 1
        level = logging.WARNING if self.stalls <= 3 or self.stalls % 6 == 0 else logging.DEBUG
        log.log(level, "no real-time UDP for %.0f s; re-requesting broadcast", silent)
        if self.stalls == 3 and self.udp_packets == 0:
            log.warning(
                "the WLL accepts real-time requests but no UDP has ever arrived: the "
                "harvester must be on the WLL's subnet (broadcasts are not routed), use "
                "host networking in Docker, and port %d must not be firewalled. "
                "Set WLL_UDP=0 to poll over HTTP only.",
                self.cfg.udp_port,
            )
        self.stall_wait = min(self.stall_wait * 2, UDP_STALL_MAX)
        self.rt_next = mono

    # -- status -------------------------------------------------------------

    def status(self) -> Json:
        return {
            "version": VERSION,
            "pid": os.getpid(),
            "wll": self.base,
            "did": self.did,
            "started": utc_iso(self.started),
            "updated": utc_iso(time.time()),
            "updated_epoch": round(time.time(), 3),
            "http": {
                "ok": self.http_ok,
                "fail": self.http_fail,
                "consecutive_fail": self.http_consec,
                "retried": self.http_retries,
                "last_ok": utc_iso(self.http_last_ok),
                "last_ok_epoch": self.http_last_ok,
                "last_error": self.http_last_error,
                "last_error_time": utc_iso(self.http_last_error_time),
                "clock_offset_s": None if self.last_offset is None else round(self.last_offset, 3),
            },
            "udp": {
                "enabled": self.sock is not None,
                "note": self.udp_note,
                "packets": self.udp_packets,
                "bad_packets": self.udp_bad,
                "duplicates_dropped": self.udp_dups,
                "last_packet": utc_iso(self.udp_last),
                "last_packet_epoch": self.udp_last,
                "rt_requests_ok": self.rt_ok,
                "rt_requests_fail": self.rt_fail,
                "last_error": self.rt_last_error,
            },
        }

    def summarize(self, t0: float, t1: float, why: str) -> None:
        try:
            n = self.store.summarize(self.cfg.summary_period, t0, t1)
            log.debug("%s: wrote %d summary rows", why, n)
        except sqlite3.Error as e:
            log.error("%s summary failed (redo with `summarize`): %s", why, e)

    def write_status(self) -> None:
        tmp = self.status_path.with_name(self.status_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.status(), indent=1) + "\n")
            os.replace(tmp, self.status_path)
        except OSError as e:
            log.error("cannot write %s: %s", self.status_path, e)

    def housekeeping(self) -> None:
        for r in self.raw.values():
            try:
                r.sync()
            except OSError as e:
                log.error("raw archive fsync failed: %s", e)
        self.write_status()
        hour = int(time.time() // 3600)
        if hour != self.hour:
            if self.hour and self.hour_polls:
                o = self.offsets
                clock = (
                    (f"; host-WLL clock offset median {statistics.median(o):+.2f} s [{min(o):+.2f}, {max(o):+.2f}]")
                    if o
                    else ""
                )
                udp = f"; {self.hour_udp} UDP packets" if self.sock else ""
                log.info("hourly: %d/%d polls ok%s%s", self.hour_ok, self.hour_polls, udp, clock)
            self.hour = hour
            self.hour_polls = self.hour_ok = self.hour_udp = 0
            self.offsets = []

    # -- main loop ----------------------------------------------------------

    def _stop(self, signum: int, _frame: FrameType | None) -> None:
        log.info("received %s, shutting down", signal.Signals(signum).name)
        self.stopping = True

    def run(self) -> None:
        cfg = self.cfg
        log.info(
            "wllharvest %s: polling %s every %g s, UDP %s, raw archive %s, data in %s",
            VERSION,
            self.base,
            cfg.interval,
            "on" if cfg.udp else "off",
            "on" if cfg.raw else "off",
            cfg.data_dir,
        )
        wake_r, wake_w = socket.socketpair()
        for s in (wake_r, wake_w):
            s.setblocking(False)
        signal.set_wakeup_fd(wake_w.fileno())
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._stop)
        if cfg.udp:
            self.open_udp()
        interval, period = cfg.interval, cfg.summary_period
        # Catch up on summaries missed while stopped (bounded; `summarize` does any range).
        now = time.time()
        first = self.store.last_summary(period)
        if first is None:
            first = self.store.db.execute("SELECT min(recv_time) FROM polls").fetchone()[0]
        self.sum_from = math.floor(now / period) * period  # first period not yet summarized
        if first is not None:
            self.summarize(max(first, now - 2 * 86400), self.sum_from, "catch-up")
        log.info("summaries every %d s in table %s", period, summary_table(period))
        next_poll = math.ceil(time.time() / interval) * interval  # aligned to UTC multiples
        next_house = time.monotonic()
        try:
            while not self.stopping:
                now, mono = time.time(), time.monotonic()
                if next_poll - now > interval:  # wall clock stepped back
                    next_poll = math.ceil(now / interval) * interval
                if now >= next_poll:
                    self.poll()
                    self.write_status()
                    next_poll = (math.floor(time.time() / interval) + 1) * interval
                if self.sock and mono >= self.rt_next:
                    self.request_rt()
                mono = time.monotonic()
                if mono >= next_house:
                    self.housekeeping()
                    next_house = mono + HOUSEKEEPING
                self.check_udp_stall(mono)
                # A period is summarized 1 s after it ends: its last poll is in by then.
                if time.time() >= self.sum_from + period + 1.0:
                    end = math.floor((time.time() - 1.0) / period) * period
                    self.summarize(self.sum_from, end, "period")
                    self.sum_from = end
                wait = min(next_poll - time.time(), next_house - mono, 1.0, self.sum_from + period + 1.0 - time.time())
                if self.sock:
                    wait = min(wait, self.rt_next - mono)
                fds: list[socket.socket] = [wake_r] + ([self.sock] if self.sock else [])
                ready = select.select(fds, [], [], max(0.0, wait))[0]
                if self.sock in ready:
                    self.read_udp()
                if wake_r in ready:
                    with contextlib.suppress(OSError):
                        wake_r.recv(64)
        finally:
            for r in self.raw.values():
                r.close()
            self.summarize(self.sum_from, time.time() + 1, "shutdown (partial period)")
            self.write_status()
            self.store.close()
            if self.sock:
                self.sock.close()
            signal.set_wakeup_fd(-1)
            log.info("stopped (%d polls ok, %d failed, %d UDP packets)", self.http_ok, self.http_fail, self.udp_packets)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> None:
    if not args.host:
        sys.exit("set WLL_HOST (or --host) to the WeatherLink Live's IP address or hostname")
    if args.interval < MIN_POLL_INTERVAL and not args.allow_fast_poll:
        log.warning(
            "poll interval %g s is below the documented %g s minimum; using %g s",
            args.interval,
            MIN_POLL_INTERVAL,
            MIN_POLL_INTERVAL,
        )
        args.interval = MIN_POLL_INTERVAL
    if not 1 <= args.rt_duration <= MAX_RT_DURATION:
        sys.exit(f"--rt-duration must be 1..{MAX_RT_DURATION} s")
    if args.summary_period < args.interval:
        sys.exit("--summary-period must be at least the poll interval")
    Harvester(args).run()


def cmd_rebuild(args: argparse.Namespace) -> None:
    """Regenerate (or top up) a database from the raw archive."""
    raw = Path(args.raw_dir)
    paths = sorted(p for p in raw.glob("*/wll_*_*.jsonl*") if not p.name.endswith(".tmp"))
    if not paths:
        sys.exit(f"no raw archive files under {raw}")
    store = Store(args.db)
    n = bad = 0
    for path in paths:
        with store.transaction():
            for _p, _n, rec in read_raw([path]):
                err, _ = store.ingest(rec)
                n += 1
                bad += err is not None
        log.info("%s: ingested", path)
    t0, t1 = store.db.execute("SELECT min(recv_time), max(recv_time) FROM polls").fetchone()
    if t0 is not None:
        log.info("%d %s rows", store.summarize(args.period, t0, t1 + 1), summary_table(args.period))
    store.close()
    log.info("rebuild done: %d lines from %d files (%d were failures/bad packets)", n, len(paths), bad)


def cmd_summarize(args: argparse.Namespace) -> None:
    """(Re)compute period summaries over a time range from the 10 s records."""
    store = Store(args.db)
    t0 = parse_utc(args.start) if args.start else store.db.execute("SELECT min(recv_time) FROM polls").fetchone()[0]
    t1 = parse_utc(args.end) if args.end else time.time() + 1
    if t0 is None:
        sys.exit("no data to summarize")
    log.info("%d %s rows", store.summarize(args.period, t0, t1), summary_table(args.period))
    store.close()


def parse_utc(s: str) -> float:
    t = dt.datetime.fromisoformat(s)
    return (t if t.tzinfo else t.replace(tzinfo=dt.UTC)).timestamp()


def parse_range(start: str | None, end: str | None) -> tuple[float | None, float | None]:
    """(t0, t1) epoch s from ISO 8601 strings (UTC unless an offset is given;
    either may be None). A date-only end includes that whole day, so
    2026-10-01..2026-10-01 is one day; an end with a time is exclusive."""
    t0 = parse_utc(start) if start else None
    t1: float | None = None
    if end:
        t1 = parse_utc(end) + (86400 if len(end.strip()) == 10 else 0)
    return t0, t1


def time_column(cols: Sequence[str]) -> str | None:
    """Column a table/view is selected and ordered on: host arrival time for
    records and polls, period start for summaries."""
    return next((c for c in ("recv_time", "t_start") if c in cols), None)


def select_range(
    db: sqlite3.Connection, table: str, t0: float | None = None, t1: float | None = None
) -> sqlite3.Cursor:
    """Cursor over table rows with t0 <= time < t1 (epoch s; None = open)."""
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({q(table)})")]
    if not cols:
        names = [
            r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name")
        ]
        raise ValueError(f"no table/view {table!r}; have: {', '.join(names)}")
    tcol = time_column(cols)
    where: list[str] = []
    params: list[float] = []
    for bound, op in ((t0, ">="), (t1, "<")):
        if bound is not None:
            if not tcol:
                raise ValueError(f"{table} has no time column")
            where.append(f"{tcol} {op} ?")
            params.append(bound)
    sql = f"SELECT * FROM {q(table)}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    if tcol:
        sql += f" ORDER BY {tcol}" + (", lsid" if "lsid" in cols else "")
    return db.execute(sql, params)


def cmd_export(args: argparse.Namespace) -> None:
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        cur = select_range(db, args.table, *parse_range(args.start, args.end))
    except ValueError as e:
        sys.exit(str(e))
    with contextlib.ExitStack() as stack:
        out = stack.enter_context(open(args.output, "w", newline="")) if args.output else sys.stdout
        w = csv.writer(out)
        w.writerow([d[0] for d in cur.description])
        w.writerows(cur)
    db.close()


def cmd_status(args: argparse.Namespace) -> None:
    data = Path(args.data_dir)
    st = data / "status.json"
    print(st.read_text() if st.exists() else f"(no {st})")
    db = sqlite3.connect(f"file:{data / 'wll.sqlite'}?mode=ro", uri=True)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for t in tables:
        if t == "meta":
            continue
        tcol = time_column([r[1] for r in db.execute(f"PRAGMA table_info({q(t)})")])
        n, t0, t1 = db.execute(f"SELECT count(*), min({tcol}), max({tcol}) FROM {q(t)}").fetchone()
        print(f"{t:14s} {n:10d} rows  {utc_iso(t0)} .. {utc_iso(t1)}")
    # ISS radio link quality as the WLL reports it on each poll:
    # rx_state 0 = synced, 1 = synced but missed 1-14 packets, 2 = scanning (lost).
    since = time.time() - args.hours * 3600
    t0, t1, n = db.execute(
        "SELECT min(recv_time), max(recv_time), count(*) FROM polls WHERE recv_time >= ?", (since,)
    ).fetchone()
    ok = db.execute("SELECT count(*) FROM polls WHERE ok AND recv_time >= ?", (since,)).fetchone()[0]
    print(f"\nlast {args.hours:g} h ({utc_iso(t0)} .. {utc_iso(t1)}): {ok}/{n} polls answered")
    for txid, total, s0, s1, s2 in db.execute(
        "SELECT txid, count(*), sum(rx_state = 0), sum(rx_state = 1), sum(rx_state = 2)"
        " FROM iss WHERE recv_time >= ? GROUP BY txid ORDER BY txid",
        (since,),
    ):
        print(
            f"ISS txid {txid}: rx_state over {total} polls: synced {100 * s0 / total:.1f}%,"
            f" missed packets {100 * s1 / total:.1f}%, lost/scanning {100 * s2 / total:.1f}%"
        )
    db.close()


def health_problems(data_dir: str | Path, max_age: float = 120.0, http_only: bool = False) -> list[str]:
    """Reasons the harvester looks unhealthy, from its status.json; [] if fine."""
    try:
        st = json.loads((Path(data_dir) / "status.json").read_text())
    except (OSError, ValueError) as e:
        return [f"cannot read status: {e}"]
    now = time.time()
    problems: list[str] = []
    if now - (st.get("updated_epoch") or 0) > max_age:
        problems.append("status not updated (harvester stalled?)")
    if now - (st["http"].get("last_ok_epoch") or 0) > max_age:
        problems.append(f"no successful poll since {st['http'].get('last_ok')}: {st['http'].get('last_error')}")
    if st["udp"].get("enabled") and not http_only and now - (st["udp"].get("last_packet_epoch") or 0) > max_age:
        problems.append(f"no UDP packet since {st['udp'].get('last_packet')}")
    return problems


def cmd_health(args: argparse.Namespace) -> int:
    """Exit 0 if the harvester is alive and fresh data is arriving, else 1."""
    problems = health_problems(args.data_dir, args.max_age, args.http_only)
    print("UNHEALTHY: " + "; ".join(problems) if problems else "OK")
    return 1 if problems else 0


# ---------------------------------------------------------------------------
# CSV web service (read-only)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dashboard data
# ---------------------------------------------------------------------------

# Summary columns the dashboard plots, and how each combines when summary rows
# are merged into coarser bins (hourly or daily, for long ranges). Means are
# weighted by n_polls; the wind columns are combined as vectors (see series()).
SERIES_COLS: dict[str, str] = {
    "n_polls": "sum",
    "n_failed": "sum",
    "rx_ok_frac": "wmean",
    "battery_low": "max",
    "rain_mm": "sum",
    "rain_rate_mmh_max": "max",
    "temp_c_min": "min",
    "temp_c_mean": "wmean",
    "temp_c_max": "max",
    "dew_point_c_mean": "wmean",
    "rh_mean": "wmean",
    "wind_ms_mean": "wmean",
    "wind_ms_max": "max",
    "wll_gust_10min_ms": "max",
    "wind_vec_ms": "vector",
    "wind_vec_dir_deg": "vector",
    "wind_dir_mean_deg": "unit",
    "wind_dir_steadiness": "unit",
    "slp_hpa_mean": "wmean",
    "solar_wm2_min": "min",
    "solar_wm2_mean": "wmean",
    "solar_wm2_max": "max",
    "uv_index_max": "max",
    "temp_in_c_mean": "wmean",
    "rh_in_mean": "wmean",
}
MAX_SERIES_BINS = 20000


def weighted_vector(rows: Sequence[tuple[float | None, float | None, float]]) -> tuple[float | None, float | None]:
    """(direction deg, magnitude) of sum(w * m * unit(d)) / sum(w) over (m, d, w);
    a row with no direction contributes a zero vector (e.g. a calm period)."""
    total = math.fsum(w for _, _, w in rows)
    if not rows or total <= 0:
        return None, None
    x = math.fsum(w * (m or 0.0) * math.sin(math.radians(d)) for m, d, w in rows if d is not None) / total
    y = math.fsum(w * (m or 0.0) * math.cos(math.radians(d)) for m, d, w in rows if d is not None) / total
    mag = math.hypot(x, y)
    return (round(math.degrees(math.atan2(x, y)), 3) % 360 if mag > 1e-12 else None), mag


def combine(rows: Sequence[Json]) -> Json:
    """Merge summary rows into one bin, per SERIES_COLS."""
    if len(rows) == 1:
        return rows[0]
    out: Json = {}
    weights = [r["n_polls"] or 0 for r in rows]
    for col, how in SERIES_COLS.items():
        vals = [(r[col], w) for r, w in zip(rows, weights, strict=True) if r[col] is not None]
        if not vals or how in ("vector", "unit"):
            out[col] = None
        elif how == "sum":
            out[col] = math.fsum(v for v, _ in vals)
        elif how == "min":
            out[col] = min(v for v, _ in vals)
        elif how == "max":
            out[col] = max(v for v, _ in vals)
        else:  # weighted mean
            total = sum(w for _, w in vals)
            out[col] = math.fsum(v * w for v, w in vals) / total if total else statistics.fmean(v for v, _ in vals)
    out["wind_vec_dir_deg"], out["wind_vec_ms"] = weighted_vector(
        [
            (r["wind_vec_ms"], r["wind_vec_dir_deg"], w)
            for r, w in zip(rows, weights, strict=True)
            if r["wind_vec_ms"] is not None
        ]
    )
    # Unit-vector direction means combine weighted by their non-calm sample counts.
    out["wind_dir_mean_deg"], out["wind_dir_steadiness"] = weighted_vector(
        [
            (r["wind_dir_steadiness"], r["wind_dir_mean_deg"], max(0, w - (r["n_calm"] or 0)))
            for r, w in zip(rows, weights, strict=True)
            if r["wind_dir_steadiness"] is not None
        ]
    )
    return out


def series(db: sqlite3.Connection, period: int, t0: float, t1: float, step: int, offset: int = 0) -> Json:
    """Columnar summary data for charts: one value per `step` seconds in
    [t0, t1), with bins aligned to local time via `offset` (s east of UTC)
    and missing bins as None so charts show gaps. `step` must be a multiple
    of the summary period."""
    if step < period or step % period:
        raise ValueError(f"step must be a multiple of {period} s")
    first = int((t0 + offset) // step) * step - offset
    n = math.ceil((t1 - first) / step)
    if not 0 < n <= MAX_SERIES_BINS:
        raise ValueError(f"range must give 1..{MAX_SERIES_BINS} bins of {step} s")
    cols = [*SERIES_COLS, "n_calm"]
    groups: collections.defaultdict[int, list[Json]] = collections.defaultdict(list)
    table = summary_table(period)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone():
        for r in db.execute(
            f"SELECT t_start, {', '.join(map(q, cols))} FROM {q(table)} WHERE t_start >= ? AND t_start < ?",
            (first, t1),
        ):
            groups[int((r[0] + offset) // step) * step - offset].append(dict(zip(cols, r[1:], strict=True)))
    out: Json = {"step": step, "t": [first + i * step for i in range(n)]}
    bins = [combine(groups[t]) if t in groups else {} for t in out["t"]]
    for col in SERIES_COLS:
        out[col] = [b.get(col) for b in bins]
    return out


def latest(db: sqlite3.Connection) -> Json:
    """The newest record of each sensor view, in SI units."""
    out: Json = {}
    for name in ("iss", "baro", "inside"):
        cur = db.execute(f"SELECT * FROM {q(name + '_si')} ORDER BY recv_time DESC LIMIT 1")
        row = cur.fetchone()
        out[name] = None if row is None else dict(zip([d[0] for d in cur.description], row, strict=True))
    return out


# Tables offered over HTTP: name -> (description, max days per request or None)
SERVE_TABLES: dict[str, tuple[str, int | None]] = {
    summary_table(SUMMARY_PERIOD): (
        (f"{SUMMARY_PERIOD // 60}-minute summaries: min/mean/median/max, rain, wind direction (SI units)"),
        None,
    ),
    "iss_si": ("outdoor sensors every 10 s (SI units)", 31),
    "baro_si": ("barometer every 10 s (hPa)", 31),
    "inside_si": ("WLL indoor temperature and humidity every 10 s (SI units)", 31),
    "polls": ("every poll attempt: success or error, latency", 31),
}
DOCS_URL = "https://github.com/mousebrains/WeatherLink#readme"
WEB_DIR = Path(__file__).resolve().parent / "web"  # dashboard page, scripts, styles
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


class CSVHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"wllharvest/{VERSION}"

    @property
    def srv(self) -> "CSVServer":
        return cast(CSVServer, self.server)

    def log_message(self, format: str, *args: Any) -> None:
        log.info("%s %s", self.address_string(), format % args)

    def send_text(self, code: int, text: str, ctype: str = "text/plain; charset=utf-8") -> None:
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj: Any) -> None:
        body = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urllib.parse.urlparse(self.path)
        qs = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/":
            return self.static("index.html")
        if url.path.startswith("/static/"):
            return self.static(urllib.parse.unquote(url.path[len("/static/") :]))
        if url.path == "/download":
            return self.send_text(200, self.page(), "text/html; charset=utf-8")
        if url.path == "/api/latest":
            return self.api_latest()
        if url.path == "/api/series":
            return self.api_series(qs)
        if url.path == "/csv":
            return self.csv(qs)
        if url.path == "/health":
            problems = health_problems(self.srv.data_dir)
            return self.send_text(
                503 if problems else 200, ("UNHEALTHY: " + "; ".join(problems) if problems else "OK") + "\n"
            )
        if url.path == "/status.json":
            try:
                return self.send_text(200, (self.srv.data_dir / "status.json").read_text(), "application/json")
            except OSError as e:
                return self.send_text(503, f"no status: {e}\n")
        self.send_text(404, "not found; try / or /csv?table=...&start=YYYY-MM-DD&end=YYYY-MM-DD\n")

    def static(self, rel: str) -> None:
        """A file from the web directory, by extension; nothing outside it."""
        root = self.srv.web_dir.resolve()
        path = (root / rel).resolve()
        if root not in path.parents or path.suffix not in STATIC_TYPES or not path.is_file():
            return self.send_text(404, "not found\n")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES[path.suffix])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if "vendor" in path.parts else "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def api_latest(self) -> None:
        try:
            status: Json | None = json.loads((self.srv.data_dir / "status.json").read_text())
        except (OSError, ValueError):
            status = None
        with contextlib.closing(sqlite3.connect(f"file:{self.srv.db_path}?mode=ro", uri=True, timeout=30)) as db:
            self.send_json({"status": status, **latest(db)})

    def api_series(self, qs: dict[str, str]) -> None:
        """Summary series: t0/t1 (epoch s) or start/end (ISO); step (s, default
        the summary period); offset (s east of UTC) to align bins to local days."""
        try:
            if "t0" in qs or "t1" in qs:
                t0, t1 = float(qs["t0"]), float(qs.get("t1", time.time()))
            else:
                start, end = parse_range(qs.get("start"), qs.get("end"))
                t1 = time.time() if end is None else end
                t0 = t1 - 86400 if start is None else start
            step = int(qs.get("step", SUMMARY_PERIOD))
            offset = int(qs.get("offset", 0))
            if abs(offset) > 14 * 3600:
                raise ValueError("offset must be within +-14 h")
            with contextlib.closing(sqlite3.connect(f"file:{self.srv.db_path}?mode=ro", uri=True, timeout=30)) as db:
                data = series(db, SUMMARY_PERIOD, t0, t1, step, offset)
        except (KeyError, ValueError) as e:
            return self.send_text(400, f"bad request: {e}\n")
        except sqlite3.Error as e:
            return self.send_text(500, f"error: {e}\n")
        self.send_json(data)

    def csv(self, qs: dict[str, str]) -> None:
        table = qs.get("table", summary_table(SUMMARY_PERIOD))
        if table not in SERVE_TABLES:
            return self.send_text(400, f"table must be one of: {', '.join(SERVE_TABLES)}\n")
        try:
            t0, t1 = parse_range(qs.get("start"), qs.get("end"))
        except ValueError:
            return self.send_text(400, "start/end must be ISO 8601 UTC, e.g. 2026-10-01 or 2026-10-01T06:00\n")
        t1 = time.time() if t1 is None else t1
        t0 = t1 - 86400 if t0 is None else t0
        limit = SERVE_TABLES[table][1]
        if t1 <= t0:
            return self.send_text(400, "end must be after start\n")
        if limit and t1 - t0 > limit * 86400:
            return self.send_text(
                400,
                f"{table} is limited to {limit} days per request; "
                f"use {summary_table(SUMMARY_PERIOD)} for longer spans\n",
            )
        db = sqlite3.connect(f"file:{self.srv.db_path}?mode=ro", uri=True, timeout=30)

        def stamp(t: float) -> str:
            return time.strftime("%Y%m%dT%H%M", time.gmtime(t))

        try:
            cur = select_range(db, table, t0, t1)
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="wll_{table}_{stamp(t0)}_{stamp(t1)}.csv"')
            self.end_headers()
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow([d[0] for d in cur.description])
            for rows in iter(lambda: cur.fetchmany(2000), []):
                w.writerows(rows)
                self.wfile.write(buf.getvalue().encode())
                buf.seek(0)
                buf.truncate()
            self.wfile.write(buf.getvalue().encode())
        except (sqlite3.Error, ValueError) as e:
            log.error("csv %s: %s", table, e)
            self.send_text(500, f"error: {e}\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            db.close()

    def page(self) -> str:
        problems = health_problems(self.srv.data_dir)
        try:
            last = json.loads((self.srv.data_dir / "status.json").read_text())["http"]["last_ok"]
        except (OSError, ValueError, KeyError):
            last = "unknown"
        today = time.strftime("%Y-%m-%d", time.gmtime())
        week = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 6 * 86400))
        options = "".join(
            f'<option value="{t}">{t} &mdash; {d}{f" (max {n} days)" if n else ""}</option>'
            for t, (d, n) in SERVE_TABLES.items()
        )
        state = "collecting" if not problems else "PROBLEM: " + "; ".join(problems)
        example = (
            f"http://{self.headers.get('Host', 'host')}/csv?table={summary_table(SUMMARY_PERIOD)}"
            f"&amp;start={week}&amp;end={today}"
        )
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Weather data</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:44rem;margin:2rem auto;padding:0 1rem}}
label{{display:block;margin:.8rem 0 .2rem}}select,input,button{{font:inherit;padding:.3rem}}
button{{margin-top:1rem}}code{{background:#eee;padding:0 .2rem}}small{{color:#555}}</style></head><body>
<h1>Weather station data</h1>
<p>Davis Vantage Pro2 via WeatherLink Live. Status: {state}; last reading {last} UTC.</p>
<form action="/csv" method="get">
<label for="table">Data</label><select id="table" name="table">{options}</select>
<label for="start">From (UTC date, inclusive)</label><input id="start" name="start" type="date" value="{week}">
<label for="end">To (UTC date, inclusive)</label><input id="end" name="end" type="date" value="{today}">
<br><button type="submit">Download CSV</button></form>
<p><small>All times are UTC. Scripts can fetch the same file, e.g.
<code>curl -OJ
'{example}'</code>.
Column definitions: <a href="{DOCS_URL}">README</a>.</small></p></body></html>"""


class CSVServer(http.server.ThreadingHTTPServer):
    """Threaded HTTP server for CSVHandler, knowing where the data live."""

    daemon_threads = True

    def __init__(self, addr: tuple[str, int], data_dir: Path, web_dir: Path = WEB_DIR) -> None:
        super().__init__(addr, CSVHandler)
        self.data_dir = data_dir
        self.db_path = data_dir / "wll.sqlite"
        self.web_dir = web_dir


def cmd_serve(args: argparse.Namespace) -> None:
    srv = CSVServer((args.bind, args.port), Path(args.data_dir))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # PID 1 in a container ignores SIG_DFL
    log.info("serving CSV extracts of %s on http://%s:%d/", srv.db_path, args.bind, args.port)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()


def env(name: str, default: Any = None, conv: Callable[[str], Any] = str) -> Any:
    v = os.environ.get(name)
    return default if v in (None, "") else conv(v)


def env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if not v else v.strip().lower() in ("1", "true", "yes", "on")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Davis WeatherLink Live local data harvester")
    p.add_argument("--log-level", default=env("WLL_LOG_LEVEL", "INFO"))
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="harvest continuously (settings also via WLL_* env vars)")
    r.add_argument("--host", default=env("WLL_HOST"), help="WLL IP or hostname [WLL_HOST]")
    r.add_argument("--port", type=int, default=env("WLL_PORT", 80, int), help="[WLL_PORT] 80")
    r.add_argument("--data-dir", default=env("WLL_DATA_DIR", "/data"), help="[WLL_DATA_DIR] /data")
    r.add_argument(
        "--interval",
        type=float,
        default=env("WLL_POLL_INTERVAL", 10.0, float),
        help="current_conditions poll interval, s, >= 10 [WLL_POLL_INTERVAL] 10",
    )
    r.add_argument(
        "--timeout",
        type=float,
        default=env("WLL_HTTP_TIMEOUT", 8.0, float),
        help="HTTP timeout, s [WLL_HTTP_TIMEOUT] 8",
    )
    r.add_argument(
        "--udp",
        action=argparse.BooleanOptionalAction,
        default=env_bool("WLL_UDP", True),
        help="request and record the 2.5 s UDP real-time stream [WLL_UDP] 1",
    )
    r.add_argument("--udp-port", type=int, default=env("WLL_UDP_PORT", 22222, int), help="[WLL_UDP_PORT] 22222")
    r.add_argument(
        "--rt-duration",
        type=int,
        default=env("WLL_RT_DURATION", 3600, int),
        help="duration of each real-time request, s; renewed at half [WLL_RT_DURATION] 3600",
    )
    r.add_argument(
        "--raw",
        action=argparse.BooleanOptionalAction,
        default=env_bool("WLL_RAW", True),
        help="keep the raw JSON-lines archive [WLL_RAW] 1",
    )
    r.add_argument(
        "--allow-fast-poll",
        action="store_true",
        default=env_bool("WLL_ALLOW_FAST_POLL", False),
        help="allow intervals < 10 s (only for tools/fake_wll.py)",
    )
    r.add_argument(
        "--summary-period", type=int, default=SUMMARY_PERIOD, help="s per summary row [WLL_SUMMARY_PERIOD] 600"
    )
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("rebuild", help="regenerate a database (with summaries) from the raw archive")
    b.add_argument("--raw-dir", default=os.path.join(env("WLL_DATA_DIR", "/data"), "raw"))
    b.add_argument("--db", required=True, help="database to create or top up")
    b.add_argument("--period", type=int, default=SUMMARY_PERIOD, help="summary period, s (default 600)")
    b.set_defaults(func=cmd_rebuild)

    m = sub.add_parser("summarize", help="(re)compute period summaries from the 10 s records")
    m.add_argument("--db", default=os.path.join(env("WLL_DATA_DIR", "/data"), "wll.sqlite"))
    m.add_argument("--period", type=int, default=SUMMARY_PERIOD, help="s (default 600 -> summary_10min)")
    m.add_argument("--start", help="UTC, ISO 8601 (default: first data)")
    m.add_argument("--end", help="UTC, ISO 8601, exclusive (default: now)")
    m.set_defaults(func=cmd_summarize)

    w = sub.add_parser("serve", help="serve CSV extracts over HTTP (read-only)")
    w.add_argument("--data-dir", default=env("WLL_DATA_DIR", "/data"))
    w.add_argument("--bind", default=env("WLL_SERVE_BIND", "0.0.0.0"), help="[WLL_SERVE_BIND] 0.0.0.0")
    w.add_argument("--port", type=int, default=env("WLL_SERVE_PORT", 8580, int), help="[WLL_SERVE_PORT] 8580")
    w.set_defaults(func=cmd_serve)

    e = sub.add_parser("export", help="write a table or view as CSV")
    e.add_argument("--db", default=os.path.join(env("WLL_DATA_DIR", "/data"), "wll.sqlite"))
    e.add_argument(
        "--table", default="summary_10min", help="e.g. summary_10min, iss_si, baro_si, inside_si, rt_iss_si, polls"
    )
    e.add_argument("--start", help="UTC, ISO 8601, inclusive (e.g. 2026-10-01 or 2026-10-01T06:00)")
    e.add_argument("--end", help="UTC, ISO 8601; a date includes that whole day, a time is exclusive")
    e.add_argument("-o", "--output", help="CSV file (default stdout)")
    e.set_defaults(func=cmd_export)

    s = sub.add_parser("status", help="show harvester status, table extents, ISS reception")
    s.add_argument("--data-dir", default=env("WLL_DATA_DIR", "/data"))
    s.add_argument("--hours", type=float, default=24.0, help="reception summary window, h (default 24)")
    s.set_defaults(func=cmd_status)

    h = sub.add_parser("health", help="exit 0 if fresh data is arriving (container healthcheck)")
    h.add_argument("--data-dir", default=env("WLL_DATA_DIR", "/data"))
    h.add_argument("--max-age", type=float, default=120.0, help="s, default 120")
    h.add_argument("--http-only", action="store_true", help="ignore the UDP stream")
    h.set_defaults(func=cmd_health)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        stream=sys.stderr,
        format="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.Formatter.converter = time.gmtime
    return cast(int | None, args.func(args)) or 0


if __name__ == "__main__":
    sys.exit(main())
