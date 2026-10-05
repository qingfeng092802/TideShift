/* ================= API 层 + 全局状态 ================= */

/* ---------- 图标：与侧栏同一套描边风格（24 网格 / stroke 1.8 / 圆头 / currentColor）
   唯一事实来源是这里的 ICONS：JS 模板里写 ${ico("name")}，静态 HTML 里写
   <i class="ico-slot" data-icon="name"></i> 由 hydrateIcons() 在 boot 时填。
   尺寸一律走 font-size（见 app.css 的 svg.ico），所以图标自动跟着所在容器
   （按钮 13px、面板标题 13px、空状态 28px、登录 logo 34px）缩放，不再各处写死。 */
const ICON_STROKE_ATTRS = 'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"';
const ICONS = {
  /* 导航那 7 个：键名即 NAV 的 icon 字段（app.js），路径只有这一份 */
  dashboard: "M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z",
  bolt: "M13 2L3 14h7l-1 8 11-13h-8l1-7z",
  trend: '<path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-6"/>',
  thermometer: "M14 4v10.5a4 4 0 1 1-4 0V4a2 2 0 1 1 4 0zM12 9v8",
  bulb: "M12 2a7 7 0 0 1 7 7c0 2.4-1.2 4.2-2 5.5-.6 1-.9 1.8-1 2.5h-8c-.1-.7-.4-1.5-1-2.5-.8-1.3-2-3.1-2-5.5a7 7 0 0 1 7-7zM9 21h6",
  history: "M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8M3 3v5h5M12 7v5l4 2",
  gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm7.4-3a7.4 7.4 0 0 0-.1-1.2l2-1.5-2-3.5-2.4 1a7.5 7.5 0 0 0-2-1.2L14.5 3h-5l-.4 2.6a7.5 7.5 0 0 0-2 1.2l-2.4-1-2 3.5 2 1.5a7.4 7.4 0 0 0 0 2.4l-2 1.5 2 3.5 2.4-1a7.5 7.5 0 0 0 2 1.2l.4 2.6h5l.4-2.6a7.5 7.5 0 0 0 2-1.2l2.4 1 2-3.5-2-1.5c.1-.4.1-.8.1-1.2z",
  /* 动作类 */
  play: '<path d="M7 4.5l12.5 7.5L7 19.5z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  chevron: '<path d="M6.5 9.5l5.5 5.5 5.5-5.5"/>',
  chevronRight: '<path d="M9.5 6.5l5.5 5.5-5.5 5.5"/>',
  send: '<path d="M21 3.5L3.5 10.8l7 2.7 2.7 7z"/><path d="M10.5 13.5L21 3.5"/>',
  download: '<path d="M12 3.5v11"/><path d="M7.5 10.5l4.5 4.5 4.5-4.5"/><path d="M4 20h16"/>',
  upload: '<path d="M12 16.5v-11"/><path d="M7.5 9.5L12 5l4.5 4.5"/><path d="M4 20h16"/>',
  refresh: '<path d="M20.5 11.5a8.5 8.5 0 1 0-2.6 7"/><path d="M20.5 5v6.5H14"/>',
  save: '<path d="M5 4h11l3 3v13H5z"/><path d="M8.5 4v5h6.5V4"/><path d="M8.5 13.5h7V20h-7z"/>',
  trash: '<path d="M4 7h16"/><path d="M9.5 7V4h5v3"/><path d="M6.5 7l1 13h9l1-13"/><path d="M10.5 11v6M13.5 11v6"/>',
  pencil: '<path d="M4 20l4.2-1L20 7.2 16.8 4 5 15.8z"/><path d="M14.5 6.2l3.3 3.3"/>',
  eye: '<path d="M2.5 12S6 6.8 12 6.8 21.5 12 21.5 12 18 17.2 12 17.2 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.7"/>',
  eyeOff: '<path d="M2.5 12S6 6.8 12 6.8 21.5 12 21.5 12 18 17.2 12 17.2 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.7"/><path d="M4.5 4l15 15"/>',
  /* 状态类 */
  check: '<path d="M4.5 12.5l5 5 10-11"/>',
  cross: '<path d="M6 6l12 12M18 6L6 18"/>',
  warn: '<path d="M12 3.6L2.6 20.2h18.8z"/><path d="M12 9.4v5"/><path d="M12 17.4v.2"/>',
  ban: '<circle cx="12" cy="12" r="8.6"/><path d="M6 18L18 6"/>',
  circle: '<circle cx="12" cy="12" r="8"/>',
  info: '<circle cx="12" cy="12" r="8.6"/><path d="M12 11v5.2"/><path d="M12 7.7v.2"/>',
  clock: '<circle cx="12" cy="12" r="8.6"/><path d="M12 7.2v5.2l3.6 2.1"/>',
  shield: '<path d="M12 3l7.2 3v5.8c0 4.2-3 7.7-7.2 9.2-4.2-1.5-7.2-5-7.2-9.2V6z"/>',
  /* 数据/图表类 */
  bars: '<path d="M4 20h16"/><path d="M7.5 20v-8M12 20V5.5M16.5 20v-5.5"/>',
  lineDown: '<path d="M3 3v18h18"/><path d="M7 8l4 4 3-3 5 5"/>',
  money: '<circle cx="12" cy="12" r="8.6"/><path d="M14.6 9.3c-.6-.9-1.5-1.4-2.6-1.4-1.6 0-2.6.9-2.6 2.1 0 1.3 1.1 1.8 2.7 2.1 1.7.3 2.8.8 2.8 2.2 0 1.3-1.1 2.2-2.8 2.2-1.2 0-2.2-.5-2.8-1.5"/><path d="M12 6.2v11.6"/>',
  ruler: '<path d="M3 16.6L16.6 3 21 7.4 7.4 21z"/><path d="M8 13l1.6 1.6M11 10l1.6 1.6M14 7l1.6 1.6"/>',
  target: '<circle cx="12" cy="12" r="8.4"/><circle cx="12" cy="12" r="3.4"/>',
  gauge: '<path d="M4 18.5a8.5 8.5 0 1 1 16 0"/><path d="M12 18.5l4.2-6.3"/>',
  /* 面板/文档类 */
  clipboard: '<rect x="6" y="4.5" width="12" height="16.5" rx="2"/><path d="M9.5 4.5V3h5v1.5"/><path d="M9.5 10h5M9.5 13.5h5M9.5 17h3"/>',
  list: '<path d="M8.5 6.5h11.5M8.5 12h11.5M8.5 17.5h11.5"/><path d="M4.3 6.5h.1M4.3 12h.1M4.3 17.5h.1"/>',
  code: '<path d="M9.2 7.5L4.6 12l4.6 4.5"/><path d="M14.8 7.5L19.4 12l-4.6 4.5"/>',
  plug: '<path d="M9 3.5v4.6M15 3.5v4.6"/><path d="M6.4 8.1h11.2v3.4a5.6 5.6 0 0 1-11.2 0z"/><path d="M12 17.1v3.4"/>',
  compass: '<circle cx="12" cy="12" r="8.6"/><path d="M15.2 8.8l-2 4.4-4.4 2 2-4.4z"/>',
  search: '<circle cx="11" cy="11" r="6.6"/><path d="M15.8 15.8L20.5 20.5"/>',
  inbox: '<path d="M4 13.2l2.6-8.2h10.8l2.6 8.2V19.5H4z"/><path d="M4 13.2h4.2l1.4 2.8h4.8l1.4-2.8H20"/>',
  archive: '<rect x="3.5" y="5" width="17" height="4.6" rx="1.4"/><path d="M5.4 9.6v9.4h13.2V9.6"/><path d="M10 13.6h4"/>',
  receipt: '<path d="M6 3.5h12V21l-3-2-3 2-3-2-3 2z"/><path d="M9 8.5h6M9 12.5h6"/>',
  box: '<path d="M12 3.2l7.8 4.4v8.8L12 20.8 4.2 16.4V7.6z"/><path d="M4.2 7.6L12 12l7.8-4.4M12 12v8.8"/>',
  folder: '<path d="M4 7.5a2.2 2.2 0 0 1 2.2-2.2H10l2 2.5h5.8A2.2 2.2 0 0 1 20 10v8a2.2 2.2 0 0 1-2.2 2.2H6.2A2.2 2.2 0 0 1 4 18z"/>',
  /* 侧栏/顶栏原有的两枚手写 SVG，路径原样搬进来：图形不变，但只有一份来源，
     尺寸也不再写在 svg 上（.sb-btn .ico-slot / .icon-btn svg 给）。 */
  folderPlus: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><path d="M12 11v6M9 14h6"/>',
  panelLeft: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
  cpu: '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10.5 3v4M13.5 3v4M10.5 17v4M13.5 17v4M3 10.5h4M3 13.5h4M17 10.5h4M17 13.5h4"/>',
  bot: '<rect x="4" y="8.5" width="16" height="11.5" rx="3"/><path d="M12 4.5v4"/><circle cx="12" cy="3.4" r="1.3"/><path d="M9.3 13v2M14.7 13v2"/>',
  user: '<circle cx="12" cy="8" r="3.6"/><path d="M5 20.2c1.2-3.6 3.8-5.4 7-5.4s5.8 1.8 7 5.4"/>',
  battery: '<rect x="3" y="8" width="15" height="8.5" rx="2"/><path d="M21 11v2.5"/><path d="M7 11v2.5M10.6 11v2.5"/>',
  palette: '<path d="M12 3.2a8.8 8.8 0 1 0 0 17.6c1.2 0 2.1-.9 2.1-2.1 0-1.2-.9-1.7-.9-2.6 0-.8.7-1.4 1.5-1.4h1.6a4.4 4.4 0 0 0 4.4-4.4c0-4.2-4-8.1-8.7-8.1z"/><circle cx="8.4" cy="10.6" r="1"/><circle cx="12" cy="7.4" r="1"/><circle cx="15.6" cy="10.2" r="1"/>',
  sliders: '<path d="M5 3.5v6M5 14.5v6M12 3.5v4M12 12.5v8M19 3.5v10M19 18.5v2"/><circle cx="5" cy="12.2" r="2"/><circle cx="12" cy="10.2" r="2"/><circle cx="19" cy="16.2" r="2"/>',
  lock: '<rect x="5" y="10.8" width="14" height="9.4" rx="2.2"/><path d="M8.2 10.8V8a3.8 3.8 0 0 1 7.6 0v2.8"/>',
  hexagon: '<path d="M12 3.2l7.6 4.4v8.8L12 20.8 4.4 16.4V7.6z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41-1.41"/>',
  moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
};

