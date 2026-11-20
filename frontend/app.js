/* Delivery route analytics SPA — vanilla JS, no dependencies.
 * Views: route (SVG map + timeline + anomalies/evidence + metrics + versions),
 *        rules (parameter catalogue, revision publishing + history),
 *        cleaning (point-level decision trail). */
"use strict";

const state = {
  routes: [], detail: null, tab: "route", selectedRoute: null,
  catalogue: null, rules: null, revisions: null, cleaning: null,
  filter: "", sort: "time",
};

const api = async (path, opts) => {
  const res = await fetch(path, opts);
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
};
const post = (path, payload) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
});

const toast = (msg, isErr) => {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  document.getElementById("toastHost").appendChild(el);
  setTimeout(() => el.remove(), 5200);
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const evidenceBlock = (obj) =>
  `<details class="ev"><summary>证据 / Evidence</summary><pre class="evidence">${esc(
    JSON.stringify(obj, null, 2))}</pre></details>`;

const DECISION_STYLE = {
  accepted: { color: "#4da3ff", label: "有效点" },
  duplicate: { color: "#8b9bb0", label: "重复点" },
  gps_jump: { color: "#ef6461", label: "跳点" },
  missing_fix: { color: "#e8a33d", label: "缺失点" },
};
const STOP_STYLE = {
  real_stop: "#a78bfa", break_stop: "#38bdf8", suspected_data_gap: "#ef6461",
};

// ============================================================== bootstrap
async function boot() {
  document.getElementById("tabs").addEventListener("click", (e) => {
    const t = e.target.closest(".tab");
    if (!t) return;
    state.tab = t.dataset.tab;
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
    render();
  });
  document.getElementById("searchBox").addEventListener("input", (e) => {
    state.filter = e.target.value.toLowerCase(); renderRouteList();
  });
  document.getElementById("sortSel").addEventListener("change", (e) => {
    state.sort = e.target.value; renderRouteList();
  });
  await refreshAll();
}

async function refreshAll() {
  const [dash, routes, catalogue, rules, revisions] = await Promise.all([
    api("/api/dashboard"), api("/api/routes"),
    api("/api/rules/catalogue"), api("/api/rules"), api("/api/revisions"),
  ]);
  state.routes = routes; state.catalogue = catalogue;
  state.rules = rules; state.revisions = revisions;
  document.getElementById("activeRule").textContent =
    `规则版本 ${dash.active_rule_version || "—"} · 修订 ${dash.rule_revision_count} 次`;
  renderStats(dash);
  renderRouteList();
  if (state.tab === "route" && state.selectedRoute) await openRoute(state.selectedRoute);
  else render();
}

function renderStats(dash) {
  const cat = dash.anomalies_by_category || {};
  const cards = [
    ["路线总数", dash.route_count], ["司机数", dash.driver_count],
    ["平均效率分", dash.avg_efficiency_score ?? "—"],
    ["数据问题", cat.DATA_ISSUE ?? 0, "red"],
    ["真实道路事件", cat.REAL_WORLD ?? 0, "green"],
  ];
  document.getElementById("statBar").innerHTML = cards.map(([k, v, tone]) =>
    `<div class="stat"><div class="k">${k}</div>
     <div class="v ${tone ? "small" : ""}" style="${tone === "red" ? "color:var(--red)" : tone === "green" ? "color:var(--green)" : ""}">${v}</div></div>`
  ).join("");
}

function scoreClass(v) {
  if (v == null) return "score-mid";
  return v >= 75 ? "score-good" : v >= 50 ? "score-mid" : "score-bad";
}

function renderRouteList() {
  let rows = state.routes.filter((r) =>
    !state.filter ||
    r.route_id.toLowerCase().includes(state.filter) ||
    r.driver_id.toLowerCase().includes(state.filter) ||
    r.order_ids.join(" ").toLowerCase().includes(state.filter));
  if (state.sort === "score") rows.sort((a, b) => (a.efficiency_score ?? -1) - (b.efficiency_score ?? -1));
  if (state.sort === "anom") rows.sort((a, b) => b.anomaly_count - a.anomaly_count);
  const host = document.getElementById("routeList");
  if (!rows.length) { host.innerHTML = `<div class="empty">暂无路线，请先通过 /api/ingest 导入数据</div>`; return; }
  host.innerHTML = rows.map((r) => `
    <div class="route-card ${r.route_id === state.selectedRoute ? "active" : ""}" data-id="${esc(r.route_id)}">
      <div class="top">
        <span class="driver">${esc(r.driver_id)} · ${esc(r.service_date)}</span>
        <span class="score-pill ${scoreClass(r.efficiency_score)}">${r.efficiency_score ?? "—"}</span>
      </div>
      <div class="meta">${esc(r.start_local)} → ${esc(r.end_local)} · ${esc(r.route_tz)} ·
        异常 ${r.anomaly_count} · <span class="point-ref">${esc(r.rule_version_id)}</span></div>
      <div class="orders">${r.order_ids.map((o) => esc(o)).join(" · ") || "无订单"}</div>
    </div>`).join("");
  host.querySelectorAll(".route-card").forEach((c) =>
    c.addEventListener("click", () => openRoute(c.dataset.id)));
}

async function openRoute(rid) {
  state.selectedRoute = rid; state.tab = "route";
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === "route"));
  try {
    state.detail = await api(`/api/routes/${encodeURIComponent(rid)}`);
  } catch (e) { toast(e.message, true); return; }
  renderRouteList(); render();
}

