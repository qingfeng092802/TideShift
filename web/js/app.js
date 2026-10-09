/* ================= 主控：导航 / 求解循环 / 对话 / 上传 / 主题 ================= */

/* 导航图标只写 ICONS 的键名（web/js/api.js），不再在两份之间抄一遍路径：
   此前 NAV 里是一条独立的 path 串，注册表又抄了一份，改一处不会改另一处。 */
const NAV = [
  { id: "dashboard", label: "数据总览", icon: "dashboard" },
  { id: "scheduling", label: "充放电调度", icon: "bolt" },
  { id: "forecast", label: "负荷预测", icon: "trend" },
  { id: "thermal", label: "电池热管理", icon: "thermometer" },
  { id: "dr", label: "需求响应", icon: "bulb" },
  { id: "traces", label: "运行追溯", icon: "history" },
  { id: "settings", label: "系统设置", icon: "gear" },
];

/* 侧栏收起状态 → .app 轨道同步（兼容不支持 :has() 的浏览器内核） */
function syncSidebarTrack() {
  const sb = $("#sidebar");
  if (sb) $("#app").classList.toggle("sb-closed", sb.classList.contains("collapsed"));
}

// 思维链节点：正文全程只用 textContent。它是模型原文，一旦走 renderMarkdown/innerHTML
// 就等于把"往页面里写标记"的能力交回了模型手上。
// 唯一的 innerHTML 是 summary，内容 = 自家图标注册表的 SVG + 写死的中文，不含模型字节。
function makeThinkNode(text) {
  const box = document.createElement("details");
  box.className = "chat-think";
  const sum = document.createElement("summary");
  sum.innerHTML = `${ico("cpu")} 思考过程`;
  const pre = document.createElement("pre");
  pre.textContent = text || "";
  box.append(sum, pre);
  return box;
}

