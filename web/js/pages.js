/* ================= 六大页面渲染器 ================= */
/* 依赖: api.js 的 State、$、esc、fmt 系列、toast、makeTabs + charts.js + app.js 的 App */

const TONE = { ok: "var(--success)", warning: "var(--warning)", danger: "var(--danger)", flat: "var(--text-2)" };

/* 引擎参数唯一来源：/api/bootstrap 的 engine_config，展示层禁止硬编码 */
function engineCfg() {
  const fb = { thermal: { capacity_kj_k: 15000, resistance_k_w: 0.001, internal_resistance_mohm: 18, efficiency_pct: 95, temp_normal_max: 45, temp_safe_max: 55 },
               price_periods: [] };
  return (State.boot && State.boot.engine_config) || fb;
}

/* ---------- 共享：顶部操作栏 ---------- */
function opBarHTML(withExport) {
  const dates = State.boot.dates;
  const sel = dates.includes(State.boot.selected_date) ? State.boot.selected_date
    : dates[Math.min(7, dates.length - 1)]; // 短数据集（<8 天）不再取到 undefined
  return `<div class="op-bar">
    <label>调度日期</label>
    <select id="op-date">${dates.map((d) => `<option ${d === sel ? "selected" : ""}>${esc(d)}</option>`).join("")}</select>
    <button class="btn btn-primary" id="op-run">🚀 运行调度</button>
    ${withExport ? `<span class="op-spacer"></span><a class="btn" id="op-export" download>📥 导出调度结果CSV</a>` : `<span class="op-spacer"></span>`}
  </div>`;
}
/* 页内 subtab 切换逻辑收敛为单一辅助函数——热管理页与需求响应页此前各自复制了一份相同实现 */
function bindSubTabs(root) {
  $$(".subtab", root).forEach((b) => b.addEventListener("click", () => {
    $$(".subtab", root).forEach((x) => x.classList.remove("active"));
    $$(".tabpane", root).forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $(`.tabpane[data-t="${b.dataset.t}"]`, root).classList.add("active");
    reflowCharts();
  }));
}

function bindOpBar(root, withExport) {  $("#op-date", root).addEventListener("change", (e) => { State.boot.selected_date = e.target.value; });
  $("#op-run", root).addEventListener("click", () => App.runSolve());
  if (withExport) $("#op-export", root).addEventListener("click", async (e) => {
    e.preventDefault();
    // location.href 导航无法携带 JWT 头 → 401；改走带认证的 blob 下载
    const btn = e.currentTarget;
    if (State.solving) { toast("求解进行中，请等进度条完成后导出", "err"); return; }
    btn.disabled = true;
    try {
      await API.download("/api/export/schedule");
      toast("已导出调度结果 CSV", "ok");
    } catch (err) {
      toast("导出失败：" + err.message, "err");
    } finally { btn.disabled = false; }
  });
}

/* ---------- 共享：AI 决策解释（总览页） ---------- */
const AI_SOURCE_LABEL = { llm: "🤖 LLM 生成", rule: "📋 规则模板", none: "⚪ 未生成" };
function aiSourceLabel(source) {
  return AI_SOURCE_LABEL[source] || source || "—";
}
/* head 内部内容单独成函数：刷新解释时只需替换 innerHTML，
   保留 <button> 元素本身（其 click 监听器绑在元素上，替换 outerHTML 会丢失）。 */
function aiExpanderHeadInner(source) {
  return `🧠 AI 决策解释 · ${aiSourceLabel(source)}（点击展开查看）` +
    `<span style="font-size:11px;color:var(--text-3);font-weight:400;">▼</span>`;
}
function aiExpanderHTML(exp) {
  return `<div class="ai-expander${exp.source === "llm" ? " open" : ""}" id="ai-exp">
    <!-- head 为 <button>（键盘可达，原生 Enter/Space 触发）；此前是 div 仅绑 click，键盘用户无法操作 -->
    <button type="button" class="head" aria-expanded="${exp.source === "llm"}">${aiExpanderHeadInner(exp.source)}</button>
    <div class="body">
      <div class="ai-text">${exp.text ? renderMarkdown(exp.text) : "调度图未生成解释（可能未启用解释层）。点击下方按钮即时生成。"}</div>
      <div style="margin-top:12px;display:flex;gap:10px;align-items:center;">
        <button class="btn btn-sm" id="btn-regen-exp">🔄 生成 / 刷新解释</button>
      </div>
      <div class="caption">说明：LLM 不参与计算，只能使用调度结果事实摘要中的数字；输出会做数字回查，未匹配的数字会被标注出来。</div>
    </div>
  </div>`;
}
function bindAiExpander(root) {
  const exp = $("#ai-exp", root);
  const head = $(".head", exp);
  head.addEventListener("click", () => {
    exp.classList.toggle("open");
    head.setAttribute("aria-expanded", exp.classList.contains("open"));
  });
  // 键盘可达：button 原生支持 Enter/Space，无需额外 keydown
  $("#btn-regen-exp", exp).addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; btn.textContent = "🧠 正在生成决策解释...";
    try {
      const r = await API.post("/api/explain");
      $(".ai-text", exp).textContent = r.text;
      // 同步刷新标题来源标签：服务端返回 source（"llm" | "rule" | "none"）。
      // 此前只替换正文，标签会停留在生成前的旧来源（如刚生成完仍显示「⚪ 未生成」）。
      if (r.source) {
        head.innerHTML = aiExpanderHeadInner(r.source);
        head.setAttribute("aria-expanded", String(r.source === "llm"));
        exp.classList.toggle("open", r.source === "llm");
      }
      toast("决策解释已生成", "ok");
    } catch (err) { toast(`解释层异常：${err.message}`, "err"); }
    btn.disabled = false; btn.textContent = "🔄 生成 / 刷新解释";
  });
}

/* ============================================================
   页面1 · 数据总览
   ============================================================ */
function renderDashboard(root, d) {
  const k = d.kpis;
  const tempWarn = k.temp.warn;
  root.innerHTML = `
    ${opBarHTML(true)}
    <div class="page-title">数据总览</div>
    <div class="page-subtitle">工商业储能多智能体优化调度系统 · 实时运行监控</div>
    ${(d.alerts && d.alerts.length) ? `
      <div style="margin-top:12px;display:flex;flex-direction:column;gap:8px;">
        ${d.alerts.map(a => `<div style="padding:10px 14px;border-radius:10px;
             background:color-mix(in srgb, var(--warning) 14%, transparent);
             border:1px solid color-mix(in srgb, var(--warning) 40%, transparent);
             color:var(--text-1);font-size:13px;line-height:1.6;">⚠️ ${esc(a)}</div>`).join("")}
      </div>` : ""}

    <div class="kpi-grid" style="margin-top:16px;">
      <div class="kpi-card">
        <div class="kpi-value tone-orange">${fmtMoney(k.net.value)}</div>
        <div class="kpi-label">日净收益</div>
        <div class="kpi-delta" style="color:var(--accent);">↑ ${k.net.delta.toFixed(1)}% vs基准</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-value tone-orange">${fmtMoney(k.arb.value)}</div>
        <div class="kpi-label">峰谷套利</div>
        <div class="kpi-delta" style="color:var(--accent);">↑ ${k.arb.delta.toFixed(1)}% vs基准</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-value tone-blue">${k.temp.value.toFixed(1)}℃</div>
        <div class="kpi-label">最高温度</div>
        <div class="kpi-delta" style="color:${tempWarn ? "var(--warning)" : "var(--success)"};">${tempWarn ? "⚠️ 降额运行 (阈值45℃)" : "✅ 安全 (阈值45℃)"}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-value tone-red">${k.cycles.value.toFixed(3)}</div>
        <div class="kpi-label"><span class="term-tip" title="等效循环（EFC）：将不同倍率、不同深度的充放电折算为一次 100% 满充放的完整循环次数，用于衡量电池寿命消耗速度。">等效循环</span></div>
        <div class="kpi-delta" style="color:var(--danger);">衰减 ${fmtMoney(k.cycles.deg)}</div>
      </div>
    </div>

    <div style="margin-top:16px;">${aiExpanderHTML(d.explanation)}</div>

    <div class="section-title">收益对比</div>
    <div class="grid grid-2" id="ov-row">
      <div class="chart-card"><div class="chart-title">日收益构成（元）</div><div class="chart" id="ov-waterfall" style="height:264px;"></div></div>
      <div class="panel-card">
        <div style="overflow-x:auto;"><table class="dtable"><thead><tr><th scope="col">指标</th><th class="r">基准策略</th><th class="r">优化策略</th><th class="r">变化</th></tr></thead>
        <tbody>${d.comp_rows.map((r) => {
          let impHtml;
          if (r.status === "flat") impHtml = `<span class="val-flat">— 持平</span>`;
          else if (r.name === "套利收益" || r.name === "净收益") {
            const up = r.imp > 0;
            impHtml = `<span class="${up ? "val-good-rev" : "val-bad"}">${up ? "↑" : "↓"} ${Math.abs(r.imp).toFixed(1)}%</span>`;
          } else {
            impHtml = r.imp > 0
              ? `<span class="val-bad">↑ 增幅 ${Math.abs(r.imp).toFixed(1)}%</span>`
              : `<span class="val-good-cut">↓ ${Math.abs(r.imp).toFixed(1)}%</span>`;
          }
          return `<tr><td>${esc(r.name)}</td><td class="r">${r.base}</td><td class="r">${r.opt}</td><td class="r">${impHtml}</td></tr>`;
        }).join("")}</tbody></table></div>
        <div class="info-note" style="margin-top:12px;">📈 <b>年化估算</b>：套利${d.annual.arb}万 + DR${d.annual.dr}万(按 50 天/年) - 衰减${d.annual.deg}万 = <b>净收益${d.annual.net}万元/年</b></div>
      </div>
    </div>

    <div class="section-title">24小时调度概览</div>
    <div class="chart-card"><div class="chart" id="ov-sched24" style="height:560px;"></div></div>
    <div class="caption">💡 阴影区域为需求响应(DR)时段</div>
  `;
  bindOpBar(root, true);
  bindAiExpander(root);
  chartWaterfall($("#ov-waterfall", root), d.waterfall);
  chartSched24($("#ov-sched24", root), d);
}

