/* video-harness 审核面板前端：纯 vanilla JS，无构建步骤、无外部依赖。 */

const state = {
  runs: [],
  currentRunId: null,
  currentRun: null,
  logsByShot: {},       // shot_id -> ShotRunLog[]，懒加载缓存
  selectedAttempt: {},  // shot_id -> attempt number（当前查看/待确认的尝试）
  activeTab: "shots",
};

const CAMERA_LABEL = {
  static: "固定", push_in: "推近", pull_out: "拉远", pan: "横摇",
  tilt: "俯仰", tracking: "跟拍", handheld: "手持",
};

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

// ---------------------------------------------------------------------------
// 启动 & run 列表
// ---------------------------------------------------------------------------

async function boot() {
  document.querySelectorAll(".tab").forEach((el) => {
    el.addEventListener("click", () => switchTab(el.dataset.tab));
  });
  document.getElementById("btn-view-final").addEventListener("click", () => {
    window.open(`/api/runs/${state.currentRunId}/media/final`, "_blank");
  });
  document.getElementById("btn-view-final-human").addEventListener("click", () => {
    window.open(`/api/runs/${state.currentRunId}/media/final_human`, "_blank");
  });
  document.getElementById("btn-assemble").addEventListener("click", onAssemble);

  await refreshRuns();
}

async function refreshRuns() {
  state.runs = await api("/api/runs");
  renderRunList();
  if (!state.currentRunId && state.runs.length) {
    selectRun(state.runs[0].run_id);
  }
}

function renderRunList() {
  const el = document.getElementById("run-list");
  if (!state.runs.length) {
    el.innerHTML = `<div class="empty-hint" style="padding:12px 4px">还没有 run。先跑一次：<br><code>python -m harness.cli ...</code></div>`;
    return;
  }
  el.innerHTML = state.runs.map((r) => `
    <div class="run-item ${r.run_id === state.currentRunId ? "active" : ""}" data-run="${escapeHtml(r.run_id)}">
      <div class="title">${escapeHtml(r.title)}</div>
      <div class="meta">
        <span>${escapeHtml(r.category || "")}</span>
        <span>${r.passed_count}/${r.shot_count} 通过</span>
        ${r.total_rmb != null ? `<span>¥${r.total_rmb.toFixed(2)}</span>` : ""}
        ${r.has_final_human ? `<span>✓人工版</span>` : ""}
      </div>
    </div>
  `).join("");
  el.querySelectorAll(".run-item").forEach((node) => {
    node.addEventListener("click", () => selectRun(node.dataset.run));
  });
}

async function selectRun(runId) {
  state.currentRunId = runId;
  state.logsByShot = {};
  renderRunList();
  document.getElementById("empty-hint").style.display = "none";
  document.getElementById("run-view").style.display = "";
  state.currentRun = await api(`/api/runs/${runId}`);
  renderRunHeader();
  renderShotsTab();
  renderCostTab();
}

function renderRunHeader() {
  const run = state.currentRun;
  const brief = run.brief || {};
  document.getElementById("run-title").textContent = brief.title || run.run_id;
  document.getElementById("run-sub").textContent =
    `${run.run_id} · ${brief.category || "未知品类"} · ${brief.target_platform || ""} · ${brief.aspect_ratio || ""}`;
  document.getElementById("btn-view-final").disabled = !run.has_final;
  document.getElementById("btn-view-final-human").disabled = !run.has_final_human;
}

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".tab").forEach((el) => el.classList.toggle("active", el.dataset.tab === tab));
  document.getElementById("tab-shots").style.display = tab === "shots" ? "" : "none";
  document.getElementById("tab-cost").style.display = tab === "cost" ? "" : "none";
}

// ---------------------------------------------------------------------------
// 镜头审核 tab
// ---------------------------------------------------------------------------