const App = {
  /* ---------- 启动 ---------- */
  async boot() {
    // 主题先行，避免闪烁
    document.documentElement.setAttribute("data-theme", State.theme);
    // 外观偏好恢复：动画关闭 / 紧凑模式
    if (localStorage.getItem("anim") === "0") document.documentElement.classList.add("no-anim");
    if (localStorage.getItem("compact") === "1") document.documentElement.classList.add("compact");
    this.renderThemeIcon();
    // 静态 HTML（欢迎页 / 求解卡 / 对话栏 / 上传弹窗）里的 <i data-icon> 槽位在这里填成
    // SVG。必须放在第一个受保护请求之前：/api/bootstrap 一失败（未登录 / 首次改密）
    // 下面就 return 了，登录层和欢迎页仍然要有图标。
    hydrateIcons();
    try {
      State.boot = await API.get("/api/bootstrap");
    } catch (e) {
      // must_change：_handleResp 已经把改密浮层弹出来了，这里静默返回即可，
      // 不能再往下抛——抛出去就是 unhandled rejection + 白屏，页面上连一行字都没有。
      // 其余错误（服务没起、网络断）也要留一行可见的话，别让人对着白屏猜。
      if (!e.mustChange) toast("初始化失败：" + e.message, "err");
      return;
    }
    // 版本号单一事实来源——侧栏副标题从 /api/bootstrap 动态渲染，不再硬编码
    const bs = $("#brand-sub");
    if (bs && State.boot.version) bs.textContent = `多智能体调度 v${State.boot.version} · LLM 解释层`;
    // 版本号一律来自 /api/bootstrap，禁止硬编码 —— 硬编码必然在发版后漂移
    const wv = $("#wl-version");
    if (wv && State.boot.version) wv.textContent = `v${State.boot.version}`;
    this.renderNav();
    this.renderAgentCard();
    this.renderDataCaption();
    this.bindGlobal();
    this.renderChat();
    this.showPage("dashboard");
    // 未就绪 → 自动触发求解并轮询，首启不需要用户先点按钮
    this.checkSolve();
    setInterval(() => this.pollProgress(), 900);
    // P2：标签页隐藏时暂停轮询（此前后台 8h ≈ 6400 次无效请求）；恢复可见立即补拉一次
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) this.pollProgress();
    });
    // 窄屏首屏死锁修复——窄屏初始化强制收起 sidebar（否则 fixed 侧栏+遮罩挡住收起按钮）
    if (window.matchMedia("(max-width: 900px)").matches) {
      $("#sidebar").classList.add("collapsed");
      syncSidebarTrack();
    }
    // 外观偏好：侧边栏默认收起（桌面端也生效）
    if (localStorage.getItem("sb_default") === "1") {
      $("#sidebar").classList.add("collapsed");
      syncSidebarTrack();
    }
    // 窄屏下点击遮罩区域（侧栏与收起按钮之外）即收起侧栏
    document.addEventListener("click", (e) => {
      if (window.matchMedia("(max-width: 900px)").matches
          && !$("#sidebar").classList.contains("collapsed")
          && !$("#sidebar").contains(e.target)
          && !$("#btn-sidebar").contains(e.target)) {
        $("#sidebar").classList.add("collapsed");
        syncSidebarTrack();
      }
    });
    // 欢迎页：boot 完成后展示（后台继续求解，进入后可见进度）
    this.bindWelcome();
    this.showWelcome();
  },

  /* ---------- 欢迎页 ---------- */
  showWelcome() {
    const w = $("#welcome");
    if (!w) return;
    w.classList.remove("hide");
    w.style.display = "block";
  },
  dismissWelcome() {
    const w = $("#welcome");
    if (!w || w.style.display === "none") return;
    w.classList.add("hide");
    setTimeout(() => { w.style.display = "none"; }, 260);
  },
  bindWelcome() {
    $$("#welcome .wl-card").forEach((c) => c.addEventListener("click", () => {
      this.dismissWelcome();
      this.showPage(c.dataset.page);
    }));
    $("#wl-enter").addEventListener("click", () => this.dismissWelcome());
    $("#wl-upload").addEventListener("click", () => {
      this.dismissWelcome();
      this.refreshUploadDlg();
      $("#upload-dlg").showModal();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") this.dismissWelcome();
    });
  },

  bindGlobal() {
    $("#btn-sidebar").addEventListener("click", () => {
      $("#sidebar").classList.toggle("collapsed");
      syncSidebarTrack();
      setTimeout(reflowCharts, 300);
    });
    $("#btn-chat").addEventListener("click", () => {
      document.body.classList.add("chat-open");
      localStorage.setItem("chatPanelCollapsed", "0");
      setTimeout(reflowCharts, 300);
    });
    $("#btn-chat-close").addEventListener("click", () => {
      document.body.classList.remove("chat-open");
      localStorage.setItem("chatPanelCollapsed", "1");
      setTimeout(reflowCharts, 300);
    });
    $("#btn-theme").addEventListener("click", () => this.setTheme(State.theme === "dark" ? "light" : "dark"));
    // 对话
    $("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); this.sendChat(); });
    $("#btn-chat-clear").addEventListener("click", async () => {
      await API.post("/api/chat/clear");
      State.chatHistory = null;
      this.renderChat();
    });
    // 上传弹窗
    const dlg = $("#upload-dlg");
    $("#btn-upload").addEventListener("click", () => {
      this.refreshUploadDlg();
      dlg.showModal();
    });
    $("#btn-upload-close").addEventListener("click", () => dlg.close());
    /* 标准格式模板下载：前端生成 CSV（96 点 · 15 分钟粒度），带 UTF-8 BOM 防 Excel 乱码。
       字段与后端识别的「标准格式」一致：timestamp, load_kw, price, temp；无需后端接口。
       电价一律按 /api/bootstrap 下发的 price_periods 计算——这里曾内联过一套
       17-22 点尖峰的私有日历，用户下载模板、只改负荷再传回来，算钱用的档位跟引擎对不上。 */
    $("#btn-upload-template").addEventListener("click", () => {
      const pad = (n) => String(n).padStart(2, "0");
      const rows = [["timestamp", "load_kw", "price", "temp"]];
      // 2024-07：与内置数据集同月，模板样例能同时展示尖/峰/平/谷四档
      const base = new Date(2024, 6, 1, 0, 0, 0);
      const periods = ((State.boot && State.boot.engine_config || {}).price_periods) || [];
      if (!periods.length) { toast("引擎参数尚未加载，请稍后重试", "err"); return; }
      const priceAt = (h, month) => {
        // 最窄窗口胜：尖峰窗口是高峰窗口的子集，靠遍历顺序判档会静默换档
        let best = null;
        for (const per of periods) {
          if (per.months && !per.months.includes(month)) continue;
          for (const [lo, hi] of per.range) {
            if (lo <= h && h < hi) {
              if (best === null || hi - lo < best.span) best = {price: per.price, span: hi - lo};
              break;
            }
          }
        }
        return best;
      };
      for (let i = 0; i < 96; i++) {
        const d = new Date(base.getTime() + i * 15 * 60000);
        const h = d.getHours() + d.getMinutes() / 60;
        const shape = Math.pow(Math.sin(((h - 6) / 24) * Math.PI * 2), 2);
        const peak = h >= 17 && h < 22 ? 260 : 0;
        const load = Math.round(600 + 420 * shape + peak);
        const found = priceAt(h, d.getMonth() + 1);
        if (!found) { toast(`电价日历未覆盖 ${pad(d.getHours())}:${pad(d.getMinutes())}`, "err"); return; }
        const price = found.price;
        const temp = (24 + 7 * Math.sin(((h - 9) / 24) * Math.PI * 2)).toFixed(1);
        rows.push([
          `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:00`,
          load, String(price), temp,
        ]);
      }
      const csv = "\uFEFF" + rows.map((r) => r.join(",")).join("\r\n");
      const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
      const a = document.createElement("a");
      a.href = url; a.download = "load_template.csv";
      document.body.appendChild(a); a.click();
      setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 120);
      toast("模板已下载：load_template.csv（96 点 · 15 分钟粒度）", "ok");
    });
    $("#dropzone").addEventListener("click", () => $("#file-input").click());
    // P1 可访问性：dropzone 键盘可达（Enter/Space 触发选文件）
    $("#dropzone").addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#file-input").click(); }
    });
    $("#file-input").addEventListener("change", async (e) => {
      if (e.target.files[0]) await this.doUpload(e.target.files[0]);
    });
    const dz = $("#dropzone");
    dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("drag"); });
    dz.addEventListener("dragleave", () => dz.classList.remove("drag"));
    dz.addEventListener("drop", async (e) => {
      e.preventDefault(); dz.classList.remove("drag");
      if (e.dataTransfer.files[0]) await this.doUpload(e.dataTransfer.files[0]);
    });
    $("#btn-upload-clear").addEventListener("click", async () => {
      // 改走统一 API 层（API.delete → _handleResp）——401 时自动弹统一登录层，
      // 其余错误给明确 toast；此前裸 fetch 绕过统一错误处理，体验与安全策略不一致。
      try {
        await API.delete("/api/upload");
        toast("已清除上传数据，恢复内置演示数据", "ok");
        State.boot = await API.get("/api/bootstrap");
        State.pages = {};
        this.renderDataCaption();
        dlg.close();
        this.runSolve();
      } catch (e) {
        if (e.status !== 401) toast("清除上传数据失败：" + e.message, "err");
      }
    });
    // 键盘可达性：对话面板
    $("#btn-chat").addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#btn-chat").click(); } });
  },

  /* ---------- 导航 ---------- */
  renderNav() {
    $("#nav").innerHTML = NAV.map((n) => `
      <button class="nav-item ${State.page === n.id ? "active" : ""}" data-page="${n.id}">
        ${ico(n.icon)}
        ${n.label}
      </button>`).join("");
    $$("#nav .nav-item").forEach((b) => b.addEventListener("click", () => this.showPage(b.dataset.page)));
  },

  /* ---------- 页面切换 ---------- */
  async showPage(id) {
    // 请求序号防竞态——await 期间用户切页时，过期响应直接丢弃，避免覆盖新页面
    const seq = (this._pageSeq = (this._pageSeq || 0) + 1);
    State.page = id;
    $$("#nav .nav-item").forEach((b) => b.classList.toggle("active", b.dataset.page === id));
    $("#crumb-page").textContent = NAV.find((n) => n.id === id).label;
    $$(".page").forEach((s) => { s.style.display = "none"; });
    this.disposeCharts();
    const sec = $("#page-" + id);
    sec.style.display = "block";
    const spec = RENDERERS[id];
    if (!spec.needsData) { spec.render(sec); reflowCharts(); return; }
    if (State.pages[id]) { spec.render(sec, State.pages[id]); reflowCharts(); return; }
    try {
      const data = await API.get("/api/page/" + id);
      if (seq !== this._pageSeq) return; // 已切走：过期响应，不渲染不写缓存
      State.pages[id] = data;
      if (State.solving) { sec.style.display = "none"; return; } // 求解中等待
      spec.render(sec, data);
      reflowCharts();
    } catch (e) {
      if (seq !== this._pageSeq) return;
      if (e.status === 409) {
        sec.innerHTML = ""; // 等待求解 veil
      } else {
        sec.innerHTML = `<div class="empty-state"><div class="ico">${ico("warn")}</div><div class="t">页面数据加载失败</div><div class="d">${esc(e.message)}</div><div style="margin-top:10px;"><button class="btn btn-sm" onclick="location.reload()">重试</button></div></div>`;
        console.error("[renderPage]", e);
      }
    }
  },

  renderPage(id, keepData) {
    const sec = $("#page-" + id);
    const spec = RENDERERS[id];
    this.disposeCharts();
    const data = State.pages[id];
    if (!spec.needsData || data) { spec.render(sec, data); reflowCharts(); }
    else { sec.style.display = "none"; }
  },

  disposeCharts() {
    State.charts.forEach((c) => { try { c.dispose(); } catch (e) { console.warn("[disposeCharts]", e); } });
    State.charts = [];
  },

  /* ---------- 求解流程（冷启动骨架 + 进度条 + 缓存秒开） ---------- */
  async checkSolve() {
    const pr = await API.get("/api/progress");
    if (!pr.solved && !pr.running) this.runSolve();
    else if (pr.running) this.showVeil(true);
    else this.hideVeil();
  },

  async runSolve(opts = {}) {
    if (State.solving) return;
    State.solving = true;
    State.pages = {};
    try {
      const r = await API.post("/api/solve", { date: State.boot.selected_date, force: !!opts.force });
      if (r.action === "cache_hit") {
        State.boot = await API.get("/api/bootstrap");
        State.solving = false;
        this.hideVeil();
        this.showPage(State.page);
        return;
      }
      this.showVeil(true);
    } catch (e) {
      State.solving = false;
      toast("求解启动失败：" + e.message, "err");
    }
  },

  async pollProgress() {
    // 空闲（已 solved）时 900ms 轮询退避为 ~4.5s 一次；求解中保持 900ms
    if (this._lastPr && this._lastPr.solved && !State.solving) {
      this._idleTick = (this._idleTick || 0) + 1;
      if (this._idleTick % 5 !== 0) return;
    } else {
      this._idleTick = 0;
    }
    // P2：后台标签页不轮询（配合 visibilitychange 恢复时补拉）
    if (document.hidden) return;
    let pr;
    try { pr = await API.get("/api/progress"); }
    catch (e) {
      // P2：不再静默吞错——401 已由 _handleResp 弹登录层，其余错误限频记录便于诊断
      if (e.status && e.status !== 401) {
        this._pollErrCount = (this._pollErrCount || 0) + 1;
        if (this._pollErrCount === 1 || this._pollErrCount % 10 === 0) console.warn("[pollProgress]", e);
      }
      return;
    }
    this._lastPr = pr;
    if (pr.running) {
      this.showVeil(true);
      // iconize：进度标签来自后端（"✅ 全部智能体执行完毕"），状态字符在呈现层换成同一套描边图标
      $("#solve-label").innerHTML = iconize(pr.label || "运行中…");
      $("#solve-fill").style.width = (pr.percent || 3) + "%";
      this.renderStages(pr.percent);
      State.solving = true;
      // ⏱ 已运行计时器
      if (this._solveStart == null) this._solveStart = Date.now();
      const el = Math.floor((Date.now() - this._solveStart) / 1000);
      const tm = $("#solve-timer");
      if (tm) tm.innerHTML = `${ico("clock")} ${String(Math.floor(el / 60)).padStart(2, "0")}:${String(el % 60).padStart(2, "0")}`;
      // 📝 实时日志：步骤文本变化即记录一行
      const lbl = pr.label || "运行中…";
      if (lbl !== this._lastLogLbl) {
        this._lastLogLbl = lbl;
        (this._logs = this._logs || []).push(`[${new Date().toTimeString().slice(0, 8)}] ${lbl}`);
        const lg = $("#solve-log");
        if (lg) { lg.innerHTML = this._logs.slice(-8).map((l) => `<div>${iconize(l)}</div>`).join(""); lg.scrollTop = lg.scrollHeight; }
      }
      // 底部系统状态栏（只展示真实可得信息，不编造 CPU/内存）
      const st = $("#solve-status");
      if (st && State.boot) {
        const di = State.boot.data_info || {};
        const llm = State.boot.llm || {};
        st.innerHTML = `<span>${ico("bot")} 当前模型: <b>${esc(llm.model || "—")}</b></span>` +
          `<span>${ico("bars")} 数据来源: <b>${di.is_custom ? `自定义数据 ${di.rows} 条` : "内置演示数据"}</b></span>` +
          `<span>${ico("trend")} 进度: <b>${pr.percent || 0}%</b></span>`;
      }
    } else if (pr.done && State.solving) {
      State.solving = false;
      this._solveRetry = 0;
      $("#solve-fill").style.width = "100%";
      setTimeout(async () => {
        this.hideVeil();
        if (pr.error) { toast("求解失败：" + pr.error, "err"); return; }
        if (pr.warning) toast(pr.warning);
        State.boot = await API.get("/api/bootstrap");
        this.renderDataCaption();
        State.pages = {};
        this.showPage(State.page);
      }, 350);
    } else if (!pr.running && !pr.solved && !State.solving) {
      // 服务端重启等场景：自动补跑（S1：指数退避 + 上限 3 次，防失败重试风暴）
      this._solveRetry = (this._solveRetry || 0) + 1;
      if (this._solveRetry <= 3) {
        setTimeout(() => this.runSolve(), this._solveRetry * 3000);
      } else if (this._solveRetry === 4) {
        toast("多次自动重跑失败，请检查服务状态后刷新页面", "err");
      }
    }
  },

  showVeil(show) {
    $("#solve-veil").style.display = show ? "block" : "none";
    $("#pages-root").style.display = show ? "none" : "block";
  },
  hideVeil() {
    $("#solve-veil").style.display = "none";
    $("#pages-root").style.display = "block";
    // 重置运行状态（计时器/日志/步骤记录）
    this._solveStart = null;
    this._logs = [];
    this._lastLogLbl = null;
    const lg = $("#solve-log");
    if (lg) lg.innerHTML = "";
    reflowCharts();
    setTimeout(reflowCharts, 120);
  },
};

