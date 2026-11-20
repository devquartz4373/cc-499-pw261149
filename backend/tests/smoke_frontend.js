/* Headless smoke test for app.js view functions (Node, no browser).
 * Stubs just enough DOM/fetch, loads the real app.js, feeds it the real API
 * payload shapes, and asserts every view renders without throwing. */
"use strict";
const fs = require("fs");
const path = require("path");

const payloads = {
  "/api/dashboard": { route_count: 3, driver_count: 3, avg_efficiency_score: 52.9,
    anomalies_by_category: { DATA_ISSUE: 7, REAL_WORLD: 8 },
    active_rule_version: "1.0.0", rule_revision_count: 1 },
  "/api/routes": [
    { route_id: "RT-X", rule_version_id: "rv-0001", driver_id: "D001", service_date: "2026-11-18",
      route_tz: "Asia/Shanghai", start_utc: "2026-11-18T01:00:00Z", end_utc: "2026-11-18T02:00:00Z",
      start_local: "2026-11-18 09:00 CST", end_local: "2026-11-18 10:00 CST",
      order_ids: ["O100"], status: "current", efficiency_score: 83.5, anomaly_count: 4 },
  ],
  "/api/rules/catalogue": { defaults: { stop_radius_m: 100 }, catalogue: {
    stop_radius_m: { default: 100, scope: "stop", description: "cluster radius" },
    crossday_enabled: { default: true, scope: "route_assembly", description: "cross day" } } },
  "/api/rules": [{ rule_version_id: "rv-0001", version: "1.0.0", status: "active",
    params: { stop_radius_m: 100, crossday_enabled: true }, change_note: "baseline",
    created_at: "2026-11-18T00:00:00Z", parent_version_id: null }],
  "/api/revisions": [{ revision_id: 1, rule_version_id: "rv-0002", parent_version_id: "rv-0001",
    changed_params: { stop_radius_m: 80 }, changed_scopes: ["stop"],
    affected_route_ids: ["RT-X"], recomputed_route_ids: ["RT-X"],
    change_note: "tighten", created_at: "2026-11-18T01:00:00Z" }],
};
payloads["/api/routes/RT-X"] = {
  route: { route_id: "RT-X", rule_version_id: "rv-0001", driver_id: "D001",
    service_date: "2026-11-18", route_tz: "Asia/Shanghai",
    start_utc: "2026-11-18T01:00:00Z", end_utc: "2026-11-18T02:00:00Z", status: "current" },
  rule_set: payloads["/api/rules"][0],
  points: [
    { point_id: "p1", ts_utc: "2026-11-18T01:00:00Z", ts_local: "09:00 CST", lat: 31.2, lng: 121.47,
      source: "gps", decision: "accepted", decision_reason: "first_fix", evidence: {} },
    { point_id: "p2", ts_utc: "2026-11-18T01:01:00Z", ts_local: "09:01 CST", lat: null, lng: null,
      source: "gps", decision: "missing_fix", decision_reason: "missing_gps_fix", evidence: {} },
    { point_id: "p3", ts_utc: "2026-11-18T01:02:00Z", ts_local: "09:02 CST", lat: 31.21, lng: 121.48,
      source: "gps", decision: "gps_jump", decision_reason: "implausible_speed",
      evidence: { speed_kmh: 320 } },
  ],
  orders: [{ order_id: "O100", region: "SH-CN", timezone: "Asia/Shanghai",
    dest_lat: 31.21, dest_lng: 121.48,
    promised_start_local: "09:00", promised_end_local: "10:00" }],
  stops: [{ stop_id: 1, stop_kind: "suspected_data_gap", lat: 31.2, lng: 121.47,
    enter_utc: "2026-11-18T01:05:00Z", exit_utc: "2026-11-18T01:13:00Z",
    duration_s: 480, observed_coverage: 0.25, point_ids: ["p4", "p5"],
    enter_local: "09:05 CST", exit_local: "09:13 CST",
    evidence: { classification_reason: "unobserved fraction exceeds ratio" } }],
  anomalies: [
    { category: "DATA_ISSUE", anomaly_type: "temporal_gap", severity: "warning",
      start_utc: "2026-11-18T01:05:00Z", end_utc: "2026-11-18T01:13:00Z",
      lat: null, lng: null, message: "8 min unobserved", start_local: "09:05 CST",
      evidence: { start_point_id: "p4", rule: { missing_gap_min: 10 } }, related_order_id: null },
    { category: "REAL_WORLD", anomaly_type: "arrival_late", severity: "critical",
      start_utc: "2026-11-18T01:30:00Z", end_utc: "2026-11-18T01:30:00Z",
      lat: 31.21, lng: 121.48, message: "late by 9 min", start_local: "09:30 CST",
      evidence: { arrival_point_id: "p9" }, related_order_id: "O100" },
  ],
  metrics: [
    { metric_key: "total_distance_m", metric_value: 4321.5, unit: "m", evidence: { point_ids: ["p1"] } },
    { metric_key: "on_time_ratio", metric_value: 0, unit: "ratio", evidence: { orders: [
      { order_id: "O100", region: "SH-CN", timezone: "Asia/Shanghai",
        promised_window_local: "09:00 ~ 10:00", arrival_point_id: "p9",
        arrival_utc: "2026-11-18T01:30:00Z", arrival_local: "09:30 CST",
        deviation_min: 9, verdict: "late" } ] } },
    { metric_key: "efficiency_score", metric_value: 42.0, unit: "score", evidence: {} },
  ],
  version_history: [
    { rule_version_id: "rv-0001", status: "current", created_at: "x", parent_route_id: null }],
};
payloads["/api/cleaning"] = [
  { point_id: "p1", driver_id: "D001", ts_utc: "2026-11-18T01:00:00Z",
    decision: "accepted", reason: "first_fix", rule_version_id: "rv-0001", evidence: {} },
  { point_id: "p2", driver_id: "D001", ts_utc: "2026-11-18T01:01:00Z",
    decision: "missing_fix", reason: "missing_gps_fix", rule_version_id: "rv-0001", evidence: {} },
];

