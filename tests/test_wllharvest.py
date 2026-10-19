"""Tests for wllharvest. Run from the repo root:  python3 -m unittest -v

The integration test runs the harvester against tools/fake_wll.py for ~15 s.
"""

import csv
import gzip
import io
import json
import math
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import wllharvest as wh  # noqa: E402

# A real /v1/current_conditions reply, 2026-10-19 (one line, as archived; device id
# replaced with the API documentation's example).
REAL_CC = ('{"data":{"did":"001D0A700002","ts":1792424779,"conditions":[{"lsid":235347,'
           '"data_structure_type":1,"txid":1,"temp":44.2,"hum":94.7,"dew_point":42.8,'
           '"wet_bulb":43.5,"heat_index":44.2,"wind_chill":44.2,"thw_index":44.2,'
           '"thsw_index":42.9,"wind_speed_last":0.0,"wind_dir_last":0,'
           '"wind_speed_avg_last_1_min":0.0,"wind_dir_scalar_avg_last_1_min":0,'
           '"wind_speed_avg_last_2_min":0.0,"wind_dir_scalar_avg_last_2_min":0,'
           '"wind_speed_hi_last_2_min":null,"wind_dir_at_hi_speed_last_2_min":null,'
           '"wind_speed_avg_last_10_min":0.18,"wind_dir_scalar_avg_last_10_min":270,'
           '"wind_speed_hi_last_10_min":1.0,"wind_dir_at_hi_speed_last_10_min":269,'
           '"rain_size":1,"rain_rate_last":0,"rain_rate_hi":0,"rainfall_last_15_min":0,'
           '"rain_rate_hi_last_15_min":0,"rainfall_last_60_min":0,"rainfall_last_24_hr":0,'
           '"rain_storm":0,"rain_storm_start_at":null,"solar_rad":56,"uv_index":0.0,'
           '"rx_state":0,"trans_battery_flag":0,"rainfall_daily":0,"rainfall_monthly":20,'
           '"rainfall_year":20,"rain_storm_last":15,"rain_storm_last_start_at":1791672301,'
           '"rain_storm_last_end_at":1791820860},{"lsid":235345,"data_structure_type":4,'
           '"temp_in":72.0,"hum_in":58.6,"dew_point_in":56.7,"heat_index_in":71.9},'
           '{"lsid":235344,"data_structure_type":3,"bar_sea_level":30.205,"bar_trend":0.016,'
           '"bar_absolute":29.85}]},"error":null}')