function statusBadge(shot) {
  const human = shot.human;
  if (human) {
    return human.decision === "approved"
      ? `<span class="badge ok">人工通过 · a${human.attempt}</span>`
      : `<span class="badge bad">人工打回</span>`;
  }
  const o = shot.outcome;
  if (!o) return `<span class="badge muted">无生成记录</span>`;
  if (o.passed) return `<span class="badge ok">机审通过</span>`;
  if (o.escalated) return `<span class="badge warn">升级人工</span>`;
  return `<span class="badge bad">机审未过</span>`;
}

function renderShotsTab() {
  const run = state.currentRun;
  const container = document.getElementById("tab-shots");
  if (!run.shots.length) {
    container.innerHTML = `<div class="empty-hint">该 run 没有镜头数据</div>`;
    return;
  }
  container.innerHTML = run.shots.map(renderShotCard).join("");

  run.shots.forEach((shot) => {
    const card = document.getElementById(`shot-${shot.shot_id}`);
    if (!card) return;
    card.querySelector(".attempt-select")?.addEventListener("change", (e) => {
      onAttemptChange(shot.shot_id, parseInt(e.target.value, 10));
    });
    card.querySelector(".btn-approve")?.addEventListener("click", () => onReview(shot.shot_id, "approved"));
    card.querySelector(".btn-reject")?.addEventListener("click", () => onReview(shot.shot_id, "rejected"));
  });

  // 默认展开每个镜头当前最相关的 attempt 的判官详情
  run.shots.forEach((shot) => {
    const attempt = defaultAttempt(shot);
    if (attempt != null) loadVerdict(shot.shot_id, attempt);
  });
}

function defaultAttempt(shot) {
  if (shot.human?.attempt) return shot.human.attempt;
  if (shot.outcome?.passed) return shot.outcome.attempts;
  if (shot.attempts_available.length) return shot.attempts_available[shot.attempts_available.length - 1];
  return null;
}

function renderShotCard(shot) {
  const m = shot.meta || {};
  const attempt = state.selectedAttempt[shot.shot_id] ?? defaultAttempt(shot);
  const hasVideo = attempt != null && shot.attempts_available.includes(attempt);

  const attemptOptions = shot.attempts_available.map(
    (a) => `<option value="${a}" ${a === attempt ? "selected" : ""}>第 ${a} 次尝试</option>`
  ).join("");

  return `
  <div class="shot-card" id="shot-${escapeHtml(shot.shot_id)}">
    <div class="shot-card-head">
      <h3>${escapeHtml(shot.shot_id)}</h3>
      ${statusBadge(shot)}
    </div>
    <div class="shot-body">
      <dl class="field-grid">
        <dt>场景</dt><dd>${escapeHtml(m.scene)}</dd>
        <dt>主体动作</dt><dd>${escapeHtml(m.subject)}</dd>
        <dt>情绪</dt><dd>${escapeHtml(m.emotion)}</dd>
        <dt>运镜</dt><dd>${escapeHtml(CAMERA_LABEL[m.camera] || m.camera)} ${escapeHtml(m.camera_note || "")}</dd>
        <dt>台词</dt><dd>${escapeHtml(m.dialogue || "—")}</dd>
        <dt>字幕</dt><dd>${escapeHtml(m.on_screen_text || "—")}</dd>
        <dt>时长</dt><dd>${m.duration_s ?? "?"}s</dd>
      </dl>
      <div>
        ${shot.attempts_available.length ? `
          <div class="attempt-row">
            <label>查看尝试：</label>
            <select class="attempt-select">${attemptOptions}</select>
          </div>
          ${hasVideo ? `<video controls src="/api/runs/${state.currentRunId}/media/shot/${shot.shot_id}/${attempt}"></video>` : ""}
        ` : `<div class="empty-hint" style="padding:20px">没有可播放的视频</div>`}
        <div class="verdict-box" id="verdict-${escapeHtml(shot.shot_id)}">加载判官结果中…</div>
        <div class="review-row">
          <input type="text" class="reviewer-input" placeholder="审核人" value="${escapeHtml(localStorage.getItem("reviewer") || "")}" />
          <textarea class="note-input" rows="1" placeholder="打回理由 / 备注（可选）"></textarea>
          <button class="ok btn-approve" ${hasVideo ? "" : "disabled"}>通过第 ${attempt ?? "-"} 次</button>
          <button class="bad btn-reject">打回</button>
        </div>
        ${shot.human ? `<div class="human-status">
          ${escapeHtml(shot.human.reviewer || "匿名")} 于 ${escapeHtml((shot.human.ts || "").replace("T", " ").slice(0, 19))}
          ${shot.human.note ? "：" + escapeHtml(shot.human.note) : ""}
        </div>` : ""}
      </div>
    </div>
  </div>`;
}