/* ============================================================
   页面2 · 充放电调度
   ============================================================ */
function renderScheduling(root, d) {
  const k = d.kpis;
  root.innerHTML = `
    ${opBarHTML(false)}
    <div class="page-title">充放电调度详情</div>
    <div class="page-subtitle">MILP混合整数线性规划 · 热约束+寿命约束联合优化</div>

    <div class="kpi-grid price-4" style="margin-top:16px;">
      ${d.price_cards.map((c) => `
      <div class="price-card">
        <div class="top"><span class="tag2" style="color:${TONE[c.tone]};">${c.tag}</span><span class="rng">${c.range}</span></div>
        <div class="pv">${c.price.toFixed(2)}<small> 元/kWh</small></div>
      </div>`).join("")}
    </div>
    <div class="caption">💡 充电集中在低谷/平段，放电集中在尖峰/高峰，价差即为套利空间</div>

    <div style="display:flex;gap:10px;margin-top:14px;">
      <button class="btn" id="btn-reopt">🔄 重新优化</button>
      <a class="btn" id="btn-export-plan" download>📥 导出计划</a>
    </div>

    <div class="kpi-grid" style="margin-top:14px;">
      <div class="kpi-card"><div class="kpi-value tone-orange">${fmtMoney(k.arb)}</div><div class="kpi-label">日套利收益</div></div>
      <div class="kpi-card"><div class="kpi-value tone-orange">${fmtNum(k.charge)}kWh</div><div class="kpi-label">充电量</div><div class="kpi-sub">额定功率${k.rated_power}kW</div></div>
      <div class="kpi-card"><div class="kpi-value tone-blue">${fmtNum(k.discharge)}kWh</div><div class="kpi-label">放电量</div><div class="kpi-sub">峰值功率${k.peak_discharge.toFixed(0)}kW</div></div>
      <div class="kpi-card"><div class="kpi-value tone-warn">${k.rt_eff.toFixed(1)}%</div><div class="kpi-label">往返效率</div>
        <div class="kpi-sub term-tip" title="放电=${k.discharge.toFixed(0)}kWh, 有效充电=${k.effective_charge.toFixed(0)}kWh(含初始储能释放${k.released.toFixed(0)}kWh)">含初始储能释放${k.released.toFixed(0)}kWh</div></div>
    </div>

    <div class="section-title">24小时充放电计划与SOC轨迹</div>
    <div class="grid grid-3-2">
      <div class="chart-card"><div class="chart" id="sc-main" style="height:460px;"></div></div>
      <div>
        <div class="panel-card">
          <div class="panel-title">🎯 优化目标</div>
          <div style="font-size:13px;line-height:1.7;margin-bottom:8px;font-weight:600;">最大化净收益：<br>峰谷套利收益 ＋ 需求响应补贴 − 电池衰减成本</div>
          <details>
            <summary style="cursor:pointer;color:var(--accent);font-size:12px;">▸ 查看数学公式</summary>
            <div style="font-family:JetBrains Mono,monospace;margin-top:6px;line-height:1.7;color:var(--text-3);font-size:11px;">
            max 净收益 =<br>套利收益 + DR补贴 − 衰减成本<br>s.t. SOC∈[${d.soc_range[0]}%,${d.soc_range[1]}%]<br>&nbsp;&nbsp;&nbsp;&nbsp;功率 ≤ ${k.rated_power}kW<br>&nbsp;&nbsp;&nbsp;&nbsp;温度 ≤ 45℃(降额)</div>
          </details>
        </div>
        <div class="panel-card" style="margin-top:12px;">
          <div class="panel-title">📐 约束参数</div>
          <div class="metric-row"><span class="metric-label">SOC范围</span><span class="metric-value">${d.soc_range[0]}% - ${d.soc_range[1]}%</span></div>
          <div class="metric-row"><span class="metric-label">功率上限</span><span class="metric-value">≤ ${k.rated_power}kW</span></div>
          <div class="metric-row"><span class="metric-label">温度约束</span><span class="metric-value">≤45℃降额</span></div>
          <details style="margin-top:10px;">
            <summary style="cursor:pointer;color:var(--accent);font-size:12px;">✏️ 修改参数后重新优化</summary>
            <div style="margin-top:10px;display:flex;flex-direction:column;gap:12px;">
              <div class="field"><label>最低SOC (%) <b class="num" id="v-socmin">${d.soc_range[0]}</b></label>
                <input type="range" id="sp-socmin" min="10" max="40" value="${d.soc_range[0]}"></div>
              <div class="field"><label>最高SOC (%) <b class="num" id="v-socmax">${d.soc_range[1]}</b></label>
                <input type="range" id="sp-socmax" min="70" max="100" value="${d.soc_range[1]}"></div>
              <div class="field"><label>额定功率 (kW) <b class="num" id="v-power">${k.rated_power}</b></label>
                <input type="range" id="sp-power" min="500" max="2000" step="100" value="${k.rated_power}"></div>
              <button class="btn btn-primary btn-sm" id="sp-apply">✅ 应用并重新优化</button>
            </div>
          </details>
        </div>
      </div>
    </div>

    <div class="section-title">策略对比</div>
    ${d.compare.opt.time_limit_hit ? `<div class="panel-card" style="border-color:var(--warning,#f59e0b);margin-bottom:10px;">
        <div class="metric-row"><span class="metric-label">⚠️ 求解状态</span>
        <span class="metric-value" style="color:var(--warning,#f59e0b);">次优解（${esc(d.compare.opt.solver_status)} · gap ${d.compare.opt.mip_gap_pct}%）</span></div>
      </div>` : ""}
    <div class="vs-grid">
      <div class="panel-card panel-hl">
        <div class="panel-title" style="color:var(--accent);">✅ 优化策略（本系统）</div>
        <div class="metric-row"><span class="metric-label">套利收益</span><span class="metric-value" style="color:var(--accent);">${fmtMoney(d.compare.opt.arbitrage_revenue_yuan)}</span></div>
        <div class="metric-row"><span class="metric-label">最高温度</span><span class="metric-value">${d.compare.opt.max_battery_temp_c.toFixed(1)}℃</span></div>
        <div class="metric-row"><span class="metric-label">等效循环</span><span class="metric-value">${d.compare.opt.equivalent_cycles.toFixed(3)}</span></div>
        <div class="metric-row"><span class="metric-label">DR补贴</span><span class="metric-value">${fmtMoney(d.compare.opt.dr_subsidy_yuan)}</span></div>
      </div>
      <div class="vs-arrow"><div class="arr">→</div><div class="pct">+${d.compare.arb_imp.toFixed(1)}%</div><div class="lbl">套利收益增幅</div></div>
      <div class="panel-card">
        <div class="panel-title" style="color:var(--text-3);">📉 基准策略（低谷充满高峰放完）</div>
        <div class="metric-row"><span class="metric-label">套利收益</span><span class="metric-value">${fmtMoney(d.compare.base.arbitrage_revenue_yuan)}</span></div>
        <div class="metric-row"><span class="metric-label">最高温度</span><span class="metric-value" style="color:var(--danger);">${d.compare.base.max_battery_temp_c.toFixed(1)}℃</span></div>
        <div class="metric-row"><span class="metric-label">等效循环</span><span class="metric-value">${d.compare.base.equivalent_cycles.toFixed(3)}</span></div>
        <div class="metric-row"><span class="metric-label">DR补贴</span><span class="metric-value">-</span></div>
      </div>
    </div>
  `;
  bindOpBar(root, false);
  $("#btn-reopt", root).addEventListener("click", () => App.runSolve({ force: true }));
  $("#btn-export-plan", root).addEventListener("click", async (e) => {
    e.preventDefault();
    const btn = e.currentTarget;
    if (State.solving) { toast("求解进行中，请等进度条完成后导出", "err"); return; }
    btn.disabled = true;
    try {
      await API.download("/api/export/plan");
      toast("已导出调度计划 CSV", "ok");
    } catch (err) {
      toast("导出失败：" + err.message, "err");
    } finally { btn.disabled = false; }
  });
  // 滑杆联动
  [["sp-socmin", "v-socmin"], ["sp-socmax", "v-socmax"], ["sp-power", "v-power"]].forEach(([i, v]) => {
    $("#" + i, root).addEventListener("input", (e) => { $("#" + v, root).textContent = e.target.value; });
  });
  $("#sp-apply", root).addEventListener("click", async () => {
    await API.post("/api/params", {
      soc_min: Number($("#sp-socmin", root).value), soc_max: Number($("#sp-socmax", root).value),
      rated_power: Number($("#sp-power", root).value), clear_cache: true,
    });
    State.boot.params = { ...State.boot.params };
    toast("参数已应用，正在重新优化…");
    App.runSolve();
  });
  chartPowerSoc($("#sc-main", root), d);
}