def cc_line(text, recv=1792424780.889, status=200, error=None):
    return json.loads(wh.raw_line(wh.Harvester.meta(recv, "cc", status, 0.947, error), text))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = wh.Store(Path(self.tmp.name) / "t.sqlite")
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ingest(self, rec):
        with self.store.transaction():
            return self.store.ingest(rec)

    def test_real_sample_native_and_si(self):
        err, data = self.ingest(cc_line(REAL_CC))
        self.assertIsNone(err)
        self.assertEqual(data["did"], "001D0A700002")
        row = self.db.execute("SELECT ts, did, lsid, temp, rainfall_monthly FROM iss").fetchone()
        self.assertEqual(row, (1792424779, "001D0A700002", 235347, 44.2, 20))
        si = self.db.execute("SELECT time_utc, temp_c, rainfall_monthly_mm, rain_storm_last_mm,"
                             " wind_speed_avg_last_10_min_ms, wind_speed_hi_last_2_min_ms FROM iss_si").fetchone()
        self.assertEqual(si[0], "2026-10-19 15:46:20.889")   # host arrival time, not ts
        self.assertAlmostEqual(si[1], (44.2 - 32) / 1.8)
        self.assertAlmostEqual(si[2], 20 * 0.254)      # rain_size 1 = 0.01 in per count
        self.assertAlmostEqual(si[3], 15 * 0.254)
        self.assertAlmostEqual(si[4], 0.18 * 0.44704)
        self.assertIsNone(si[5])                       # null stays null
        bar = self.db.execute("SELECT bar_sea_level_hpa, bar_trend_hpa FROM baro_si").fetchone()
        self.assertAlmostEqual(bar[0], 30.205 * 33.8638866667)
        self.assertAlmostEqual(bar[1], 0.016 * 33.8638866667)
        tin = self.db.execute("SELECT temp_in_c, hum_in FROM inside_si").fetchone()
        self.assertAlmostEqual(tin[0], (72.0 - 32) / 1.8)
        self.assertEqual(tin[1], 58.6)
        poll = self.db.execute("SELECT ts, did, ok, status, latency, error FROM polls").fetchone()
        self.assertEqual(poll, (1792424779, "001D0A700002", 1, 200, 0.947, None))

    def test_rain_size_codes(self):
        for code, mm in ((1, 0.254), (2, 0.2), (3, 0.1), (4, 0.0254)):
            body = {"data": {"did": "x", "ts": 1000 + code, "conditions": [
                {"lsid": 1, "data_structure_type": 1, "rain_size": code, "rainfall_daily": 63,
                 "rain_rate_last": 10}]}, "error": None}
            self.ingest(cc_line(json.dumps(body), recv=1000.5 + code))
            got = self.db.execute("SELECT rainfall_daily_mm, rain_rate_last_mmh FROM iss_si WHERE ts=?",
                                  (1000 + code,)).fetchone()
            self.assertAlmostEqual(got[0], 63 * mm)
            self.assertAlmostEqual(got[1], 10 * mm)

    def test_idempotent(self):
        rec = cc_line(REAL_CC)
        self.ingest(rec)
        self.ingest(rec)
        for t in ("iss", "baro", "inside", "polls"):
            self.assertEqual(self.db.execute(f"SELECT count(*) FROM {t}").fetchone()[0], 1, t)

    def test_new_fields_and_types(self):
        body = {"data": {"did": "x", "ts": 5, "conditions": [
            {"lsid": 1, "data_structure_type": 1, "temp": 50.0, "new_field": 7, "ts": 4,
             "weird-name": {"a": 1}},
            {"lsid": 2, "data_structure_type": 99, "mystery": [1, 2]}]}, "error": None}
        self.assertIsNone(self.ingest(cc_line(json.dumps(body)))[0])
        row = self.db.execute("SELECT ts, new_field, rec_ts, weird_name FROM iss").fetchone()
        self.assertEqual(row, (5, 7, 4, '{"a":1}'))
        self.assertEqual(self.db.execute("SELECT mystery FROM http_type99").fetchone(), ("[1,2]",))
        # A fresh Store on the same file sees the added columns.
        self.store.close()
        self.store = wh.Store(Path(self.tmp.name) / "t.sqlite")
        self.assertIn("new_field", self.store.cols["iss"] | {
            r[1] for r in self.store.db.execute("PRAGMA table_info(iss)")})

    def test_failures_are_recorded(self):
        cases = [
            (cc_line(None, recv=1.0, status=None, error="TimeoutError: timed out"), "TimeoutError"),
            (cc_line('{"data":null,"error":{"code":500,"message":"Internal"}}', recv=2.0, status=500),
             "device error"),
            (cc_line('{"data": {"did": "trunc', recv=3.0), "not JSON"),
            (cc_line('{"data":null,"error":null}', recv=4.0), "no data"),
        ]
        for rec, expect in cases:
            err, data = self.ingest(rec)
            self.assertIn(expect, err)
            self.assertIsNone(data)
        rows = self.db.execute("SELECT ok, error FROM polls ORDER BY recv_time").fetchall()
        self.assertEqual([r[0] for r in rows], [0, 0, 0, 0])
        self.assertEqual(self.db.execute("SELECT count(*) FROM iss").fetchone()[0], 0)

    def test_udp_and_rt(self):
        pkt = {"did": "x", "ts": 10, "conditions": [
            {"lsid": 1, "data_structure_type": 1, "txid": 1, "wind_speed_last": 5.0,
             "wind_dir_last": 346, "rain_size": 2, "rain_24_hr": 3}]}
        err, _ = self.ingest(json.loads(wh.raw_line({"recv": 10.2, "kind": "udp", "from": "1.2.3.4"},
                                                   json.dumps(pkt))))
        self.assertIsNone(err)
        self.assertEqual(self.db.execute("SELECT wind_speed_last_ms, rain_24_hr_mm FROM rt_iss_si").fetchone(),
                         (5.0 * 0.44704, 3 * 0.2))
        err, _ = self.ingest(json.loads(wh.raw_line(wh.Harvester.meta(11.0, "rt", 200, 0.1),
                                                   '{"data":{"broadcast_port":22222,"duration":3600},"error":null}')))
        self.assertIsNone(err)
        self.assertEqual(self.db.execute("SELECT ok, duration, broadcast_port FROM rt_requests").fetchone(),
                         (1, 3600, 22222))

    def test_every_si_view_compiles(self):
        for t in wh.FIELDS:
            self.db.execute(f"SELECT * FROM {t}_si LIMIT 1").fetchall()