function onAttemptChange(shotId, attempt) {
  state.selectedAttempt[shotId] = attempt;
  renderShotsTab();
  loadVerdict(shotId, attempt);
}

async function loadVerdict(shotId, attempt) {
  const box = document.getElementById(`verdict-${shotId}`);
  if (!box) return;
  try {
    if (!state.logsByShot[shotId]) {
      state.logsByShot[shotId] = await api(`/api/runs/${state.currentRunId}/shots/${shotId}/logs`);
    }
    const entry = state.logsByShot[shotId].find((e) => e.attempt === attempt);
    box.innerHTML = renderVerdict(entry);
  } catch (e) {
    box.textContent = "判官结果加载失败：" + e.message;
  }
}

function renderVerdict(entry) {
  if (!entry) return "该次尝试没有日志记录";
  const v = entry.verdict;
  const gen = entry.generation;
  if (!gen?.ok) {
    return `生成失败：${escapeHtml(gen?.error || "未知错误")}`;
  }
  if (!v) return "该次生成没有判官记录";
  const items = Object.entries(v.item_results || {}).map(
    ([k, ok]) => `<span class="check-chip ${ok ? "pass" : "fail"}">${ok ? "✓" : "✗"} ${escapeHtml(k)}</span>`
  ).join("");
  return `
    <div><strong>得分 ${v.score}</strong>（${v.passed ? "通过" : "未通过"} 阈值）</div>
    <div class="verdict-items">${items}</div>
    ${v.critique ? `<div>批注：${escapeHtml(v.critique)}</div>` : ""}
    ${gen.cost_estimate_rmb ? `<div>本次生成成本估算：¥${gen.cost_estimate_rmb.toFixed(2)}</div>` : ""}
  `;
}