// ============================================================== top-level render
function render() {
  const view = document.getElementById("view");
  if (state.tab === "route") view.innerHTML = state.detail ? routeView(state.detail) :
    `<div class="empty">从左侧选择一条路线</div>`;
  if (state.tab === "rules") view.innerHTML = rulesView();
  if (state.tab === "cleaning") view.innerHTML = cleaningView(state.cleaning);
  if (state.tab === "route" && state.detail) afterRouteRender(state.detail);
  if (state.tab === "rules") afterRulesRender();
  if (state.tab === "cleaning") loadCleaning();
}

// ============================================================== route view
function routeView(d) {
  const { route, rule_set } = d;
  return `
  <div class="card">
    <div class="flex wrap" style="justify-content:space-between">
      <div>
        <h3 style="margin:0">${esc(route.driver_id)} · 服务日 ${esc(route.service_date)}
          <span class="hint">(${esc(route.route_tz)})</span></h3>
        <div class="kv" style="margin-top:5px">
          本地时间 <b>${esc(d.points[0]?.ts_local || "")}</b> → <b>${esc(d.points[d.points.length - 1]?.ts_local || "")}</b>
          · 规则版本 <b class="point-ref">${esc(rule_set.version)} / ${esc(rule_set.rule_version_id)}</b>
          · ${esc(rule_set.change_note)}</div>
      </div>
      <div class="flex">
        <select id="versionSel" class="filters" style="padding:6px 9px">
          ${d.version_history.map((v) =>
            `<option value="${esc(v.rule_version_id)}" ${v.rule_version_id === route.rule_version_id ? "selected" : ""}>
              ${esc(v.rule_version_id)} · ${esc(v.status)}</option>`).join("")}
        </select>
      </div>
    </div>
  </div>

  <div class="grid-2">
    <div class="card" style="margin-bottom:0">
      <h3>路线地图 <span class="hint">颜色=点级清洗决策；紫色圆=停留；红色圆=数据缺口；星形=订单目的地</span></h3>
      <div class="map-wrap">${mapSvg(d)}</div>
    </div>
    <div class="card" style="margin-bottom:0">
      <h3>事件时间线 <span class="hint">全部为本地时间（${esc(route.route_tz)}）</span></h3>
      <div class="timeline" id="timeline"></div>
    </div>
  </div>

  <div class="grid-2 even" style="margin-top:16px">
    <div class="card" style="margin-bottom:0">
      <h3>异常与证据 <span class="hint">区分数据问题 / 真实道路事件，可回溯到点位与规则阈值</span></h3>
      ${anomalyList(d.anomalies)}
    </div>
    <div>
      <div class="card">
        <h3>效率指标</h3>${metricsTable(d.metrics)}
      </div>
      <div class="card" style="margin-bottom:0">
        <h3>停留事件 <span class="hint">真实停留 vs 不可证实（疑似日志中断）</span></h3>
        ${stopsList(d.stops)}
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <h3>关联订单与迟到判定 <span class="hint">承诺窗口与实际到达均按订单所在时区渲染；比较基于 UTC 绝对时刻</span></h3>
    ${ordersTable(d)}
  </div>`;
}