// --- DOM stub ---
const elements = {};
function makeEl(id) {
  return {
    id, innerHTML: "", textContent: "", value: "", style: {}, dataset: {},
    classList: { toggle() {}, add() {}, remove() {} },
    addEventListener() {}, appendChild() {}, querySelectorAll: () => [],
    closest: () => null,
  };
}
global.document = {
  getElementById: (id) => (elements[id] ??= makeEl(id)),
  querySelectorAll: () => [], createElement: () => makeEl("dyn"),
  addEventListener() {},
};
global.fetch = async (url) => ({
  json: async () => payloads[url.split("?")[0]],
  ok: true, status: 200,
});
global.setTimeout = (fn) => fn();

const src = fs.readFileSync(path.join(__dirname, "..", "..", "frontend", "app.js"), "utf8");
const sandbox = { ...global };
const fn = new Function(...Object.keys(sandbox), `${src}\n;return { state, routeView, rulesView, cleaningView, mapSvg, drawTimeline, anomalyList, metricsTable, ordersTable, revisionList, renderStats, esc };`);
const app = fn(...Object.values(sandbox));

let failures = 0;
const check = (name, cond) => {
  if (!cond) { failures++; console.error("FAIL:", name); }
  else console.log("ok:", name);
};

(async () => {
  // populate state exactly as boot()/refreshAll() would
  app.state.routes = payloads["/api/routes"];
  app.state.catalogue = payloads["/api/rules/catalogue"];
  app.state.rules = payloads["/api/rules"];
  app.state.revisions = payloads["/api/revisions"];
  app.state.detail = payloads["/api/routes/RT-X"];

  const routeHtml = app.routeView(app.state.detail);
  check("route view renders all sections",
    ["路线地图", "事件时间线", "异常与证据", "效率指标", "停留事件", "关联订单"].every((s) => routeHtml.includes(s)));
  check("svg map emitted", routeHtml.includes("<svg") && routeHtml.includes("circle"));
  check("data issue tag rendered", routeHtml.includes("数据问题"));
  check("real world tag rendered", routeHtml.includes("真实事件"));
  check("evidence blocks rendered", (routeHtml.match(/证据 \/ Evidence/g) || []).length >= 3);
  check("point id traceability visible", routeHtml.includes("p9"));

  const map = app.mapSvg(app.state.detail);
  check("map handles null-coordinate point without NaN", !map.includes("NaN"));

  app.anomalyList; // sanity: functions exist
  check("anomaly list separates categories",
    app.anomalyList(payloads["/api/routes/RT-X"].anomalies).includes('class="anom data"') &&
    app.anomalyList(payloads["/api/routes/RT-X"].anomalies).includes('class="anom real"'));
  check("metrics table renders evidence", app.metricsTable(
    payloads["/api/routes/RT-X"].metrics).includes("total_distance_m"));
  check("orders table shows local tz", app.ordersTable(
    payloads["/api/routes/RT-X"]).includes("Asia/Shanghai"));

  const rulesHtml = app.rulesView();
  check("rules view shows params + publish",
    rulesHtml.includes("发布修订") && rulesHtml.includes("stop_radius_m") && rulesHtml.includes("scope: stop"));
  check("revision history rendered", app.revisionList().includes("tighten") &&
    app.revisionList().includes("RT-X"));

  check("cleaning view renders decisions",
    app.cleaningView(payloads["/api/cleaning"]).includes("missing_fix"));

  app.renderStats(payloads["/api/dashboard"]);
  check("stat bar populated", elements["statBar"].innerHTML.includes("路线总数"));

  // XSS escaping
  check("html escaping works", app.esc(`<script>x</script>`).includes("&lt;"));

  process.exit(failures ? 1 : 0);
})();