async function onReview(shotId, decision) {
  const card = document.getElementById(`shot-${shotId}`);
  const reviewer = card.querySelector(".reviewer-input").value.trim();
  const note = card.querySelector(".note-input").value.trim();
  if (reviewer) localStorage.setItem("reviewer", reviewer);

  const shot = state.currentRun.shots.find((s) => s.shot_id === shotId);
  const attempt = state.selectedAttempt[shotId] ?? defaultAttempt(shot);
  if (decision === "approved" && attempt == null) {
    alert("没有可确认的视频尝试");
    return;
  }

  try {
    await api(`/api/runs/${state.currentRunId}/shots/${shotId}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, attempt: decision === "approved" ? attempt : null, note, reviewer }),
    });
    state.currentRun = await api(`/api/runs/${state.currentRunId}`);
    renderShotsTab();
    renderRunHeader();
    refreshRuns();
  } catch (e) {
    alert("提交失败：" + e.message);
  }
}

async function onAssemble() {
  const btn = document.getElementById("btn-assemble");
  btn.disabled = true;
  btn.textContent = "装配中…（ffmpeg 拼接，可能需要几秒）";
  try {
    const result = await api(`/api/runs/${state.currentRunId}/assemble`, { method: "POST" });
    state.currentRun = await api(`/api/runs/${state.currentRunId}`);
    renderRunHeader();
    refreshRuns();
    const skipped = result.skipped.length
      ? `\n跳过 ${result.skipped.length} 个镜头：\n` + result.skipped.map((s) => `${s.shot_id}（${s.reason}）`).join("\n")
      : "";
    alert(`人工确认版成片已生成：${result.path}\n使用 ${result.used.length} 个镜头${skipped}`);
  } catch (e) {
    alert("装配失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "生成人工确认版成片";
  }
}

// ---------------------------------------------------------------------------
// 成本看板 tab
// ---------------------------------------------------------------------------

const STAGE_LABEL = {
  video: "视频生成（含重试）",
  llm_storyboard: "分镜 LLM",
  llm_surgeon: "Prompt 外科医生 LLM",
  judge: "VLM 判官",
};

function renderCostTab() {
  const cost = state.currentRun.cost;
  const container = document.getElementById("tab-cost");
  const stageEntries = Object.entries(cost.by_stage_rmb || {});
  const maxStage = Math.max(1e-9, ...stageEntries.map(([, v]) => v));
  const shotEntries = Object.entries(cost.by_shot_rmb || {}).sort((a, b) => b[1] - a[1]);
  const maxShot = Math.max(1e-9, ...shotEntries.map(([, v]) => v));

  container.innerHTML = `
    ${cost.legacy ? `<div class="legacy-note">该 run 早于成本台账功能，只有视频成本，没有 LLM/判官明细。</div>` : ""}
    <div class="cost-summary">
      <div class="stat-card"><div class="num">¥${(cost.total_rmb || 0).toFixed(2)}</div><div class="label">全链路总成本</div></div>
      <div class="stat-card"><div class="num">${cost.rmb_per_final_second != null ? "¥" + cost.rmb_per_final_second.toFixed(3) : "—"}</div><div class="label">每成片秒成本</div></div>
      <div class="stat-card"><div class="num">${cost.final_duration_s ?? "—"}</div><div class="label">成片时长(s)</div></div>
      <div class="stat-card"><div class="num">${cost.event_count ?? 0}</div><div class="label">花费事件数</div></div>
    </div>

    <div class="section-title">按阶段</div>
    ${stageEntries.map(([stage, v]) => barRow(STAGE_LABEL[stage] || stage, v, maxStage)).join("") || `<div class="empty-hint">无数据</div>`}

    <div class="section-title">按镜头</div>
    ${shotEntries.map(([shot, v]) => barRow(shot, v, maxShot)).join("") || `<div class="empty-hint">无数据</div>`}

    <div class="section-title">花费事件明细</div>
    <div id="cost-events">加载中…</div>
  `;

  loadCostEvents();
}

function barRow(label, value, max) {
  const pct = Math.max(2, Math.round((value / max) * 100));
  return `
    <div class="bar-row">
      <div>${escapeHtml(label)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
      <div>¥${value.toFixed(3)}</div>
    </div>`;
}

async function loadCostEvents() {
  const el = document.getElementById("cost-events");
  if (!el) return;
  try {
    const events = await api(`/api/runs/${state.currentRunId}/cost/events`);
    if (!events.length) { el.innerHTML = `<div class="empty-hint">无事件记录</div>`; return; }
    el.innerHTML = `
      <table class="events">
        <thead><tr><th>阶段</th><th>镜头</th><th>成本(¥)</th><th>模型</th><th>tokens_in</th><th>tokens_out</th></tr></thead>
        <tbody>
          ${events.map((e) => `
            <tr>
              <td>${escapeHtml(STAGE_LABEL[e.stage] || e.stage)}</td>
              <td>${escapeHtml(e.shot_id || "—")}</td>
              <td>${(e.cost_rmb || 0).toFixed(4)}</td>
              <td>${escapeHtml(e.model || "—")}</td>
              <td>${e.tokens_in ?? "—"}</td>
              <td>${e.tokens_out ?? "—"}</td>
            </tr>`).join("")}
        </tbody>
      </table>`;
  } catch (e) {
    el.textContent = "加载失败：" + e.message;
  }
}

boot();