function afterRouteRender(d) {
  const sel = document.getElementById("versionSel");
  if (sel) sel.addEventListener("change", async () => {
    state.detail = await api(`/api/routes/${encodeURIComponent(d.route.route_id)}?rule_version_id=${sel.value}`);
    render();
  });
  drawTimeline(d);
}

// ---- custom SVG map (offline, no tile dependency) ----
function mapSvg(d) {
  const W = 640, H = 460, PAD = 36;
  const positioned = d.points.filter((p) => p.lat != null);
  if (!positioned.length) return `<div class="empty">该路线没有可用定位点</div>`;
  const merc = (lat, lng) => {
    const x = lng * Math.PI / 180;
    const c = Math.max(-1.4844, Math.min(1.4844, lat * Math.PI / 180));
    return [x, Math.log(Math.tan(Math.PI / 4 + c / 2))];
  };
  const mp = positioned.map((p) => merc(p.lat, p.lng));
  const minX = Math.min(...mp.map((m) => m[0])), maxX = Math.max(...mp.map((m) => m[0]));
  const minY = Math.min(...mp.map((m) => m[1])), maxY = Math.max(...mp.map((m) => m[1]));
  const sx = (W - 2 * PAD) / Math.max(maxX - minX, 1e-9);
  const sy = (H - 2 * PAD) / Math.max(maxY - minY, 1e-9);
  const s = Math.min(sx, sy);
  const px = (lat, lng) => {
    const [mx, my] = merc(lat, lng);
    return [PAD + (mx - minX) * s, H - PAD - (my - minY) * s];
  };
  const pathPts = positioned.map((p) => px(p.lat, p.lng));
  const line = pathPts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");

  const circles = d.points.filter((p) => p.lat != null).map((p) => {
    const [x, y] = px(p.lat, p.lng);
    const st = DECISION_STYLE[p.decision] || DECISION_STYLE.accepted;
    return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${p.decision === "gps_jump" ? 5 : 3.2}"
      fill="${st.color}" fill-opacity="${p.decision === "duplicate" ? .45 : .9}"
      data-pid="${esc(p.point_id)}"><title>${esc(p.point_id)} · ${st.label} · ${esc(p.ts_local)}</title></circle>`;
  }).join("");

  const stops = d.stops.map((st) => {
    const [x, y] = px(st.lat, st.lng);
    return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="11" fill="none"
      stroke="${STOP_STYLE[st.stop_kind]}" stroke-width="2.2"><title>${esc(st.stop_kind)} ${st.duration_s}s</title></circle>`;
  }).join("");

  const orders = d.orders.map((o) => {
    const [x, y] = px(o.dest_lat, o.dest_lng);
    return `<path d="M${x},${y - 7} L${x + 6},${y + 4} L${x - 6},${y + 4} Z" fill="#38bdf8"
      stroke="#0b1018" stroke-width="1"><title>${esc(o.order_id)} 目的地</title></path>`;
  }).join("");

  const anomPts = d.anomalies.filter((a) => a.lat != null).map((a) => {
    const [x, y] = px(a.lat, a.lng);
    return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="14" fill="none"
      stroke="${a.category === "DATA_ISSUE" ? "var(--red)" : "var(--green)"}" stroke-dasharray="4 3" stroke-width="1.6"/>`;
  }).join("");

  return `<svg class="map" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
    <path d="${line}" fill="none" stroke="#34507a" stroke-width="2.2" stroke-linejoin="round"/>
    ${anomPts}${stops}${orders}${circles}
    <g class="map-legend">
      ${Object.entries(DECISION_STYLE).map(([k, v]) =>
        `<div class="row"><span class="dot" style="background:${v.color}"></span>${v.label}</div>`).join("")}
      <div class="row"><span class="dot" style="background:none;border:2px solid #a78bfa"></span>停留</div>
      <div class="row"><span class="dot" style="background:#38bdf8;clip-path:polygon(50% 0,100% 100%,0 100%)"></span>订单目的地</div>
    </g></svg>`;
}

// ---- timeline (points of interest; raw points stay in the cleaning tab) ----
function drawTimeline(d) {
  const items = [];
  for (const a of d.anomalies) {
    items.push({
      t: a.start_utc || d.route.start_utc,
      cls: a.category === "DATA_ISSUE" ? "anom-data" : "anom-real",
      title: `[${a.category === "DATA_ISSUE" ? "数据问题" : "真实事件"}] ${esc(a.anomaly_type)}`,
      sub: esc(a.message), local: a.start_local || "路线区间内",
    });
  }
  for (const s of d.stops) {
    const label = { real_stop: "真实停留", break_stop: "休息停留", suspected_data_gap: "不可证实停留（数据缺口）" }[s.stop_kind];
    items.push({
      t: s.enter_utc, cls: s.stop_kind === "suspected_data_gap" ? "gap" : "stop",
      title: `${label} · ${Math.round(s.duration_s / 60)} 分钟`,
      sub: `观测覆盖率 ${(s.observed_coverage * 100).toFixed(0)}% ·
        <span class="point-ref">${s.point_ids.length} 点</span>`,
      local: s.enter_local,
    });
  }
  const orderRows = d.metrics.find((m) => m.metric_key === "on_time_ratio");
  if (orderRows) {
    for (const o of orderRows.evidence.orders.filter((x) => x.arrival_utc)) {
      items.push({
        t: o.arrival_utc, cls: "",
        title: `订单 ${esc(o.order_id)} 到达 · ${esc(o.verdict)}`,
        sub: `偏差 ${o.deviation_min ?? "—"} 分钟 · <span class="point-ref">${esc(o.arrival_point_id)}</span>`,
        local: o.arrival_local,
      });
    }
  }
  items.sort((a, b) => String(a.t).localeCompare(String(b.t)));
  document.getElementById("timeline").innerHTML = items.map((i) =>
    `<div class="tl-item ${i.cls}">
      <div class="tl-time">${esc(i.local)}</div>
      <div class="tl-title">${i.title}</div><div class="tl-sub">${i.sub}</div></div>`).join("")
    || `<div class="empty">无显著事件</div>`;
}

function anomalyList(list) {
  if (!list.length) return `<div class="empty">无异常</div>`;
  return list.map((a) => `
    <div class="anom ${a.category === "DATA_ISSUE" ? "data" : "real"}">
      <div class="head">
        <span class="tag ${a.category === "DATA_ISSUE" ? "data" : "real"}">
          ${a.category === "DATA_ISSUE" ? "数据问题" : "真实事件"}</span>
        <span class="tag sev-${esc(a.severity)}">${esc(a.severity)}</span>
        <span class="point-ref">${esc(a.anomaly_type)}</span>
        ${a.related_order_id ? `<span class="chip">订单 ${esc(a.related_order_id)}</span>` : ""}
      </div>
      <div class="msg">${esc(a.message)}</div>
      ${evidenceBlock(a.evidence)}
    </div>`).join("");
}

function metricsTable(metrics) {
  return `<table><thead><tr><th>指标</th><th>数值</th><th>单位</th><th>回溯</th></tr></thead><tbody>
    ${metrics.map((m) => `<tr>
      <td>${esc(m.metric_key)}</td>
      <td class="num">${m.metric_value ?? "—"}</td><td>${esc(m.unit)}</td>
      <td>${evidenceBlock(m.evidence)}</td></tr>`).join("")}
    </tbody></table>`;
}

function stopsList(stops) {
  if (!stops.length) return `<div class="empty">无停留</div>`;
  return stops.map((s) => `
    <div class="anom ${s.stop_kind === "suspected_data_gap" ? "data" : "real"}">
      <div class="head">
        <span class="tag ${s.stop_kind === "suspected_data_gap" ? "data" : "real"}">
          ${{ real_stop: "真实停留", break_stop: "休息停留", suspected_data_gap: "数据缺口" }[s.stop_kind]}</span>
        <span class="muted">${esc(s.enter_local)} → ${esc(s.exit_local)}</span>
      </div>
      <div class="msg">${Math.round(s.duration_s / 60)} 分钟 · 观测覆盖率
        ${(s.observed_coverage * 100).toFixed(0)}% · ${esc(s.evidence.classification_reason || "")}</div>
      ${evidenceBlock(s.evidence)}
    </div>`).join("");
}

function ordersTable(d) {
  const m = d.metrics.find((x) => x.metric_key === "on_time_ratio");
  if (!m) return `<div class="empty">无关联订单</div>`;
  const rows = m.evidence.orders;
  return `<table><thead><tr>
    <th>订单</th><th>区域/时区</th><th>承诺窗口（本地）</th><th>实际到达（本地）</th><th>偏差(分)</th><th>判定</th><th>到达点</th>
    </tr></thead><tbody>${rows.map((o) => `
    <tr>
      <td>${esc(o.order_id)}</td><td>${esc(o.region)}<div class="muted point-ref">${esc(o.timezone)}</div></td>
      <td style="font-size:11.5px">${esc(o.promised_window_local)}</td>
      <td style="font-size:11.5px">${esc(o.arrival_local || "未进入围栏")}</td>
      <td class="num">${o.deviation_min ?? "—"}</td>
      <td><span class="tag sev-${o.verdict === "late" ? "critical" : o.verdict === "on_time" ? "info" : "warning"}">${esc(o.verdict)}</span></td>
      <td class="point-ref">${esc(o.arrival_point_id || "—")}</td>
    </tr>`).join("")}</tbody></table>`;
}

// ============================================================== rules view
function rulesView() {
  const active = state.rules.find((r) => r.status === "active") || state.rules[state.rules.length - 1];
  const cat = state.catalogue.catalogue;
  return `
  <div class="card">
    <h3>规则参数修订 <span class="hint">当前生效：${esc(active.version)} (${esc(active.rule_version_id)}) — 修改后只重算受影响路线，旧结果保留为 superseded</span></h3>
    <div class="param-grid">
      ${Object.entries(cat).map(([key, meta]) => `
        <div class="param">
          <label>${esc(key)}</label>
          <div class="desc">${esc(meta.description)}</div>
          <input data-key="${esc(key)}" type="${typeof meta.default === "boolean" ? "text" : "number" }"
            value="${esc(active.params[key] ?? meta.default)}"
            ${typeof meta.default === "boolean" ? 'placeholder="true / false"' : ""}>
          <div class="scope">scope: ${esc(meta.scope)}</div>
        </div>`).join("")}
    </div>
    <div class="flex" style="margin-top:14px">
      <input id="revNote" placeholder="修订说明（必填），如：拥堵路段停留判定半径收紧到 80m"
        style="flex:1;background:#0b1018;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:9px 12px">
      <button class="btn" id="publishBtn">发布修订并按影响范围重算</button>
      <button class="btn secondary" id="resetBtn">恢复当前值</button>
    </div>
  </div>
  <div class="card" style="margin-bottom:0">
    <h3>修订历史 <span class="hint">每次记录变更参数、影响 scope、受影响路线与实际重算路线</span></h3>
    ${revisionList()}</div>`;
}

function afterRulesRender() {
  const active = state.rules.find((r) => r.status === "active") || state.rules[state.rules.length - 1];
  document.getElementById("resetBtn").onclick = () => render();
  document.getElementById("publishBtn").onclick = async () => {
    const note = document.getElementById("revNote").value.trim();
    if (!note) return toast("修订说明必填", true);
    const patch = {};
    for (const inp of document.querySelectorAll(".param input")) {
      const key = inp.dataset.key;
      const cur = active.params[key];
      let val = inp.value.trim();
      if (typeof cur === "boolean") val = val === "true";
      else val = Number(val);
      if (val !== cur) patch[key] = val;
    }
    if (!Object.keys(patch).length) return toast("没有参数发生变化", true);
    try {
      const r = await post("/api/rules/revision", { params: patch, change_note: note });
      toast(`已发布 ${r.version}：影响 ${r.affected_routes.length} 条，重算 ${r.recomputed_routes.length} 条，跳过 ${r.skipped_routes.length} 条`);
      await refreshAll();
      state.tab = "rules"; render();
    } catch (e) { toast(e.message, true); }
  };
}

function revisionList() {
  if (!state.revisions.length)
    return `<div class="empty">尚无修订；基线版本 1.0.0 已用于当前全部结果</div>`;
  return state.revisions.map((r) => `
    <div class="rev">
      <div class="flex" style="justify-content:space-between">
        <span class="ver">${esc(r.rule_version_id)}</span>
        <span class="muted" style="font-size:11.5px">${esc(r.created_at)} · 父版本 ${esc(r.parent_version_id || "—")}</span>
      </div>
      <div class="msg" style="margin:5px 0">${esc(r.change_note)}</div>
      <div class="chips">
        ${r.changed_scopes.map((s) => `<span class="chip scope">scope: ${esc(s)}</span>`).join("")}
        ${Object.entries(r.changed_params).map(([k, v]) => `<span class="chip">${esc(k)} = ${esc(v)}</span>`).join("")}
      </div>
      <div class="kv">受影响路线 <b>${r.affected_route_ids.length}</b> ·
        实际重算 <b>${r.recomputed_route_ids.length}</b></div>
      <div class="point-ref" style="margin-top:4px">${r.affected_route_ids.map(esc).join(", ") || "（无）"}</div>
    </div>`).join("");
}

// ============================================================== cleaning view
async function loadCleaning() {
  if (state.cleaning) return;
  try { state.cleaning = await api("/api/cleaning"); }
  catch (e) { toast(e.message, true); state.cleaning = []; }
  render();
}

function cleaningView(rows) {
  if (!rows) return `<div class="empty">加载中…</div>`;
  const counts = {};
  for (const r of rows) counts[r.decision] = (counts[r.decision] || 0) + 1;
  return `
  <div class="card">
    <h3>点级清洗轨迹 <span class="hint">每个原始定位点在当前规则版本下的决策与证据（重复/跳点/缺失/有效）</span></h3>
    <div class="chips" style="margin-bottom:10px">
      ${Object.entries(counts).map(([k, v]) =>
        `<span class="chip" style="color:${DECISION_STYLE[k]?.color || "var(--text)"}">${esc(k)}: ${v}</span>`).join("")}
    </div>
    <table><thead><tr><th>点 ID</th><th>司机</th><th>UTC 时间</th><th>决策</th><th>原因</th><th>规则版本</th><th>证据</th></tr></thead>
    <tbody>${rows.slice(0, 400).map((r) => `<tr>
      <td class="point-ref">${esc(r.point_id)}</td><td>${esc(r.driver_id)}</td>
      <td class="num" style="font-size:11.5px">${esc(r.ts_utc)}</td>
      <td><span class="tag sev-${r.decision === "accepted" ? "info" : "warning"}"
        style="color:${DECISION_STYLE[r.decision]?.color}">${esc(r.decision)}</span></td>
      <td>${esc(r.reason)}</td><td class="point-ref">${esc(r.rule_version_id)}</td>
      <td>${evidenceBlock(r.evidence)}</td></tr>`).join("")}</tbody></table>
    ${rows.length > 400 ? `<div class="muted" style="margin-top:8px">仅展示前 400 条，共 ${rows.length} 条</div>` : ""}
  </div>`;
}

boot().catch((e) => toast("初始化失败: " + e.message, true));