/* ---------- 登录浮层（原生 dialog + showModal：自带焦点陷阱，Tab 无法穿透背景） ---------- */
window.__showLogin = function () {
  if ($("#login-veil")) return;
  const dlg = document.createElement("dialog");
  dlg.id = "login-veil";
  dlg.innerHTML = `
    <div class="login-logo">${ico("bolt")}</div>
    <div class="login-title">储能调度系统</div>
    <div class="login-sub">请登录以继续（JWT 会话 · 12小时有效）</div>
    <div class="field"><label for="login-user">用户名</label><input id="login-user" autocomplete="username" value="admin"></div>
    <div class="field"><label for="login-pass">口令</label><input id="login-pass" type="password" autocomplete="current-password"></div>
    <button class="btn btn-primary" id="login-btn" style="width:100%;margin-top:12px;">登 录</button>
    <div id="login-msg" class="caption" style="margin-top:8px;color:var(--danger,#ff6b6b);"></div>`;
  document.body.appendChild(dlg);
  dlg.addEventListener("cancel", (e) => e.preventDefault()); // 未登录不允许 Esc 关闭登录层
  dlg.showModal();
  const doLogin = async () => {
    const u = $("#login-user").value.trim(), p = $("#login-pass").value;
    if (!u || !p) { $("#login-msg").textContent = "请输入用户名与口令"; return; }
    $("#login-btn").disabled = true;
    $("#login-msg").textContent = "登录中…";
    try {
      const r = await fetch("/api/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: u, password: p }),
      });
      const d = await r.json();
      if (!r.ok) { $("#login-msg").textContent = d.detail || "登录失败"; return; }
      localStorage.setItem(AUTH_TOKEN_KEY, d.token);
      if (d.must_change) {
        // P1 修复：原生 prompt() → uiPrompt 弹窗（与全局 UI 风格一致，支持 Enter 确认）
        const np = await uiPrompt("首次登录请设置新口令（至少 8 位）：", { minLength: 8 });
        let changed = false;
        if (np && np.length >= 8) {
          const cr = await fetch("/api/auth/change-password", {
            method: "POST", headers: { "Content-Type": "application/json", "Authorization": "Bearer " + d.token },
            body: JSON.stringify({ old_password: p, new_password: np }),
          });
          changed = cr.ok;
          if (!changed) toast("口令修改失败，可在弹窗中重试", "err");
        }
        // 没改成就不能 reload：带着一个全接口 403 的 token 回首页只会白屏。
        // 交给改密浮层，它有自己的重试与"退出登录"活口。
        if (!changed) { dlg.remove(); window.__requireChangePassword(p); return; }
      }
      dlg.remove();
      location.reload();
    } catch (e) {
      $("#login-msg").textContent = "登录失败：" + e.message;
    } finally { $("#login-btn").disabled = false; }
  };
  $("#login-btn").addEventListener("click", doLogin);
  dlg.addEventListener("keydown", (ev) => { if (ev.key === "Enter") doLogin(); });
  setTimeout(() => $("#login-pass").focus(), 50);
};

