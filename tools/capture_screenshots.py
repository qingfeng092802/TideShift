#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""README 界面截图采集脚本 —— 驱动真实浏览器跑真实链路，而不是摆拍。

为什么需要这个脚本
------------------
2026-09-16 那批截图是一次性手工录的，脚本没入库。结果电价改版（`e0bcae8`）与
图标/布局改版（1.2.0~1.2.2）之后没人能低成本重录，README 只能挂着"图已作废"的
说明继续发布。**重录成本高于收益时，文档就会开始说谎** —— 这个脚本把重录压回
一条命令。

设计约束
--------
- **零新增依赖**：只用标准库手写 CDP over WebSocket（RFC 6455 客户端帧）。本机
  虽有 `websocket-client` / `playwright`，但都不在 `requirements*.txt` 里，为一个
  文档工具引入依赖不划算，也违背"完全离线可用"的项目口径。
- **不必装 Playwright 浏览器**：优先 `--chrome` 显式路径 → 其次
  `%LOCALAPPDATA%/ms-playwright/chromium-*/chrome-win64/chrome.exe` → 最后系统 Chrome。
- **口令只从环境变量读**（默认 `CAPTURE_ADMIN_PASSWORD`），绝不进 argv ——
  命令行会被 `ps`、shell 历史与 CI 日志留下明文副本。
- **必须打真实求解**：截图里的数字要能对上 README 量化成果表，所以脚本点
  「运行调度」并等 MILP 真跑完，而不是截一个空看板。

用法
----
    # 1) 起一个隔离实例（不碰用户自己的 config/auth.json）
    set AUTH_CONFIG_DIR=%LOCALAPPDATA%\\TideShift-capture-auth
    set ENERGY_AUTH_MODE=env
    set ADMIN_INITIAL_PASSWORD=***
    set ENERGY_PORT=8802
    python backend/server.py

    # 2) 采集
    set CAPTURE_ADMIN_PASSWORD=***
    python tools/capture_screenshots.py --base-url http://127.0.0.1:8802

只想看页内读数、不写文件时加 `--probe`。
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "docs" / "screenshots"
TOKEN_KEY = "jwt_token"          # 与 web/js/api.js 的 AUTH_TOKEN_KEY 同源


# --------------------------------------------------------------------------
# 极简 CDP 客户端：标准库实现 WebSocket，无第三方依赖
# --------------------------------------------------------------------------
class CDP:
    """Chrome DevTools Protocol 客户端（同步、单页面、够一个脚本用）。"""

    def __init__(self, ws_url: str, timeout: float = 300.0):
        rest = ws_url.split("://", 1)[1]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self._s = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self._s.settimeout(timeout)
        self._buf = b""
        self._id = 0
        self._handshake(host, port or "80", "/" + path)

    def _handshake(self, host: str, port: str, path: str) -> None:
        key = base64.b64encode(uuid.uuid4().bytes).decode()[:24]
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self._s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._s.recv(1)
            if not chunk:
                raise ConnectionError("WebSocket 握手时被对端关闭")
            resp += chunk
        status = resp.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise ConnectionError("WebSocket 握手失败: " + status.decode("latin-1"))

    # ---- 帧编解码：客户端出帧必须掩码，服务端入帧不掩码 -------------------
    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._s.recv(65536)
            if not chunk:
                raise ConnectionError("WebSocket 连接中断")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        head = bytearray([0x80 | opcode])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = uuid.uuid4().bytes[:4]
        head += mask
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._s.sendall(bytes(head) + body)

    def _send_json(self, obj: dict) -> None:
        self._frame(0x1, json.dumps(obj).encode())

    def _recv_json(self) -> dict | None:
        while True:
            head = self._recv_exact(2)
            fin, opcode = bool(head[0] & 0x80), head[0] & 0x0F
            ln = head[1] & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._recv_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._recv_exact(8))[0]
            payload = self._recv_exact(ln) if ln else b""
            if opcode == 0x8:
                raise ConnectionError("CDP 对端关闭了会话")
            if opcode == 0x9:                      # ping → pong
                self._frame(0xA, payload)
                continue
            if not fin:                            # 分片：拼到 FIN 为止
                parts = [payload]
                while True:
                    h2 = self._recv_exact(2)
                    l2 = h2[1] & 0x7F
                    if l2 == 126:
                        l2 = struct.unpack(">H", self._recv_exact(2))[0]
                    elif l2 == 127:
                        l2 = struct.unpack(">Q", self._recv_exact(8))[0]
                    p2 = self._recv_exact(l2) if l2 else b""
                    parts.append(p2)
                    if h2[0] & 0x80:
                        break
                payload = b"".join(parts)
            if opcode != 0x1:
                continue
            try:
                return json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None

    # ---- 调用 -------------------------------------------------------------
    def call(self, method: str, **params) -> dict:
        self._id += 1
        msg = {"id": self._id, "method": method}
        if params:
            msg["params"] = params
        self._send_json(msg)
        while True:                                # 事件帧直接丢，只等本请求
            got = self._recv_json()
            if got is None or got.get("id") != self._id:
                continue
            if "error" in got:
                raise RuntimeError(f"{method} 失败：{got['error']}")
            return got.get("result", {})

    def js(self, body: str, await_promise: bool = True):
        """把 body 当函数体执行 —— 页内可以用 `return`，也能跑 await fetch()。

        一律 `awaitPromise=True`：表达式被包成 async 函数后**必然**返回 Promise，
        不 await 的话 returnByValue 会把它序列化成 `{}`，而空 dict 在 Python 里是
        假值 —— 于是所有判据恒不成立、wait_for 一路超时（本机真实踩过）。
        """
        src = "(async function(){" + body + "})()"
        r = self.call("Runtime.evaluate", expression=src,
                      returnByValue=True, awaitPromise=await_promise)
        exc = r.get("exceptionDetails")
        if exc:
            raise RuntimeError("页内 JS 抛错：" + json.dumps(exc, ensure_ascii=False)[:400])
        return r.get("result", {}).get("value")

    def wait_for(self, body: str, what: str, timeout: float = 120.0,
                 interval: float = 0.4) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.js(body):
                    return
            except RuntimeError:
                pass                              # 页面正在跳转，下一轮再问
            time.sleep(interval)
        raise TimeoutError(f"等待超时（{timeout:.0f}s）：{what}")

    def png(self) -> bytes:
        r = self.call("Page.captureScreenshot", format="png",
                      captureBeyondViewport=False)
        return base64.b64decode(r["data"])

    def close(self) -> None:
        try:
            self._s.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# 浏览器与调试目标发现
