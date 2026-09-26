/* 多通道传感器故障规则台 — 原生 JavaScript 前端 */
"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const PALETTE = ["#ef4444", "#8b5cf6", "#0ea5e9", "#10b981", "#f59e0b", "#ec4899", "#14b8a6", "#f97316"];
const ANN_COLOR = "#f59e0b";
const MAINT_COLOR = "#0d9488";
const SUPP_COLOR = "#b45309";

const state = {
  rules: [],
  channels: [],
  channelTtls: [],
  dataStart: null,
  dataEnd: null,
  viewStart: null,
  viewEnd: null,
  timeline: null,
  faultEvents: [],
  faultView: "faults", // "faults" | "events"
  expandedEventId: null,
  selectedFaultId: null,
  editingRuleId: null,
  editingMaintenanceId: null,
  editingTtlKey: null,
  rulePreview: null,
  eventOffset: 0,
  eventLimit: 200,
  eventTotal: 0,
  brush: null,
  ledgerSelected: new Set(),
  ledgerResult: null,
  auditDevice: "",
  auditMetric: "",
  auditResult: null,
  consDeviceA: "",
  consMetricA: "",
  consDeviceB: "",
  consMetricB: "",
  consResult: null,
  pairCauseId: null,
  pairEffectId: null,
  pairResult: null,
  recDevice: "",
  recMetric: "",
  recResult: null,
  imports: [],
};

const $ = (id) => document.getElementById(id);

/* ------------------------------ utilities ------------------------------ */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: options.body ? { "Content-Type": "application/json" } : {},
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