function ico(name, extraClass) {
  const body = ICONS[name];
  if (!body) return "";                     // 认不出的名字宁可什么都不画，也不要画出半个 emoji
  const inner = body.startsWith("<") ? body : `<path d="${body}"/>`;
  return `<svg class="ico${extraClass ? " " + extraClass : ""}" viewBox="0 0 24 24" ${ICON_STROKE_ATTRS} aria-hidden="true" focusable="false">${inner}</svg>`;
}

/* 静态 HTML 里的 <i class="ico-slot" data-icon="name"></i> 槽位 → 同一套 SVG，
   这样 index.html 不必把路径抄第二份（两处抄必然漂移）。
   属性名刻意用 data-icon 而不是 data-i：makeTabs()（下方页内 tab 工厂）早就拿
   `data-i="${i}"` 当页签序号用了，全局选择器 [data-i] 会一并选中它们，
   然后 ico("0") 认不出名字返回空串 ⇒ 页签文字被 innerHTML="" 抹平。 */
function hydrateIcons(root) {
  (root || document).querySelectorAll(".ico-slot[data-icon]").forEach((el) => {
    if (el.dataset.iconDone === "1") return;
    el.innerHTML = ico(el.dataset.icon);
    el.dataset.iconDone = "1";
  });
}

/* 后端文案里带的状态字符换成注册表图标。两个入口：
   - applyTextIcons(已转义串)：给 markdown.js 的行内管线用（它开头已对整段 esc 过）；
   - iconize(原始串)：先 esc 再替换，给直接写 innerHTML 的落点用。
   为什么在呈现层做而不改后端：这些串是 chat_agent / server 的产品文案，
   测试与 docs 里引用原文，改一处要连改一片口径。
   落点：#solve-label、#solve-log、热管理校验表 <td>、toast()、
        对话气泡与 AI 决策解释（经 renderMarkdown 的 inline 管线）。 */