# --------------------------------------------------------------------------
def find_chrome(explicit: str = "") -> str:
    cands: list[str] = [explicit, os.environ.get("CHROME_PATH", "")]
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        cands += sorted(glob.glob(os.path.join(
            local, "ms-playwright", "chromium-*", "chrome-win64", "chrome.exe")), reverse=True)
    cands += [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
    for c in cands:
        if c and Path(c).exists():
            return c
    raise SystemExit("找不到 Chromium/Chrome：用 --chrome 指定路径，或设 CHROME_PATH。")


def launch_chrome(exe: str, profile: Path) -> subprocess.Popen:
    return subprocess.Popen([
        exe, "--headless=new",
        "--remote-debugging-port=0",       # 让 Chrome 自选端口，绝不撞用户实例
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        # 本机实测：带 sandbox 的 headless Chrome 会在写出 DevToolsActivePort 之前就退出
        # （受限沙箱策略），所以必须 --no-sandbox。风险由三点兜住：profile 是一次性的、
        # 只访问 127.0.0.1 上的本项目端口、进程用完即杀。
        "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1",   # 1 CSS px = 1 物理 px，尺寸才可复现
        "--lang=zh-CN", "--accept-lang=zh-CN",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def read_devtools_port(profile: Path, timeout: float = 40.0) -> int:
    """`--remote-debugging-port=0` 时真实端口写在 DevToolsActivePort 第一行。"""
    f = profile / "DevToolsActivePort"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            first = f.read_text(encoding="utf-8").splitlines()[0].strip()
            if first:
                return int(first)
        except (OSError, ValueError):
            time.sleep(0.2)
    raise SystemExit("Chrome 没写出 DevToolsActivePort —— 进程可能被 SAC/杀软拦下了")


def page_target(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as r:
        for t in json.load(r):
            if t.get("type") == "page":
                return t["webSocketDebuggerUrl"]
    raise SystemExit("Chrome 没有可用的 page target")


# --------------------------------------------------------------------------
# 采集流程
# --------------------------------------------------------------------------
def login(cdp: CDP, base: str, user: str, password: str) -> None:
    cdp.call("Page.navigate", url=base + "/")
    cdp.wait_for("return document.readyState==='complete'", "页面加载", 60)
    cdp.wait_for("return !!document.getElementById('login-veil')", "登录层出现", 30)
    cdp.js(
        "var u=document.getElementById('login-user'),p=document.getElementById('login-pass');"
        f"u.value={json.dumps(user)};p.value={json.dumps(password)};"
        "u.dispatchEvent(new Event('input',{bubbles:true}));"
        "p.dispatchEvent(new Event('input',{bubbles:true}));"
        "document.getElementById('login-btn').click();return true")
    # 登录成功后 app.js 会整页 reload。判据必须同时要求登录层消失：欢迎页在登录前
    # 就在底下渲染着，只等 #wl-enter 存在会立刻为真，于是截到一张盖着登录层的图。
    cdp.wait_for("return !document.getElementById('login-veil')"
                 "&& !!document.getElementById('wl-enter')", "登录完成并回到欢迎页", 60)
    msg = cdp.js("return (document.getElementById('login-msg')||{}).textContent||''") or ""
    if msg:
        print(f"  ! 登录层残留提示：{msg}")


PROGRESS_DONE = (
    "return fetch('/api/progress',{headers:{Authorization:'Bearer '"
    "+localStorage.getItem('%s')}}).then(function(r){return r.json()})"
    ".then(function(j){return !!j.solved && !j.running})"
    ".catch(function(){return false})" % TOKEN_KEY)


def run_solve(cdp: CDP, timeout: float) -> dict:
    """点「运行调度」并等 MILP 真跑完；返回看板读数，用于核对截图口径。

    完成判据取 `/api/progress` 的 `solved && !running`，而不是"遮罩消失"——
    遮罩在点按钮后 0.9 s 的轮询里才出现，直接等它消失会误判成"已经跑完"。
    """
    cdp.js("return document.getElementById('wl-enter').click(), true")
    cdp.wait_for("return !!document.getElementById('op-run')", "看板渲染", 60)
    time.sleep(1.0)                                  # 等首屏 KPI 从占位切到真实数据
    cdp.js("return document.getElementById('op-run').click(), true")
    cdp.wait_for(PROGRESS_DONE, "MILP 求解出结果", timeout, interval=1.0)
    cdp.wait_for("return getComputedStyle(document.getElementById('solve-veil')).display==='none'",
                 "求解遮罩收起", 30)
    return read_dashboard(cdp)


READ_DASHBOARD = """
var q=function(s){var e=document.querySelector(s);return e?(e.textContent||'').trim():''};
var out={};
out.version=q('#wl-version');
out.brand=q('#brand-sub');
out.date=(document.getElementById('op-date')||{}).value||'';
out.kpi=[].slice.call(document.querySelectorAll('.kpi-card'))
        .map(function(c){return ((c.querySelector('.kpi-value')||{}).textContent||'').trim()
            +' | '+((c.querySelector('.kpi-label')||{}).textContent||'').trim()});
var note='';
[].forEach.call(document.querySelectorAll('.info-note'),function(e){
  if((e.textContent||'').indexOf('年化')>=0)note=(e.textContent||'').trim()});
out.annual_note=note;
out.emoji=(document.body.innerText.match(/[\\u{1F300}-\\u{1FAFF}\\u{2600}-\\u{27BF}]/gu)||[]).length;
return JSON.stringify(out);"""


def read_dashboard(cdp: CDP) -> dict:
    raw = cdp.js(READ_DASHBOARD)
    return json.loads(raw) if raw else {}


def nav(cdp: CDP, page: str) -> None:
    cdp.js("var b=document.querySelector('#nav .nav-item[data-page=\"%s\"]');"
           "if(!b)throw new Error('找不到侧栏项 %s');b.click();return true" % (page, page))
    time.sleep(1.4)                                  # 页面切换 + ECharts 入场动画


def collapse_explain(cdp: CDP) -> None:
    """把「AI 决策解释」折叠回收缩状态再截总览页。

    解释层是 LLM 生成时默认展开（`#ai-exp.open`），一屏中文长文会把「收益对比」表、
    年化行与基本电费卡整段推出首屏 —— 缩略图尺寸下只剩一坨读不清的正文。
    README 这张图要一眼看见的是钱与曲线，所以点 `.head` 收起来。

    解释正文是**求解完成后异步**渲染的（`#ai-exp` 先以未展开状态存在，LLM 回来才加
    `open`），所以必须"等一下再点、点完再验"。第一版没验，折叠跑在展开之前，
    于是静默失效、截出一张全文展开的图 —— 现在折叠不成立就直接抛错。
    """
    time.sleep(1.5)                                  # 等异步解释落定
    cdp.js("var e=document.getElementById('ai-exp');"
           "if(e&&e.classList.contains('open'))e.querySelector('.head').click();return true")
    cdp.wait_for("var e=document.getElementById('ai-exp');"
                 "return !e||!e.classList.contains('open')", "解释层已折叠", 10)


def shot(cdp: CDP, out_dir: Path, name: str, settle: float = 1.0) -> Path:
    time.sleep(settle)
    # 每次截图都回滚到顶：切页/点按钮可能留下滚动位置，README 的图必须是同一取景。
    # 主内容区是内层 overflow 容器，类名不该被工具写死，所以把所有 scrollTop>0 的都归零。
    cdp.js("[].forEach.call(document.querySelectorAll('*'),"
           "function(e){if(e.scrollTop>0)e.scrollTop=0});window.scrollTo(0,0);return true")
    time.sleep(0.25)
    p = out_dir / name
    data = cdp.png()
    p.write_bytes(data)   # 二进制一律 write_bytes：write_text 在 Windows 会动行尾
    print(f"  ✓ {name:26s} {len(data) / 1024:7.0f} KB")
    return p


def main() -> int:
    ap = argparse.ArgumentParser(
        description="采集 README 界面截图（真实登录 + 真实 MILP 求解，非摆拍）")
    ap.add_argument("--base-url", default="http://127.0.0.1:8802")
    ap.add_argument("--username", default="admin")
    ap.add_argument("--password-env", default="CAPTURE_ADMIN_PASSWORD",
                    help="口令所在环境变量名；不给默认值，避免口令进命令行")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--chrome", default="")
    ap.add_argument("--width", type=int, default=1680, help="与既有截图同尺寸，README 排版不跳")
    ap.add_argument("--height", type=int, default=1050)
    ap.add_argument("--solve-timeout", type=float, default=240.0)
    ap.add_argument("--probe", action="store_true",
                    help="只打印登录 + 求解后的页内读数，不写图")
    args = ap.parse_args()

    password = os.environ.get(args.password_env, "")
    if not password:
        print(f"缺少口令：把口令放进环境变量 {args.password_env} 再运行（不落命令行、不落盘）。",
              file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="tideshift-capture-"))
    exe = find_chrome(args.chrome)
    print(f"浏览器：{exe}\n目标：  {args.base_url}   视口：{args.width}×{args.height}")

    proc = launch_chrome(exe, profile)
    cdp = None
    try:
        port = read_devtools_port(profile)
        cdp = CDP(page_target(port))
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Emulation.setDeviceMetricsOverride",
                 width=args.width, height=args.height, deviceScaleFactor=1, mobile=False)
        login(cdp, args.base_url, args.username, password)
        if not args.probe:
            shot(cdp, out_dir, "01-welcome.png", settle=0.8)
        print("登录完成，开始真实求解（MILP，约 40~60 s）…")
        kpis = run_solve(cdp, args.solve_timeout)
        print("看板读数（截图必须与 README 量化成果同源，逐条核对）：")
        for k in ("version", "brand", "date", "kpi", "annual_note", "emoji"):
            # emoji 计数为 0 也要打——"界面里已经没有 emoji"本身就是需要留证的读数
            if kpis.get(k) is not None and kpis.get(k) != "":
                print(f"  {k:11s} = {kpis[k]}")
        if args.probe:
            print("--probe：不写图。")
            return 0
        collapse_explain(cdp)
        shot(cdp, out_dir, "02-dashboard-light.png", settle=1.2)
        nav(cdp, "scheduling")
        shot(cdp, out_dir, "03-scheduling.png")
        nav(cdp, "thermal")
        shot(cdp, out_dir, "04-thermal.png")
        nav(cdp, "dashboard")
        cdp.js("return document.getElementById('btn-theme').click(), true")
        cdp.wait_for("return document.documentElement.getAttribute('data-theme')==='dark'",
                     "深色主题生效", 15)
        shot(cdp, out_dir, "05-dashboard-dark.png", settle=1.2)
        print("完成。逐张目视核对版本号 / 温度 / 年化，再更新 README。")
        return 0
    finally:
        if cdp:
            cdp.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        # 一次性 profile 里只有缓存与 DevToolsActivePort，删掉不留痕
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(profile)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    sys.exit(main())
