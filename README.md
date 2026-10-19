# WeatherLink — local data harvester for a Davis WeatherLink Live

[![CI](https://github.com/mousebrains/WeatherLink/actions/workflows/ci.yml/badge.svg)](https://github.com/mousebrains/WeatherLink/actions/workflows/ci.yml)

`wllharvest` records everything a Davis WeatherLink Live (WLL) reports on its
[local API](https://weatherlink.github.io/weatherlink-live-local-api/).
It also builds 10-minute summaries and serves CSV extracts over HTTP. It
needs no WeatherLink subscription or internet access, only the LAN.

The WLL keeps **no local history**, so anything not collected is gone. The
harvester is built to run unattended for years. It survives restarts and
device outages, and every gap is recorded.

## Why the local API (v1) rather than the WeatherLink cloud API (v2)

The WLL's only local interface is `/v1/...`. The
[v2 API](https://weatherlink.github.io/v2-api/) is Davis's *cloud* service at
`api.weatherlink.com/v2`, which needs an API key and secret. Its
[data permissions](https://weatherlink.github.io/v2-api/data-permissions)
depend on the subscription:

| Plan | Current conditions | History |
|---|---|---|
| Basic (free) | most recent 15-minute record | none |
| Pro | most recent 5-minute record | yes |
| Pro+ | most recent record | yes |

The v2 API is also limited to 1,000 calls per hour and depends on the internet
and Davis's servers. Polling the WLL directly every 10 s gives finer data, at no
cost, with no outside dependency. The previous v2 cloud client in this
repository (`WeatherLink.py`, `current.py`) is in git history at commit
`5d00b10`.

## What is recorded

| Stream | Contents | Cadence | Reach |
|---|---|---|---|
| HTTP `GET /v1/current_conditions` | every sensor: ISS temperature, humidity, wind (instantaneous plus the WLL's 1/2/10-minute averages and gusts), rain, solar, UV, battery and radio-link state; the WLL's barometer and indoor T/RH; leaf/soil stations | every 10 s (the documented minimum) | any routed network |
| UDP broadcast, port 22222 (optional, **off** by default in `compose.yaml`) | ISS wind and rain only | every 2.5 s | same subnet/VLAN only |

The HTTP data contain every UDP field. On the development station, the two
streams were compared over about 70 polls. Instantaneous wind matched
sample for sample. The 10-minute gust statistics carried the same values,
with the HTTP copy updating about a minute later. The rain fields matched,
but it was dry, so that match is untested in rain. UDP adds only the full-rate
2.5 s wind series. When enabled, it is requested with
`GET /v1/real_time?duration=3600`, renewed every 30 minutes, and re-requested
with a backoff if it stops. A host with two interfaces on the subnet receives
each broadcast twice, so byte-identical copies within 1 s are dropped and
counted.

## Outputs (`DATA_DIR`, `/data` in the container)

```
raw/2026/wll_http_20261019.jsonl.gz   every HTTP reply or failure, one JSON line each
raw/2026/wll_udp_20261019.jsonl.gz    every UDP packet (when enabled)
wll.sqlite                            parsed tables, SI views, 10-minute summaries
status.json                           live health and counters (rewritten every poll)
```

* **The raw archive is the record of truth.** Each line is
  `{"recv": <host UTC epoch s>, "kind": "cc"|"rt"|"udp", "status", "latency", "body": <the WLL's JSON, verbatim>}`,
  or `"error"` instead of a body when a request failed. Files are split by UTC day and
  gzipped when the day ends. The database is derived from these files, and
  `wllharvest.py rebuild` regenerates it exactly, summaries included. An
  integration test checks that table for table.
* **Native tables:** `iss`, `baro`, `inside`, `soil_leaf` and `rt_iss` (UDP) hold the WLL's
  fields under the API's names, in its units: °F, mph, inHg, and rain in *counts*.
  Each row also has `recv_time` (host clock), `ts` (WLL clock, integer s), `did` (device
  id) and `lsid` (sensor id). If a firmware update adds fields or record types, they
  get new columns or tables automatically, and the log says so.
* **SI views:** `iss_si`, `baro_si`, `inside_si`, `soil_leaf_si` and `rt_iss_si` add `time_utc`
  (host arrival time, ms) and convert units:

  | From | To | Column suffix |
  |---|---|---|
  | °F | °C | `_c` |
  | mph | m/s | `_ms` |
  | inHg | hPa | `_hpa` |
  | rain counts | mm | `_mm` |
  | counts/h | mm/h | `_mmh` |

  Rain conversions use each row's `rain_size` code: 1 = 0.01 in, 2 = 0.2 mm,
  3 = 0.1 mm, 4 = 0.001 in.
* **Provenance:** `polls` has one row per poll attempt, with ok/error, HTTP status,
  latency and both clocks. So a gap can always be traced to its cause: no row
  means the harvester was down, and `ok = 0` means the WLL was unreachable.
  `rt_requests` does the same for real-time requests.

### 10-minute summaries: `summary_10min`

There is one row per UTC 10-minute period, keyed by `t_start` (epoch s) and
`time_utc` (the period start). The harvester writes each row 1 s after its
period ends. The rows are derived from the 10 s records, which are kept, so any
other statistic can be computed later. `wllharvest.py summarize` recomputes any
range or a different `--period`. A period cut short by a restart gets a partial
row, with a low `n_polls`, until it is recomputed.

| Columns | Meaning |
|---|---|
| `X_min`, `X_mean`, `X_median`, `X_max` | statistics of the 10 s samples, for each `X` in the table below |
| `n_polls`, `n_failed` | successful and failed polls (60 = complete) |
| `rx_ok_frac` | fraction of samples with the ISS radio link fully synced (`rx_state` 0) |
| `battery_low` | 1 if the ISS battery flag was set in any sample |
| `rain_mm` | rain in the period. It is summed from increases in the `rainfall_daily` counter (which resets at the WLL's local midnight) between samples at most 60 s apart. Rain that falls during a longer gap is credited to no period, but still shows in the counters. |
| `wind_dir_mean_deg`, `wind_dir_steadiness` | mean of the wind directions treated as unit vectors, with calms excluded. Steadiness is 1 for a constant direction and near 0 for one that varies widely. Directions are where the wind comes from, in degrees. |
| `wind_vec_ms`, `wind_vec_dir_deg`, `n_calm` | speed-weighted (vector) mean wind, and the number of calm samples |
| `wll_wind_avg_10min_ms`, `wll_wind_dir_10min_deg`, `wll_gust_10min_ms`, `wll_gust_dir_10min_deg` | the WLL's own 10-minute statistics, from the period's last poll. These use every 2.5 s ISS sample, but their window runs about 1 minute behind the period. `wind_ms_max` is aligned to the period but sees only every 4th sample. |

| `X` (prefix) | Field (SI) |
|---|---|
| `temp_c`, `rh`, `dew_point_c` | outdoor temperature, humidity, dew point |
| `solar_wm2`, `uv_index` | solar radiation, UV index |
| `wind_ms` | instantaneous wind speed (10 s samples) |
| `rain_rate_mmh` | rain rate |
| `slp_hpa`, `station_p_hpa` | sea-level and station pressure |
| `temp_in_c`, `rh_in` | temperature and humidity where the WLL sits |

## CSV over HTTP (`serve`)

A second, read-only container serves a small page at `http://<host>:8580/`. It
lets anyone on the LAN download CSV for a date range. Scripts can fetch the same data:

```sh
curl -OJ 'http://nas0.mousebrains.com:8580/csv?table=summary_10min&start=2026-10-01&end=2026-10-07'
```

* **Tables:**
  * `summary_10min`, the default, with any range.
  * `iss_si`, `baro_si`, `inside_si` and `polls`: the 10 s data, at most 31 days per request.
* **Dates:** ISO 8601 UTC. A date-only `end` includes that whole day; an end with a
  time is exclusive. With no `start`, the last 24 hours are returned.
* **Other endpoints:** `/status.json` (harvester counters) and `/health`
  (`OK`, or HTTP 503 with the reason).

There is no login, so it is meant for a trusted LAN. Don't forward the port to the internet.

## Deploy on TrueNAS SCALE

Copy the files to the NAS and run the install script with `sudo`. It runs on the
NAS's own Python (3.11 on TrueNAS 25.10) and uses the TrueNAS API.

```sh
scp wllharvest.py compose.yaml deploy_truenas.py nas0:wllharvest/
ssh -t nas0 'sudo python3 ~/wllharvest/deploy_truenas.py'   # defaults: --dataset SSD/WeatherLink --app wllharvest
```

Each run does the following:

1. Creates the dataset with TrueNAS's APPS preset, if it's missing.
2. Gives the user running `sudo` inherited read access, through the ACL API.
3. Installs the code into `<dataset>/code`.
4. Makes `<dataset>/data` writable by the apps user (568) and checks access.
5. Creates the app, or updates and restarts it only if `compose.yaml` or the code changed.
6. Waits for fresh data and checks the web page.

`--no-app` stops after the dataset, permission and code steps. To upgrade,
copy the new files and run the script again; the data carries over.

To install by hand instead: create the dataset with `code/` and `data/`
directories, `chown 568:568 data`, and copy `wllharvest.py` into `code/`. Then use
**Apps → Discover Apps → ⋮ → Install via YAML** with `compose.yaml`, after
replacing `/mnt/POOL/apps/wllharvest`.

**Checking on it:**

* Open `http://<nas>:8580/status.json`.
* Read the app logs, which include an hourly summary line.
* Watch the container healthchecks. The harvester turns unhealthy after 2 minutes
  without a successful poll (or without a UDP packet, if UDP is on).

**On a VM or any Linux host:** use `wllharvest.service` and
`wllharvest-web.service`. Their header comments list the commands.

## Configuration

Settings are read from environment variables. Each has a matching command-line flag.

| Variable | Default | Meaning |
|---|---|---|
| `WLL_HOST` | — (required) | WLL IP or hostname; give the WLL a DHCP reservation |
| `WLL_PORT` | 80 | |
| `WLL_DATA_DIR` | `/data` | |
| `WLL_POLL_INTERVAL` | 10 | s; values below 10 are raised to 10. Polls are aligned to UTC multiples. |
| `WLL_HTTP_TIMEOUT` | 8 | s (a wired WLL usually answers in 0.03 s, occasionally about 1 s) |
| `WLL_SUMMARY_PERIOD` | 600 | s per summary row; table `summary_<N>min` |
| `WLL_UDP` | 1 (`compose.yaml` sets 0) | request and record the 2.5 s UDP stream |
| `WLL_UDP_PORT` | 22222 | |
| `WLL_RT_DURATION` | 3600 | s per real-time request, renewed at half |
| `WLL_RAW` | 1 | keep the raw archive (strongly recommended) |
| `WLL_SERVE_BIND`, `WLL_SERVE_PORT` | 0.0.0.0, 8580 | address and port for `serve` |
| `WLL_LOG_LEVEL` | INFO | |

## Using the data

```sh
python3 wllharvest.py export --db data/wll.sqlite --start 2026-10-01 --end 2026-10-31 -o oct.csv   # summary_10min
python3 wllharvest.py export --db data/wll.sqlite --table iss_si --start 2026-10-19 -o day.csv     # 10 s data
python3 wllharvest.py summarize --db data/wll.sqlite --period 3600                                  # summary_60min
```

```python
import pandas as pd

s = pd.read_csv("http://nas0.mousebrains.com:8580/csv?start=2026-10-01&end=2026-10-07")
s.index = pd.to_datetime(s.t_start, unit="s", utc=True)
```

Don't open the live `wll.sqlite` over SMB/NFS from another machine. SQLite's
WAL mode needs shared memory, which network shares don't provide reliably. Use
the web service or `export`, or copy a snapshot first
(`sqlite3 wll.sqlite "vacuum into 'copy.sqlite'"`).

## Things to know about the data

These measurements were made on the development station on 2026-10-19.

* **Use `recv_time` / `time_utc`, not `ts`.**
  * `ts` is the WLL's clock, truncated to a whole second.
  * A test sent 25 requests at varying fractions of a second over wired Ethernet
    (0.03 s round trip). It put the WLL's clock 0.31 ± 0.03 s behind the host's,
    and NTP checks put the host within ±0.024 s of `time.apple.com`.
  * So `ts` reads 0.3–1.3 s early. Whether the lag drifts is unmeasured.
  * `recv_time` is the host's arrival time; for HTTP records, the request went out
    `polls.latency` seconds earlier.
  * The host must run NTP. The hourly log line shows the host−WLL offset.
* **Each value is the sensor's "most recent valid" reading.** The ISS sends different
  sensors at different rates, so 10 s polls repeat slow channels. In one test,
  temperature and humidity were unchanged for 40 s while solar radiation updated once.
* **When the ISS link is lost** (`rx_state` = 2), the WLL keeps reporting stale
  values. Filter on `rx_state`, or check `rx_ok_frac` in the summaries.
* **Rain counters:** `rainfall_daily` resets at local midnight, in the WLL's configured
  time zone, and `rainfall_year` at the season-start month set in WeatherLink.
* **Wind direction 0** appeared together with speed 0. Davis consoles conventionally
  use 0 for calm and 360 for north; that convention is unverified for the WLL.
  The summaries exclude calms from direction statistics.
* **Evapotranspiration:** neither the local API documentation nor the WLL's actual
  responses include an ET field. It can be computed later from the stored
  temperature, humidity, wind, solar radiation and pressure, for example with ASCE
  standardized Penman–Monteith.
* **Battery:** `trans_battery_flag` = 1 means low. It can flicker with solar charging,
  so judge it at night.

**Radio-range testing:** run the harvester on a laptop next to a candidate WLL
location (HTTP works across routers), then check the link:

```sh
python3 wllharvest.py run --host <wll> --data-dir ./range1 --no-udp   # Ctrl-C to stop
python3 wllharvest.py status --data-dir ./range1 --hours 1             # % synced / missing / lost
```

**Storage** for 10 s HTTP polls, in round numbers:

| | Size |
|---|---|
| Raw lines, before gzip | 11.7 MB per day (1,358 B per poll) |
| Raw lines, gzipped | ≈ 0.35 MB per day; that was calm weather, so expect more in active weather |
| `wll.sqlite` | 4.2 MB per day, ≈ 1.5 GB per year |
| UDP, if enabled | 467 B per packet raw (≈ 16 MB per day before gzip) and about 1.3 GB per year in the database |

## Files and testing

```
wllharvest.py               harvester, summaries, CSV server, CLI (Python ≥ 3.13, standard library only)
compose.yaml                Docker / TrueNAS app: harvester + web containers
deploy_truenas.py           TrueNAS install/upgrade via its API (runs on the NAS's Python 3.11)
wllharvest*.service         systemd units for a VM
tools/fake_wll.py           simulated WLL with fault injection
tests/                      unit, end-to-end, install-script and container smoke tests
pyproject.toml              ruff and mypy settings
.github/workflows/ci.yml    CI: lint, types, tests, container smoke test
```

The unit tests use a real WLL response (with Davis's documentation device ID
substituted), plus synthetic periods with known statistics: rain-counter resets,
data gaps, and wind directions on either side of north.

The end-to-end test runs the harvester against the simulator. The simulator
injects HTTP 500s, malformed JSON, dropped connections, timeouts, duplicate UDP
packets, an unknown field and an unknown record type. The test checks that all of
these were recorded, that SIGTERM shuts the harvester down cleanly, and that the raw
archive rebuilds an identical database.

The install-script tests use a fake TrueNAS API client. They check which changes
trigger an app create, update or restart, and that an unchanged app is left
running. They also check the ACL grant and the path substitution.

**CI** runs on every push and pull request, and weekly:

| Job | What it runs |
|---|---|
| lint | `ruff check` and `ruff format --check`; `mypy` on the 3.13 code, with every shipped function annotated; `mypy --python-version 3.11 deploy_truenas.py` |
| test | the unit and end-to-end tests on Python 3.13 and 3.14 (Linux) |
| deploy-script | the install-script tests on Python 3.11, the TrueNAS host's version |
| container | validates `compose.yaml`, then runs the real app on `python:3.13-slim` as user 568 against the simulator. It checks polls, summaries, the CSV and health endpoints, and a clean shutdown. |

The weekly run catches changes to the floating `python:3.13-slim` image before a
TrueNAS redeploy pulls it.

To run the same checks locally:

```sh
python3 -m unittest discover -s tests          # ~20 s; PyYAML needed for the install-script tests
uvx ruff check . && uvx ruff format --check .
uvx --with types-PyYAML mypy && uvx --with types-PyYAML mypy --python-version 3.11 deploy_truenas.py
```

The license is GPL-3.0.