function toast(message, isErr = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

function pad(n) { return String(n).padStart(2, "0"); }

function fmtTime(epoch) {
  if (epoch === null || epoch === undefined) return "—";
  const d = new Date(epoch * 1000);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} UTC`;
}

function fmtClock(epoch) {
  const d = new Date(epoch * 1000);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

function fmtDur(sec) {
  sec = Math.round(sec);
  if (sec < 60) return `${sec}s`;
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? `${h}h ` : "") + (h || m ? `${m}m ` : "") + (s || !h && !m ? `${s}s` : "");
}

function fmtNum(v) {
  if (v === null || v === undefined) return "—";
  return Math.abs(v) >= 10000 ? v.toExponential(2) : Number(v.toFixed(4)).toString();
}

function toLocalInput(epoch) {
  const d = new Date(epoch * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fromLocalInput(value) {
  if (!value) return null;
  const t = new Date(value).getTime();
  return Number.isNaN(t) ? null : t / 1000;
}

function ruleColor(ruleId) {
  const idx = state.rules.findIndex((r) => r.id === ruleId);
  return PALETTE[(idx < 0 ? ruleId : idx) % PALETTE.length];
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* -------------------------------- rules -------------------------------- */

function renderRules() {
  const box = $("rule-list");
  if (!state.rules.length) {
    box.innerHTML = `<div class="empty">尚无规则，点击「新建」创建。</div>`;
    return;
  }
  box.innerHTML = state.rules.map((r) => {
    const faultCount = state.timeline ? state.timeline.faults.filter((f) => f.rule_id === r.id).length : 0;
    return `
      <div class="rule-item ${state.selectedFaultId ? "" : ""}" data-id="${r.id}">
        <div class="rname">
          <span class="swatch" style="background:${ruleColor(r.id)}"></span>
          <span>${esc(r.name)}</span>
          <span class="rule-ops">
            <button class="icon-btn act-edit" title="编辑">✎</button>
            <button class="icon-btn act-del" title="删除">✕</button>
          </span>
        </div>
        <div class="rmeta">${r.logic} · 持续 ≥ ${fmtDur(r.duration)} · ${r.conditions.length} 个条件</div>
        <div class="rule-badges">
          ${r.conditions.map((c) => `<span class="tag"${c.reset_threshold != null ? ' style="background:#f0fdfa;border-color:#99f6e4;color:#115e59"' : ""}>${esc(c.device)}/${esc(c.metric)} ${c.op} ${fmtNum(c.threshold)}${c.reset_threshold != null ? `（复位 ${fmtNum(c.reset_threshold)}）` : ""}</span>`).join("")}
          <span class="tag" style="background:#fef2f2;border-color:#fecaca;color:#991b1b">故障 ${faultCount}</span>
        </div>
      </div>`;
  }).join("");

  box.querySelectorAll(".rule-item").forEach((el) => {
    const id = Number(el.dataset.id);
    el.querySelector(".act-edit").addEventListener("click", (e) => { e.stopPropagation(); openEditor(id); });
    el.querySelector(".act-del").addEventListener("click", (e) => { e.stopPropagation(); deleteRule(id); });
    el.addEventListener("click", () => openEditor(id));
  });

  // datalists
  const devs = new Set(), mets = new Set();
  state.channels.forEach((c) => { devs.add(c.device); mets.add(c.metric); });
  state.rules.forEach((r) => r.conditions.forEach((c) => { devs.add(c.device); mets.add(c.metric); }));
  (state.timeline ? state.timeline.maintenance || [] : []).forEach((w) => devs.add(w.device));
  $("device-list").innerHTML = [...devs].map((d) => `<option value="${esc(d)}">`).join("");
  $("metric-list").innerHTML = [...mets].map((m) => `<option value="${esc(m)}">`).join("");
}

const RESET_OPS = [">", ">=", "<", "<="];

function resetHint(op) {
  if (op === ">" || op === ">=") return "复位值 < 阈值";
  if (op === "<" || op === "<=") return "复位值 > 阈值";
  return "仅 >、>=、<、<= 可设置";
}

function condRowHtml(c = {}) {
  const resetSupported = RESET_OPS.includes(c.op);
  return `
    <div class="cond-row">
      <input class="c-device" list="device-list" placeholder="设备" value="${esc(c.device || "")}" />
      <input class="c-metric" list="metric-list" placeholder="指标" value="${esc(c.metric || "")}" />
      <select class="c-op">
        ${[">", ">=", "<", "<=", "==", "!="].map((op) =>
          `<option value="${op}" ${c.op === op ? "selected" : ""}>${op}</option>`).join("")}
      </select>
      <input class="c-threshold" type="number" step="any" placeholder="触发阈值" value="${c.threshold ?? ""}" />
      <input class="c-reset" type="number" step="any" placeholder="复位阈值（可选）" title="复位阈值（可选）：${resetHint(c.op)}"
        value="${c.reset_threshold ?? ""}" ${resetSupported ? "" : "disabled"} />
      <button type="button" class="icon-btn c-del" title="删除条件">✕</button>
    </div>
    <div class="cond-reset-hint small muted">${resetHint(c.op)}</div>`;
}

function syncCondRowReset(row) {
  const op = row.querySelector(".c-op").value;
  const supported = RESET_OPS.includes(op);
  const resetInput = row.querySelector(".c-reset");
  resetInput.disabled = !supported;
  if (!supported) resetInput.value = "";
  const hint = row.nextElementSibling;
  if (hint && hint.classList.contains("cond-reset-hint")) hint.textContent = resetHint(op);
}

function openEditor(ruleId = null) {
  state.editingRuleId = ruleId;
  const rule = ruleId ? state.rules.find((r) => r.id === ruleId) : null;
  $("editor-title").textContent = rule ? "编辑规则" : "新建规则";
  $("rule-name").value = rule ? rule.name : "";
  $("rule-logic").value = rule ? rule.logic : "AND";
  $("rule-duration").value = rule ? rule.duration : 0;
  const rows = $("cond-rows");
  rows.innerHTML = "";
  (rule ? rule.conditions : [{}]).forEach((c) => rows.insertAdjacentHTML("beforeend", condRowHtml(c)));
  bindCondRows();
  $("rule-editor").hidden = false;
  clearRulePreview();
}

function bindCondRow(row) {
  syncCondRowReset(row);
  row.querySelector(".c-op").addEventListener("change", () => {
    syncCondRowReset(row);
    markPreviewStale();
  });
  row.querySelector(".c-del").addEventListener("click", () => {
    if ($("cond-rows").querySelectorAll(".cond-row").length > 1) {
      const hint = row.nextElementSibling;
      if (hint && hint.classList.contains("cond-reset-hint")) hint.remove();
      row.remove();
      markPreviewStale();
    }
    else toast("至少需要一个条件", true);
  });
}

function bindCondRows() {
  $("cond-rows").querySelectorAll(".cond-row").forEach(bindCondRow);
}

function closeEditor() {
  state.editingRuleId = null;
  $("rule-editor").hidden = true;
  clearRulePreview();
}

function collectRule() {
  const conditions = [...$("cond-rows").querySelectorAll(".cond-row")].map((row) => {
    const op = row.querySelector(".c-op").value;
    const resetRaw = row.querySelector(".c-reset").value.trim();
    return {
      device: row.querySelector(".c-device").value.trim(),
      metric: row.querySelector(".c-metric").value.trim(),
      op,
      threshold: Number(row.querySelector(".c-threshold").value),
      reset_threshold: resetRaw === "" ? null : Number(resetRaw),
    };
  });
  const bad = conditions.find((c) => !c.device || !c.metric || Number.isNaN(c.threshold));
  if (bad) throw new Error("每个条件都需要设备、指标和数值阈值");
  const badReset = conditions.find((c) => c.reset_threshold !== null &&
    (!Number.isFinite(c.reset_threshold) || !RESET_OPS.includes(c.op)));
  if (badReset) throw new Error("复位阈值仅支持 >、>=、<、<= 条件，且必须为有限数值");
  const badSide = conditions.find((c) => c.reset_threshold !== null &&
    ((c.op === ">" || c.op === ">=") ? c.reset_threshold >= c.threshold : c.reset_threshold <= c.threshold));
  if (badSide) {
    throw new Error(badSide.op === ">" || badSide.op === ">="
      ? "高值触发（>、>=）的复位阈值必须低于触发阈值"
      : "低值触发（<、<=）的复位阈值必须高于触发阈值");
  }
  return {
    name: $("rule-name").value.trim(),
    logic: $("rule-logic").value,
    duration: Number($("rule-duration").value) || 0,
    conditions,
  };
}

async function saveRule() {
  let payload;
  try {
    payload = collectRule();
  } catch (e) { toast(e.message, true); return; }
  try {
    if (state.editingRuleId) await api(`/api/rules/${state.editingRuleId}`, { method: "PUT", body: JSON.stringify(payload) });
    else await api("/api/rules", { method: "POST", body: JSON.stringify(payload) });
    closeEditor();
    await refreshAfterRuleChange();
    toast("规则已保存");
  } catch (e) { toast(e.message, true); }
}

/* ----------------------- unsaved-draft impact preview ----------------------- */

function clearRulePreview() {
  state.rulePreview = null;
  $("rule-preview").hidden = true;
  $("rule-preview").innerHTML = "";
}

function markPreviewStale() {
  if (!state.rulePreview) return;
  state.rulePreview.stale = true;
  const banner = $("rule-preview").querySelector(".preview-stale");
  if (banner) banner.hidden = false;
}

async function previewRule() {
  let payload;
  try {
    payload = collectRule();
  } catch (e) { toast(e.message, true); return; }
  const btn = $("btn-preview-rule");
  btn.disabled = true;
  const oldText = btn.textContent;
  btn.textContent = "评估中…";
  try {
    const body = { ...payload, rule_id: state.editingRuleId || null };
    const result = await api("/api/rules/preview", { method: "POST", body: JSON.stringify(body) });
    state.rulePreview = { result, stale: false };
    renderRulePreview();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

const PREVIEW_KIND_META = {
  added: { label: "新增片段", cls: "pv-added", note: "草稿生效后出现的已确认故障时间" },
  removed: { label: "消失片段", cls: "pv-removed", note: "草稿生效后不再成立的已确认故障时间" },
  overlap_changed: {
    label: "确认时刻变化", cls: "pv-changed",
    note: "区间仍被两侧故障覆盖，但确认时刻发生了变化",
  },
  overlap_unchanged: { label: "无变化片段", cls: "pv-same", note: "区间与确认时刻均一致" },
};

function previewSegmentHtml(seg) {
  const meta = PREVIEW_KIND_META[seg.kind] || { label: seg.kind, cls: "" };
  let confirmLine;
  if (seg.kind === "overlap_changed") {
    confirmLine = `<span class="pv-confirm">原确认 ${fmtClock(seg.baseline_confirmed_at)} → 草稿确认 ${fmtClock(seg.draft_confirmed_at)}</span>`;
  } else if (seg.kind === "overlap_unchanged") {
    confirmLine = `<span class="pv-confirm">确认时刻 ${fmtClock(seg.baseline_confirmed_at)}</span>`;
  } else if (seg.kind === "added") {
    confirmLine = `<span class="pv-confirm">草稿确认 ${fmtClock(seg.draft_confirmed_at)}</span>`;
  } else {
    confirmLine = `<span class="pv-confirm">原确认 ${fmtClock(seg.baseline_confirmed_at)}</span>`;
  }
  return `
    <div class="pv-seg ${meta.cls}">
      <span class="pv-kind">${meta.label}</span>
      <span class="fault-time">${fmtTime(seg.start)} → ${fmtTime(seg.end)}</span>
      <span class="tag">${fmtDur(seg.end - seg.start)}</span>
      ${confirmLine}
    </div>`;
}

function renderRulePreview() {
  const box = $("rule-preview");
  const { result, stale } = state.rulePreview;
  const c = result.diff.counts;
  const summary =
    `原规则 ${result.baseline.faults.length} 个故障 → 草稿 ${result.draft.faults.length} 个故障` +
    `（当前 ${result.maintenance_windows} 个维护窗口、${result.channel_ttls || 0} 个采样有效期参与评估；不落库，保存仍走原流程）`;
  const sections = [
    ["added", c.added],
    ["removed", c.removed],
    ["overlap_changed", c.overlap_changed],
  ];
  const sectionsHtml = sections.map(([kind, n]) => {
    if (!n) return "";
    const meta = PREVIEW_KIND_META[kind];
    const segs = result.diff.segments.filter((s) => s.kind === kind);
    return `
      <div class="pv-group">
        <div class="pv-group-head ${meta.cls}">${meta.label} <span class="tab-count">${n}</span><span class="muted small"> · ${meta.note}</span></div>
        ${segs.map(previewSegmentHtml).join("")}
      </div>`;
  }).join("");

  const unchangedOpen = result.diff.changed ? "" : "open";
  const unchangedBody = c.overlap_unchanged
    ? `<details class="pv-group pv-same-group" ${unchangedOpen}>
         <summary class="pv-group-head pv-same">无变化片段 <span class="tab-count">${c.overlap_unchanged}</span><span class="muted small"> · 区间与确认时刻均一致（点击展开）</span></summary>
         ${result.diff.segments.filter((s) => s.kind === "overlap_unchanged").map(previewSegmentHtml).join("")}
       </details>`
    : "";

  const noChangeHtml = result.diff.changed ? "" : `
    <div class="pv-nochange">✓ 草稿与原规则的已确认故障完全一致：无新增、无消失、确认时刻无变化。</div>`;

  box.innerHTML = `
    <div class="pv-title">预览影响（只读）</div>
    <div class="pv-summary">${esc(summary)}</div>
    <div class="preview-stale pv-stale" ${stale ? "" : "hidden"}>草稿在上次预览后已修改，结果可能已过期，请重新预览。</div>
    ${sectionsHtml}
    ${unchangedBody}
    ${noChangeHtml}`;
  box.hidden = false;
}

async function deleteRule(id) {
  if (!confirm("删除该规则及其全部故障区间？")) return;
  await api(`/api/rules/${id}`, { method: "DELETE" });
  await refreshAfterRuleChange();
}

async function evaluateRules() {
  try {
    const r = await api("/api/evaluate", { method: "POST" });
    await loadTimeline();
    await loadFaultEvents();
    renderFaults();
    renderFaultEvents();
    toast(`评估完成：${r.rules_evaluated} 条规则，${r.fault_intervals} 个故障区间` +
      (r.suppressed_segments ? `，${r.suppressed_segments} 段被维护抑制` : ""));
  } catch (e) { toast(e.message, true); }
}

/* ---------------------------- planned maintenance ---------------------------- */

function renderMaintenance() {
  const list = state.timeline ? state.timeline.maintenance || [] : [];
  const box = $("maint-list");
  if (!list.length) {
    box.innerHTML = `<div class="empty">尚无维护窗口。点击「新建」或在时间轴上拖拽框选创建。</div>`;
    return;
  }
  box.innerHTML = list.map((w) => `
    <div class="maint-item" data-id="${w.id}">
      <div class="rname">
        <span class="swatch" style="background:${MAINT_COLOR}"></span>
        <span>${esc(w.name)}</span>
        <span class="rule-ops">
          <button class="icon-btn act-edit" title="编辑">✎</button>
          <button class="icon-btn act-del" title="删除">✕</button>
        </span>
      </div>
      <div class="rmeta">${esc(w.device)} · ${fmtTime(w.start)} → ${fmtTime(w.end)}（${fmtDur(w.end - w.start)}）</div>
      ${w.note ? `<div class="rmeta">${esc(w.note)}</div>` : ""}
    </div>`).join("");

  box.querySelectorAll(".maint-item").forEach((el) => {
    const id = Number(el.dataset.id);
    el.querySelector(".act-edit").addEventListener("click", (e) => { e.stopPropagation(); openMaintEditor(id); });
    el.querySelector(".act-del").addEventListener("click", (e) => { e.stopPropagation(); deleteMaintenance(id); });
    el.addEventListener("click", () => openMaintEditor(id));
  });
}

function openMaintEditor(windowId = null, preset = null) {
  const list = state.timeline ? state.timeline.maintenance || [] : [];
  const w = windowId ? list.find((x) => x.id === windowId) : null;
  state.editingMaintenanceId = w ? w.id : null;
  $("maint-editor-title").textContent = w ? "编辑维护窗口" : "新建维护窗口";
  $("maint-device").value = w ? w.device : (preset && preset.device) || "";
  $("maint-name").value = w ? w.name : "";
  $("maint-start").value = w ? toLocalInput(w.start) : preset ? toLocalInput(preset.start) : "";
  $("maint-end").value = w ? toLocalInput(w.end) : preset ? toLocalInput(preset.end) : "";
  $("maint-note").value = w && w.note ? w.note : "";
  $("maint-editor").hidden = false;
}

function closeMaintEditor() {
  state.editingMaintenanceId = null;
  $("maint-editor").hidden = true;
}

async function saveMaintenance() {
  const start = fromLocalInput($("maint-start").value);
  const end = fromLocalInput($("maint-end").value);
  const payload = {
    device: $("maint-device").value.trim(),
    name: $("maint-name").value.trim(),
    start, end,
    note: $("maint-note").value.trim() || null,
  };
  if (!payload.device || !payload.name) { toast("请填写设备与名称", true); return; }
  if (start === null || end === null || end <= start) { toast("请填写有效的开始/结束时间（半开区间）", true); return; }
  try {
    // 服务端在写入后自动重新评估全部规则
    if (state.editingMaintenanceId) {
      await api(`/api/maintenance/${state.editingMaintenanceId}`, { method: "PUT", body: JSON.stringify(payload) });
    } else {
      await api("/api/maintenance", { method: "POST", body: JSON.stringify(payload) });
    }
    closeMaintEditor();
    await reloadAll();
    toast("维护窗口已保存，规则已重新评估");
  } catch (e) { toast(e.message, true); }
}

async function deleteMaintenance(id) {
  if (!confirm("删除该维护窗口并重新评估全部规则？")) return;
  try {
    await api(`/api/maintenance/${id}`, { method: "DELETE" });
    await reloadAll();
    toast("维护窗口已删除，规则已重新评估");
  } catch (e) { toast(e.message, true); }
}

/* ----------------------- per-channel sample validity (TTL) ----------------------- */

const TTL_COLOR = "#7c3aed";

function ttlMap() {
  const m = new Map();
  state.channelTtls.forEach((t) => m.set(t.device + "|" + t.metric, t.ttl_seconds));
  return m;
}

function renderTtls() {
  const box = $("ttl-list");
  const ttls = state.channelTtls || [];
  if (!ttls.length) {
    box.innerHTML = `<div class="empty">未配置有效期：所有通道沿用「样本值保持到下一样本（末样本保持到数据终点）」语义。</div>`;
    return;
  }
  box.innerHTML = ttls.map((t) => `
    <div class="maint-item" data-device="${esc(t.device)}" data-metric="${esc(t.metric)}">
      <div class="rname">
        <span class="swatch" style="background:${TTL_COLOR}"></span>
        <span>${esc(t.device)}/${esc(t.metric)}</span>
        <span class="rule-ops">
          <button class="icon-btn act-edit" title="编辑">✎</button>
          <button class="icon-btn act-del" title="清除有效期">✕</button>
        </span>
      </div>
      <div class="rmeta">样本有效期 <b>${fmtDur(t.ttl_seconds)}</b>（${Number(t.ttl_seconds)} 秒）· 到期空档不计条件、重置持续时长</div>
    </div>`).join("");

  box.querySelectorAll(".maint-item").forEach((el) => {
    const device = el.dataset.device, metric = el.dataset.metric;
    el.querySelector(".act-edit").addEventListener("click", (e) => { e.stopPropagation(); openTtlEditor(device, metric); });
    el.querySelector(".act-del").addEventListener("click", (e) => { e.stopPropagation(); clearTtl(device, metric); });
    el.addEventListener("click", () => openTtlEditor(device, metric));
  });
}

function openTtlEditor(device = null, metric = null) {
  const ttl = device !== null ? (state.channelTtls || []).find((t) => t.device === device && t.metric === metric) : null;
  state.editingTtlKey = ttl ? { device: ttl.device, metric: ttl.metric } : null;
  $("ttl-editor-title").textContent = ttl ? "编辑采样有效期" : "设置采样有效期";
  $("ttl-device").value = ttl ? ttl.device : device || "";
  $("ttl-metric").value = ttl ? ttl.metric : metric || "";
  $("ttl-seconds").value = ttl ? ttl.ttl_seconds : "";
  $("btn-clear-ttl").hidden = !ttl;
  $("ttl-editor").hidden = false;
  $("ttl-seconds").focus();
}

function closeTtlEditor() {
  state.editingTtlKey = null;
  $("ttl-editor").hidden = true;
}

function collectTtl() {
  const device = $("ttl-device").value.trim();
  const metric = $("ttl-metric").value.trim();
  const seconds = Number($("ttl-seconds").value);
  if (!device || !metric) throw new Error("请填写设备与指标");
  if (!Number.isFinite(seconds) || seconds <= 0) throw new Error("有效期必须为正秒数");
  return { device, metric, ttl_seconds: seconds };
}

async function saveTtl() {
  let payload;
  try {
    payload = collectTtl();
  } catch (e) { toast(e.message, true); return; }
  try {
    await api("/api/channel-ttls", { method: "PUT", body: JSON.stringify(payload) });
    closeTtlEditor();
    await reloadAll();
    toast("采样有效期已保存，规则已重新评估");
  } catch (e) { toast(e.message, true); }
}

async function clearTtl(device, metric) {
  const label = `${device}/${metric}`;
  if (!confirm(`清除通道 ${label} 的采样有效期？清除后沿用「保持到下一样本」语义，并重新评估全部规则。`)) return;
  try {
    const params = new URLSearchParams({ device, metric });
    await api(`/api/channel-ttls?${params}`, { method: "DELETE" });
    if (state.editingTtlKey && state.editingTtlKey.device === device && state.editingTtlKey.metric === metric) {
      closeTtlEditor();
    }
    await reloadAll();
    toast(`已清除 ${label} 的有效期，规则已重新评估`);
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------ timeline ------------------------------ */

const TL = { laneH: 64, laneGap: 14, top: 26, bottom: 26, left: 150, right: 26, annoH: 20, maintH: 16, faultBands: 3 };

function xScale(epoch) {
  const width = state.svgWidth;
  const span = Math.max(state.viewEnd - state.viewStart, 1e-9);
  return TL.left + ((epoch - state.viewStart) / span) * (width - TL.left - TL.right);
}

function xUnscale(x) {
  const width = state.svgWidth;
  const span = Math.max(state.viewEnd - state.viewStart, 1e-9);
  return state.viewStart + ((x - TL.left) / (width - TL.left - TL.right)) * span;
}

function yValue(v, laneY, ch) {
  let lo = ch.min_value, hi = ch.max_value;
  if (lo === hi) { lo -= 1; hi += 1; }
  const padv = (hi - lo) * 0.08;
  lo -= padv; hi += padv;
  const h = TL.laneH - 16;
  return laneY + 6 + (1 - (v - lo) / (hi - lo)) * h;
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  children.forEach((c) => node.appendChild(c));
  return node;
}

function renderTimeline() {
  const svg = $("timeline");
  const wrap = $("timeline-wrap");
  svg.innerHTML = "";
  const data = state.timeline;
  if (!data || !data.channels.length) {
    svg.setAttribute("height", 80);
    svg.appendChild(el("text", { x: 20, y: 44, class: "lane-label", text: "当前范围内没有可绘制的通道数据（先导入 JSONL）" }));
    return;
  }

  state.svgWidth = wrap.clientWidth || 800;
  const channels = data.channels;
  const maintenance = data.maintenance || [];
  const suppressed = data.suppressed || [];
  const maintLanes = stackLanes(maintenance);
  const maintRows = maintenance.length ? Math.max(...maintLanes.values()) + 1 : 0;
  const maintBand = maintenance.length ? maintRows * (TL.maintH + 2) + 6 : 0;
  const annoBand = data.annotations.length ? TL.annoH + 8 : 0;
  const totalH = TL.top + maintBand + annoBand + channels.length * (TL.laneH + TL.laneGap) + TL.bottom;
  svg.setAttribute("width", state.svgWidth);
  svg.setAttribute("height", totalH);

  // hatch patterns for maintenance bands and suppressed segments
  const defs = el("defs");
  const mkPattern = (id, color) => {
    const p = el("pattern", { id, width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
    p.appendChild(el("rect", { width: 6, height: 6, fill: color, "fill-opacity": 0.08 }));
    p.appendChild(el("line", { x1: 0, y1: 0, x2: 0, y2: 6, stroke: color, "stroke-width": 1.6, "stroke-opacity": 0.5 }));
    return p;
  };
  defs.appendChild(mkPattern("maintHatch", MAINT_COLOR));
  defs.appendChild(mkPattern("suppHatch", SUPP_COLOR));
  svg.appendChild(defs);

  // time axis ticks
  const axisY = totalH - TL.bottom + 4;
  const span = state.viewEnd - state.viewStart;
  const ticks = chooseTicks(span);
  const axis = el("g", { class: "axis" });
  for (let t = Math.ceil(state.viewStart / ticks.step) * ticks.step; t <= state.viewEnd; t += ticks.step) {
    const x = xScale(t);
    axis.appendChild(el("line", { x1: x, y1: TL.top, x2: x, y2: axisY - 2, "stroke-dasharray": "2 4", stroke: "#e2e8f0" }));
    axis.appendChild(el("text", { x, y: axisY + 14, "text-anchor": "middle", text: ticks.fmt(t) }));
  }
  svg.appendChild(axis);

  const laneTop0 = TL.top + maintBand + annoBand;

  // maintenance band (planned maintenance windows, stacked like annotations)
  if (maintenance.length) {
    maintenance.forEach((w) => {
      const my = TL.top + maintLanes.get(w.id) * (TL.maintH + 2);
      const x1 = xScale(Math.max(w.start, state.viewStart));
      const x2 = xScale(Math.min(w.end, state.viewEnd));
      if (x2 <= x1) return;
      const g = el("g", { class: "maint-band" });
      g.appendChild(el("rect", { x: x1, y: my, width: x2 - x1, height: TL.maintH, class: "maint-rect" }));
      g.appendChild(el("line", { x1, y1: my, x2: x1, y2: my + TL.maintH, class: "maint-edge" }));
      g.appendChild(el("line", { x1: x2, y1: my, x2: x2, y2: my + TL.maintH, class: "maint-edge" }));
      g.appendChild(el("text", {
        x: Math.min(x1 + 4, state.svgWidth - TL.right - 4), y: my + 12,
        class: "maint-label-text", text: `🛠 ${w.device} · ${w.name}`.slice(0, 36),
      }));
      attachTip(g, () =>
        `维护窗口：${w.name}\n设备 ${w.device}\n${fmtTime(w.start)} → ${fmtTime(w.end)}（${fmtDur(w.end - w.start)}）` +
        `${w.note ? "\n" + w.note : ""}\n半开区间 [start, end)，评估时从该设备真值段中扣除`);
      svg.appendChild(g);
    });
  }

  // annotation band
  if (data.annotations.length) {
    const ay = TL.top + maintBand;
    data.annotations.forEach((a) => {
      const x1 = xScale(Math.max(a.start, state.viewStart));
      const x2 = xScale(Math.min(a.end, state.viewEnd));
      if (x2 <= x1) return;
      const g = el("g", { class: "anno-band" });
      g.appendChild(el("rect", { x: x1, y: ay, width: x2 - x1, height: TL.annoH, class: "anno-rect" }));
      g.appendChild(el("line", { x1, y1: ay, x2: x1, y2: ay + TL.annoH, class: "anno-edge" }));
      g.appendChild(el("line", { x1: x2, y1: ay, x2: x2, y2: ay + TL.annoH, class: "anno-edge" }));
      const txt = el("text", { x: Math.min(x1 + 4, state.svgWidth - TL.right - 4), y: ay + 13, class: "anno-label-text", text: esc(a.label).slice(0, 30) });
      g.appendChild(txt);
      attachTip(g, () => `标注：${a.label}\n${fmtTime(a.start)} → ${fmtTime(a.end)}${a.note ? "\n" + a.note : ""}`);
      svg.appendChild(g);
    });
  }

  // threshold index: (device|metric) -> conditions across rules
  const thresholds = new Map();
  state.rules.forEach((r) => r.conditions.forEach((c) => {
    const key = c.device + "|" + c.metric;
    if (!thresholds.has(key)) thresholds.set(key, []);
    thresholds.get(key).push({ rule: r, cond: c });
  }));

  channels.forEach((ch, ci) => {
    const laneY = laneTop0 + ci * (TL.laneH + TL.laneGap);
    const g = el("g", { class: "lane" });

    g.appendChild(el("rect", { x: TL.left, y: laneY, width: state.svgWidth - TL.left - TL.right, height: TL.laneH, class: "lane-bg", rx: 4 }));

    // maintenance shading: every window on this channel's device
    maintenance.filter((w) => w.device === ch.device).forEach((w) => {
      const x1 = xScale(Math.max(w.start, state.viewStart));
      const x2 = xScale(Math.min(w.end, state.viewEnd));
      if (x2 <= x1) return;
      g.appendChild(el("rect", { x: x1, y: laneY, width: x2 - x1, height: TL.laneH, class: "maint-shade" }));
    });

    g.appendChild(el("rect", { x: TL.left, y: laneY, width: state.svgWidth - TL.left - TL.right, height: TL.laneH, class: "lane-frame", rx: 4 }));
    const label = el("text", { x: TL.left - 8, y: laneY + TL.laneH / 2 + 4, "text-anchor": "end", class: "lane-label" });
    label.textContent = `${ch.device}/${ch.metric}`;
    g.appendChild(label);
    const range = el("text", { x: TL.left - 8, y: laneY + TL.laneH / 2 + 17, "text-anchor": "end", class: "lane-label", "font-size": 9, fill: "#94a3b8" });
    range.textContent = `${fmtNum(ch.min_value)}~${fmtNum(ch.max_value)}`;
    g.appendChild(range);

    // per-channel sample validity badge (TTL): value held only ttl seconds
    const ttlSec = ttlMap().get(ch.device + "|" + ch.metric);
    if (ttlSec !== undefined) {
      const bx = TL.left + 6, by = laneY + 4;
      const bw = 74, bh = 13;
      const badge = el("g", { class: "ttl-badge" });
      badge.appendChild(el("rect", { x: bx, y: by, width: bw, height: bh, rx: 6, fill: TTL_COLOR, "fill-opacity": 0.14, stroke: TTL_COLOR, "stroke-opacity": 0.45, "stroke-width": 0.8 }));
      badge.appendChild(el("text", { x: bx + bw / 2, y: by + 9.5, "text-anchor": "middle", "font-size": 9, fill: TTL_COLOR, text: `⏳ 有效期 ${fmtDur(ttlSec)}` }));
      attachTip(badge, () =>
        `采样有效期：${ch.device}/${ch.metric} 每个样本值仅保持 ${fmtDur(ttlSec)}（${Number(ttlSec)} 秒）\n` +
        `持有区间 [采样时刻, min(下一样本时刻, 采样时刻+有效期, 数据终点))；\n到期空档不满足该通道条件并重置持续时长，故障与证据不跨越空档。`);
      g.appendChild(badge);
    }

    // threshold lines
    const thItems = thresholds.get(ch.device + "|" + ch.metric) || [];
    thItems.forEach(({ rule, cond }) => {
      const y = yValue(cond.threshold, laneY, ch);
      if (y < laneY + 4 || y > laneY + TL.laneH - 10) return;
      const x2 = state.svgWidth - TL.right;
      g.appendChild(el("line", { x1: TL.left, y1: y, x2, y2: y, class: "threshold" }));
      g.appendChild(el("text", { x: x2 - 2, y: y - 2, "text-anchor": "end", class: "threshold-label", text: `${cond.op} ${fmtNum(cond.threshold)}` }));
      // optional latching reset boundary (dashed, same color as the rule)
      if (cond.reset_threshold != null) {
        const yr = yValue(cond.reset_threshold, laneY, ch);
        if (yr >= laneY + 4 && yr <= laneY + TL.laneH - 10) {
          const color = ruleColor(rule.id);
          g.appendChild(el("line", {
            x1: TL.left, y1: yr, x2, y2: yr, class: "reset-threshold",
            stroke: color, "stroke-dasharray": "4 3", "stroke-opacity": 0.75,
          }));
          g.appendChild(el("text", {
            x: x2 - 2, y: yr - 2, "text-anchor": "end", class: "threshold-label",
            fill: color, text: `复位 ${fmtNum(cond.reset_threshold)}`,
          }));
        }
      }
    });

    // step signal
    const pts = ch.values;
    if (pts.length) {
      let d = "";
      pts.forEach((p, i) => {
        const x = xScale(p.ts), y = yValue(p.v, laneY, ch);
        if (i === 0) d += `M ${x} ${y}`;
        else {
          d += ` L ${x} ${yValue(pts[i - 1].v, laneY, ch)} L ${x} ${y}`;
        }
      });
      g.appendChild(el("path", { d, class: "signal" }));
    }

    // fault rectangles (stack overlapping into bands)
    const faults = data.faults.filter((f) =>
      f.evidence.some((ev) => ev.condition.device === ch.device && ev.condition.metric === ch.metric));
    const lanes = stackLanes(faults);
    const bandH = 8;
    const maxBands = TL.faultBands;
    faults.forEach((f) => {
      const band = Math.min(lanes.get(f.id), maxBands - 1);
      const by = laneY + TL.laneH - 4 - (band + 1) * (bandH + 1);
      const x1 = xScale(Math.max(f.start, state.viewStart));
      const x2 = xScale(Math.min(f.end, state.viewEnd));
      if (x2 <= x1) return;
      const color = ruleColor(f.rule_id);
      const rect = el("rect", {
        x: x1, y: by, width: x2 - x1, height: bandH, rx: 2,
        class: "fault-rect" + (state.selectedFaultId === f.id ? " selected" : ""),
        fill: color, "fill-opacity": 0.55,
      });
      rect.addEventListener("click", (e) => { e.stopPropagation(); selectFault(f.id); });
      attachTip(rect, () => faultTooltip(f));
      g.appendChild(rect);

      // confirmation marker
      if (f.confirmed_at >= state.viewStart && f.confirmed_at <= state.viewEnd) {
        const cx = xScale(f.confirmed_at);
        g.appendChild(el("path", {
          d: `M ${cx - 3} ${by - 1} L ${cx + 3} ${by - 1} L ${cx} ${by - 5} Z`,
          fill: "#0f172a",
        }));
      }
    });

    // suppressed segments: rule truth on this channel that maintenance cut out
    suppressed.filter((s) => s.device === ch.device && s.metric === ch.metric).forEach((s) => {
      const x1 = xScale(Math.max(s.start, state.viewStart));
      const x2 = xScale(Math.min(s.end, state.viewEnd));
      if (x2 <= x1) return;
      const rect = el("rect", { x: x1, y: laneY + 4, width: x2 - x1, height: 8, rx: 2, class: "supp-rect" });
      attachTip(rect, () =>
        `被维护抑制：${s.rule_name}\n${s.device}/${s.metric}\n${fmtTime(s.start)} → ${fmtTime(s.end)}（${fmtDur(s.end - s.start)}）\n` +
        `关联维护窗口：${(s.maintenance_windows || []).map((w) => w.name || `#${w.id}`).join("、") || "—"}`);
      g.appendChild(rect);
    });

    // evidence dots for selected fault
    if (state.selectedFaultId) {
      const f = data.faults.find((x) => x.id === state.selectedFaultId);
      if (f) {
        const evGroup = f.evidence.find((ev) => ev.condition.device === ch.device && ev.condition.metric === ch.metric);
        if (evGroup) {
          evGroup.events.forEach((p) => {
            if (p.ts < state.viewStart || p.ts > state.viewEnd) return;
            g.appendChild(el("circle", { cx: xScale(p.ts), cy: yValue(p.value, laneY, ch), r: 3.2, fill: "#f97316", stroke: "#fff", "stroke-width": 1 }));
          });
        }
      }
    }

    svg.appendChild(g);
  });

  // brush layer (transparent, on top but below nothing interactive except rects)
  const brushLayer = el("rect", {
    x: TL.left, y: TL.top, width: state.svgWidth - TL.left - TL.right,
    height: totalH - TL.top - TL.bottom, fill: "transparent", class: "brush-layer",
  });
  svg.appendChild(brushLayer);

  // crosshair
  const crossV = el("line", { class: "crosshair", y1: TL.top, y2: totalH - TL.bottom, visibility: "hidden" });
  const crossLabel = el("text", { class: "crosshair-label", visibility: "hidden" });
  svg.appendChild(crossV);
  svg.appendChild(crossLabel);

  svg.addEventListener("mousemove", (e) => onSvgMove(e, crossV, crossLabel));
  svg.addEventListener("mouseleave", () => { crossV.setAttribute("visibility", "hidden"); crossLabel.setAttribute("visibility", "hidden"); hideTip(); });

  let dragging = false, brushRect = null;
  const toSvgX = (clientX) => {
    const rect = svg.getBoundingClientRect();
    return Math.min(Math.max(clientX - rect.left, TL.left), state.svgWidth - TL.right);
  };
  brushLayer.addEventListener("mousedown", (e) => {
    dragging = true;
    const x0 = toSvgX(e.clientX);
    brushRect = el("rect", { class: "brush-rect", y: TL.top, height: totalH - TL.top - TL.bottom });
    svg.appendChild(brushRect);
    state._brushStart = x0;
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragging || !brushRect) return;
    const x = toSvgX(e.clientX);
    const start = state._brushStart;
    brushRect.setAttribute("x", Math.min(start, x));
    brushRect.setAttribute("width", Math.abs(x - start));
  });
  window.addEventListener("mouseup", (e) => {
    if (!dragging) return;
    dragging = false;
    if (!brushRect) return;
    const x = toSvgX(e.clientX);
    const start = state._brushStart;
    const x0 = Math.min(start, x), w = Math.abs(x - start);
    brushRect.remove(); brushRect = null;
    if (w < 6) return; // treated as a click
    const t0 = xUnscale(x0), t1 = xUnscale(x0 + w);
    openBrushPop(t0, t1, x0);
  });

  renderLegend();
}

function stackLanes(faults) {
  // assign each fault a band index so overlapping rects don't overlap visually
  const sorted = [...faults].sort((a, b) => a.start - b.start || a.end - b.end);
  const assigned = new Map();
  const ends = [];
  for (const f of sorted) {
    let lane = ends.findIndex((end) => end <= f.start + 1e-6);
    if (lane === -1) { lane = ends.length; ends.push(f.end); }
    else ends[lane] = f.end;
    assigned.set(f.id, lane);
  }
  return assigned;
}

function chooseTicks(span) {
  const targets = [1, 5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400];
  const want = Math.max(4, Math.floor((state.svgWidth - TL.left - TL.right) / 110));
  let step = targets[targets.length - 1];
  for (const t of targets) {
    if (span / t <= want * 1.6) { step = t; break; }
  }
  const fmt = step >= 86400
    ? (t) => fmtTime(t).slice(5, 16)
    : (t) => fmtClock(t);
  return { step, fmt };
}

function faultTooltip(f) {
  const lines = [`[${f.rule_name}] ${f.logic} · ≥${fmtDur(f.duration_required)}`,
    `${fmtTime(f.start)} → ${fmtTime(f.end)}`,
    `持续 ${fmtDur(f.end - f.start)}，确认时刻 ${fmtClock(f.confirmed_at)}`];
  f.evidence.forEach((ev) => {
    lines.push(`证据 ${ev.condition.device}/${ev.condition.metric} ${ev.condition.op} ${ev.condition.threshold}` +
      `${resetText(ev.condition)}: ${ev.events.length} 个样本`);
  });
  return lines.join("\n");
}

/* --------------------------- tooltip / crosshair --------------------------- */

let tipTarget = null;
function attachTip(node, contentFn) {
  node.addEventListener("mouseenter", (e) => {
    tipTarget = { node, contentFn };
    showTip(contentFn(), e.clientX, e.clientY);
  });
  node.addEventListener("mousemove", (e) => showTip(tipTarget.contentFn(), e.clientX, e.clientY));
  node.addEventListener("mouseleave", hideTip);
}

function showTip(content, cx, cy) {
  const tip = $("tooltip");
  tip.hidden = false;
  tip.textContent = content;
  const wrap = $("timeline-wrap").getBoundingClientRect();
  let x = cx - wrap.left + 14, y = cy - wrap.top + 14;
  if (x + tip.offsetWidth > wrap.width) x = cx - wrap.left - tip.offsetWidth - 10;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}
function hideTip() { $("tooltip").hidden = true; tipTarget = null; }

function onSvgMove(e, crossV, crossLabel) {
  const x = e.offsetX;
  if (x < TL.left || x > state.svgWidth - TL.right) {
    crossV.setAttribute("visibility", "hidden");
    crossLabel.setAttribute("visibility", "hidden");
    return;
  }
  const t = xUnscale(x);
  crossV.setAttribute("x1", x); crossV.setAttribute("x2", x);
  crossV.setAttribute("visibility", "visible");
  crossLabel.setAttribute("x", Math.min(x + 4, state.svgWidth - 120));
  crossLabel.setAttribute("y", TL.top - 8);
  crossLabel.setAttribute("visibility", "visible");
  crossLabel.textContent = fmtTime(t);
}

/* ------------------------------- brush pop ------------------------------- */

function openBrushPop(t0, t1, _xClient, xSvg) {
  const pop = $("brush-pop");
  state.brush = { start: t0, end: t1 };
  $("brush-time").textContent = `${fmtTime(t0)} → ${fmtTime(t1)}（${fmtDur(t1 - t0)}）`;
  $("brush-label").value = "";
  $("brush-note").value = "";
  $("brush-device").value = "";
  document.querySelector('input[name="brush-kind"][value="annotation"]').checked = true;
  syncBrushKind();
  const wrapRect = $("timeline-wrap").getBoundingClientRect();
  const svgRect = $("timeline").getBoundingClientRect();
  let left = (svgRect.left - wrapRect.left) + Math.min(Math.max(xSvg, TL.left), state.svgWidth - 260);
  left = Math.min(left, wrapRect.width - 250);
  pop.style.left = left + "px";
  pop.style.top = TL.top + 10 + "px";
  pop.hidden = false;
  $("brush-label").focus();
}

function brushKind() {
  const r = document.querySelector('input[name="brush-kind"]:checked');
  return r ? r.value : "annotation";
}

function syncBrushKind() {
  const isMaint = brushKind() === "maintenance";
  $("brush-device").hidden = !isMaint;
  $("brush-label").placeholder = isMaint ? "维护名称，如：季度检修" : "标注名称，如：计划检修";
  $("brush-save").textContent = isMaint ? "创建维护窗口" : "创建标注";
}

async function saveBrush() {
  if (!state.brush) return;
  const label = $("brush-label").value.trim();
  if (!label) { toast("请填写名称", true); return; }
  if (brushKind() === "maintenance") {
    const device = $("brush-device").value.trim();
    if (!device) { toast("维护窗口需要填写设备", true); return; }
    try {
      // 服务端在写入后自动重新评估全部规则
      await api("/api/maintenance", {
        method: "POST",
        body: JSON.stringify({
          device, name: label,
          start: state.brush.start, end: state.brush.end,
          note: $("brush-note").value.trim() || null,
        }),
      });
      $("brush-pop").hidden = true;
      state.brush = null;
      await reloadAll();
      toast("维护窗口已创建，规则已重新评估");
    } catch (e) { toast(e.message, true); }
    return;
  }
  try {
    await api("/api/annotations", {
      method: "POST",
      body: JSON.stringify({
        start: state.brush.start, end: state.brush.end,
        label, note: $("brush-note").value.trim() || null, color: ANN_COLOR,
      }),
    });
    $("brush-pop").hidden = true;
    state.brush = null;
    await loadTimeline();
    renderAnnotations();
    toast("标注已创建");
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------ fault list ------------------------------ */

function resetText(cond) {
  if (cond.reset_threshold == null) return "";
  const dir = cond.op === ">" || cond.op === ">=" ? "≤" : "≥";
  return ` · 复位 ${dir} ${fmtNum(cond.reset_threshold)}`;
}

function evidenceHtml(ruleId, evidence) {
  return evidence.map((ev, i) => `
    <div class="evidence-block">
      <div><strong>条件 ${i + 1}：</strong>
        <span class="pill" style="background:${ruleColor(ruleId)}">${esc(ev.condition.device)}/${esc(ev.condition.metric)}</span>
        ${ev.condition.op} ${fmtNum(ev.condition.threshold)}${resetText(ev.condition)}
        · 命中 ${ev.events.length} 个样本
        · 成立子区间 ${ev.held_intervals.map((iv) => `[${iv[0]}, ${iv[1]})`).join("，") || "无"}
      </div>
      <table>
        <thead><tr><th>event_id</th><th>时间 (UTC)</th><th>值</th></tr></thead>
        <tbody>
          ${ev.events.map((p) => `<tr><td>${p.event_id}</td><td>${fmtTime(p.ts)}</td><td>${fmtNum(p.value)}</td></tr>`).join("")}
        </tbody>
      </table>
    </div>`).join("");
}

function renderFaults() {
  const faults = state.timeline ? state.timeline.faults : [];
  $("fault-count").textContent = `${faults.length}`;
  const box = $("fault-list");
  if (!faults.length) {
    box.innerHTML = `<div class="empty">当前范围内无故障区间。导入数据、定义规则后点击「评估规则」。</div>`;
    return;
  }
  box.innerHTML = faults.map((f) => `
    <div class="fault-row ${state.selectedFaultId === f.id ? "open" : ""}" data-id="${f.id}">
      <div class="fault-summary">
        <span class="swatch" style="background:${ruleColor(f.rule_id)};width:12px;height:12px"></span>
        <span class="fault-name">${esc(f.rule_name)}</span>
        <span class="tag">${f.logic}</span>
        <span class="fault-time">${fmtTime(f.start)} → ${fmtTime(f.end)}</span>
        <span class="tag" style="background:#ecfdf5;border-color:#a7f3d0;color:#065f46">持续 ${fmtDur(f.end - f.start)}</span>
        <span class="confirm-note">持续满 ${fmtDur(f.duration_required)} 于 ${fmtClock(f.confirmed_at)} 确认</span>
      </div>
      <div class="fault-detail">
        <div><strong>半开区间：</strong>[${f.start}, ${f.end})</div>
        ${evidenceHtml(f.rule_id, f.evidence)}
      </div>
    </div>`).join("");

  box.querySelectorAll(".fault-row").forEach((row) => {
    row.querySelector(".fault-summary").addEventListener("click", () => selectFault(Number(row.dataset.id)));
  });
}

function selectFault(id) {
  state.selectedFaultId = state.selectedFaultId === id ? null : id;
  renderFaults();
  renderFaultEvents();
  renderTimeline();
  if (state.selectedFaultId) {
    const selectors = [`.fault-row[data-id="${state.selectedFaultId}"]`, `.event-member[data-id="${state.selectedFaultId}"]`];
    const row = document.querySelector(selectors.join(","));
    if (row) row.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

/* ---------------------------- fault event view --------------------------- */

// Assign lane indexes to event members so their mini-track bars stack like
// the timeline fault bands; touching/overlapping members share no lane.
function memberLanes(members) {
  const sorted = [...members].sort((a, b) => a.start - b.start || a.end - b.end);
  const lanes = new Map();
  const ends = [];
  for (const m of sorted) {
    let lane = ends.findIndex((end) => end <= m.start + 1e-6);
    if (lane === -1) { lane = ends.length; ends.push(m.end); }
    else ends[lane] = m.end;
    lanes.set(m.fault_id, lane);
  }
  return lanes;
}

function eventMiniTrackHtml(ev) {
  const lanes = memberLanes(ev.members);
  const span = Math.max(ev.end - ev.start, 1e-9);
  const rows = Math.max(...lanes.values()) + 1;
  const barH = 8, rowGap = 2, pad = 1;
  const height = pad * 2 + rows * barH + (rows - 1) * rowGap;
  const bars = ev.members.map((m) => {
    const x = ((m.start - ev.start) / span) * 100;
    const w = Math.max(((m.end - m.start) / span) * 100, 0.6);
    const y = pad + lanes.get(m.fault_id) * (barH + rowGap);
    return `<rect data-fid="${m.fault_id}" x="${x.toFixed(2)}%" y="${y}" width="${w.toFixed(2)}%" height="${barH}" rx="2"
      fill="${ruleColor(m.rule_id)}" fill-opacity="${m.in_window ? 0.85 : 0.3}"
      stroke="${state.selectedFaultId === m.fault_id ? "#0f172a" : "rgba(0,0,0,0.25)"}"
      stroke-width="${state.selectedFaultId === m.fault_id ? 1.4 : 0.5}"></rect>`;
  }).join("");
  return `<svg class="event-track" viewBox="0 0 100 ${height}" preserveAspectRatio="none" style="height:${height}px">${bars}</svg>`;
}

function renderFaultEvents() {
  const events = state.faultEvents || [];
  $("event-grp-count").textContent = `${events.length}`;
  const box = $("fault-event-list");
  if (!events.length) {
    box.innerHTML = `<div class="empty">当前范围内无故障事件。导入数据、定义规则并点击「评估规则」后，重叠或首尾相接的故障会归并为事件。</div>`;
    return;
  }
  box.innerHTML = events.map((ev) => `
    <div class="event-row ${state.expandedEventId === ev.id ? "open" : ""}" data-id="${esc(ev.id)}">
      <div class="event-summary">
        <span class="event-chevron">${state.expandedEventId === ev.id ? "▾" : "▸"}</span>
        <span class="event-id-pill">事件 ${esc(ev.id)}</span>
        <span class="fault-time">${fmtTime(ev.start)} → ${fmtTime(ev.end)}</span>
        <span class="tag" style="background:#fef2f2;border-color:#fecaca;color:#991b1b">跨度 ${fmtDur(ev.end - ev.start)}</span>
        <span class="tag">${ev.rule_count} 条规则 · ${ev.fault_count} 个成员故障</span>
        ${ev.member_count_in_window < ev.fault_count
          ? `<span class="tag" style="background:#fffbeb;border-color:#fde68a;color:#92400e">${ev.fault_count - ev.member_count_in_window} 个成员在筛选窗口外</span>`
          : ""}
        <span class="event-rules">${ev.rule_names.map((n) => esc(n)).join("、")}</span>
      </div>
      <div class="event-track-wrap">${eventMiniTrackHtml(ev)}</div>
      <div class="event-detail">
        <div class="event-members-head">成员规则与各自区间（半开区间，保留原触发证据）：</div>
        ${ev.members.map((m) => `
          <div class="event-member ${state.selectedFaultId === m.fault_id ? "open" : ""} ${m.in_window ? "" : "out-of-window"}" data-id="${m.fault_id}">
            <div class="member-summary">
              <span class="swatch" style="background:${ruleColor(m.rule_id)};width:10px;height:10px"></span>
              <span class="fault-name">${esc(m.rule_name)}</span>
              <span class="tag">${m.logic}</span>
              <span class="fault-time">[${m.start}, ${m.end})</span>
              <span class="tag" style="background:#ecfdf5;border-color:#a7f3d0;color:#065f46">${fmtDur(m.end - m.start)}</span>
              <span class="confirm-note">${fmtClock(m.start)} → ${fmtClock(m.end)} · ${fmtClock(m.confirmed_at)} 确认</span>
              ${m.in_window ? "" : `<span class="tag" style="background:#fffbeb;border-color:#fde68a;color:#92400e">窗口外</span>`}
            </div>
            <div class="member-detail">${evidenceHtml(m.rule_id, m.evidence)}</div>
          </div>`).join("")}
      </div>
    </div>`).join("");

  box.querySelectorAll(".event-row").forEach((row) => {
    const id = row.dataset.id;
    row.querySelector(".event-summary").addEventListener("click", () => toggleEvent(id));
    row.querySelector(".event-track-wrap").addEventListener("click", () => toggleEvent(id));
  });
  box.querySelectorAll(".event-member").forEach((row) => {
    const fid = Number(row.dataset.id);
    row.querySelector(".member-summary").addEventListener("click", (e) => {
      e.stopPropagation();
      selectFault(state.selectedFaultId === fid ? null : fid);
    });
  });
}

function toggleEvent(id) {
  state.expandedEventId = state.expandedEventId === id ? null : id;
  renderFaultEvents();
  const row = document.querySelector(`.event-row[data-id="${state.expandedEventId}"]`);
  if (row) row.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function switchFaultView(view) {
  state.faultView = view;
  $("fault-list").hidden = view !== "faults";
  $("fault-event-list").hidden = view !== "events";
  $("fault-event-hint").hidden = view !== "events";
  $("tab-faults").classList.toggle("active", view === "faults");
  $("tab-events").classList.toggle("active", view === "events");
}

/* --------------------- rule-combination fault daily ledger --------------------- */

function fmtPct(ratio) {
  if (ratio === null || ratio === undefined) return "—";
  return `${(ratio * 100).toFixed(1)}%`;
}

function renderLedgerRules() {
  const box = $("ledger-rules");
  // keep selections only for rules that still exist
  const ids = new Set(state.rules.map((r) => r.id));
  state.ledgerSelected = new Set([...state.ledgerSelected].filter((id) => ids.has(id)));
  if (!state.rules.length) {
    box.innerHTML = `<div class="empty">尚无规则。先在左侧创建并评估规则，再回来查询日账。</div>`;
    return;
  }
  box.innerHTML = state.rules.map((r) => `
    <label class="ledger-rule" data-id="${r.id}">
      <input type="checkbox" ${state.ledgerSelected.has(r.id) ? "checked" : ""} />
      <span class="swatch" style="background:${ruleColor(r.id)}"></span>
      <span class="ledger-rule-name">${esc(r.name)}</span>
    </label>`).join("");
  box.querySelectorAll(".ledger-rule").forEach((el) => {
    const id = Number(el.dataset.id);
    el.querySelector("input").addEventListener("change", (e) => {
      if (e.target.checked) state.ledgerSelected.add(id);
      else state.ledgerSelected.delete(id);
    });
  });
}

function fillLedgerRange() {
  ensureView();
  if (typeof state.viewStart === "number" && isFinite(state.viewStart)) {
    $("ledger-start").value = toLocalInput(state.viewStart);
  }
  if (typeof state.viewEnd === "number" && isFinite(state.viewEnd)) {
    $("ledger-end").value = toLocalInput(state.viewEnd);
  }
}

async function runLedger() {
  const ids = [...state.ledgerSelected];
  if (!ids.length) { toast("请先勾选至少一条规则", true); return; }
  const s = fromLocalInput($("ledger-start").value);
  const e = fromLocalInput($("ledger-end").value);
  if (s === null || e === null || e <= s) { toast("请填写有效的起始/结束时间（结束须晚于起始）", true); return; }
  const params = new URLSearchParams({ start: s, end: e });
  ids.forEach((id) => params.append("rules", id));
  const btn = $("btn-ledger-query");
  btn.disabled = true;
  try {
    state.ledgerResult = await api(`/api/fault-daily-ledger?${params}`);
    renderLedger();
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

function renderLedger() {
  const r = state.ledgerResult;
  const summary = $("ledger-summary");
  const wrap = $("ledger-table-wrap");
  if (!r) {
    summary.hidden = true;
    wrap.hidden = true;
    return;
  }
  const t = r.totals;
  summary.hidden = false;
  summary.innerHTML = `
    <div class="ledger-line"><b>选中规则（${r.rules.length}）：</b>
      ${r.rules.map((x) => `<span class="tag">${esc(x.name)}</span>`).join(" ")}</div>
    <div class="ledger-line"><b>查询范围：</b>${fmtTime(r.start)} → ${fmtTime(r.end)}
      （共 ${fmtDur(r.end - r.start)}，按 UTC 午夜切分 ${r.days.length} 天）</div>
    <div class="ledger-line"><b>合并后片段：</b>${t.merged_segments} 段
      · 去重故障 ${Math.round(t.fault_seconds)} 秒 / 查询 ${Math.round(t.queried_seconds)} 秒
      · 新起 ${t.new_segments} · 跨日延续 ${t.continuing_segments} · 总覆盖率 ${fmtPct(t.coverage)}</div>`;

  $("ledger-rows").innerHTML = r.days.map((d) => {
    const pct = fmtPct(d.coverage);
    return `
      <tr>
        <td>${esc(d.date)}</td>
        <td title="${fmtDur(d.queried_seconds)}">${Math.round(d.queried_seconds)}</td>
        <td title="${fmtDur(d.fault_seconds)}">${Math.round(d.fault_seconds)}</td>
        <td>${d.new_segments}</td>
        <td>${d.continuing_segments}</td>
        <td><span class="ledger-cov"><span class="ledger-cov-bar" style="width:${(d.coverage * 100).toFixed(1)}%"></span></span> ${pct}</td>
      </tr>`;
  }).join("");
  $("ledger-total").innerHTML = `
    <tr>
      <td>合计</td>
      <td>${Math.round(t.queried_seconds)}</td>
      <td>${Math.round(t.fault_seconds)}</td>
      <td>${t.new_segments}</td>
      <td>${t.continuing_segments}</td>
      <td>${fmtPct(t.coverage)}</td>
    </tr>`;
  wrap.hidden = false;
}

/* ---------------------------- sampling-cadence audit ---------------------------- */

function renderAuditChannels() {
  const devSel = $("audit-device");
  const metSel = $("audit-metric");
  const devices = [...new Set(state.channels.map((c) => c.device))].sort();
  // keep the selected device/metric only while they still exist
  if (state.auditDevice && !devices.includes(state.auditDevice)) state.auditDevice = "";
  if (!state.auditDevice && devices.length) state.auditDevice = devices[0];
  devSel.innerHTML = devices.map((d) =>
    `<option value="${esc(d)}"${d === state.auditDevice ? " selected" : ""}>${esc(d)}</option>`).join("");

  const metrics = state.channels
    .filter((c) => c.device === state.auditDevice)
    .map((c) => c.metric)
    .sort();
  if (state.auditMetric && !metrics.includes(state.auditMetric)) state.auditMetric = "";
  if (!state.auditMetric && metrics.length) state.auditMetric = metrics[0];
  metSel.innerHTML = metrics.map((m) =>
    `<option value="${esc(m)}"${m === state.auditMetric ? " selected" : ""}>${esc(m)}</option>`).join("");
}

function fillAuditRange() {
  ensureView();
  if (typeof state.viewStart === "number" && isFinite(state.viewStart)) {
    $("audit-start").value = toLocalInput(state.viewStart);
  }
  if (typeof state.viewEnd === "number" && isFinite(state.viewEnd)) {
    $("audit-end").value = toLocalInput(state.viewEnd);
  }
}

async function runSamplingAudit() {
  const device = $("audit-device").value;
  const metric = $("audit-metric").value;
  const s = fromLocalInput($("audit-start").value);
  const e = fromLocalInput($("audit-end").value);
  const period = Number($("audit-period").value);
  const tolerance = Number($("audit-tolerance").value);
  if (!device || !metric) { toast("请先选择设备与指标（需已导入该通道事件）", true); return; }
  if (s === null || e === null || e <= s) { toast("请填写有效的起始/结束 UTC 时间（半开区间，结束须晚于起始）", true); return; }
  if (!Number.isFinite(period) || period <= 0) { toast("预期采样周期必须为正秒数", true); return; }
  if (!Number.isFinite(tolerance) || tolerance < 0) { toast("允许偏差必须为非负有限秒数", true); return; }
  if (tolerance >= period / 2) { toast("允许偏差必须严格小于半个周期（半个周期为 " + (period / 2) + " 秒）", true); return; }
  const params = new URLSearchParams({
    device, metric, start: s, end: e,
    period_seconds: period, tolerance_seconds: tolerance,
  });
  const btn = $("btn-audit-query");
  btn.disabled = true;
  try {
    state.auditResult = await api(`/api/sampling-audit?${params}`);
    renderSamplingAudit();
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

const AUDIT_EXTRA_META = {
  out_of_tolerance: { label: "偏拍：最近预期时刻也超出允许偏差" },
  duplicate: { label: "重复：该时刻已被更近的事件计入" },
};

function renderSamplingAudit() {
  const r = state.auditResult;
  const summary = $("audit-summary");
  const missWrap = $("audit-missing-wrap");
  const extraWrap = $("audit-extra-wrap");
  if (!r) {
    summary.hidden = true;
    missWrap.hidden = true;
    extraWrap.hidden = true;
    return;
  }
  summary.hidden = false;
  summary.innerHTML = `
    <div class="ledger-line"><b>通道：</b><span class="tag">${esc(r.device)}/${esc(r.metric)}</span>
      <span class="muted small">（该通道共 ${r.channel.event_count} 个事件，${fmtTime(r.channel.min_ts)} ~ ${fmtTime(r.channel.max_ts)}）</span></div>
    <div class="ledger-line"><b>稽核范围：</b>${fmtTime(r.start)} → ${fmtTime(r.end)}
      （半开 · ${fmtDur(r.end - r.start)}）· 锚点为范围起点 · 周期 ${Number(r.period_seconds)} 秒 · 允许偏差 ${Number(r.tolerance_seconds)} 秒（&lt; ${Number(r.period_seconds) / 2} 秒）</div>
    <div class="ledger-line"><b>预期时刻：</b>${r.expected_count} 个
      · <b>命中 ${r.hit_count}</b> · 未命中 ${r.miss_count}
      · 范围内实际事件 ${r.actual_count_in_range}（计入 ${r.matched_event_count}，未计入 ${r.extra_event_count}）
      · <b>整体命中率 ${fmtPct(r.hit_rate)}</b></div>`;

  const rows = $("audit-missing-rows");
  if (r.missing_segments.length) {
    rows.innerHTML = r.missing_segments.map((seg, i) => `
      <tr>
        <td>${i + 1}</td>
        <td>${fmtTime(seg.start)}</td>
        <td>${fmtTime(seg.end)}</td>
        <td>${fmtDur(seg.span_seconds)}</td>
        <td><b>${seg.miss_count}</b></td>
      </tr>`).join("");
    missWrap.hidden = false;
  } else {
    rows.innerHTML = `<tr><td colspan="5" class="empty">无缺采段：每个预期时刻均有范围内、偏差内的事件命中。</td></tr>`;
    missWrap.hidden = false;
  }

  if (r.extra_events.length) {
    $("audit-extra-count").textContent = `（${r.extra_event_count} 个，最多展示前 500 个）`;
    $("audit-extra-rows").innerHTML = r.extra_events.slice(0, 500).map((x) => `
      <tr>
        <td>${fmtTime(x.ts)}</td>
        <td>${esc((AUDIT_EXTRA_META[x.reason] || { label: x.reason }).label)}</td>
      </tr>`).join("");
    extraWrap.hidden = false;
  } else {
    extraWrap.hidden = true;
  }
}

/* --------------------- redundant-channel consistency check --------------------- */

function renderConsChannels() {
  const devices = [...new Set(state.channels.map((c) => c.device))].sort();
  const metricsFor = (dev) => state.channels
    .filter((c) => c.device === dev)
    .map((c) => c.metric)
    .sort();
  const fill = (devSel, metSel, dev, met, mets) => {
    devSel.innerHTML = devices.map((d) =>
      `<option value="${esc(d)}"${d === dev ? " selected" : ""}>${esc(d)}</option>`).join("");
    metSel.innerHTML = mets.map((m) =>
      `<option value="${esc(m)}"${m === met ? " selected" : ""}>${esc(m)}</option>`).join("");
  };
  // keep selections only while the channels still exist; default A to the
  // first channel and B to the first channel different from A
  if (state.consDeviceA && !devices.includes(state.consDeviceA)) state.consDeviceA = "";
  if (!state.consDeviceA && devices.length) state.consDeviceA = devices[0];
  let mets = metricsFor(state.consDeviceA);
  if (state.consMetricA && !mets.includes(state.consMetricA)) state.consMetricA = "";
  if (!state.consMetricA && mets.length) state.consMetricA = mets[0];
  fill($("cons-device-a"), $("cons-metric-a"), state.consDeviceA, state.consMetricA, mets);

  if (state.consDeviceB && !devices.includes(state.consDeviceB)) state.consDeviceB = "";
  if (!state.consDeviceB) {
    state.consDeviceB = devices.find((d) => d !== state.consDeviceA) || state.consDeviceA;
  }
  mets = metricsFor(state.consDeviceB);
  if (state.consMetricB && !mets.includes(state.consMetricB)) state.consMetricB = "";
  if (!state.consMetricB) {
    state.consMetricB = state.consDeviceB === state.consDeviceA
      ? (mets.find((m) => m !== state.consMetricA) || state.consMetricA)
      : mets[0];
  }
  fill($("cons-device-b"), $("cons-metric-b"), state.consDeviceB, state.consMetricB, mets);
}

function fillConsRange() {
  ensureView();
  if (typeof state.viewStart === "number" && isFinite(state.viewStart)) {
    $("cons-start").value = toLocalInput(state.viewStart);
  }
  if (typeof state.viewEnd === "number" && isFinite(state.viewEnd)) {
    $("cons-end").value = toLocalInput(state.viewEnd);
  }
}

async function runConsistencyCheck() {
  const deviceA = $("cons-device-a").value, metricA = $("cons-metric-a").value;
  const deviceB = $("cons-device-b").value, metricB = $("cons-metric-b").value;
  const s = fromLocalInput($("cons-start").value);
  const e = fromLocalInput($("cons-end").value);
  const threshold = Number($("cons-threshold").value);
  if (!deviceA || !metricA || !deviceB || !metricB) { toast("请选择两条通道（需已导入该通道事件）", true); return; }
  if (deviceA === deviceB && metricA === metricB) { toast("请选择两条不同的通道", true); return; }
  if (s === null || e === null || e <= s) { toast("请填写有效的起始/结束 UTC 时间（半开区间，结束须晚于起始）", true); return; }
  if (!Number.isFinite(threshold) || threshold < 0) { toast("绝对差阈值必须为非负有限数", true); return; }
  const params = new URLSearchParams({
    device_a: deviceA, metric_a: metricA,
    device_b: deviceB, metric_b: metricB,
    start: s, end: e, threshold,
  });
  const btn = $("btn-cons-query");
  btn.disabled = true;
  try {
    state.consResult = await api(`/api/consistency-check?${params}`);
    renderConsistency();
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

function renderConsistency() {
  const r = state.consResult;
  const summary = $("cons-summary");
  const wrap = $("cons-segments-wrap");
  if (!r) {
    summary.hidden = true;
    wrap.hidden = true;
    return;
  }
  const ttlText = (ch) => ch.ttl_seconds === null
    ? "保持到下一样本"
    : `有效期 ${fmtDur(ch.ttl_seconds)}（${Number(ch.ttl_seconds)} 秒）`;
  summary.hidden = false;
  summary.innerHTML = `
    <div class="ledger-line"><b>通道 A：</b><span class="tag">${esc(r.channel_a.device)}/${esc(r.channel_a.metric)}</span>
      <span class="muted small">（${ttlText(r.channel_a)} · ${r.channel_a.event_count} 个事件）</span>
      <b>通道 B：</b><span class="tag">${esc(r.channel_b.device)}/${esc(r.channel_b.metric)}</span>
      <span class="muted small">（${ttlText(r.channel_b)} · ${r.channel_b.event_count} 个事件）</span></div>
    <div class="ledger-line"><b>核查范围：</b>${fmtTime(r.start)} → ${fmtTime(r.end)}
      （半开 · ${fmtDur(r.end - r.start)}）· 绝对差阈值 ${fmtNum(r.threshold)}（严格大于判不一致）
      · 全局数据终点 ${fmtTime(r.horizon)}</div>
    <div class="ledger-line"><b>可比时长：</b>${Math.round(r.comparable_seconds)} 秒
      · <b>不一致时长：</b>${Math.round(r.inconsistent_seconds)} 秒（占可比 ${fmtPct(r.inconsistent_ratio)}）
      · 不一致片段 <b>${r.segment_count}</b> 段</div>`;

  const rows = $("cons-segment-rows");
  if (r.segments.length) {
    rows.innerHTML = r.segments.map((seg, i) => `
      <tr>
        <td>${i + 1}</td>
        <td>${fmtTime(seg.start)}</td>
        <td>${fmtTime(seg.end)}</td>
        <td>${Math.round(seg.duration_seconds)}</td>
        <td><b>${fmtNum(seg.max_abs_diff)}</b></td>
        <td>${fmtTime(seg.max_at.a.ts)} · 值 ${fmtNum(seg.max_at.a.value)}</td>
        <td>${fmtTime(seg.max_at.b.ts)} · 值 ${fmtNum(seg.max_at.b.value)}</td>
      </tr>`).join("");
  } else {
    rows.innerHTML = `<tr><td colspan="7" class="empty">无不一致片段：两通道同时有效的交集上绝对差均未严格超过阈值（或交集为空）。</td></tr>`;
  }
  wrap.hidden = false;
}

/* --------------------- rule fault sequence pairing --------------------- */

function renderPairRules() {
  const ids = state.rules.map((r) => r.id);
  // keep selections only while the rules still exist and stay different;
  // default cause to the first rule and effect to the first different rule
  if (!ids.includes(state.pairCauseId)) state.pairCauseId = ids.length ? ids[0] : null;
  if (!ids.includes(state.pairEffectId) || state.pairEffectId === state.pairCauseId) {
    state.pairEffectId = ids.find((id) => id !== state.pairCauseId) ?? null;
  }
  const options = (selected) => state.rules.map((r) =>
    `<option value="${r.id}"${r.id === selected ? " selected" : ""}>${esc(r.name)}</option>`).join("");
  $("pair-rule-cause").innerHTML = options(state.pairCauseId);
  $("pair-rule-effect").innerHTML = options(state.pairEffectId);
}

function fillPairRange() {
  ensureView();
  if (typeof state.viewStart === "number" && isFinite(state.viewStart)) {
    $("pair-start").value = toLocalInput(state.viewStart);
  }
  if (typeof state.viewEnd === "number" && isFinite(state.viewEnd)) {
    $("pair-end").value = toLocalInput(state.viewEnd);
  }
}

async function runFaultPairing() {
  const causeId = Number($("pair-rule-cause").value);
  const effectId = Number($("pair-rule-effect").value);
  const s = fromLocalInput($("pair-start").value);
  const e = fromLocalInput($("pair-end").value);
  const maxLag = Number($("pair-max-lag").value);
  if (!causeId || !effectId) { toast("请先创建并评估至少两条规则", true); return; }
  if (causeId === effectId) { toast("前因与后续必须是两条不同的规则", true); return; }
  if (s === null || e === null || e <= s) { toast("请填写有效的起始/结束 UTC 时间（半开区间，结束须晚于起始）", true); return; }
  if (!Number.isFinite(maxLag) || maxLag <= 0) { toast("最大滞后必须为正秒数", true); return; }
  const params = new URLSearchParams({
    cause_rule: causeId, effect_rule: effectId,
    start: s, end: e, max_lag_seconds: maxLag,
  });
  const btn = $("btn-pair-query");
  btn.disabled = true;
  try {
    state.pairResult = await api(`/api/fault-pairing?${params}`);
    renderPairing();
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

function pairUnmatchedRows(list) {
  if (!list.length) {
    return `<tr><td colspan="3" class="empty">无未配对段：范围内该侧故障全部配对。</td></tr>`;
  }
  return list.map((f) => `
    <tr>
      <td>#${f.fault_id}</td>
      <td>${fmtTime(f.start)}</td>
      <td>[${fmtClock(f.start)}, ${fmtClock(f.end)}) · ${fmtDur(f.end - f.start)}</td>
    </tr>`).join("");
}

function renderPairing() {
  const r = state.pairResult;
  const summary = $("pair-summary");
  const wrap = $("pair-table-wrap");
  const unmatchedWrap = $("pair-unmatched-wrap");
  if (!r) {
    summary.hidden = true;
    wrap.hidden = true;
    unmatchedWrap.hidden = true;
    return;
  }
  summary.hidden = false;
  summary.innerHTML = `
    <div class="ledger-line"><b>前因规则：</b><span class="tag">${esc(r.cause_rule.name)}</span>
      <b>后续规则：</b><span class="tag">${esc(r.effect_rule.name)}</span></div>
    <div class="ledger-line"><b>查询范围：</b>${fmtTime(r.start)} → ${fmtTime(r.end)}
      （半开 · ${fmtDur(r.end - r.start)}）· 最大滞后 ${Number(r.max_lag_seconds)} 秒
      · 只取起点落在范围内的已保存故障</div>
    <div class="ledger-line"><b>范围内故障：</b>前因 ${r.cause_faults_in_range} 段 · 后续 ${r.effect_faults_in_range} 段
      · <b>配对 ${r.pair_count} 对</b>（总滞后 ${Math.round(r.total_lag_seconds)} 秒）
      · 前因配对率 ${fmtPct(r.cause_pair_rate)} · 后续配对率 ${fmtPct(r.effect_pair_rate)}</div>`;

  const rows = $("pair-rows");
  if (r.pairs.length) {
    rows.innerHTML = r.pairs.map((p, i) => `
      <tr>
        <td>${i + 1}</td>
        <td>#${p.cause_fault_id}</td>
        <td>${fmtTime(p.cause_start)}</td>
        <td>#${p.effect_fault_id}</td>
        <td>${fmtTime(p.effect_start)}</td>
        <td><b>${Math.round(p.lag_seconds)}</b></td>
      </tr>`).join("");
  } else {
    rows.innerHTML = `<tr><td colspan="6" class="empty">无可配对故障：范围内没有满足「后续起点晚于前因起点且不超过最大滞后」的组合。</td></tr>`;
  }
  wrap.hidden = false;

  $("pair-unmatched-cause-count").textContent = `${r.unmatched_cause.length}`;
  $("pair-unmatched-effect-count").textContent = `${r.unmatched_effect.length}`;
  $("pair-unmatched-cause-rows").innerHTML = pairUnmatchedRows(r.unmatched_cause);
  $("pair-unmatched-effect-rows").innerHTML = pairUnmatchedRows(r.unmatched_effect);
  unmatchedWrap.hidden = false;
}

/* --------------------- single-channel mutation recovery --------------------- */

function renderRecoveryChannels() {
  const devSel = $("rec-device");
  const metSel = $("rec-metric");
  const devices = [...new Set(state.channels.map((c) => c.device))].sort();
  if (state.recDevice && !devices.includes(state.recDevice)) state.recDevice = "";
  if (!state.recDevice && devices.length) state.recDevice = devices[0];
  devSel.innerHTML = devices.map((d) =>
    `<option value="${esc(d)}"${d === state.recDevice ? " selected" : ""}>${esc(d)}</option>`).join("");

  const metrics = state.channels
    .filter((c) => c.device === state.recDevice)
    .map((c) => c.metric)
    .sort();
  if (state.recMetric && !metrics.includes(state.recMetric)) state.recMetric = "";
  if (!state.recMetric && metrics.length) state.recMetric = metrics[0];
  metSel.innerHTML = metrics.map((m) =>
    `<option value="${esc(m)}"${m === state.recMetric ? " selected" : ""}>${esc(m)}</option>`).join("");
}

function fillRecoveryRange() {
  ensureView();
  if (typeof state.viewStart === "number" && isFinite(state.viewStart)) {
    $("rec-start").value = toLocalInput(state.viewStart);
  }
  if (typeof state.viewEnd === "number" && isFinite(state.viewEnd)) {
    $("rec-end").value = toLocalInput(state.viewEnd);
  }
}

async function runMutationRecovery() {
  const device = $("rec-device").value;
  const metric = $("rec-metric").value;
  const s = fromLocalInput($("rec-start").value);
  const e = fromLocalInput($("rec-end").value);
  const rate = Number($("rec-rate").value);
  const tolerance = Number($("rec-tolerance").value);
  const observation = Number($("rec-observation").value);
  if (!device || !metric) { toast("请先选择设备与指标（需已导入该通道事件）", true); return; }
  if (s === null || e === null || e <= s) { toast("请填写有效的起始/结束 UTC 时间（半开区间，结束须晚于起始）", true); return; }
  if (!Number.isFinite(rate) || rate <= 0) { toast("跳变速率阈值必须为正有限数（单位 / 秒）", true); return; }
  if (!Number.isFinite(tolerance) || tolerance < 0) { toast("回归容差必须为非负有限数", true); return; }
  if (!Number.isFinite(observation) || observation <= 0) { toast("观察时长必须为正秒数", true); return; }
  const params = new URLSearchParams({
    device, metric, start: s, end: e,
    rate_threshold: rate,
    regression_tolerance: tolerance,
    observation_seconds: observation,
  });
  const btn = $("btn-rec-query");
  btn.disabled = true;
  try {
    state.recResult = await api(`/api/mutation-recovery?${params}`);
    renderMutationRecovery();
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

const RECOVERY_REASON_META = {
  observation_expired: { label: "未恢复：观察时长内未回到基线容差", cls: "rec-unrec" },
  validity_gap: { label: "未恢复：采样有效期空档切断，无法继续观察", cls: "rec-gap" },
  range_end: { label: "未恢复：查询范围结束时仍偏离基线", cls: "rec-unrec" },
};

function recSampleHtml(sample) {
  return `${fmtTime(sample.ts)}<br/><span class="muted small">event ${sample.event_id} · 值 ${fmtNum(sample.value)}</span>`;
}

function renderMutationRecovery() {
  const r = state.recResult;
  const summary = $("rec-summary");
  const wrap = $("rec-segments-wrap");
  if (!r) {
    summary.hidden = true;
    wrap.hidden = true;
    return;
  }
  const ttlText = r.channel.ttl_seconds === null
    ? "保持到下一样本（无空档）"
    : `有效期 ${fmtDur(r.channel.ttl_seconds)}（${Number(r.channel.ttl_seconds)} 秒），到期空档切断连续性`;
  summary.hidden = false;
  summary.innerHTML = `
    <div class="ledger-line"><b>通道：</b><span class="tag">${esc(r.device)}/${esc(r.metric)}</span>
      <span class="muted small">（${ttlText} · 该通道共 ${r.channel.event_count} 个事件）</span></div>
    <div class="ledger-line"><b>诊断范围：</b>${fmtTime(r.start)} → ${fmtTime(r.end)}
      （半开 · ${fmtDur(r.end - r.start)}）· 跳变速率阈值 ${fmtNum(r.rate_threshold)}（单位/秒，严格大于判突变）
      · 回归容差 ${fmtNum(r.regression_tolerance)} · 观察时长 ${fmtDur(r.observation_seconds)}</div>
    <div class="ledger-line"><b>突变片段：</b>${r.segment_count} 段
      · <span style="color:#065f46">已恢复 ${r.recovered_count}</span>
      · <span style="color:#991b1b">未恢复 ${r.unrecovered_count}</span></div>`;

  const rows = $("rec-segment-rows");
  if (r.segments.length) {
    rows.innerHTML = r.segments.map((seg, i) => {
      const status = seg.recovered
        ? `<span class="rec-status recovered">已恢复</span>`
        : `<span class="rec-status unrecovered">未恢复</span>`;
      const outcome = seg.recovered
        ? `<b>${Math.round(seg.recovery_seconds)}</b> 秒（${fmtDur(seg.recovery_seconds)}）`
        : esc((RECOVERY_REASON_META[seg.unrecovered_reason] || { label: seg.unrecovered_reason }).label);
      return `
      <tr class="${seg.recovered ? "rec-row-ok" : "rec-row-bad"}">
        <td>${i + 1}</td>
        <td>${status}</td>
        <td>${recSampleHtml(seg.baseline_sample)}</td>
        <td>${recSampleHtml(seg.start_sample)}<br/><span class="muted small">速率 ${fmtNum(seg.jump_rate)}/秒</span></td>
        <td>${recSampleHtml(seg.end_sample)}</td>
        <td><b>${fmtNum(seg.jump_rate)}</b>/秒</td>
        <td><b>${fmtNum(seg.max_deviation)}</b><br/><span class="muted small">@ ${fmtTime(seg.max_deviation_sample.ts)}<br/>值 ${fmtNum(seg.max_deviation_sample.value)}</span></td>
        <td>${outcome}</td>
      </tr>`;
    }).join("");
  } else {
    rows.innerHTML = `<tr><td colspan="8" class="empty">范围内没有超过跳变速率阈值的相邻样本突变（或范围内没有有效样本）。</td></tr>`;
  }
  wrap.hidden = false;
}

/* ----------------------------- annotations ----------------------------- */

function renderAnnotations() {
  const anns = state.timeline ? state.timeline.annotations : [];
  $("anno-count").textContent = `${anns.length} 个标注`;
  const box = $("anno-list");
  if (!anns.length) {
    box.innerHTML = `<div class="empty">在时间轴上拖拽框选即可创建区间标注。</div>`;
    return;
  }
  box.innerHTML = anns.map((a) => `
    <div class="anno-row">
      <span class="anno-chip" style="background:${esc(a.color || ANN_COLOR)}"></span>
      <strong>${esc(a.label)}</strong>
      <span class="fault-time">${fmtTime(a.start)} → ${fmtTime(a.end)}</span>
      <span class="muted small">(${fmtDur(a.end - a.start)})</span>
      ${a.note ? `<span class="muted small">${esc(a.note)}</span>` : ""}
      <button class="icon-btn act-del-anno" data-id="${a.id}" style="margin-left:auto" title="删除标注">✕</button>
    </div>`).join("");
  box.querySelectorAll(".act-del-anno").forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("删除该标注？")) return;
      await api(`/api/annotations/${btn.dataset.id}`, { method: "DELETE" });
      await loadTimeline();
      renderAnnotations();
    }));
}

/* ------------------------------ legend ------------------------------ */

function renderLegend() {
  $("legend").innerHTML = state.rules.map((r) =>
    `<span class="legend-item"><span class="swatch" style="background:${ruleColor(r.id)}"></span>${esc(r.name)}</span>`
  ).join("") +
    `<span class="legend-item"><span class="swatch" style="background:${ANN_COLOR};opacity:0.5"></span>区间标注</span>
     <span class="legend-item"><span class="swatch" style="background:${MAINT_COLOR};opacity:0.55"></span>计划维护窗口（顶部维护带 + 设备泳道底色）</span>
     <span class="legend-item"><span class="swatch" style="background:${SUPP_COLOR};opacity:0.65"></span>被维护抑制的片段</span>
     <span class="legend-item"><span class="swatch" style="background:${TTL_COLOR};opacity:0.45"></span>采样有效期（TTL）：泳道 ⏳ 徽标表示样本到期后留空档</span>
     <span class="legend-item"><span style="color:#0f172a">▴</span> 故障确认时刻（start + 持续时长）</span>
     <span class="legend-item"><span style="display:inline-block;width:14px;border-top:2px dashed #0d9488"></span> 复位阈值：命中保持到值到达复位边界</span>
     <span class="legend-item"><span style="color:#f97316">●</span> 选中故障的证据样本</span>`;
}

/* --------------------------- import batches / undo --------------------------- */

async function loadImportBatches() {
  state.imports = await api("/api/imports");
  renderImportBatches();
}

function renderImportBatches() {
  const box = $("import-batch-list");
  if (!state.imports.length) {
    box.innerHTML = `<div class="empty">尚无导入批次。</div>`;
    return;
  }
  box.innerHTML = state.imports.map((b, idx) => {
    const name = b.filename ? esc(b.filename) : "文本导入";
    const cls = b.revoked ? "revoked" : (idx === 0 ? "latest" : "");
    let badge;
    if (b.revoked) badge = `<span class="batch-badge revoked">已撤销</span>`;
    else if (b.can_undo) badge = `<span class="batch-badge undoable">可撤销（最近一次）</span>`;
    else badge = `<span class="batch-badge" title="${esc(b.undo_reason || "")}">不可撤销</span>`;
    const undoBtn = b.can_undo
      ? `<button class="btn-undo" data-id="${b.id}">撤销本批</button>`
      : "";
    return `
      <div class="import-batch-item ${cls}">
        <div class="rname">
          <span>#${b.id} · ${name}</span>
          <span class="batch-ops">${badge}${undoBtn}</span>
        </div>
        <div class="rmeta">
          ${fmtTime(b.imported_at)}<br/>
          ${b.inserted_rows} 新增 · ${b.overwritten_rows} 覆盖 · ${b.skipped_rows} 跳过（共 ${b.total_rows} 行）
          · 快照 ${b.snapshot_keys} 键（其中 ${b.overwritten_keys} 个为覆盖）
          ${b.revoked ? `<br/>撤销于 ${fmtTime(b.revoked_at)}` : (!b.can_undo && b.undo_reason ? `<br/>${esc(b.undo_reason)}` : "")}
        </div>
      </div>`;
  }).join("");
  box.querySelectorAll(".btn-undo").forEach((btn) =>
    btn.addEventListener("click", () => undoImportBatch(Number(btn.dataset.id))));
}

async function undoImportBatch(id) {
  const batch = state.imports.find((b) => b.id === id);
  const name = batch && batch.filename ? batch.filename : `批次 #${id}`;
  if (!confirm(`确定撤销「${name}」？\n\n将删除本批新增事件、恢复被覆盖事件的原值与原批次归属，并刷新故障；失败会全部回滚。`)) return;
  try {
    const r = await api(`/api/imports/${id}/undo`, { method: "POST" });
    const evalNote = r.evaluation && r.evaluation.rules_evaluated
      ? `；已重算 ${r.evaluation.rules_evaluated} 条规则、${r.evaluation.fault_intervals} 个故障区间`
      : "";
    await reloadAll();
    toast(`已撤销批次 #${id}：删除 ${r.deleted_events} 条新增事件，恢复 ${r.restored_events} 条覆盖事件${evalNote}`);
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------- events ------------------------------- */

async function loadEvents(append = false) {
  const params = new URLSearchParams({ limit: state.eventLimit, offset: state.eventOffset });
  if (state.viewStart !== null && state.dataStart !== null && state.viewStart !== state.dataStart) params.set("start", state.viewStart);
  if (state.viewEnd !== null && state.dataEnd !== null && state.viewEnd !== state.dataEnd) params.set("end", state.viewEnd);
  const data = await api(`/api/events?${params}`);
  state.eventTotal = data.total;
  const rows = data.events;
  const tbody = $("event-rows");
  const html = rows.map((e) =>
    `<tr><td>${fmtTime(e.ts)}</td><td>${esc(e.device)}</td><td>${esc(e.metric)}</td><td>${fmtNum(e.value)}</td></tr>`).join("");
  tbody.insertAdjacentHTML("beforeend", html);
  if (!append) tbody.innerHTML = html;
  state.eventOffset += rows.length;
  $("event-count").textContent = `${state.eventTotal} 条（显示 ${state.eventOffset}）`;
  $("btn-more-events").hidden = state.eventOffset >= state.eventTotal;
}

/* ------------------------------ data loading ------------------------------ */

async function loadMeta() {
  const meta = await api("/api/meta");
  state.dataStart = meta.data_start;
  state.dataEnd = meta.data_end;
}

async function loadRules() {
  state.rules = await api("/api/rules");
}

async function loadChannels() {
  state.channels = await api("/api/channels");
}

async function loadTimeline() {
  ensureView();
  const params = new URLSearchParams();
  if (state.viewStart !== state.dataStart) params.set("start", state.viewStart);
  if (state.viewEnd !== state.dataEnd) params.set("end", state.viewEnd);
  state.timeline = await api(`/api/timeline?${params}`);
}

async function loadFaultEvents() {
  const params = new URLSearchParams();
  if (state.viewStart !== state.dataStart) params.set("start", state.viewStart);
  if (state.viewEnd !== state.dataEnd) params.set("end", state.viewEnd);
  state.faultEvents = await api(`/api/fault-events?${params}`);
}

function ensureView() {
  if (state.viewStart === null || state.viewEnd === null) {
    state.viewStart = state.dataStart;
    state.viewEnd = state.dataEnd;
  }
  if (state.viewStart === state.viewEnd && state.dataStart !== null) {
    state.viewStart = state.dataStart;
    state.viewEnd = state.dataEnd;
  }
}

async function reloadAll() {
  await loadMeta();
  await loadRules();
  await loadChannels();
  await loadTimeline();
  state.channelTtls = state.timeline.channel_ttls || [];
  await loadFaultEvents();
  state.selectedFaultId = null;
  state.expandedEventId = null;
  renderRules();
  renderTimeline();
  renderFaults();
  renderFaultEvents();
  renderAnnotations();
  renderMaintenance();
  renderTtls();
  renderLedgerRules();
  renderAuditChannels();
  renderConsChannels();
  renderPairRules();
  renderRecoveryChannels();
  if (!$("ledger-start").value || !$("ledger-end").value) fillLedgerRange();
  if (!$("audit-start").value || !$("audit-end").value) fillAuditRange();
  if (!$("cons-start").value || !$("cons-end").value) fillConsRange();
  if (!$("pair-start").value || !$("pair-end").value) fillPairRange();
  if (!$("rec-start").value || !$("rec-end").value) fillRecoveryRange();
  if (state.auditResult) renderSamplingAudit();
  if (state.consResult) renderConsistency();
  if (state.pairResult) renderPairing();
  if (state.recResult) renderMutationRecovery();
  syncFilterInputs();
  state.eventOffset = 0;
  await loadEvents(false);
  await loadImportBatches();
  updateStatus();
}

async function refreshAfterRuleChange() {
  await loadRules();
  await loadChannels();
  await loadFaultEvents();
  renderRules();
  renderTimeline();
  renderFaultEvents();
  renderLedgerRules();
  renderPairRules();
}

function updateStatus() {
  $("status").textContent = state.dataStart === null
    ? "无数据"
    : `${state.channels.length} 通道 · 时间范围 ${fmtTime(state.dataStart)} ~ ${fmtTime(state.dataEnd)}`;
}

/* ------------------------------ filters ------------------------------ */

function syncFilterInputs() {
  ensureView();
  $("filter-start").value = state.viewStart !== null && state.viewStart !== undefined ? toLocalInput(state.viewStart) : "";
  $("filter-end").value = state.viewEnd !== null && state.viewEnd !== undefined ? toLocalInput(state.viewEnd) : "";
  const filtered = state.viewStart !== state.dataStart || state.viewEnd !== state.dataEnd;
  $("filter-hint").textContent = filtered
    ? `已筛选：${fmtTime(state.viewStart)} ~ ${fmtTime(state.viewEnd)}（事件含端点；区间按半开相交）`
    : "";
}

async function applyFilter() {
  const s = fromLocalInput($("filter-start").value);
  const e = fromLocalInput($("filter-end").value);
  if (s === null || e === null || e <= s) { toast("请填写有效的起始/结束时间", true); return; }
  state.viewStart = s; state.viewEnd = e;
  await loadTimeline();
  await loadFaultEvents();
  state.selectedFaultId = null;
  state.expandedEventId = null;
  renderTimeline(); renderFaults(); renderFaultEvents(); renderAnnotations(); renderMaintenance();
  state.eventOffset = 0;
  await loadEvents(false);
  syncFilterInputs();
}

async function clearFilter() {
  state.viewStart = state.dataStart;
  state.viewEnd = state.dataEnd;
  await loadTimeline();
  await loadFaultEvents();
  state.selectedFaultId = null;
  state.expandedEventId = null;
  renderTimeline(); renderFaults(); renderFaultEvents(); renderAnnotations(); renderMaintenance();
  state.eventOffset = 0;
  await loadEvents(false);
  syncFilterInputs();
}

/* ------------------------------ import ------------------------------ */

async function doImport() {
  const fileInput = $("import-file");
  const text = $("import-text").value;
  const mapping = {
    device_field: $("f-device").value.trim(),
    metric_field: $("f-metric").value.trim(),
    ts_field: $("f-ts").value.trim(),
    value_field: $("f-value").value.trim(),
  };
  const result = $("import-result");
  try {
    let res;
    if (fileInput.files.length) {
      const fd = new FormData();
      fd.append("file", fileInput.files[0]);
      Object.entries(mapping).forEach(([k, v]) => v && fd.append(k, v));
      const r = await fetch("/api/import", { method: "POST", body: fd });
      if (!r.ok) throw new Error((await r.json()).detail || "导入失败");
      res = await r.json();
    } else if (text.trim()) {
      res = await api("/api/import/text", { method: "POST", body: JSON.stringify({ text, ...mapping }) });
    } else {
      toast("请选择文件或粘贴 JSONL 文本", true);
      return;
    }
    result.className = "import-result ok";
    const evalNote = res.evaluation && res.evaluation.rules_evaluated
      ? `；已重算 ${res.evaluation.rules_evaluated} 条规则、${res.evaluation.fault_intervals} 个故障区间`
      : "";
    result.textContent = `导入完成：${res.inserted} 新增，${res.overwritten} 覆盖，${res.skipped} 跳过（共 ${res.total_rows} 行）${evalNote}`;
    await reloadAll();
    toast("导入成功" + evalNote);
  } catch (e) {
    result.className = "import-result err";
    result.textContent = "导入失败：" + e.message;
    toast(e.message, true);
  }
}

/* ------------------------------ export / reset / demo ------------------------------ */

function doExport() {
  const params = new URLSearchParams({ download: "true" });
  if (state.viewStart !== state.dataStart) params.set("start", state.viewStart);
  if (state.viewEnd !== state.dataEnd) params.set("end", state.viewEnd);
  window.open(`/api/export?${params}`, "_blank");
}

async function doReset() {
  if (!confirm("将删除全部事件、规则、故障区间、维护窗口与标注，确定吗？")) return;
  await api("/api/reset", { method: "POST" });
  state.viewStart = state.viewEnd = state.dataStart = state.dataEnd = null;
  await reloadAll();
  toast("数据已清空");
}

async function loadDemo() {
  try {
    const r = await api("/api/demo", { method: "POST" });
    await reloadAll();
    await evaluateRules();
    toast(`示例已载入：${r.inserted} 个样本，${r.rules_created} 条示例规则`);
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------ init / events ------------------------------ */

function bind() {
  $("btn-import").addEventListener("click", () => { $("import-panel").hidden = !$("import-panel").hidden; });
  $("btn-do-import").addEventListener("click", doImport);
  $("import-file").addEventListener("change", () => {
    $("import-filename").textContent = $("import-file").files[0]?.name || "未选择文件";
  });
  $("btn-demo").addEventListener("click", loadDemo);
  $("btn-evaluate").addEventListener("click", evaluateRules);
  $("btn-export").addEventListener("click", doExport);
  $("btn-reset").addEventListener("click", doReset);

  $("btn-new-rule").addEventListener("click", () => openEditor(null));
  $("btn-save-rule").addEventListener("click", saveRule);
  $("btn-preview-rule").addEventListener("click", previewRule);
  $("btn-cancel-rule").addEventListener("click", closeEditor);
  $("btn-add-cond").addEventListener("click", () => {
    $("cond-rows").insertAdjacentHTML("beforeend", condRowHtml());
    bindCondRow($("cond-rows").lastElementChild.previousElementSibling);
    markPreviewStale();
  });
  // any edit of the draft invalidates a previously rendered preview
  $("rule-editor").addEventListener("input", markPreviewStale);
  $("rule-editor").addEventListener("change", markPreviewStale);

  $("btn-new-maint").addEventListener("click", () => openMaintEditor(null));
  $("btn-save-maint").addEventListener("click", saveMaintenance);
  $("btn-cancel-maint").addEventListener("click", closeMaintEditor);

  $("btn-new-ttl").addEventListener("click", () => openTtlEditor(null));
  $("btn-save-ttl").addEventListener("click", saveTtl);
  $("btn-clear-ttl").addEventListener("click", () => {
    const k = state.editingTtlKey;
    if (k) clearTtl(k.device, k.metric);
  });
  $("btn-cancel-ttl").addEventListener("click", closeTtlEditor);

  $("btn-apply-filter").addEventListener("click", applyFilter);
  $("btn-clear-filter").addEventListener("click", clearFilter);
  $("btn-fit").addEventListener("click", clearFilter);
  $("btn-more-events").addEventListener("click", () => loadEvents(true));
  $("btn-refresh-imports").addEventListener("click", () =>
    loadImportBatches().catch((e) => toast(e.message, true)));

  $("tab-faults").addEventListener("click", () => switchFaultView("faults"));
  $("tab-events").addEventListener("click", () => switchFaultView("events"));

  $("btn-ledger-query").addEventListener("click", runLedger);
  $("btn-ledger-clear").addEventListener("click", () => {
    state.ledgerSelected.clear();
    renderLedgerRules();
  });
  $("btn-ledger-fill").addEventListener("click", fillLedgerRange);

  $("audit-device").addEventListener("change", () => {
    state.auditDevice = $("audit-device").value;
    state.auditMetric = "";
    renderAuditChannels();
  });
  $("audit-metric").addEventListener("change", () => {
    state.auditMetric = $("audit-metric").value;
  });
  $("btn-audit-query").addEventListener("click", runSamplingAudit);
  $("btn-audit-fill").addEventListener("click", fillAuditRange);

  $("cons-device-a").addEventListener("change", () => {
    state.consDeviceA = $("cons-device-a").value;
    state.consMetricA = "";
    renderConsChannels();
  });
  $("cons-metric-a").addEventListener("change", () => {
    state.consMetricA = $("cons-metric-a").value;
  });
  $("cons-device-b").addEventListener("change", () => {
    state.consDeviceB = $("cons-device-b").value;
    state.consMetricB = "";
    renderConsChannels();
  });
  $("cons-metric-b").addEventListener("change", () => {
    state.consMetricB = $("cons-metric-b").value;
  });
  $("btn-cons-query").addEventListener("click", runConsistencyCheck);
  $("btn-cons-fill").addEventListener("click", fillConsRange);

  $("pair-rule-cause").addEventListener("change", () => {
    state.pairCauseId = Number($("pair-rule-cause").value) || null;
    if (state.pairCauseId !== null && state.pairCauseId === state.pairEffectId) {
      state.pairEffectId = state.rules.map((r) => r.id).find((id) => id !== state.pairCauseId) ?? null;
    }
    renderPairRules();
  });
  $("pair-rule-effect").addEventListener("change", () => {
    state.pairEffectId = Number($("pair-rule-effect").value) || null;
    if (state.pairEffectId !== null && state.pairEffectId === state.pairCauseId) {
      state.pairCauseId = state.rules.map((r) => r.id).find((id) => id !== state.pairEffectId) ?? null;
    }
    renderPairRules();
  });
  $("btn-pair-query").addEventListener("click", runFaultPairing);
  $("btn-pair-fill").addEventListener("click", fillPairRange);

  $("rec-device").addEventListener("change", () => {
    state.recDevice = $("rec-device").value;
    state.recMetric = "";
    renderRecoveryChannels();
  });
  $("rec-metric").addEventListener("change", () => {
    state.recMetric = $("rec-metric").value;
  });
  $("btn-rec-query").addEventListener("click", runMutationRecovery);
  $("btn-rec-fill").addEventListener("click", fillRecoveryRange);

  $("brush-save").addEventListener("click", saveBrush);
  $("brush-cancel").addEventListener("click", () => { $("brush-pop").hidden = true; state.brush = null; });
  document.querySelectorAll('input[name="brush-kind"]').forEach((r) =>
    r.addEventListener("change", syncBrushKind));

  window.addEventListener("resize", () => { if (state.timeline) renderTimeline(); });
}

(async function init() {
  bind();
  try {
    await reloadAll();
  } catch (e) {
    toast("初始化失败：" + e.message, true);
  }
})();