/* ============================================================
   页面3 · 负荷预测
   ============================================================ */
function renderForecast(root, d) {
  const k = d.kpis;
  const physStr = (k.phys_imp >= 0 ? "+" : "") + k.phys_imp.toFixed(1) + "%";
  root.innerHTML = `
    <div class="page-title">负荷预测</div>
    <div class="page-subtitle">XGBoost时序预测 · 传热学温度物理修正 · 96点15分钟粒度</div>

    <div style="display:flex;gap:10px;margin-top:14px;">
      <button class="btn" id="btn-refc">🔄 重新预测</button>
      <button class="btn" id="btn-mccfg">⚙️ 模型配置</button>
    </div>
    <div class="ai-expander" id="fc-mccfg" style="display:none;margin-top:12px;">
      <div class="head">📊 XGBoost模型配置<span style="font-size:11px;color:var(--text-3);font-weight:400;">▼</span></div>
      <div class="body">
        <div class="grid grid-2" style="gap:12px 18px;">
          <div class="field"><label>最大树深度 (3-12)</label><input type="number" id="xgb-depth" min="3" max="12" value="${State.boot.params.xgb_max_depth}"></div>
          <div class="field"><label>启用温度物理修正</label><label class="switch"><input type="checkbox" id="xgb-tc" ${State.boot.params.xgb_temp_corr ? "checked" : ""}><span class="track"></span></label></div>
          <div class="field"><label>学习率 (0.01-0.5)</label><input type="number" id="xgb-lr" min="0.01" max="0.5" step="0.01" value="${State.boot.params.xgb_lr}"></div>
          <div class="field"><label>启用工艺负荷保底</label><label class="switch"><input type="checkbox" id="xgb-floor" ${State.boot.params.xgb_floor ? "checked" : ""}><span class="track"></span></label></div>
          <div class="field"><label>估计器数量 (50-500)</label><input type="number" id="xgb-n" min="50" max="500" step="10" value="${State.boot.params.xgb_n_est}"></div>
          <div class="field"><label>启用递归预测</label><label class="switch"><input type="checkbox" id="xgb-rec" ${State.boot.params.xgb_recursive ? "checked" : ""}><span class="track"></span></label></div>
        </div>
        <div class="caption">修改配置后点击「重新预测」生效</div>
      </div>
    </div>

    <div class="kpi-grid" style="margin-top:16px;">
      <div class="kpi-card"><div class="kpi-value tone-blue">${k.mape.toFixed(1)}%</div>
        <div class="kpi-label"><span class="term-tip" title="MAPE（平均绝对百分比误差）：预测值与真实值偏差的平均百分比，越低越准。10%以内对调度决策已经足够可靠。">预测MAPE</span></div></div>
      <div class="kpi-card"><div class="kpi-value tone-blue">${fmtNum(k.peak)}kW</div><div class="kpi-label">峰值负荷</div>
        ${k.is_custom ? `<div style="font-size:9px;color:var(--warning);margin-top:3px;">⚠ 基于上传数据</div>` : ""}</div>
      <div class="kpi-card"><div class="kpi-value tone-blue">${fmtNum(k.valley)}kW</div><div class="kpi-label">谷值负荷</div></div>
      <div class="kpi-card"><div class="kpi-value tone-warn">${physStr}</div>
        <div class="kpi-label"><span class="term-tip" title="引入传热学温度修正后，预测误差相对纯AI拟合下降的百分比。">物理修正增益</span></div></div>
    </div>

    <div class="section-title">次日96点负荷预测曲线</div>
    <div class="grid grid-3-2">
      <div class="chart-card"><div class="chart" id="fc-main" style="height:380px;"></div></div>
      <div>
        <div class="chart-card"><div class="chart-title">XGBoost特征重要性（模型真实输出）</div><div class="chart" id="fc-fi" style="height:210px;"></div></div>
        ${d.feature_importance.length === 0 ? `
        <div class="empty-state" style="margin-top:12px;">
          <div class="ico">🧠</div><div class="t">模型尚未训练</div>
          <div class="d">开启 XGBoost 负荷预测并运行一次调度后，<br>这里会展示模型学到的关键特征（滞后负荷、时段、温度等）</div>
          <button class="btn btn-primary btn-sm" style="margin-top:10px;" id="btn-enable-ml">🚀 去开启预测并运行</button>
        </div>` : ""}
        <div class="panel-card" style="margin-top:12px;">
          <div class="panel-title">🌡️ 传热学物理修正</div>
          <div class="metric-row"><span class="metric-label">商业冷负荷</span><span class="metric-value" style="color:var(--blue);">+4-6%/℃</span></div>
          <div class="metric-row"><span class="metric-label">工业制冷负荷</span><span class="metric-value" style="color:var(--accent);">+2-3%/℃</span></div>
          <div class="metric-row"><span class="metric-label">工艺保底</span><span class="metric-value" style="color:var(--accent);">最低负荷约束</span></div>
          <div class="caption">基于传热学推导，比纯AI拟合准确率提升3-5%</div>
        </div>
      </div>
    </div>

    <div class="section-title">模型解释面板 · 预测是怎么做出来的</div>
    <div class="grid grid-2-1-2">
      <div class="chart-card"><div class="chart-title">误差分布（MAPE）</div><div class="chart" id="fc-hist" style="height:210px;"></div></div>
      <div class="panel-card" style="display:flex;flex-direction:column;justify-content:center;text-align:center;">
        <div class="panel-title" style="text-align:center;">🌡️ 物理修正效果</div>
        <div style="font-size:26px;color:var(--accent);font-weight:700;font-family:var(--font-num);">${physStr}</div>
        <div style="font-size:10px;color:var(--text-3);">准确率提升</div>
        <div class="metric-row" style="margin-top:10px;border:none;"><span class="metric-label">纯AI拟合</span><span class="metric-value" style="color:var(--danger);">MAPE ${k.mape_raw.toFixed(1)}%</span></div>
        <div class="metric-row" style="border:none;"><span class="metric-label">+物理修正</span><span class="metric-value" style="color:var(--accent);">MAPE ${k.mape.toFixed(1)}%</span></div>
      </div>
      <div class="chart-card"><div class="chart-title">日期类型负荷形态</div><div class="chart" id="fc-profile" style="height:210px;"></div></div>
    </div>
  `;
  $("#btn-refc", root).addEventListener("click", async () => {
    // 先收集模型配置面板当前值再重跑
    const p = State.boot.params;
    p.xgb_max_depth = Number($("#xgb-depth", root).value || p.xgb_max_depth);
    p.xgb_lr = Number($("#xgb-lr", root).value || p.xgb_lr);
    p.xgb_n_est = Number($("#xgb-n", root).value || p.xgb_n_est);
    await API.post("/api/params", { xgb_max_depth: p.xgb_max_depth, xgb_lr: p.xgb_lr, xgb_n_est: p.xgb_n_est, clear_cache: true });
    toast("已重新预测");
    App.runSolve();
  });
  $("#btn-mccfg", root).addEventListener("click", () => {
    const box = $("#fc-mccfg", root);
    box.style.display = box.style.display === "none" ? "block" : "none";
  });
  $(".head", $("#fc-mccfg", root)).addEventListener("click", () => $("#fc-mccfg", root).classList.toggle("open"));
  const enBtn = $("#btn-enable-ml", root);
  if (enBtn) enBtn.addEventListener("click", async () => {
    await API.post("/api/params", { use_ml_forecast: true, clear_cache: true });
    toast("已开启 XGBoost 预测，正在重新计算…");
    App.runSolve();
  });
  chartLoad($("#fc-main", root), d, false);
  if (d.feature_importance.length) {
    chartHBar($("#fc-fi", root), d.feature_importance.map((x) => x.name),
      d.feature_importance.map((x) => x.value / 100), { fmt: (v) => (v * 100).toFixed(1) + "%", max: Math.max(...d.feature_importance.map((x) => x.value / 100)) * 1.3 });
  } else {
    chartHBar($("#fc-fi", root), ["（模型未训练）"], [0.0], { fmt: () => "-" });
  }
  // 无实际负荷时后端返回 synthetic=true（空直方图）——明确显示"暂无数据"而非编造分布
  const histEl = $("#fc-hist", root);
  if (d.error_hist_synthetic) {
    const box = histEl.closest ? histEl.closest(".panel-card") : null;
    if (box && box.querySelector(".panel-title")) {
      box.querySelector(".panel-title").insertAdjacentHTML("beforeend",
        ` <span class="status-tag st-warn">暂无实际数据</span>`);
    }
  } else {
    chartHist(histEl, d.error_hist);
  }
  chartProfile($("#fc-profile", root), d.weekday_profile);
}