/* must_change 期间的改密引导。存在理由：这是白屏的唯一出口。
   此前改密只在"登录成功后"用 uiPrompt 顺手问一次，用户一取消或改密失败，
   代码照样 location.reload()，于是带着一个所有 /api/* 都 403 的 token 回到首页，
   boot() 第一个请求就抛错 → 白屏，且再没有任何界面能救回来。
   退出按钮是刻意留的活口：没有它，浮层本身就是第二个死锁。 */
window.__requireChangePassword = function (prefillOld) {
  if (document.getElementById("pwd-veil")) return;
  const dlg = document.createElement("dialog");
  dlg.id = "pwd-veil";
  dlg.innerHTML = `
    <div class="login-logo">${ico("lock")}</div>
    <div class="login-title">请先修改初始口令</div>
    <div class="login-sub">首次登录（或口令为初始值）时，除改密外的接口一律 403。</div>
    <div class="field"><label for="pw-old">原口令</label><input id="pw-old" type="password" autocomplete="current-password"></div>
    <div class="field"><label for="pw-new">新口令（至少 8 位）</label><input id="pw-new" type="password" autocomplete="new-password"></div>
    <div style="display:flex;gap:10px;margin-top:12px;">
      <button class="btn btn-primary" id="pw-btn" style="flex:1;">确认修改</button>
      <button class="btn" id="pw-out" style="flex:1;">退出登录</button>
    </div>
    <div id="pw-msg" class="caption" style="margin-top:8px;color:var(--danger,#ff6b6b);"></div>`;
  document.body.appendChild(dlg);
  dlg.addEventListener("cancel", (e) => e.preventDefault());   // Esc 不给关，关掉就是白屏
  if (prefillOld) dlg.querySelector("#pw-old").value = prefillOld;
  dlg.showModal();
  const msg = dlg.querySelector("#pw-msg");
  const submit = async () => {
    const o = dlg.querySelector("#pw-old").value, n = dlg.querySelector("#pw-new").value;
    if (!o || !n) { msg.textContent = "原口令与新口令都要填"; return; }
    if (n.length < 8) { msg.textContent = "新口令至少 8 位"; return; }
    const btn = dlg.querySelector("#pw-btn");
    btn.disabled = true; msg.textContent = "提交中…";
    try {
      const r = await fetch("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ old_password: o, new_password: n }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.status === 401) {          // token 过期：回到登录层，别卡在改密框里
        authLogout(); dlg.remove(); window.__showLogin(); return;
      }
      if (!r.ok) { msg.textContent = d.detail || `修改失败（HTTP ${r.status}）`; return; }
      dlg.remove();
      location.reload();               // 只有真改成功才重载，否则又是白屏一趟
    } catch (e) {
      msg.textContent = "请求失败：" + e.message;
    } finally { btn.disabled = false; }
  };
  dlg.querySelector("#pw-btn").addEventListener("click", submit);
  dlg.querySelector("#pw-out").addEventListener("click", () => {
    authLogout(); dlg.remove(); window.__showLogin();
  });
  dlg.addEventListener("keydown", (ev) => { if (ev.key === "Enter") submit(); });
  setTimeout(() => dlg.querySelector(prefillOld ? "#pw-new" : "#pw-old").focus(), 50);
};

