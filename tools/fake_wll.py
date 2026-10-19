#!/usr/bin/env python3
"""Simulated WeatherLink Live for testing wllharvest without the real device.

Serves /v1/current_conditions and /v1/real_time with the documented API v1
behaviour (record layout copied from a real WLL, ISS + barometer + inside
T/RH), broadcasts real-time UDP every 2.5 s while a request is active, and
can inject faults and firmware quirks:

  python3 tools/fake_wll.py --port 8080 --udp-target 127.0.0.1:22222 --fault-every 5 --quirks
"""

import argparse
import json
import math
import random
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, urlparse

DID = "001D0A7FFFFF"
FAULTS = ("http500", "badjson", "drop", "slow")


class Weather:
    """Smooth synthetic weather; rain counts tick up now and then."""

    def __init__(self):
        self.rain_daily = 0
        self.lock = threading.Lock()

    def iss(self, t):
        with self.lock:
            if random.random() < 0.05:
                self.rain_daily += 1
            rain = self.rain_daily
        temp = 55 + 10 * math.sin(2 * math.pi * t / 86400)
        wind = max(0.0, 4 + 3 * math.sin(t / 60) + random.gauss(0, 1))
        wdir = int(270 + 30 * math.sin(t / 300)) % 360 or 360
        return {
            "lsid": 235347,
            "data_structure_type": 1,
            "txid": 1,
            "temp": round(temp, 1),
            "hum": 80.0,
            "dew_point": round(temp - 4, 1),
            "wet_bulb": round(temp - 2, 1),
            "heat_index": round(temp, 1),
            "wind_chill": round(temp, 1),
            "thw_index": round(temp, 1),
            "thsw_index": round(temp, 1),
            "wind_speed_last": round(wind, 1),
            "wind_dir_last": wdir,
            "wind_speed_avg_last_1_min": 4.0,
            "wind_dir_scalar_avg_last_1_min": 270,
            "wind_speed_avg_last_2_min": 4.0,
            "wind_dir_scalar_avg_last_2_min": 270,
            "wind_speed_hi_last_2_min": 7.0,
            "wind_dir_at_hi_speed_last_2_min": 268,
            "wind_speed_avg_last_10_min": 4.18,
            "wind_dir_scalar_avg_last_10_min": 270,
            "wind_speed_hi_last_10_min": 9.0,
            "wind_dir_at_hi_speed_last_10_min": 269,
            "rain_size": 1,
            "rain_rate_last": 0,
            "rain_rate_hi": 0,
            "rainfall_last_15_min": 0,
            "rain_rate_hi_last_15_min": 0,
            "rainfall_last_60_min": 0,
            "rainfall_last_24_hr": rain,
            "rain_storm": rain,
            "rain_storm_start_at": None,
            "solar_rad": 56,
            "uv_index": 0.0,
            "rx_state": 0,
            "trans_battery_flag": 0,
            "rainfall_daily": rain,
            "rainfall_monthly": 20 + rain,
            "rainfall_year": 20 + rain,
            "rain_storm_last": 15,
            "rain_storm_last_start_at": 1791672301,
            "rain_storm_last_end_at": 1791820860,
        }


class FakeWLL(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, opts):
        super().__init__(addr, Handler)
        self.opts = opts
        self.weather = Weather()
        self.cc_count = 0
        self.rt_end = 0.0
        self.lock = threading.Lock()

    def conditions(self, t):
        recs = [
            self.weather.iss(t),
            {
                "lsid": 235345,
                "data_structure_type": 4,
                "temp_in": 72.0,
                "hum_in": 58.6,
                "dew_point_in": 56.7,
                "heat_index_in": 71.9,
            },
            {
                "lsid": 235344,
                "data_structure_type": 3,
                "bar_sea_level": 30.205,
                "bar_trend": 0.016,
                "bar_absolute": 29.85,
            },
        ]
        if self.opts.quirks:  # what a future firmware might add
            recs[0]["new_field_x"] = 1.5
            recs.append({"lsid": 999, "data_structure_type": 99, "mystery": [1, 2]})
        return recs

    def udp_loop(self):
        host, port = self.opts.udp_target.rsplit(":", 1)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while True:
            t = time.time()
            if t < self.rt_end:
                iss = self.weather.iss(t)
                pkt = {
                    "did": DID,
                    "ts": int(t),
                    "conditions": [
                        {
                            "lsid": 235347,
                            "data_structure_type": 1,
                            "txid": 1,
                            "wind_speed_last": iss["wind_speed_last"],
                            "wind_dir_last": iss["wind_dir_last"],
                            "rain_size": 1,
                            "rain_rate_last": 0,
                            "rain_15_min": 0,
                            "rain_60_min": 0,
                            "rain_24_hr": iss["rainfall_daily"],
                            "rain_storm": iss["rain_storm"],
                            "rain_storm_start_at": 0,
                            "rainfall_daily": iss["rainfall_daily"],
                            "rainfall_monthly": iss["rainfall_monthly"],
                            "rainfall_year": iss["rainfall_year"],
                            "wind_speed_hi_last_10_min": 9.0,
                            "wind_dir_at_hi_speed_last_10_min": 269,
                        }
                    ],
                }
                for _ in range(self.opts.udp_copies):
                    s.sendto(json.dumps(pkt).encode(), (host, int(port)))
            time.sleep(2.5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code, obj=None, raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def error(self, code, msg):
        self.reply(code, {"data": None, "error": {"code": code, "message": msg}})

    def do_GET(self):
        srv, url, t = cast(FakeWLL, self.server), urlparse(self.path), time.time()
        if url.path == "/v1/current_conditions":
            with srv.lock:
                srv.cc_count += 1
                n = srv.cc_count
            fe = srv.opts.fault_every
            if fe and n % fe == 0:
                fault = FAULTS[(n // fe - 1) % len(FAULTS)]
                if fault == "http500":
                    return self.error(500, "Internal Server Error")
                if fault == "badjson":
                    return self.reply(200, raw=b'{"data": {"did": "trunc')
                if fault == "drop":
                    self.close_connection = True
                    return
                time.sleep(srv.opts.slow)
            return self.reply(200, {"data": {"did": DID, "ts": int(t), "conditions": srv.conditions(t)}, "error": None})
        if url.path == "/v1/real_time":
            try:
                dur = int(parse_qs(url.query).get("duration", ["1200"])[0])
            except ValueError:
                return self.error(400, "Bad Request")
            if not 0 < dur <= 86400:
                return self.error(400, "Bad Request")
            with srv.lock:  # shorter requests are ignored, longer ones reset the end
                srv.rt_end = max(srv.rt_end, t + dur)
                left = int(srv.rt_end - t)
            return self.reply(200, {"data": {"broadcast_port": 22222, "duration": left}, "error": None})
        self.error(404, "Not Found")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--udp-target", default="255.255.255.255:22222", help="host:port for UDP packets")
    p.add_argument(
        "--fault-every",
        type=int,
        default=0,
        help="every Nth current_conditions request fails, cycling " + ", ".join(FAULTS),
    )
    p.add_argument("--slow", type=float, default=10.0, help="delay for the 'slow' fault, s")
    p.add_argument("--quirks", action="store_true", help="add an unknown field and record type")
    p.add_argument(
        "--udp-copies", type=int, default=1, help="send each UDP packet N times (mimics a dual-homed receiver)"
    )
    opts = p.parse_args()
    srv = FakeWLL((opts.bind, opts.port), opts)
    threading.Thread(target=srv.udp_loop, daemon=True).start()
    print(f"fake WLL on http://{opts.bind}:{opts.port}, UDP to {opts.udp_target}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