/* ============================================================
   页面4 · 电池热管理
   ============================================================ */
function socRing(pct) {
  const r = 26, c = 2 * Math.PI * r;
  const off = c * (1 - pct / 100);
  return `<svg width="64" height="64" style="flex-shrink:0;">
    <circle cx="32" cy="32" r="${r}" stroke="${State.theme === "dark" ? "#1c1c26" : "#e5e6eb"}" stroke-width="5" fill="none"/>
    <circle cx="32" cy="32" r="${r}" stroke="var(--accent)" stroke-width="5" fill="none"
      stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}" stroke-linecap="round" transform="rotate(-90 32 32)"/>
    <text x="32" y="37" text-anchor="middle" fill="${State.theme === "dark" ? "#fff" : "#1d2129"}" font-size="14" font-weight="600">${pct.toFixed(0)}%</text></svg>`;
}

function renderThermal(root, d) {
  const eng = State.thermalMode === "eng";
  const r = (eng ? d.eng : d.pure_econ).report;
  const cur = d.current;
  const modeTag = eng
    ? `<span class="status-tag st-ok">✅ 工程可行策略</span>`
    : `<span class="status-tag st-danger">⚠️ 纯经济策略（无热约束）</span>`;
  const tempStatus = r.max_battery_temp_c < 45 ? ["安全", "st-ok", "var(--accent)"]
    : r.max_battery_temp_c < 55 ? ["降额", "st-warn", "var(--warning)"] : ["超温", "st-danger", "var(--danger)"];
  const scopes = { "全天 00-24": [0, 24], "凌晨 00-08": [0, 8], "日间 08-16": [8, 16], "晚峰 16-24": [16, 24] };

  root.innerHTML = `
    <div class="page-title">电池热管理与寿命监控</div>
    <div class="page-subtitle">一阶RC热模型实时仿真 · 工程可行策略 vs 纯经济策略对比</div>

    <div style="display:flex;align-items:center;gap:14px;margin-top:16px;flex-wrap:wrap;">
      <label class="switch"><input type="checkbox" id="th-toggle" ${eng ? "checked" : ""}><span class="track"></span></label>
      <span style="font-size:13px;color:var(--text-2);">🛡️ 工程可行策略（热约束 + 寿命衰减计入目标函数）</span>
      ${modeTag}
    </div>

    <div class="kpi-grid" style="margin-top:14px;">
      <div class="kpi-card">
        <div class="kpi-label">日净收益 NET PROFIT</div>
        <div class="kpi-value" style="color:var(--accent);font-size:29px;">${fmtMoney(r.net_revenue_yuan)}</div>
        <div style="font-size:10px;color:var(--text-3);margin-top:8px;line-height:1.7;">
          <div>套利 ${fmtMoney(r.arbitrage_revenue_yuan)} + DR ${fmtMoney(r.dr_subsidy_yuan)}</div>
          <div style="color:var(--danger);">− 衰减 ${fmtMoney(r.degradation_cost_yuan)}</div></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">日衰减成本 DEGRADATION</div>
        <div class="kpi-value" style="color:var(--danger);">${fmtMoney(r.degradation_cost_yuan)}</div>
        <div style="font-size:10px;color:var(--text-3);margin-top:8px;">循环 ${r.equivalent_cycles.toFixed(3)} · 寿命损耗</div>
      </div>
      <div class="kpi-card" style="display:flex;align-items:center;gap:14px;">
        ${socRing(cur.soc)}
        <div><div class="kpi-label">当前SOC</div>
          <div style="font-size:11px;color:var(--text-3);margin-top:5px;">${cur.label}</div>
          <div style="font-size:10px;color:var(--text-3);margin-top:2px;">区间 ${d.soc_range[0]}%-${d.soc_range[1]}%</div></div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">最高温度 MAX TEMP</div>
        <div class="kpi-value" style="color:${tempStatus[2]};">${r.max_battery_temp_c.toFixed(1)}℃</div>
        <div style="font-size:10px;color:var(--text-3);margin-top:7px;"><span class="status-tag ${tempStatus[1]}">${tempStatus[0]}</span> 阈值45℃</div>
        <div style="font-size:10px;color:var(--text-3);margin-top:2px;">当前 ${cur.temp.toFixed(1)}℃</div>
      </div>
    </div>

    <div class="section-title">24小时多维度监控（96点 · 15分钟粒度）</div>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
      <label style="font-size:12px;color:var(--text-3);">时间筛选</label>
      <select id="th-scope" style="background:var(--bg-input);color:var(--text-1);border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:12.5px;">
        ${Object.keys(scopes).map((s) => `<option>${s}</option>`).join("")}
      </select>
    </div>
    <div class="chart-card" style="padding-bottom:0;">
      <div class="subtabs" style="margin-bottom:0;">
        <button class="subtab active" data-t="temp">🌡️ 温度趋势</button>
        <button class="subtab" data-t="load">📈 负荷对比</button>
        <button class="subtab" data-t="power">⚡ 功率与SOC</button>
        <button class="subtab" data-t="rev">💰 收益分解</button>
      </div>
      <div style="padding:6px 0 10px;">
        <div class="tabpane active" data-t="temp"><div class="chart" id="th-temp" style="height:380px;"></div></div>
        <div class="tabpane" data-t="load"><div class="chart" id="th-load" style="height:380px;"></div></div>
        <div class="tabpane" data-t="power"><div class="chart" id="th-power" style="height:380px;"></div></div>
        <div class="tabpane" data-t="rev"><div class="chart" id="th-rev" style="height:380px;"></div></div>
      </div>
    </div>
    <div class="price-legend">${(() => {
      const pps = engineCfg().price_periods || [];
      if (!pps.length) return `<span class="lg" style="opacity:.6;">电价时段未配置（设置页可配置）</span>`;
      return pps.map((pp) => {
        const clsColor = { valley: "rgba(255,138,61,0.35)", flat: "rgba(133,129,143,0.25)", peak: "rgba(255,176,32,0.35)", sharp: "rgba(255,92,92,0.4)" };
        return `<span class="lg"><i style="background:${clsColor[pp.cls] || "rgba(133,129,143,0.25)"};"></i>${esc(pp.name)} ${(pp.price ?? 0).toFixed ? pp.price.toFixed(2) : pp.price}元/kWh</span>`;
      }).join("");
    })()}</div>

    <div class="section-title">📋 需求响应事件日志（24小时调度记录）</div>
    <div class="table-wrap"><table class="dtable">
      <thead><tr><th scope="col">时间</th><th scope="col">事件类型</th><th scope="col">动作</th><th scope="col">热安全校验</th><th style="text-align:center;">结果</th><th class="r">净收益</th></tr></thead>
      <tbody>${d.dr_log.map((row) => {
        const rej = row.status === "拒绝";
        return `<tr style="${rej ? "background:var(--danger-soft);" : ""}">
          <td class="num">${esc(row.time)}</td><td>${esc(row.type)}</td><td>${esc(row.action)}</td>
          <td>${esc(row.thermal)}</td>
          <td style="text-align:center;"><span class="status-tag ${rej ? "st-danger" : "st-ok"}">${esc(row.status)}</span></td>
          <td class="r" style="color:${rej ? "var(--text-3)" : "var(--accent)"};font-weight:600;">${esc(row.net)}</td></tr>`;
      }).join("")}</tbody></table></div>
    <div class="caption">💡 需求响应Agent执行三重校验：热安全校验 → 用能底线校验 → 收益校验，净收益为正且安全可行才执行</div>

    <div class="section-title">🔬 热模型与寿命衰减参数</div>
    <div class="grid grid-2">
      <div class="panel-card">
        <div class="panel-title">一阶RC热模型</div>
        <div style="font-family:JetBrains Mono,monospace;font-size:10px;color:var(--text-3);margin-bottom:10px;">
          C·dT/dt = I²R + Q_reaction - (T-T_amb)/R_th</div>
        <div class="metric-row"><span class="metric-label">热容 C</span><span class="metric-value">${engineCfg().thermal.capacity_kj_k} kJ/K</span></div>
        <div class="metric-row"><span class="metric-label">热阻 R_th</span><span class="metric-value">${engineCfg().thermal.resistance_k_w} K/W</span></div>
        <div class="metric-row"><span class="metric-label">内阻 R</span><span class="metric-value">${engineCfg().thermal.internal_resistance_mohm} mΩ</span></div>
        <div class="metric-row"><span class="metric-label">环境温度</span><span class="metric-value">${d.ambient_mean.toFixed(1)}℃</span></div>
        <div class="metric-row"><span class="metric-label">降额阈值</span><span class="metric-value" style="color:var(--accent);">${engineCfg().thermal.temp_normal_max}℃</span></div>
        <div class="metric-row"><span class="metric-label">停止阈值</span><span class="metric-value" style="color:var(--danger);">${engineCfg().thermal.temp_safe_max}℃</span></div>
      </div>
      <div class="chart-card"><div class="chart" id="th-decay" style="height:230px;"></div></div>
    </div>
  `;
  // 交互
  $("#th-toggle", root).addEventListener("change", (e) => {
    State.thermalMode = e.target.checked ? "eng" : "econ";
    App.renderPage("thermal", true);
  });
  $("#th-scope", root).addEventListener("change", (e) => {
    State.thermalScope = scopes[e.target.value];
    // 重绘温度/负荷/功率三个图的范围
    State.charts.forEach((c) => c.dispose()); State.charts = [];
    mountThermalCharts(root, d);
  });
  // 页内 tabs（复用 bindSubTabs，删除重复实现）
  bindSubTabs(root);
  mountThermalCharts(root, d);
}