class RawTests(unittest.TestCase):
    def test_raw_line_splices_verbatim(self):
        text = '{\n "data": {"name": "café \\n", "v": 1.50},\r\n "error": null}\n'
        line = wh.raw_line({"recv": 1.0, "kind": "cc"}, text)
        self.assertNotIn("\n", line)
        self.assertIn('"v": 1.50', line)                 # number text kept as sent
        rec = json.loads(line)
        self.assertEqual(rec["body"], json.loads(text))
        bad = json.loads(wh.raw_line({"recv": 1.0, "kind": "cc"}, "<html>oops\n</html>"))
        self.assertEqual(bad["text"], "<html>oops\n</html>")
        self.assertEqual(json.loads(wh.raw_line({"recv": 1.0, "kind": "cc", "error": "x"}, None)),
                         {"recv": 1.0, "kind": "cc", "error": "x"})

    def test_daily_rollover_and_gzip(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            arch = wh.RawArchive(root, "http")
            t_mid = 1792454400   # 2026-10-20T00:00:00Z
            arch.write(t_mid - 1, '{"n":1}')
            arch.write(t_mid + 1, '{"n":2}')   # new day: previous file gzipped
            day1 = root / "2026" / "wll_http_20261019.jsonl"
            self.assertFalse(day1.exists())
            self.assertEqual(gzip.open(str(day1) + ".gz", "rt").read(), '{"n":1}\n')
            arch.write(t_mid - 2, '{"n":3}')   # clock stepped back: reopens 10-19
            arch.write(t_mid + 2, '{"n":4}')   # ...and appends a second gzip member
            arch.close()
            self.assertEqual(gzip.open(str(day1) + ".gz", "rt").read(), '{"n":1}\n{"n":3}\n')
            # A restart on a later day compresses the leftover open file.
            arch = wh.RawArchive(root, "http")
            arch.write(t_mid + 86400, '{"n":5}')
            arch.close()
            ns = sorted(r["n"] for _, _, r in wh.read_raw(sorted(root.glob("*/wll_*"))))
            self.assertEqual(ns, [1, 2, 3, 4, 5])
            self.assertEqual(sorted(p.name for p in (root / "2026").iterdir()),
                             ["wll_http_20261019.jsonl.gz", "wll_http_20261020.jsonl.gz",
                              "wll_http_20261021.jsonl"])

    def test_truncated_tail_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jsonl"
            p.write_text('{"a":1}\n{"a":2}\n{"a":')
            self.assertEqual([r for _, _, r in wh.read_raw([p])], [{"a": 1}, {"a": 2}])


T0 = 1792454400   # 2026-10-20T00:00:00Z, a multiple of 600


def iss_poll(t, temp_c, wind_mph, wdir, rain, rx=0):
    body = {"data": {"did": "x", "ts": int(t), "conditions": [
        {"lsid": 1, "data_structure_type": 1, "txid": 1, "temp": 32 + 1.8 * temp_c, "hum": 50.0,
         "dew_point": 41.0, "solar_rad": 100, "uv_index": 1.0, "wind_speed_last": wind_mph,
         "wind_dir_last": wdir, "rain_size": 1, "rain_rate_last": 0, "rainfall_daily": rain,
         "rx_state": rx, "trans_battery_flag": 0, "wind_speed_avg_last_10_min": 2.0,
         "wind_dir_scalar_avg_last_10_min": 5, "wind_speed_hi_last_10_min": 4.0,
         "wind_dir_at_hi_speed_last_10_min": 350},
        {"lsid": 2, "data_structure_type": 3, "bar_sea_level": 30.0, "bar_absolute": 29.0, "bar_trend": 0.0},
        {"lsid": 3, "data_structure_type": 4, "temp_in": 68.0, "hum_in": 40.0}]}, "error": None}
    return cc_line(json.dumps(body), recv=t)


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = wh.Store(Path(self.tmp.name) / "t.sqlite")
        lines = [iss_poll(T0 - 10 + 0.03, 0, 0, 0, 5)]          # previous period: rain counter 5
        rain = {**{i: 5 for i in range(10)}, **{i: 6 for i in range(10, 20)},
                **{i: 8 for i in range(20, 30)}, 30: 0, **{i: 1 for i in range(31, 40)},
                **{i: 4 for i in range(46, 60)}}                 # +1 +2, midnight reset, +1, +3 across a gap
        self.present = sorted(rain)
        for i in self.present:
            calm = i in (0, 1)
            lines.append(iss_poll(T0 + 10 * i + 0.03, i, 0 if calm else 2, 0 if calm else (350 if i % 2 == 0 else 10),
                                  rain[i], rx=1 if i == 5 else 0))
        for i in (40, 41):                                       # failed polls; 42..45 never attempted
            lines.append(cc_line(None, recv=T0 + 10 * i + 1.0, status=None, error="TimeoutError: timed out"))
        lines.append(iss_poll(T0 + 600.03, 60, 2, 10, 4))       # next period
        with self.store.transaction():
            for rec in lines:
                self.store.ingest(rec)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def row(self, t_start):
        cur = self.store.db.execute("SELECT * FROM summary_10min WHERE t_start = ?", (t_start,))
        return dict(zip([d[0] for d in cur.description], cur.fetchone()))

    def test_period_statistics(self):
        self.assertEqual(self.store.summarize(600, T0 - 600, T0 + 1200), 3)
        r = self.row(T0)
        self.assertEqual(r["time_utc"], "2026-10-20 00:00:00")
        self.assertEqual((r["n_polls"], r["n_failed"]), (54, 2))
        temps = [float(i) for i in self.present]
        self.assertAlmostEqual(r["temp_c_min"], 0.0)
        self.assertAlmostEqual(r["temp_c_max"], 59.0)
        self.assertAlmostEqual(r["temp_c_mean"], sum(temps) / len(temps))
        self.assertAlmostEqual(r["temp_c_median"], (temps[26] + temps[27]) / 2)
        self.assertAlmostEqual(r["slp_hpa_mean"], 30.0 * 33.8638866667)
        self.assertAlmostEqual(r["temp_in_c_median"], 20.0)
        self.assertAlmostEqual(r["rx_ok_frac"], 53 / 54)
        # 1 + 2 + 0 (reset) + 1 counts of 0.254 mm; the +3 across the 70 s gap is not credited
        self.assertAlmostEqual(r["rain_mm"], 4 * 0.254)
        # 350 and 10 degrees average to north, not south; calms excluded from direction
        self.assertEqual(r["n_calm"], 2)
        self.assertAlmostEqual(min(r["wind_dir_mean_deg"], 360 - r["wind_dir_mean_deg"]), 0.0, places=6)
        self.assertAlmostEqual(r["wind_dir_steadiness"], math.cos(math.radians(10)))
        self.assertAlmostEqual(r["wind_vec_ms"], 52 / 54 * 2 * 0.44704 * math.cos(math.radians(10)))
        self.assertAlmostEqual(r["wind_ms_min"], 0.0)
        self.assertAlmostEqual(r["wind_ms_max"], 2 * 0.44704)
        self.assertAlmostEqual(r["wll_gust_10min_ms"], 4.0 * 0.44704)
        self.assertEqual(r["wll_gust_dir_10min_deg"], 350)
        nxt = self.row(T0 + 600)
        self.assertEqual(nxt["n_polls"], 1)
        self.assertAlmostEqual(nxt["rain_mm"], 0.0)              # counter steady across the boundary
        self.assertEqual(self.row(T0 - 600)["n_polls"], 1)

    def test_recompute_is_idempotent(self):
        self.store.summarize(600, T0, T0 + 600)
        first = self.row(T0)
        self.store.summarize(600, T0, T0 + 600)
        self.assertEqual(self.row(T0), first)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM summary_10min").fetchone()[0], 1)


class ServeTests(unittest.TestCase):
    def test_csv_endpoint(self):
        import threading
        import urllib.error
        import urllib.request
        with tempfile.TemporaryDirectory() as d:
            store = wh.Store(Path(d) / "wll.sqlite")
            with store.transaction():
                store.ingest(cc_line(REAL_CC))
            store.summarize(600, 1792424400, 1792425000)
            store.close()
            srv = wh.http.server.ThreadingHTTPServer(("127.0.0.1", 0), wh.CSVHandler)
            srv.data_dir, srv.db_path = Path(d), Path(d) / "wll.sqlite"
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{srv.server_address[1]}"

            def get(path):
                try:
                    with urllib.request.urlopen(base + path, timeout=5) as r:
                        return r.status, r.read().decode(), r.headers
                except urllib.error.HTTPError as e:
                    with e:
                        return e.code, e.read().decode(), e.headers
            try:
                code, body, hdr = get("/csv?table=summary_10min&start=2026-10-19&end=2026-10-19")
                self.assertEqual(code, 200, body)
                rows = list(csv.DictReader(io.StringIO(body)))
                self.assertEqual([r["time_utc"] for r in rows], ["2026-10-19 15:40:00"])
                self.assertIn("wll_summary_10min_20261019T0000_20261020T0000.csv",
                              hdr["Content-Disposition"])
                code, body, _ = get("/csv?table=iss_si&start=2026-10-19T15:46&end=2026-10-19T15:47")
                rows = list(csv.DictReader(io.StringIO(body)))
                self.assertEqual(len(rows), 1)
                self.assertAlmostEqual(float(rows[0]["temp_c"]), (44.2 - 32) / 1.8)
                self.assertEqual(get("/csv?table=iss_si&start=2026-10-20T00:00&end=2026-10-21")[1].count("\n"), 1)
                self.assertEqual(get("/csv?table=iss_si&start=2026-01-01&end=2026-10-19")[0], 400)
                self.assertEqual(get("/csv?table=sqlite_master")[0], 400)
                self.assertEqual(get("/csv?start=yesterday")[0], 400)
                self.assertEqual(get("/csv?start=2026-10-20&end=2026-10-19")[0], 400)
                code, body, _ = get("/")
                self.assertEqual(code, 200)
                self.assertIn("Download CSV", body)
                self.assertEqual(get("/health")[0], 503)        # no status.json here
                self.assertEqual(get("/nope")[0], 404)
            finally:
                srv.shutdown()
                srv.server_close()

    def test_date_range_rules(self):
        self.assertEqual(wh.parse_range("2026-10-01", "2026-10-01"), (1790812800.0, 1790899200.0))
        self.assertEqual(wh.parse_range(None, "2026-10-01T06:00")[1], 1790834400.0)


def free_port(kind):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class IntegrationTest(unittest.TestCase):
    """Harvester vs. fake WLL with faults; then the raw archive must rebuild
    exactly the database the live run produced."""

    def test_end_to_end(self):
        http_port, udp_port = free_port(socket.SOCK_STREAM), free_port(socket.SOCK_DGRAM)
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / "data"
            fake = subprocess.Popen(
                [sys.executable, str(ROOT / "tools" / "fake_wll.py"), "--port", str(http_port),
                 "--udp-target", f"127.0.0.1:{udp_port}", "--fault-every", "3", "--slow", "2",
                 "--quirks", "--udp-copies", "2"], stdout=subprocess.PIPE, text=True)
            try:
                fake.stdout.readline()   # wait until it is serving
                env = dict(os.environ, WLL_HOST="127.0.0.1", WLL_PORT=str(http_port),
                           WLL_DATA_DIR=str(data), WLL_POLL_INTERVAL="1", WLL_ALLOW_FAST_POLL="1",
                           WLL_HTTP_TIMEOUT="1", WLL_UDP_PORT=str(udp_port), WLL_RT_DURATION="20",
                           WLL_SUMMARY_PERIOD="5")
                harv = subprocess.Popen([sys.executable, str(ROOT / "wllharvest.py"), "run"],
                                        env=env, stderr=subprocess.PIPE, text=True)
                time.sleep(16)
                t0 = time.monotonic()
                harv.send_signal(signal.SIGTERM)
                _, logtext = harv.communicate(timeout=10)
                self.assertLess(time.monotonic() - t0, 3, "slow shutdown")
                self.assertEqual(harv.returncode, 0, logtext)
            finally:
                fake.kill()
                fake.wait()
                fake.stdout.close()

            db = sqlite3.connect(data / "wll.sqlite")
            polls = db.execute("SELECT ok, error FROM polls").fetchall()
            errors = " | ".join(e for ok, e in polls if not ok)
            self.assertGreaterEqual(len(polls), 10, logtext)
            for fault in ("HTTP 500", "not JSON", "Disconnected", "timed out"):
                self.assertIn(fault, errors)
            n_ok = sum(ok for ok, _ in polls)
            self.assertEqual(db.execute("SELECT count(*) FROM iss").fetchone()[0], n_ok)
            self.assertEqual(db.execute("SELECT count(*) FROM iss WHERE new_field_x = 1.5").fetchone()[0], n_ok)
            self.assertEqual(db.execute("SELECT count(*) FROM http_type99").fetchone()[0], n_ok)
            n_udp = db.execute("SELECT count(*) FROM rt_iss").fetchone()[0]
            self.assertGreaterEqual(n_udp, 4, logtext)
            self.assertGreaterEqual(db.execute("SELECT count(*) FROM rt_requests WHERE ok").fetchone()[0], 1)
            self.assertIn("new field: added column iss.new_field_x", logtext)
            st = json.loads((data / "status.json").read_text())
            self.assertEqual((st["http"]["ok"], st["udp"]["packets"]), (n_ok, n_udp))
            # Every packet arrived twice; copies are dropped before the archive.
            self.assertGreaterEqual(st["udp"]["duplicates_dropped"], n_udp - 1)
            udp_lines = list(wh.read_raw(sorted((data / "raw").glob("*/wll_udp_*"))))
            self.assertEqual(len(udp_lines), n_udp)
            db.close()

            # The raw archive alone must reproduce every table exactly.
            rebuilt = Path(d) / "rebuilt.sqlite"
            self.assertEqual(wh.main(["rebuild", "--raw-dir", str(data / "raw"), "--db", str(rebuilt),
                                      "--period", "5"]), 0)
            live, again = sqlite3.connect(data / "wll.sqlite"), sqlite3.connect(rebuilt)
            names = [r[0] for r in live.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            self.assertIn("rt_iss", names)
            self.assertIn("summary_5s", names)
            n_sum, n_polled = live.execute("SELECT count(*), sum(n_polls) FROM summary_5s").fetchone()
            self.assertGreaterEqual(n_sum, 3)
            self.assertEqual(n_polled, n_ok)               # every good poll lands in one period
            for t in names:
                if t == "meta":
                    continue
                dump = lambda c: sorted(json.dumps(dict(zip([x[0] for x in cur.description], r)),
                                                   sort_keys=True)
                                        for cur in [c.execute(f'SELECT * FROM "{t}"')] for r in cur)
                self.assertEqual(dump(live), dump(again), t)

            live.close()
            again.close()

            # Export and health work on the result.
            buf = io.StringIO()
            with redirect_stdout(buf):
                wh.main(["export", "--db", str(data / "wll.sqlite"), "--table", "iss_si"])
            rows = list(csv.DictReader(io.StringIO(buf.getvalue())))
            self.assertEqual(len(rows), n_ok)
            self.assertIn("temp_c", rows[0])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(wh.main(["health", "--data-dir", str(data)]), 0)


if __name__ == "__main__":
    unittest.main()