/* 启动即检查会话：无 token 先登录再拉 bootstrap */
(function () {
  if (!localStorage.getItem(AUTH_TOKEN_KEY)) window.__showLogin();
})();

Object.assign(App, {
  renderStages(pct) {
    const stages = [
      [3, "准备调度引擎"], [25, "负荷预测（XGBoost）"], [40, "MILP 优化求解（HiGHS）"],
      [75, "需求响应叠加"], [85, "日报表汇总"], [92, "AI 决策解释"], [100, "全部完成"],
    ];
    const descs = [
      "加载数据与电池参数，构建优化上下文",
      "XGBoost 时序预测 · 96 点 15 分钟粒度",
      "HiGHS 求解器 · 热安全 + 寿命约束联合优化",
      "DR 事件评估与三重校验",
      "收益构成与 SOC 轨迹汇总",
      "规则模板 / LLM 生成决策解释",
      "调度结果已生成，可查看各页看板",
    ];
    let cur = stages.findIndex(([p]) => pct < p);
    if (cur === -1) cur = stages.length - 1;
    $("#solve-stages").innerHTML = stages.map(([p, label], i) => {
      const st = pct >= p ? "done" : i === cur ? "cur" : "todo";
      const mark = st === "done" ? ico("check") : st === "cur" ? ico("play") : ico("circle");
      return `<div class="tl-item ${st}"><span class="tl-ico">${mark}</span><b>${esc(label)}</b></div>`;
    }).join("");
    const det = $("#solve-detail");
    if (det) det.textContent = descs[cur];
  },

  /* ---------- 侧栏信息 ---------- */
  renderAgentCard() {
    const l = State.boot.llm;
    $("#ag-name").textContent = l.provider;
    $("#ag-model").textContent = l.model;
    $("#ag-key").innerHTML = l.has_key
      ? `<span class="tag tag-ok">Key已配置</span>`
      : `<span class="tag tag-neutral">规则模式</span>`;
  },
  async refreshAgentCard() {
    State.boot = await API.get("/api/bootstrap");
    this.renderAgentCard();
  },
  renderDataCaption() {
    const di = State.boot.data_info;
    // innerHTML 而不是 textContent：这里带自家图标。textContent 会把 <svg> 源码
    // 原样打印成一行文字（之前 lexer 替换后就出过这个现象）。数字来自 /api/bootstrap。
    $("#sb-data-caption").innerHTML = di.is_custom
      ? `${ico("check")} 已加载 ${Number(di.rows) || 0} 条 · ${Number(di.days) || 0} 天`
      : "使用内置演示数据 · 上传后自动切换";
  },

  /* ---------- 对话 ---------- */
  async renderChat() {
    if (!State.chatHistory) {
      const r = await API.get("/api/chat/history").catch(() => ({ history: [] }));
      State.chatHistory = r.history;
    }
    const box = $("#chat-msgs");
    box.innerHTML = State.chatHistory.slice(-16).map((m) => `
      <div class="msg ${m.role === "user" ? "user" : "bot"}">
        <div class="avatar">${m.role === "user" ? ico("user") : ico("bot")}</div>
        <div class="bubble">${m.role === "user" ? esc(m.content) : renderMarkdown(m.content)}</div>
      </div>`).join("");
    box.scrollTop = box.scrollHeight;
  },
  async sendChat() {
    const input = $("#chat-input");
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    State.chatHistory.push({ role: "user", content: q });
    this.renderChat();
    const box = $("#chat-msgs");

    // 助手气泡：流式过程中原地更新（开启流式输出时逐字显示）
    // 三个互不覆盖的区块：aux（降级说明/求解等待）、think（思维链）、main（正文）。
    // 分开是因为 paint 会整体重写正文的 innerHTML——若把 <details> 一起重绘，
    // 用户刚手动展开的思考过程会在下一个 token 被折叠回去。
    const holder = document.createElement("div");
    holder.className = "msg bot";
    holder.innerHTML = `<div class="avatar">${ico("bot")}</div><div class="bubble">
      <div class="chat-aux" style="display:none;"></div>
      <div class="chat-main"><span class="spinner" style="display:inline-block;vertical-align:-4px;"></span> 思考中…</div>
    </div>`;
    box.appendChild(holder);
    box.scrollTop = box.scrollHeight;
    const aux = holder.querySelector(".chat-aux");
    const main = holder.querySelector(".chat-main");
    let think = null;   // 首个 reasoning 事件到达时才建节点

    let acc = "";
    let thinking = "";
    let gotDone = false;
    // Markdown 全量重渲有开销，流式期间节流到 ~16fps；结束后 renderChat 会做最终全量重绘
    let lastPaint = 0;
    const paint = () => {
      const now = performance.now();
      if (now - lastPaint < 60) return;
      lastPaint = now;
      main.innerHTML = renderMarkdown(acc);
      box.scrollTop = box.scrollHeight;
    };
    try {
      // SSE：POST + ReadableStream（EventSource 仅支持 GET，无法携带 JWT 头）
      const r = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ message: q }),
      });
      if (r.status === 401) {
        authLogout();
        if (window.__showLogin) window.__showLogin();
        throw new Error("未登录或会话已过期");
      }
      if (!r.ok) {
        const b = await r.json().catch(() => ({}));
        throw new Error(b.detail || r.statusText);
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const ev = buf.slice(0, i).trim();
          buf = buf.slice(i + 2);
          if (!ev.startsWith("data:")) continue;
          let pl;
          try { pl = JSON.parse(ev.slice(5).trim()); } catch { continue; }
          if (typeof pl.delta === "string") { acc += pl.delta; paint(); }
          if (typeof pl.reasoning === "string") {
            thinking += pl.reasoning;
            if (!think) { think = makeThinkNode(""); main.before(think); }
            think.querySelector("pre").textContent = thinking;
            box.scrollTop = box.scrollHeight;
          }
          if (typeof pl.notice === "string" && pl.notice) {
            aux.style.display = "";
            aux.innerHTML = ico("warn") + " " + esc(pl.notice);
          }
          if (typeof pl.heartbeat === "number") {
            // 工具在跑 MILP（实测单次求解 p50 49.7 s），这段时间一个 token 都没有
            aux.style.display = "";
            aux.innerHTML = ico("clock") + ` 正在执行工具/求解，已等待 ${pl.heartbeat.toFixed(0)} s…`;
          }
          if (pl.done) { gotDone = true; if (pl.history) State.chatHistory = pl.history; }
        }
      }
    } catch (e) {
      if (!acc) acc = `${ico("warn")} 对话失败：${e.message}`;
    }
    if (!gotDone) {
      State.chatHistory.push({ role: "assistant", content: acc || "（无响应）" });
      State.chatHistory = State.chatHistory.slice(-16);
    }
    this.renderChat();
    // renderChat 整体重绘会带走流式期间的思维链/提示节点，这里重新挂到最后一条气泡上。
    // 思维链不入 history（它不是回复正文），所以只能由本次请求的内存变量重建。
    const lastBot = [...box.querySelectorAll(".msg.bot .bubble")].pop();
    if (lastBot) {
      if (thinking) lastBot.prepend(makeThinkNode(thinking));
      if (aux.textContent && aux.style.display !== "none") {
        const note = document.createElement("div");
        note.className = "chat-aux";
        note.textContent = aux.textContent;
        lastBot.prepend(note);
      }
    }
    // 对话工具可能重跑了调度 → 拉新数据
    const pr = await API.get("/api/progress");
    if (!pr.running && pr.solved) {
      State.pages = {};
      if (!State.solving) this.showPage(State.page);
    }
  },

  /* ---------- 上传 ---------- */
  async doUpload(file) {
    const resBox = $("#upload-result");
    resBox.className = "upload-msg busy";
    resBox.innerHTML = ico("clock") + " 正在解析文件…";
    // 兜底 try/catch——此前网络断/401/5xx 时 unhandled rejection，
    // 弹窗永久卡在"解析中…"假死
    try {
      const r = await API.upload(file);
      resBox.className = "upload-msg " + (r.ok ? "ok" : "err");
      resBox.textContent = r.msg || (r.ok ? "已加载" : "解析失败");
      if (r.ok) {
        toast(r.msg, "ok");
        State.boot = await API.get("/api/bootstrap");
        State.pages = {};
        this.renderDataCaption();
        this.refreshUploadDlg();
        setTimeout(() => { $("#upload-dlg").close(); this.runSolve(); }, 700);
      } else {
        toast(r.msg || "解析失败", "err");
      }
    } catch (e) {
      resBox.className = "upload-msg err";
      resBox.innerHTML = ico("warn") + " " + esc(e.message);
      toast("上传失败：" + e.message, "err");
    }
  },
  refreshUploadDlg() {
    const di = State.boot.data_info;
    const rb = $("#upload-result");
    rb.className = "upload-msg" + (di.is_custom ? " ok" : "");
    rb.textContent = di.is_custom ? `当前已加载自定义数据：${di.rows} 条 · ${di.days} 天` : "";
    $("#upload-loaded").style.display = di.is_custom ? "block" : "none";
  },

  /* ---------- 主题 ---------- */
  setTheme(t) {
    State.theme = t;
    // 跟随系统模式下不落盘，保持自动切换能力
    if (localStorage.getItem("theme_follow") !== "1") localStorage.setItem("theme", t);
    document.documentElement.setAttribute("data-theme", t);
    this.renderThemeIcon();
    // 主题是纯前端关注点——保留数据缓存，仅用缓存重渲染当前页。
    // 图表配色在渲染时经 chartPalette() 读取 State.theme，无需重拉后端数据。
    if (State.solving) return;
    // P1：推迟一帧渲染，让 body/卡片背景渐变先启动，消除主题切换的整页"瞬切感"
    requestAnimationFrame(() => setTimeout(() => this.renderPage(State.page, true), 60));
  },
  renderThemeIcon() {
    // 路径不再抄第二份：sun/moon 与 ICONS 里逐字相同（当初就是从这里复制的）。
    // 顶栏要的是强调色，所以用 ico() 的第二个参数挂 .ico-accent，由 CSS 覆盖 stroke。
    $("#btn-theme").innerHTML = State.theme === "light"
      ? ico("sun", "ico-accent")
      : ico("moon", "ico-accent");
  },
});

/* 对话面板默认收起，展开状态由 localStorage 恢复 */
try {
  if (localStorage.getItem("chatPanelCollapsed") === "0") document.body.classList.add("chat-open");
} catch (e) {}

/* P2：resize 防抖——拖拽窗口时不再每帧对全部 ECharts 实例调 resize() */
let _resizeTimer;
window.addEventListener("resize", () => { clearTimeout(_resizeTimer); _resizeTimer = setTimeout(reflowCharts, 150); });
/* P1：系统深浅色变化时跟随（仅当用户未手动设置过主题） */
window.matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", (e) => {
  if (!localStorage.getItem("theme")) App.setTheme(e.matches ? "dark" : "light");
});
document.addEventListener("DOMContentLoaded", () => App.boot());