function mountThermalCharts(root, d) {
  const eng = State.thermalMode === "eng";
  const r = (eng ? d.eng : d.pure_econ).report;
  chartTemp($("#th-temp", root), d, State.thermalScope);
  chartLoad($("#th-load", root), d, true, eng ? "eng" : "pure_econ");
  chartPowerDual($("#th-power", root), d);
  chartWaterfall($("#th-rev", root), {
    x: ["峰谷套利", "DR补贴", "衰减成本", "净收益"],
    y: [r.arbitrage_revenue_yuan, r.dr_subsidy_yuan, -r.degradation_cost_yuan],
    total: r.net_revenue_yuan,
  }, true);
  chartHBar($("#th-decay", root), ["0-20%", "20-50%", "50-80%", "80-100%"], [3.0, 1.5, 1.0, 1.8], {
    title: "SOC区间寿命衰减系数", showX: true, max: 3.6,
    fmt: (v) => v.toFixed(1) + "x",
  });
  reflowCharts();
}

/* ============================================================
   页面5 · 需求响应
   ============================================================ */
function renderDemandResponse(root, d) {
  root.innerHTML = `
    <div class="page-title">需求响应管理</div>
    <div class="page-subtitle">电网DR信号实时接入 · 三重可行性校验</div>

    <div class="chart-card" style="margin-top:16px;padding-bottom:12px;">
      <div class="subtabs" style="margin-bottom:0;">
        <button class="subtab active" data-t="manual">⚡ 手动触发</button>
        <button class="subtab" data-t="hist">📋 历史记录</button>
      </div>
      <div style="padding-top:14px;">
        <div class="tabpane active" data-t="manual">
          <div class="grid grid-3">
            <div style="display:flex;flex-direction:column;gap:10px;">
              <div class="field"><label>开始时间</label><input type="time" id="dr-start" value="15:00"></div>
              <div class="field"><label>结束时间</label><input type="time" id="dr-end" value="17:00"></div>
            </div>
            <div style="display:flex;flex-direction:column;gap:10px;">
              <div class="field"><label>目标削减功率(kW) (100-2000)</label><input type="number" id="dr-target" min="100" max="2000" step="50" value="400"></div>
              <div class="field"><label>补贴单价(元/kWh) (0.1-5.0)</label><input type="number" id="dr-subsidy" min="0.1" max="5" step="0.1" value="0.8"></div>
            </div>
            <div style="display:flex;flex-direction:column;gap:10px;">
              <div class="field"><label>DR类型</label>
                <select id="dr-type"><option>削峰</option><option>填谷</option><option>备用</option></select></div>
              <button class="btn btn-primary" id="dr-confirm" style="margin-top:auto;">✅ 确认触发</button>
            </div>
          </div>
        </div>
        <div class="tabpane" data-t="hist">
          ${d.history.length ? `
          <div class="table-wrap" style="border:none;"><table class="dtable">
            <thead><tr><th scope="col">时间</th><th scope="col">类型</th><th class="r">目标(kW)</th><th class="r">实际响应(kWh)</th><th class="r">补贴(元)</th><th style="text-align:center;">状态</th><th scope="col">拒绝原因</th></tr></thead>
            <tbody>${d.history.map((h) => `
              <tr><td class="num">${esc(h.time)}</td><td>${esc(h.type)}</td><td class="r">${esc(h.target)}</td>
              <td class="r">${esc(h.energy)}</td><td class="r">¥${esc(h.subsidy)}</td>
              <td style="text-align:center;">${h.accepted ? "✅ 接受" : "🚫 拒绝"}</td>
              <td style="font-size:11.5px;">${esc(h.reason)}</td></tr>`).join("")}</tbody>
          </table></div>` : `
          <div class="empty-state">
            <div class="ico">📭</div><div class="t">暂无DR事件记录</div>
            <div class="d">切换到「⚡ 手动触发」创建一个需求响应事件，或等待电网DR信号接入</div>
          </div>`}
        </div>
      </div>
    </div>

    <div class="kpi-grid" style="margin-top:14px;">
      <div class="kpi-card"><div class="kpi-value tone-blue">${d.kpis.total}</div><div class="kpi-label">今日DR事件</div></div>
      <div class="kpi-card"><div class="kpi-value tone-orange">${fmtMoney(d.kpis.subsidy)}</div><div class="kpi-label">累计补贴</div></div>
      <div class="kpi-card"><div class="kpi-value tone-blue">${fmtNum(d.kpis.energy)}kWh</div><div class="kpi-label">响应电量</div></div>
      <div class="kpi-card"><div class="kpi-value tone-red">${d.kpis.rejected}</div><div class="kpi-label">拒绝次数</div></div>
    </div>

    <div class="section-title">今日DR事件时间轴</div>
    <div class="grid grid-3-2">
      <div class="chart-card">${d.details.length ? `<div class="chart" id="dr-gantt" style="height:230px;"></div>`
        : `<div class="empty-state" style="border:none;">📭 当前无需求响应事件</div>`}</div>
      <div class="panel-card">
        <div class="panel-title">📋 事件详情</div>
        ${d.details.map((x) => {
          const rate = x.target > 0 ? Math.min(100, x.actual / x.target * 100) : 0;
          return `<div style="padding:8px 0;border-bottom:1px solid var(--border);">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
              <span style="font-weight:600;font-size:12.5px;">DR事件 #${x.idx}</span>
              <span class="status-tag ${x.accepted ? "st-ok" : "st-danger"}">${x.accepted ? "✅ 接受" : "❌ 拒绝"}</span></div>
            <div class="metric-row" style="border:none;padding:3px 0;"><span class="metric-label">时段</span><span class="metric-value">${esc(x.time)}</span></div>
            <div class="metric-row" style="border:none;padding:3px 0;"><span class="metric-label">目标/实际</span><span class="metric-value">${x.target.toFixed(0)}/${x.actual.toFixed(0)}kW</span></div>
            <div class="metric-row" style="border:none;padding:3px 0;"><span class="metric-label">补贴/净收益</span><span class="metric-value" style="color:var(--accent);">¥${x.subsidy.toFixed(2)}/¥${x.net.toFixed(2)}</span></div>
            <div class="dr-progress"><div style="width:${rate.toFixed(0)}%;"></div></div>
            <div class="dr-progress-label"><span>完成率</span><span>${rate.toFixed(0)}%</span></div>
          </div>`;
        }).join("")}
      </div>
    </div>

    <div class="section-title">三重可行性校验</div>
    <div class="feas-list">
      <div class="feas-item pass"><div class="fi">✅</div><div class="fn">SOC可行性</div><div class="fr">电量充足 · 可响应</div></div>
      <div class="feas-item pass"><div class="fi">✅</div><div class="fn">功率可行性</div><div class="fr">≤额定功率${d.feasibility.rated_power}kW</div></div>
      <div class="feas-item ${d.feasibility.temp_pass ? "pass" : "fail"}">
        <div class="fi">${d.feasibility.temp_pass ? "✅" : "❌"}</div><div class="fn">温度可行性</div>
        <div class="fr">${d.feasibility.temp_pass ? `最高${d.feasibility.max_temp.toFixed(1)}℃ · 安全` : `预计${d.feasibility.max_temp.toFixed(1)}℃ · 超温驳回`}</div>
        ${d.feasibility.temp_pass ? "" : `<div class="fs">原因：响应时段温升将超过45℃降额阈值。<br>建议：降低该时段放电功率至800kW以下，或改在温度较低时段响应。</div>`}
      </div>
    </div>
    <div style="text-align:center;margin-top:10px;font-size:11px;color:var(--text-3);">
      校验顺序：热安全 → 用能底线 → 收益净现值，三项全通过才执行响应 · 悬停各项可查看说明
    </div>
  `;
  // 页内 tabs（复用 bindSubTabs，删除重复实现）
  bindSubTabs(root);
  $("#dr-confirm", root).addEventListener("click", async () => {
    const start = $("#dr-start", root).value, end = $("#dr-end", root).value;
    const target = Number($("#dr-target", root).value), subsidy = Number($("#dr-subsidy", root).value);
    const type = $("#dr-type", root).value;
    try {
      const _fire = (force) => API.post("/api/dr/trigger", { start, end, target, subsidy, dr_type: type, force });
      let r;
      try { r = await _fire(false); }
      catch (e) {
        if (e.status === 409 && e.body && e.body.overlap && await uiConfirm(e.message + "\n\n确要在重叠时段叠加该DR事件？")) {
          r = await _fire(true); /* 二次确认后强制叠加 */
        } else { toast("触发失败：" + e.message, "err"); return; }
      }
      toast(r.msg);
      App.runSolve();
    } catch (e) { toast("触发失败：" + e.message, "err"); }
  });
  if (d.details.length) chartGantt($("#dr-gantt", root), d.details);
}