const TEXT_ICON = [
  /* 求解进度：backend/server.py:246-251 六个阶段标签全部以 ✓ 开头 */
  [/\u{2713}|\u{2714}/gu, "check"],                                   // ✓ ✔
  [/\u{2717}|\u{2718}/gu, "cross"],                                   // ✗ ✘
  [/\u{2705}\u{FE0F}?/gu, "check"],                                   // ✅
  [/\u{274C}\u{FE0F}?/gu, "cross"],                                   // ❌
  [/\u{26A0}\u{FE0F}?/gu, "warn"],                                    // ⚠️
  [/\u{26D4}/gu, "ban"],                                              // ⛔
  [/\u{1F6D1}/gu, "ban"],                                             // 🛑
  [/\u{26AA}/gu, "circle"],                                           // ⚪
  /* 规则模式对话（src/agents/chat_agent.py）每条答案的行首标记 */
  [/\u{23F0}\u{FE0F}?|\u{23F3}|\u{23F1}\u{FE0F}?/gu, "clock"],        // ⏰  ⏱️
  [/\u{1F4A1}/gu, "info"],                                            // 💡 提示语义（bulb 归需求响应）
  [/\u{1F4CA}/gu, "bars"],                                            // 📊
  [/\u{1F4C8}/gu, "trend"],                                           // 📈
  [/\u{1F321}\u{FE0F}?/gu, "thermometer"],                            // 🌡️
  [/\u{1F4E1}/gu, "bulb"],                                            // 📡 与侧栏「需求响应」同图形
  [/\u{1F504}/gu, "refresh"],                                         // 🔄
  [/\u{1F50D}/gu, "search"],                                          // 🔍
  [/\u{1F916}/gu, "bot"],                                             // 🤖
  [/\u{1F4CB}/gu, "clipboard"],                                       // 📋
];
function applyTextIcons(escaped) {
  let h = escaped;
  for (const [re, name] of TEXT_ICON) h = h.replace(re, ico(name));
  return h;
}
function iconize(s) { return applyTextIcons(esc(s)); }

