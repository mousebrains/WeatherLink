"use strict";
// Weather dashboard: current conditions from api/latest, charts from
// api/series (10-minute summaries, merged hourly or daily for long ranges).
(() => {
  const STEP = { 1: 600, 7: 600, 30: 3600, 365: 86400 }; // range in days -> bin, s
  const STEP_NAME = { 600: "10 min", 3600: "hour", 86400: "day" };
  const POLL_S = 10; // harvester poll interval
  const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
  const RX = ["link OK", "link missing packets", "link LOST"];

  const $ = (sel) => document.querySelector(sel);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const fmt = (v, digits = 1) => (v == null || !Number.isFinite(v) ? "–" : v.toFixed(digits));
  const compass = (deg) => (deg == null ? "" : COMPASS[Math.round((((deg % 360) + 360) % 360) / 22.5) % 16]);
  const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const maxOf = (a, b) => (a == null ? b : b == null ? a : Math.max(a, b));
  const tzOffset = () => -new Date().getTimezoneOffset() * 60; // s east of UTC
  const store = {
    get: (k) => {
      try {
        return localStorage.getItem(k);
      } catch {
        return null;
      }
    },
    set: (k, v) => {
      try {
        localStorage.setItem(k, v);
      } catch {
        /* storage unavailable (private mode): the choice just isn't remembered */
      }
    },
  };

  // Display units. The data arrive in SI; US customary is the default and the
  // choice is remembered in this browser.
  const UNITS = {
    us: {
      temp: { unit: "°F", conv: (c) => c * 1.8 + 32, digits: 1 },
      wind: { unit: "mph", conv: (ms) => ms / 0.44704, digits: 1 },
      rain: { unit: "in", conv: (mm) => mm / 25.4, digits: 2 },
      rate: { unit: "in/h", conv: (mmh) => mmh / 25.4, digits: 2 },
      press: { unit: "inHg", conv: (hpa) => hpa / 33.8638866667, digits: 2 },
    },
    si: {
      temp: { unit: "°C", conv: (c) => c, digits: 1 },
      wind: { unit: "m/s", conv: (ms) => ms, digits: 1 },
      rain: { unit: "mm", conv: (mm) => mm, digits: 1 },
      rate: { unit: "mm/h", conv: (mmh) => mmh, digits: 1 },
      press: { unit: "hPa", conv: (hpa) => hpa, digits: 1 },
    },
  };
  let units = store.get("wx-units") === "si" ? "si" : "us";
  const U = (kind) => UNITS[units][kind];
  const cv = (kind, v) => (v == null ? null : U(kind).conv(v));
  const cvs = (kind, arr) => arr.map((v) => cv(kind, v));
  const show = (kind, v) => fmt(cv(kind, v), U(kind).digits);
  const otherTemp = (c) => {
    const o = UNITS[units === "us" ? "si" : "us"].temp;
    return `${fmt(c == null ? null : o.conv(c), 0)} ${o.unit}`;
  };

  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
    return r.json();
  }

  function setStatus(text, alert) {
    const el = $("#status");
    el.textContent = text;
    el.classList.toggle("alert", Boolean(alert));
  }

  function ago(s) {
    if (s == null) return "never";
    if (s < 90) return `${Math.round(s)} s ago`;
    if (s < 5400) return `${Math.round(s / 60)} min ago`;
    return `${(s / 3600).toFixed(1)} h ago`;
  }

  // Text-only DOM building: values never become HTML.
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function tile(label, value, unit, sub) {
    const box = el("div", "tile");
    const val = el("div", "value", value);
    val.append(el("span", "unit", unit));
    box.append(el("div", "label", label), val, el("div", "sub", sub));
    return box;
  }

  // -- current conditions ---------------------------------------------------

  async function refreshLatest() {
    try {
      const d = await getJSON("api/latest");
      const s = d.status || {};
      const iss = d.iss || {};
      const baro = d.baro || {};
      const inside = d.inside || {};
      const age = s.updated_epoch ? Date.now() / 1000 - s.updated_epoch : null;
      const fails = (s.http && s.http.consecutive_fail) || 0;
      const parts = [`Updated ${ago(age)}`, `ISS ${RX[iss.rx_state] ?? "link unknown"}`];
      parts.push(`battery ${iss.trans_battery_flag ? "LOW" : "OK"}`);
      if (fails) parts.push(`${fails} failed polls in a row`);
      setStatus(parts.join(" · "), age == null || age > 120 || iss.rx_state === 2 || fails > 2);

      const wind = iss.wind_speed_avg_last_2_min_ms;
      const dir = iss.wind_dir_scalar_avg_last_2_min;
      const trend = baro.bar_trend_hpa;
      const arrow = trend == null ? "" : trend > 0.5 ? "↑ " : trend < -0.5 ? "↓ " : "→ ";
      const t = U("temp").unit;
      $("#tiles").replaceChildren(
        tile("Temperature", show("temp", iss.temp_c), t, `${otherTemp(iss.temp_c)} · dew point ${show("temp", iss.dew_point_c)} ${t}`),
        tile("Humidity", fmt(iss.hum, 0), "%", `feels like ${show("temp", iss.thw_index_c)} ${t}`),
        tile(
          "Wind (2-min avg)",
          show("wind", wind),
          U("wind").unit,
          `${wind ? `${compass(dir)} ${fmt(dir, 0)}°` : "calm"} · 10-min gust ${show("wind", iss.wind_speed_hi_last_10_min_ms)}`,
        ),
        tile("Rain today", show("rain", iss.rainfall_daily_mm), U("rain").unit, `rate ${show("rate", iss.rain_rate_last_mmh)} ${U("rate").unit}`),
        tile("Pressure", show("press", baro.bar_sea_level_hpa), U("press").unit, `${arrow}${show("press", trend)} ${U("press").unit} in 3 h`),
        tile("Sunshine", fmt(iss.solar_rad, 0), "W/m²", `UV index ${fmt(iss.uv_index)}`),
        tile("Indoors", show("temp", inside.temp_in_c), t, `${fmt(inside.hum_in, 0)} % humidity`),
      );
    } catch (e) {
      setStatus(`Cannot reach the weather service: ${e.message}`, true);
    }
  }

  // -- today's extremes (10-minute summaries since local midnight) -----------

  async function refreshToday() {
    const midnight = new Date();
    midnight.setHours(0, 0, 0, 0);
    try {
      const d = await getJSON(`api/series?t0=${midnight.getTime() / 1000}&t1=${Date.now() / 1000}&step=600`);
      const extreme = (arr, better) => {
        let best = null;
        let at = null;
        arr.forEach((v, i) => {
          if (v != null && (best == null || better(v, best))) [best, at] = [v, d.t[i]];
        });
        return [best, at];
      };
      const [hi, hiAt] = extreme(d.temp_c_max, (a, b) => a > b);
      const [lo, loAt] = extreme(d.temp_c_min, (a, b) => a < b);
      const gusts = d.t.map((_, i) => maxOf(d.wll_gust_10min_ms[i], d.wind_ms_max[i]));
      const [gust, gustAt] = extreme(gusts, (a, b) => a > b);
      const p = $("#today");
      if (hi == null) {
        p.textContent = "No data yet today.";
        return;
      }
      p.replaceChildren(
        "Today: high ",
        el("b", null, `${show("temp", hi)} ${U("temp").unit}`),
        ` at ${clock(hiAt)} · low `,
        el("b", null, `${show("temp", lo)} ${U("temp").unit}`),
        ` at ${clock(loAt)} · strongest gust `,
        el("b", null, `${show("wind", gust)} ${U("wind").unit}`),
        ` at ${clock(gustAt)} (10-minute periods starting at those times)`,
      );
    } catch (e) {
      $("#today").textContent = `Cannot load today's summary: ${e.message}`;
    }
  }

  // -- charts -----------------------------------------------------------------

  const axis = (extra = {}) => ({
    stroke: css("--muted"),
    grid: { stroke: css("--grid"), width: 1 },
    ticks: { stroke: css("--grid"), width: 1 },
    ...extra,
  });
  const line = (label, color, extra = {}) => ({ label, stroke: color, width: 1.5, value: (u, v) => fmt(v), ...extra });
  const fromZero = (u, min, max) => [0, max > 0 ? max * 1.08 : 1];
  const plain = (u, splits) => splits.map((v) => (v == null ? "" : String(+v.toFixed(2))));

  function panels(d) {
    const gust = d.t.map((_, i) => maxOf(d.wll_gust_10min_ms[i], d.wind_ms_max[i]));
    let total = 0;
    const rainSum = d.rain_mm.map((v, i) => (d.n_polls[i] ? (total += v || 0) : null));
    const expected = d.step / POLL_S;
    const polled = d.n_polls.map((v) => (v == null ? null : Math.min(100, (100 * v) / expected)));
    const synced = d.rx_ok_frac.map((v) => (v == null ? null : 100 * v));
    const whole = (u, v) => fmt(v, 0);
    return [
      {
        title: `Temperature (${U("temp").unit}): range and mean, with dew point`,
        data: [cvs("temp", d.temp_c_min), cvs("temp", d.temp_c_max), cvs("temp", d.temp_c_mean), cvs("temp", d.dew_point_c_mean)],
        series: [
          line("min", css("--temp-edge"), { width: 1 }),
          line("max", css("--temp-edge"), { width: 1 }),
          line("mean", css("--temp"), { width: 2 }),
          line("dew point", css("--dew"), { dash: [5, 4] }),
        ],
        bands: [{ series: [2, 1], fill: css("--temp-band") }],
      },
      {
        title: "Relative humidity (%)",
        data: [d.rh_mean],
        series: [line("humidity", css("--rh"), { value: whole })],
        y: { range: [0, 100] },
      },
      {
        title: `Wind (${U("wind").unit}): mean, gust and direction`,
        height: 200,
        data: [cvs("wind", d.wind_ms_mean), cvs("wind", gust), d.wind_vec_dir_deg],
        series: [
          line("mean", css("--wind"), { width: 2 }),
          line("gust", css("--gust"), { width: 1 }),
          {
            label: "direction",
            scale: "deg",
            stroke: css("--dir"),
            width: 0,
            paths: () => null,
            points: { show: true, size: 4, width: 0, fill: css("--dir") },
            value: (u, v) => (v == null ? "–" : `${compass(v)} ${v.toFixed(0)}°`),
          },
        ],
        y: { range: fromZero },
        right: {
          scale: "deg",
          range: [0, 360],
          splits: () => [0, 90, 180, 270, 360],
          values: (u, splits) => splits.map((v) => COMPASS[(v / 22.5) % 16]),
        },
      },
      {
        title: `Rain (${U("rain").unit}): per ${STEP_NAME[d.step]} and accumulated over the range`,
        data: [cvs("rain", d.rain_mm), cvs("rain", rainSum)],
        series: [
          line(`per ${STEP_NAME[d.step]}`, css("--rain"), {
            width: 0,
            fill: css("--rain"),
            paths: uPlot.paths.bars({ size: [0.8, 40] }),
            value: (u, v) => fmt(v, 2),
          }),
          line("accumulated", css("--rain-sum"), { scale: "sum", value: (u, v) => fmt(v, 2) }),
        ],
        y: { range: fromZero },
        right: { scale: "sum", range: fromZero },
      },
      {
        title: `Sea-level pressure (${U("press").unit})`,
        data: [cvs("press", d.slp_hpa_mean)],
        series: [line("pressure", css("--press"), { value: (u, v) => fmt(v, U("press").digits) })],
      },
      {
        title: "Solar radiation (W/m²): range and mean, with peak UV index",
        data: [d.solar_wm2_min, d.solar_wm2_max, d.solar_wm2_mean, d.uv_index_max],
        series: [
          line("min", css("--solar-band"), { width: 1, value: whole }),
          line("max", css("--solar-band"), { width: 1, value: whole }),
          line("mean", css("--solar"), { width: 2, value: whole }),
          line("UV index", css("--uv"), { scale: "uv" }),
        ],
        bands: [{ series: [2, 1], fill: css("--solar-band") }],
        y: { range: fromZero },
        right: { scale: "uv", range: fromZero },
      },
      {
        title: "Data quality (%): polls answered and ISS radio link synced",
        height: 120,
        data: [polled, synced],
        series: [line("polls answered", css("--ok"), { value: whole }), line("link synced", css("--accent"), { value: whole })],
        y: { range: [0, 105] },
      },
    ];
  }

  const charts = [];
  let builtFor = null;

  function build(d, key) {
    const list = panels(d);
    if (builtFor === key && charts.length === list.length) {
      list.forEach((p, i) => charts[i].setData([d.t, ...p.data]));
      return;
    }
    charts.splice(0).forEach((c) => c.destroy());
    const holder = $("#charts");
    holder.replaceChildren();
    for (const p of list) {
      const box = el("div", "panel");
      box.append(el("h2", null, p.title));
      holder.append(box);
      const scales = { x: { time: true }, y: p.y || {} };
      // Fixed axis widths on both sides keep every panel's time axis aligned
      // for the shared cursor; tick labels without thousands separators fit.
      const axes = [axis(), axis({ size: 62, values: plain })];
      if (p.right) {
        scales[p.right.scale] = { range: p.right.range };
        const { scale, splits, values } = p.right;
        axes.push(axis({ scale, side: 1, size: 46, grid: { show: false }, splits, values: values || plain }));
      } else {
        axes.push(axis({ side: 1, size: 46, grid: { show: false }, ticks: { show: false }, values: () => [] }));
      }
      const opts = {
        width: Math.max(280, box.clientWidth - 8),
        height: p.height || 170,
        cursor: { sync: { key: "wx" } },
        scales,
        axes,
        series: [{}, ...p.series],
        bands: p.bands || [],
      };
      charts.push(new uPlot(opts, [d.t, ...p.data], box));
    }
    builtFor = key;
  }

  let days = STEP[store.get("wx-days")] ? store.get("wx-days") : "1";
  let chartTimer = null;

  async function refreshCharts() {
    clearTimeout(chartTimer);
    const now = Date.now() / 1000;
    const step = STEP[days];
    try {
      const d = await getJSON(`api/series?t0=${now - days * 86400}&t1=${now}&step=${step}&offset=${tzOffset()}`);
      build(d, `${days}:${units}:${matchMedia("(prefers-color-scheme: dark)").matches}`);
      $("#range-note").textContent = `${STEP_NAME[step]} values, local time`;
    } catch (e) {
      setStatus(`Cannot load charts: ${e.message}`, true);
    }
    chartTimer = setTimeout(refreshCharts, days === "1" ? 60_000 : 600_000);
  }

  for (const b of document.querySelectorAll("button[data-days]")) {
    b.setAttribute("aria-pressed", String(b.dataset.days === days));
    b.addEventListener("click", () => {
      days = b.dataset.days;
      store.set("wx-days", days);
      for (const x of document.querySelectorAll("button[data-days]")) x.setAttribute("aria-pressed", String(x === b));
      refreshCharts();
    });
  }

  for (const b of document.querySelectorAll("button[data-units]")) {
    b.setAttribute("aria-pressed", String(b.dataset.units === units));
    b.addEventListener("click", () => {
      units = b.dataset.units;
      store.set("wx-units", units);
      for (const x of document.querySelectorAll("button[data-units]")) x.setAttribute("aria-pressed", String(x === b));
      refreshLatest();
      refreshToday();
      refreshCharts();
    });
  }

  let resizeTimer = null;
  addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      for (const c of charts) c.setSize({ width: Math.max(280, c.root.parentElement.clientWidth - 8), height: c.height });
    }, 150);
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", refreshCharts);

  refreshLatest();
  refreshToday();
  refreshCharts();
  setInterval(refreshLatest, 30_000);
  setInterval(refreshToday, 300_000);
})();