/* ============================================================
   页面6 · 系统设置
   ============================================================ */
/* 设置页渲染器异常兜底——此前 /api/providers 失败时 promise 无人接住，
   页面静默空白 + unhandled rejection。失败时渲染可重试的错误态。 */
async function renderSettings(root) {
  try {
    await _renderSettings(root);
  } catch (e) {
    if (e && e.status === 401) return; // 401 已弹登录层
    console.error("[renderSettings]", e);
    root.innerHTML = `<div class="empty-state" style="margin-top:40px;">
      <div class="ico">⚠️</div><div class="t">设置页加载失败</div>
      <div class="d">${esc(e.message || "未知错误")}</div>
      <div style="margin-top:10px;"><button class="btn btn-sm" id="st-retry">重试</button></div>
    </div>`;
    $("#st-retry", root).addEventListener("click", () => App.showPage("settings"));
  }
}
async function _renderSettings(root) {
  const mp = await API.get("/api/providers");
  const p = State.boot.params;
  const lp = mp.llm_params;
  const edit = mp.providers.find((x) => x.id === (State.mpEditId || mp.active_id)) || mp.providers[0];
  State.mpEditId = edit.id;
  const fmtOptions = ["OpenAI 兼容 (/v1/chat/completions)", "Anthropic Messages (/v1/messages)"];
  root.innerHTML = `
    <div class="settings-layout">
      <aside class="set-nav">
        <div class="set-nav-title">设置</div>
        <button class="set-nav-item active" data-panel="model">🤖 模型设置</button>
        <button class="set-nav-item" data-panel="schedule">🔋 调度参数</button>
        <button class="set-nav-item" data-panel="appearance">🎨 外观主题</button>
      </aside>
      <div class="set-resizer" title="拖拽调整导航宽度（170-360）" role="separator" aria-orientation="vertical" tabindex="0"></div>
      <div class="set-body">

        <section class="set-panel active" data-p="model">
          <div class="mp-head">
            <div>
              <div class="panel-title" style="font-size:15px;">模型设置</div>
              <div class="caption" style="margin:2px 0 0;">管理自定义模型供应商，配置后对话Agent与AI决策解释层即时生效</div>
            </div>
          </div>
          <div class="mp-toolbar">
            <select id="mp-sel" class="mp-sel">${mp.providers.map((pr) => `<option value="${esc(pr.id)}" ${pr.id === edit.id ? "selected" : ""}>${esc(pr.name || "未命名")}</option>`).join("")}</select>
            <button class="btn btn-add-green" id="mp-add">＋ 添加供应商</button>
          </div>

          <div class="mp-grid3">
            <div class="panel-card">
              <div class="panel-title">编辑供应商<span class="chev">▼</span></div>
              <div class="field"><label>名称</label><input type="text" id="mp-name" value="${esc(edit.name || "")}" placeholder="如：智谱 GLM"></div>
              <div class="field" style="margin-top:10px;"><label>Base URL</label><input type="text" id="mp-base" value="${esc(edit.base_url || "")}" placeholder="https://api.example.com/v1"></div>
              <div class="mp-duo" style="margin-top:10px;">
                <div class="field"><label>API Key</label>
                  <div class="pw-wrap"><input type="password" id="mp-key" value="" placeholder="${edit.has_key ? "留空保持不变" : "输入 API Key"}"><button type="button" class="pw-eye" id="mp-eye" title="显示/隐藏">👁</button></div>
                  <div class="field-hint" id="mp-key-hint">${edit.has_key ? "已配置 " + esc(edit.api_key_masked || "****") : "尚未配置"}</div></div>
                <div class="field"><label>格式</label>
                  <select id="mp-fmt">${fmtOptions.map((f) => `<option ${f === edit.api_format ? "selected" : ""}>${f}</option>`).join("")}</select></div>
              </div>
              <div class="field" style="margin-top:10px;"><label>超时 (秒) (5-180)</label><input type="number" id="lp-timeout" min="5" max="180" step="5" value="${lp.timeout}"></div>
            </div>

            <div class="mp-col">
              <div class="panel-card">
                <div class="panel-title">模型列表<span class="chev">▼</span></div>
                ${(edit.models || []).length === 0 ? `<div class="caption">ℹ️ 当前没有配置模型，添加后可在聊天中使用。</div>` : ""}
                <div id="mp-models" style="display:flex;flex-direction:column;gap:4px;">
                  ${(edit.models || []).map((m) => {
                    const enabled = (edit.enabled_models || edit.models || []).includes(m);
                    return `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;">
                      <span style="font-size:12.5px;color:var(--text-2);">📦 ${esc(m)}</span>
                      <label class="switch"><input type="checkbox" data-model="${esc(m)}" ${enabled ? "checked" : ""}><span class="track"></span></label></div>`;
                  }).join("")}
                </div>
                <div style="display:flex;gap:8px;margin-top:10px;">
                  <input type="text" id="mp-new-model" placeholder="如：glm-4.7" style="flex:1;background:var(--bg-input);border:1px solid var(--border);color:var(--text-1);border-radius:8px;padding:7px 11px;font-size:12.5px;outline:none;">
                  <button class="btn btn-sm" id="mp-add-model">＋ 添加模型</button>
                </div>
              </div>
              <div class="panel-card" style="margin-top:14px;">
                <div class="mp-actions" style="margin-top:0;">
                  <button class="btn" id="mp-test">⊕ 测试连接</button>
                  <button class="btn btn-primary" id="mp-set-active">设为当前使用</button>
                  <button class="btn" id="mp-save">💾 保存修改</button>
                </div>
              </div>
            </div>

            <div class="mp-col">
              <div class="panel-card">
                <div class="panel-title">操作参数<span class="chev">▼</span></div>
                <div style="display:flex;align-items:center;justify-content:space-between;padding:4px 0;">
                  <span style="font-size:12.5px;color:var(--text-2);">🌡 Temperature <b class="num" id="lp-tv2">${lp.temperature}</b></span>
                  <label class="switch"><input type="checkbox" id="lp-stream" ${lp.stream ? "checked" : ""}><span class="track"></span></label>
                </div>
                <div class="caption" style="margin-top:6px;">开关 = 流式输出（开启后对话逐字返回；关闭则整段返回）</div>
              </div>
              <div class="panel-card" style="margin-top:14px;">
                <div class="panel-title">推理参数<span class="chev">▼</span></div>
                <div class="field"><label>温度 Temperature <b id="lp-tv">${lp.temperature}</b></label>
                  <input type="range" id="lp-temp" min="0" max="1" step="0.05" value="${lp.temperature}"></div>
                <div class="field" style="margin-top:10px;"><label>最大 Tokens (128-8192)</label><input type="number" id="lp-tok" min="128" max="8192" step="128" value="${lp.max_tokens}"></div>
                <div style="display:flex;justify-content:flex-end;margin-top:14px;">
                  <button class="btn btn-sm" id="lp-save">💾 保存推理参数</button>
                </div>
              </div>
              <div class="mp-danger">
                <span class="caption" style="margin:0;display:block;text-align:center;">⚠️ 危险操作（不可撤销）</span>
                <button class="btn btn-danger-ghost" id="mp-del" style="width:100%;margin-top:8px;">🗑 删除该供应商</button>
              </div>
            </div>
          </div>
        </section>

        <section class="set-panel" data-p="schedule">
          <div class="mp-head">
            <div>
              <div class="panel-title" style="font-size:15px;">调度参数</div>
              <div class="caption" style="margin:2px 0 0;">电池 · 电价 · 优化选项与编排引擎；修改后需重新运行调度生效</div>
            </div>
          </div>
          <div style="display:flex;gap:10px;margin-bottom:14px;align-items:center;">
      <button class="btn" id="btn-reset-cfg">🔄 恢复默认</button>
      <span style="flex:1;"></span>
      <button class="btn btn-primary" id="btn-save-cfg">💾 保存配置</button>
    </div>
    <div id="save-cfg-msg" class="caption" style="text-align:right;margin:-6px 0 10px;"></div>

    <div class="sch-grid">

      <div class="panel-card">
        <div class="panel-title">🔋 电池参数</div>
        <div class="bat-grid">
          <div class="field"><label>最低SOC (%) <b id="v-p-socmin">${p.soc_min}</b></label>
            <input type="range" id="p-socmin" min="10" max="40" value="${p.soc_min}"></div>
          <div class="field"><label>最高SOC (%) <b id="v-p-socmax">${p.soc_max}</b></label>
            <input type="range" id="p-socmax" min="70" max="100" value="${p.soc_max}"></div>
          <div class="field"><label>额定功率 (kW) <b id="v-p-power">${p.rated_power}</b></label>
            <input type="range" id="p-power" min="500" max="2000" step="100" value="${p.rated_power}"></div>
        </div>
        <div class="ro-grid">
          <div class="metric-row"><span class="metric-label">充放电效率</span><span class="metric-value">${engineCfg().thermal.efficiency_pct}%</span></div>
          <div class="metric-row"><span class="metric-label">热容 C</span><span class="metric-value">${engineCfg().thermal.capacity_kj_k} kJ/K</span></div>
          <div class="metric-row"><span class="metric-label">热阻 R_th</span><span class="metric-value">${engineCfg().thermal.resistance_k_w} K/W</span></div>
          <div class="metric-row"><span class="metric-label">内阻 R</span><span class="metric-value">${engineCfg().thermal.internal_resistance_mohm} mΩ</span></div>
        </div>
      </div>
      <div class="panel-card">
        <div class="panel-title">💰 电价配置</div>
        <div class="grid grid-2" style="gap:10px;">
          <div class="field"><label>尖峰 (元/kWh)</label><input type="number" id="price-peak" step="0.01" value="${p.price_peak}"></div>
          <div class="field"><label>高峰 (元/kWh)</label><input type="number" id="price-high" step="0.01" value="${p.price_high}"></div>
          <div class="field"><label>平段 (元/kWh)</label><input type="number" id="price-flat" step="0.01" value="${p.price_flat}"></div>
          <div class="field"><label>低谷 (元/kWh)</label><input type="number" id="price-valley" step="0.01" value="${p.price_valley}"></div>
        </div>
        <div style="margin-top:10px;">
          ${(engineCfg().price_periods || []).map((pp) => `<div class="metric-row"><span class="metric-label"><span class="status-tag ${pp.cls}">${pp.name}</span></span><span class="metric-value">${pp.hours}</span></div>`).join("")}
        </div>
      </div>
      <div class="panel-card">
        <div class="panel-title">⚙️ 优化选项</div>
        <div class="opt-grid">
          <label class="check"><input type="checkbox" id="opt-thermal" ${p.include_thermal ? "checked" : ""}>热安全约束</label>
          <label class="check"><input type="checkbox" id="opt-deg" ${p.include_degradation ? "checked" : ""}>寿命衰减成本</label>
          <label class="check"><input type="checkbox" id="opt-ml" ${p.use_ml_forecast ? "checked" : ""}>XGBoost负荷预测</label>
          <label class="check"><input type="checkbox" id="opt-dr" ${p.enable_dr ? "checked" : ""}>需求响应</label>
        </div>
        <div style="border-top:1px solid var(--border);margin:10px 0;padding-top:10px;font-size:12.5px;font-weight:600;">编排引擎</div>
        <div class="eng-grid">
          <button class="btn" id="eng-lg" style="${p.orchestrator === "LangGraph" ? "background:var(--accent-soft);border-color:var(--accent);color:var(--accent);" : ""}">⬡ LangGraph</button>
          <button class="btn" id="eng-py" style="${p.orchestrator === "纯Python" ? "background:var(--accent-soft);border-color:var(--accent);color:var(--accent);" : ""}">◉ 纯Python</button>
        </div>
        <div class="caption">LangGraph: 有向图状态机编排 | 纯Python: 线性流程编排</div>
      </div>
    </div>
        </section>

        <section class="set-panel" data-p="appearance">
          <div class="mp-head">
            <div>
              <div class="panel-title" style="font-size:15px;">外观主题</div>
              <div class="caption" style="margin:2px 0 0;">BlockPulse 风格 · 浅色，Arco Pro 风格，切换即时生效，图表自动跟随</div>
            </div>
          </div>
          <div class="ap-wrap">
            <div class="panel-card">
              <div class="panel-title">🎨 主题模式</div>
              <div class="theme-cards">
                <button type="button" class="theme-card ${State.theme === "light" ? "sel" : ""}" data-th="light">
                  <span class="tc-check">✓</span>
                  <span class="tc-preview tc-light" aria-hidden="true"><i class="tp-side"></i><i class="tp-main"><i class="tp-bar"></i><i class="tp-chart"></i><i class="tp-btn"></i></i></span>
                  <b>☀️ 浅色模式</b><span class="tc-d">Arco Pro 风格，适合日间使用</span>
                </button>
                <button type="button" class="theme-card ${State.theme === "dark" ? "sel" : ""}" data-th="dark">
                  <span class="tc-check">✓</span>
                  <span class="tc-preview tc-dark" aria-hidden="true"><i class="tp-side"></i><i class="tp-main"><i class="tp-bar"></i><i class="tp-chart"></i><i class="tp-btn"></i></i></span>
                  <b>🌙 深色模式</b><span class="tc-d">BlockPulse 风格，适合夜间使用</span>
                </button>
              </div>
            </div>
            <div class="panel-card">
              <div class="panel-title">🎛️ 外观偏好</div>
              <div class="pref-grid">
                <div class="pref-row">
                  <div><div class="pt">跟随系统主题</div><div class="pd">开启后自动切换深浅色，手动选择将关闭此项</div></div>
                  <label class="switch"><input type="checkbox" id="pref-sys"><span class="track"></span></label>
                </div>
                <div class="pref-row">
                  <div><div class="pt">界面动画效果</div><div class="pd">关闭后停用过渡与动画，可提升低性能设备流畅度</div></div>
                  <label class="switch"><input type="checkbox" id="pref-anim"><span class="track"></span></label>
                </div>
                <div class="pref-row">
                  <div><div class="pt">侧边栏默认收起</div><div class="pd">下次打开页面时侧栏直接收起，窄屏友好</div></div>
                  <label class="switch"><input type="checkbox" id="pref-sb"><span class="track"></span></label>
                </div>
                <div class="pref-row">
                  <div><div class="pt">紧凑模式</div><div class="pd">减少卡片与页面间距，一屏显示更多内容</div></div>
                  <label class="switch"><input type="checkbox" id="pref-compact"><span class="track"></span></label>
                </div>
              </div>
            </div>
            <div class="caption ap-tip">💡 提示：主题与外观偏好保存在本地浏览器，清除缓存后恢复默认</div>
          </div>
        </section>

      </div>
    </div>

  `;

  /* 左侧分类导航切换 */
  $$(".set-nav-item", root).forEach((b) => b.addEventListener("click", () => {
    $$(".set-nav-item", root).forEach((x) => x.classList.remove("active"));
    $$(".set-panel", root).forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $(`.set-panel[data-p="${b.dataset.panel}"]`, root).classList.add("active");
  }));

  /* 可拖拽分隔条：调整左导航宽度（170-360px），localStorage 持久化，键盘可微调 */
  const layout = $(".settings-layout", root);
  const resizer = $(".set-resizer", root);
  const NAV_MIN = 170, NAV_MAX = 360, NAV_KEY = "set_nav_w";
  const applyNavW = (w) => {
    w = Math.min(NAV_MAX, Math.max(NAV_MIN, Math.round(w)));
    layout.style.setProperty("--nav-w", w + "px");
    return w;
  };
  const saveNavW = () => {
    const w = parseInt(layout.style.getPropertyValue("--nav-w")) || 200;
    localStorage.setItem(NAV_KEY, String(w));
    return w;
  };
  applyNavW(Number(localStorage.getItem(NAV_KEY)) || 200);
  let navDrag = null;
  resizer.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    navDrag = { x: e.clientX, w: parseInt(layout.style.getPropertyValue("--nav-w")) || 200 };
    layout.classList.add("dragging");
    resizer.classList.add("active");
    resizer.setPointerCapture(e.pointerId);
  });
  resizer.addEventListener("pointermove", (e) => {
    if (!navDrag) return;
    applyNavW(navDrag.w + (e.clientX - navDrag.x));
  });
  const endNavDrag = () => {
    if (!navDrag) return;
    navDrag = null;
    layout.classList.remove("dragging");
    resizer.classList.remove("active");
    saveNavW();
  };
  resizer.addEventListener("pointerup", endNavDrag);
  resizer.addEventListener("pointercancel", endNavDrag);
  resizer.addEventListener("keydown", (e) => {
    const cur = parseInt(layout.style.getPropertyValue("--nav-w")) || 200;
    if (e.key === "ArrowLeft") { e.preventDefault(); applyNavW(cur - 12); saveNavW(); }
    if (e.key === "ArrowRight") { e.preventDefault(); applyNavW(cur + 12); saveNavW(); }
  });

  const refreshProviderUI = (selId) => { State.mpEditId = selId; App.renderPage("settings", true); };
  $("#mp-sel", root).addEventListener("change", (e) => refreshProviderUI(e.target.value));
  $("#mp-add", root).addEventListener("click", () => {
    const nid = "custom-" + Math.random().toString(16).slice(2, 10);
    mp.providers.push({ id: nid, name: "新供应商", base_url: "", api_key: "", api_format: fmtOptions[0], models: [] });
    refreshProviderUI(nid);
  });
  const collectEdit = () => ({
    ...edit,
    name: $("#mp-name", root).value.trim() || edit.name,
    base_url: $("#mp-base", root).value.trim(),
    api_key: $("#mp-key", root).value.trim(),
    api_format: $("#mp-fmt", root).value,
  });
  $("#mp-add-model", root).addEventListener("click", () => {
    const nm = $("#mp-new-model", root).value.trim();
    if (nm && !(edit.models || []).includes(nm)) { (edit.models = edit.models || []).push(nm); refreshProviderUI(edit.id); }
  });
  $("#mp-test", root).addEventListener("click", async (e) => {
    const btn = e.currentTarget; btn.disabled = true;
    const prov = collectEdit();
    Object.assign(edit, prov);
    const r = await API.post("/api/providers/test", { provider: prov });
    toast(r.msg, r.ok ? "ok" : "err");
    btn.disabled = false;
  });
  $("#mp-set-active", root).addEventListener("click", async () => {
    Object.assign(edit, collectEdit());
    mp.active_id = edit.id;
    await API.post("/api/providers/save", { state: mp });
    toast(`当前使用：${edit.name}`, "ok");
    App.refreshAgentCard();
  });
  $("#mp-save", root).addEventListener("click", async () => {
    Object.assign(edit, collectEdit());
    const enabled = $$("#mp-models input[type=checkbox]", root).filter((c) => c.checked).map((c) => c.dataset.model);
    if (enabled.length) edit.enabled_models = enabled;
    await API.post("/api/providers/save", { state: mp });
    toast("供应商配置已保存", "ok");
  });
  $("#mp-del", root).addEventListener("click", async () => {
    mp.providers = mp.providers.filter((x) => x.id !== edit.id);
    if (!mp.providers.length) mp.providers.push({ id: "deepseek-official", name: "DeepSeek 官方", base_url: "https://api.deepseek.com/v1", api_key: "", api_format: fmtOptions[0], models: ["deepseek-chat"] });
    if (mp.active_id === edit.id) mp.active_id = mp.providers[0].id;
    await API.post("/api/providers/save", { state: mp });
    toast("供应商已删除", "ok");
    refreshProviderUI(mp.active_id);
  });
  $("#lp-temp", root).addEventListener("input", (e) => {
    $("#lp-tv", root).textContent = e.target.value;
    const tv2 = $("#lp-tv2", root);
    if (tv2) tv2.textContent = e.target.value;
  });
  /* API Key 显示/隐藏切换 */
  $("#mp-eye", root).addEventListener("click", () => {
    const inp = $("#mp-key", root);
    const show = inp.type === "password";
    inp.type = show ? "text" : "password";
    $("#mp-eye", root).textContent = show ? "🙈" : "👁";
  });
  /* 流式输出开关：即时生效——此前必须另点「保存推理参数」才落盘，
     用户拨了开关以为已开启，对话仍是整段返回。 */
  $("#lp-stream", root).addEventListener("change", async (e) => {
    mp.llm_params = {
      ...(mp.llm_params || {}),
      temperature: Number($("#lp-temp", root).value),
      max_tokens: Number($("#lp-tok", root).value),
      timeout: Number($("#lp-timeout", root).value),
      stream: e.target.checked,
    };
    try {
      await API.post("/api/providers/save", { state: mp });
      toast(e.target.checked ? "已开启流式输出（对话逐字返回）" : "已关闭流式输出（整段返回）", "ok");
    } catch (err) {
      e.target.checked = !e.target.checked;
      toast("保存失败：" + err.message);
    }
  });
  $("#lp-save", root).addEventListener("click", async () => {
    mp.llm_params = { temperature: Number($("#lp-temp", root).value), max_tokens: Number($("#lp-tok", root).value),
      timeout: Number($("#lp-timeout", root).value), stream: $("#lp-stream", root).checked };
    await API.post("/api/providers/save", { state: mp });
    toast("推理参数已保存", "ok");
  });
  // 配置保存/恢复
  $("#btn-save-cfg", root).addEventListener("click", async () => {
    await collectSettingsParams(root);
    const r = await API.post("/api/settings/save");
    $("#save-cfg-msg", root).textContent = "✅ " + r.msg;
    toast("配置已保存", "ok");
  });
  $("#btn-reset-cfg", root).addEventListener("click", async () => {
    const r = await API.post("/api/settings/reset");
    State.boot.params = r.params;
    toast("已恢复默认配置", "ok");
    App.renderPage("settings", true);
  });
  // 主题
  /* 外观主题卡片 + 外观偏好开关 */
  $$(".theme-card", root).forEach((c) => c.addEventListener("click", () => App.setTheme(c.dataset.th)));
  const prefSys = $("#pref-sys", root);
  prefSys.checked = !localStorage.getItem("theme");
  prefSys.addEventListener("change", () => {
    if (prefSys.checked) {
      localStorage.setItem("theme_follow", "1");
      localStorage.removeItem("theme");
      const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      App.setTheme(dark ? "dark" : "light");
      toast("已跟随系统主题", "ok");
    } else {
      localStorage.setItem("theme_follow", "0");
      localStorage.setItem("theme", State.theme);
      toast("已改为手动选择主题", "ok");
    }
  });
  const prefAnim = $("#pref-anim", root);
  prefAnim.checked = localStorage.getItem("anim") !== "0";
  prefAnim.addEventListener("change", () => {
    const on = prefAnim.checked;
    document.documentElement.classList.toggle("no-anim", !on);
    localStorage.setItem("anim", on ? "1" : "0");
    toast(on ? "界面动画已开启" : "界面动画已关闭", "ok");
  });
  const prefSb = $("#pref-sb", root);
  prefSb.checked = localStorage.getItem("sb_default") === "1";
  prefSb.addEventListener("change", () => {
    localStorage.setItem("sb_default", prefSb.checked ? "1" : "0");
    toast(prefSb.checked ? "下次打开页面时侧栏将默认收起" : "下次打开页面时侧栏默认展开", "ok");
  });
  const prefCp = $("#pref-compact", root);
  prefCp.checked = localStorage.getItem("compact") === "1";
  prefCp.addEventListener("change", () => {
    document.documentElement.classList.toggle("compact", prefCp.checked);
    localStorage.setItem("compact", prefCp.checked ? "1" : "0");
    setTimeout(reflowCharts, 300);
    toast(prefCp.checked ? "已切换为紧凑模式" : "已恢复标准间距", "ok");
  });
  // 参数滑杆
  [["p-socmin", "v-p-socmin"], ["p-socmax", "v-p-socmax"], ["p-power", "v-p-power"]].forEach(([i, v]) => {
    $("#" + i, root).addEventListener("input", (e) => { $("#" + v, root).textContent = e.target.value; });
  });
  // 编排引擎
  $("#eng-lg", root).addEventListener("click", async () => {
    await API.post("/api/params", { orchestrator: "LangGraph", clear_cache: true });
    State.boot.params.orchestrator = "LangGraph"; App.renderPage("settings", true);
  });
  $("#eng-py", root).addEventListener("click", async () => {
    await API.post("/api/params", { orchestrator: "纯Python", clear_cache: true });
    State.boot.params.orchestrator = "纯Python"; App.renderPage("settings", true);
  });
}

/* 收集设置页参数（保存配置前调用） */
async function collectSettingsParams(root) {
  const g = (id) => $(id, root);
  const payload = {
    soc_min: Number(g("#p-socmin").value), soc_max: Number(g("#p-socmax").value),
    rated_power: Number(g("#p-power").value),
    price_peak: Number(g("#price-peak").value), price_high: Number(g("#price-high").value),
    price_flat: Number(g("#price-flat").value), price_valley: Number(g("#price-valley").value),
    include_thermal: g("#opt-thermal").checked, include_degradation: g("#opt-deg").checked,
    use_ml_forecast: g("#opt-ml").checked, enable_dr: g("#opt-dr").checked,
  };
  await API.post("/api/params", payload);
  Object.assign(State.boot.params, payload);
}

const RENDERERS = {
  dashboard: { render: renderDashboard, needsData: true },
  scheduling: { render: renderScheduling, needsData: true },
  forecast: { render: renderForecast, needsData: true },
  thermal: { render: renderThermal, needsData: true },
  dr: { render: renderDemandResponse, needsData: true },
  settings: { render: renderSettings, needsData: false },
};