/* JWT 会话——token 存 localStorage，401 统一弹登录层 */
const AUTH_TOKEN_KEY = "jwt_token";
function authHeaders() {
  const t = localStorage.getItem(AUTH_TOKEN_KEY);
  return t ? { "Authorization": "Bearer " + t } : {};
}
function authLogout() { localStorage.removeItem(AUTH_TOKEN_KEY); }

async function _handleResp(r) {
  if (r.status === 401) {
    authLogout();
    if (window.__showLogin) window.__showLogin();
    const e = new Error("未登录或会话已过期"); e.status = 401; throw e;
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    // must_change 期间的 403 必须在这里就地转成"改密引导"，不能只抛错：
    // boot() 里第一个受保护请求就是 /api/bootstrap，它一抛，后面的 renderNav /
    // bindGlobal / showPage 全都不执行 —— 页面停在白屏，改密浮层永远没有触发点。
    // 注意**不**清 token：/api/auth/change-password 自身要带 JWT。
    if (r.status === 403 && body && body.must_change) {
      if (window.__requireChangePassword) window.__requireChangePassword();
      const e = new Error(body.detail || "首次登录请先修改初始口令");
      e.status = 403; e.mustChange = true; e.body = body; throw e;
    }
    const detail = Array.isArray(body.detail)
      ? body.detail.map((d) => `${(d.loc || []).slice(-1)[0] || ""}: ${d.msg}`).join("；")
      : body.detail;
    const e = new Error(detail || r.statusText); e.status = r.status; e.body = body;
    throw e;
  }
  return r.json();
}
const API = {
  async get(path) {
    const r = await fetch(path, { headers: authHeaders() });
    return _handleResp(r);
  },
  async post(path, data) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: data === undefined ? "{}" : JSON.stringify(data),
    });
    return _handleResp(r);
  },
  async upload(file) {
    const fd = new FormData();
    fd.append("file", file);
    const r = await fetch("/api/upload", { method: "POST", headers: authHeaders(), body: fd });
    return _handleResp(r);
  },
  /* 统一 DELETE 通道——此前清除上传用裸 fetch，401 时只弹失败 toast
     而不走 _handleResp 的统一登录浮层；未来 API 层新增安全策略对此接口也无效。 */
  async delete(path) {
    const r = await fetch(path, { method: "DELETE", headers: authHeaders() });
    return _handleResp(r);
  },
  /* 带 Authorization 的文件下载（替代 location.href 导航——导航无法携带
     JWT 头，在后端全局 /api/* 认证中间件下必 401）。401 时走统一的登录浮层。 */
  async download(path) {
    const r = await fetch(path, { headers: authHeaders() });
    if (!r.ok) {
      try { await _handleResp(r); }
      catch (e) {
        // 409 = 求解未完成（ensure_solved 拦截），给用户可读的中文提示
        if (e.status === 409) e.message = "调度结果尚未生成（可能正在求解），请等进度条完成后重试";
        throw e;
      }
    }
    const blob = await r.blob();
    const name = (r.headers.get("Content-Disposition") || "").match(/filename=([^;]+)/);
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name ? name[1].replace(/^"|"$/g, "") : "export.csv";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 100);
  },
};

