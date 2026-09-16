/* ================= API 层 + 全局状态 ================= */
/* P0-02：JWT 会话——token 存 localStorage，401 统一弹登录层 */
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
  /* P0-F3 修复：统一 DELETE 通道——此前清除上传用裸 fetch，401 时只弹失败 toast
     而不走 _handleResp 的统一登录浮层；未来 API 层新增安全策略对此接口也无效。 */
  async delete(path) {
    const r = await fetch(path, { method: "DELETE", headers: authHeaders() });
    return _handleResp(r);
  },
  /* P0-1 修复：带 Authorization 的文件下载（替代 location.href 导航——导航无法携带
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
  el.textContent = msg;
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