/* ---------- 全局状态 ---------- */
/* P1：首次访问尊重系统深浅色偏好；用户手动切换后以 localStorage 为准 */
const _sysDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
const State = {
  page: "dashboard",
  theme: localStorage.getItem("theme") || (_sysDark ? "dark" : "light"),
  boot: null,          // /api/bootstrap
  pages: {},           // 每页 payload 缓存 {dashboard: {...}}
  solving: false,
  charts: [],          // 当前页 echarts 实例（主题切换时销毁重建）
  subTabs: {},         // 页内 tab 状态
  thermalMode: "eng",  // 工程可行 / 纯经济
  thermalScope: [0, 24],
  mpEditId: null,
};

/* ---------- 工具 ---------- */
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtMoney(v) { return Math.abs(v - Math.round(v)) < 0.005 ? `¥${v.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}` : `¥${v.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`; }
function fmtNum(v, d = 0) { return v.toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d }); }
function fmtPct(v) { return `${v.toFixed(1)}%`; }

function toast(msg, type = "") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  /* iconize = 先 esc 再换图标：toast 里会出现后端串（pr.warning 的 "⚠️ DR事件时间重叠…"） */
  el.innerHTML = iconize(msg);
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; setTimeout(() => el.remove(), 320); }, 3400);
}

/* P1：promise 化确认弹窗——替代原生 confirm()，与全局 UI 风格一致，返回 Promise<boolean> */
function uiConfirm(msg) {
  return new Promise((resolve) => {
    const d = document.createElement("dialog");
    d.className = "ui-confirm";
    d.innerHTML = `
      <div class="dlg-body" style="white-space:pre-wrap;font-size:13px;line-height:1.7;">${esc(msg)}</div>
      <div style="display:flex;gap:10px;justify-content:flex-end;padding:0 20px 18px;">
        <button class="btn" data-r="0">取消</button>
        <button class="btn btn-primary" data-r="1">确认</button>
      </div>`;
    document.body.appendChild(d);
    d.showModal();
    d.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-r]");
      if (b) d.close(b.dataset.r);
    });
    d.addEventListener("close", () => { d.remove(); resolve(d.returnValue === "1"); });
  });
}

/* P1：promise 化输入弹窗——替代原生 prompt()（首次改密场景），与全局 UI 风格一致 */
function uiPrompt(msg, opts = {}) {
  return new Promise((resolve) => {
    const d = document.createElement("dialog");
    d.className = "ui-confirm";
    d.innerHTML = `
      <div class="dlg-body" style="white-space:pre-wrap;font-size:13px;line-height:1.7;">${esc(msg)}</div>
      <div style="padding:0 20px 4px;">
        <input type="${opts.type || "password"}" id="ui-prompt-input" class="field-input" autocomplete="new-password"
               style="width:100%;background:var(--bg-input);border:1px solid var(--border);color:var(--text-1);border-radius:8px;padding:8px 11px;font-size:13px;outline:none;"
               ${opts.minLength ? `minlength="${opts.minLength}"` : ""}>
      </div>
      <div style="display:flex;gap:10px;justify-content:flex-end;padding:14px 20px 18px;">
        <button class="btn" data-r="0">取消</button>
        <button class="btn btn-primary" data-r="1">确认</button>
      </div>`;
    document.body.appendChild(d);
    d.showModal();
    setTimeout(() => $("#ui-prompt-input", d).focus(), 50);
    d.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-r]");
      if (b) d.close(b.dataset.r);
    });
    d.addEventListener("keydown", (e) => { if (e.key === "Enter") d.close("1"); });
    d.addEventListener("close", () => {
      const ok = d.returnValue === "1";
      const val = $("#ui-prompt-input", d).value;
      d.remove();
      resolve(ok ? val : null);
    });
  });
}

/* 页内 tab 工厂 */
function makeTabs(containerId, tabs, renderFn) {  const box = $(containerId);
  box.innerHTML = `<div class="subtabs">${tabs.map((t, i) =>
    `<button class="subtab${i === 0 ? " active" : ""}" data-i="${i}">${t}</button>`).join("")}</div>
    ${tabs.map((_, i) => `<div class="tabpane${i === 0 ? " active" : ""}" data-i="${i}"></div>`).join("")}`;
  $$(".subtab", box).forEach((b) => b.addEventListener("click", () => {
    $$(".subtab", box).forEach((x) => x.classList.remove("active"));
    $$(".tabpane", box).forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $(`.tabpane[data-i="${b.dataset.i}"]`, box).classList.add("active");
    renderFn(Number(b.dataset.i), $(`.tabpane[data-i="${b.dataset.i}"]`, box));
  }));
  renderFn(0, $(".tabpane[data-i='0']", box));
}
