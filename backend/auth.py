"""
P0-02 安全模块（第二批代码审计修复）
- JWT（HS256，标准库手写，无 PyJWT 依赖）：登录会话，默认 12h 过期
- Fernet（cryptography，venv 已内置）：API Key 落盘加密，config/.api_secret 保管密钥（0600）
- PBKDF2-HMAC-SHA256（200k 迭代）：登录口令哈希，config/auth.json 存储
- 账户存储 config/auth.json

🔴#3 修复：首启不再创建 admin/admin123——生成随机强口令打印到控制台（一次性展示），
  可用 ADMIN_INITIAL_PASSWORD 环境变量显式指定；must_change=true 由 server.py
  中间件强制拦截（仅放行登录与改密接口），不再依赖前端自觉。
🔴#22 修复：verify() 的 hmac.compare_digest 比较前统一 encode 成 bytes——
  str 含非 ASCII（如中文用户名）时 compare_digest 会抛 TypeError 导致 500。
P0-47 修复：密钥文件生成加 O_EXCL 锁，防首启并发双写互相覆盖。
"""
from src.utils.logger import get_logger
log = get_logger(__name__)
import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

_JWT_TTL_S = 12 * 3600
_PBKDF2_ITERS = 200_000

# 首启随机口令的**一次性落盘**文件名（位于配置目录）。首登改密成功后自动删除。
# 存在的意义：原实现只在控制台打印一次随机口令，容器里日志被刷掉或用户漏看就再也拿不到，
# 唯一恢复手段是手动删 auth.json——对开源使用者门槛过高。
INITIAL_PWD_FILENAME = "INITIAL_PASSWORD.txt"

# 认证模式（ENERGY_AUTH_MODE）：
#   "persistent"（默认）——口令哈希落盘到 config/auth.json，文件是唯一真相；
#                          ADMIN_INITIAL_PASSWORD 只在**首次创建**时读取
#   "env"              ——口令只来自 ADMIN_INITIAL_PASSWORD，每次启动都以后者为准、不落盘；
#                          适合容器 / CI / 演示环境（避免"改了环境变量却不生效"这类困惑）
_AUTH_MODES = ("persistent", "env")


class AuthModeError(RuntimeError):
    """在 env 认证模式下尝试应用内改密等"与模式冲突"的操作。

    单独定义一个异常类型，是为了让 API 层能给出**可执行**的提示
    （"请改环境变量后重启"），而不是笼统的 500。
    """


class AuthConfigError(RuntimeError):
    """认证配置不完整，服务**拒绝启动**（而非带病运行）。

    当前唯一触发条件：`ENERGY_AUTH_MODE=env` 但 `ADMIN_INITIAL_PASSWORD` 为空。

    该语义是刻意定死的三选一里的一个：
      - **不**静默回退 `persistent`（会让用户以为 env 生效了，实际写盘）；
      - **不**每次启动生成随机口令（口令会随重启变化，且只打印一次，无法运维）；
      - 而是**拒绝启动**并在提示里说明改法——配置错误在启动时暴露，比在登录页暴露好。
    """


def auth_mode() -> str:
    """解析认证模式；非法值回退 persistent 并告警（不因配置写错而拒绝启动）。"""
    raw = (os.environ.get("ENERGY_AUTH_MODE") or "").strip().lower()
    if not raw:
        return "persistent"
    if raw not in _AUTH_MODES:
        log.warning("ENERGY_AUTH_MODE=%r 不是合法取值（可选：%s），已回退 persistent",
                    raw, " / ".join(_AUTH_MODES))
        return "persistent"
    return raw


def env_password() -> str:
    """环境变量提供的管理员口令（两种模式共用同一变量名，避免文档分裂）。"""
    return (os.environ.get("ADMIN_INITIAL_PASSWORD") or "").strip()


def _harden_permissions(path: str) -> bool:
    """把文件访问收敛到"仅当前用户"。返回是否**真正生效**（而非尽力而为）。

    为什么要单独一个函数：`os.chmod(0o600)` 只在 POSIX 上是权限边界。
    **Windows 上 `os.chmod` 只能切换只读位**，实测 `0o600` 写完后 `stat` 仍是 `0o666`
    ——也就是说在 Windows 上"0600 的一次性口令文件"这个说法不成立，文件对同机其他用户
    依然可读。因此在 Windows 上额外用 `icacls` 显式收紧 ACL：
        icacls <file> /inheritance:r /grant:r "<user>:F"
    切断继承、只给当前用户完全控制（保留 F 而非 R，否则后续 _atomic_write 的
    os.replace 会因为无权删除目标文件而失败）。

    icacls 不可用或执行失败时返回 False（调用方记录告警），不阻断主流程。
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name != "nt":
        return True                      # POSIX：chmod 已构成权限边界
    user = (os.environ.get("USERNAME") or os.environ.get("USER") or "").strip()
    if not user:
        return False
    try:
        r = subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _atomic_write(path: str, data: bytes):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    if not _harden_permissions(path):
        log.warning("无法把 %s 的访问权限收敛到当前用户（Windows 下应装/可用 icacls）。"
                    "该文件目前依赖所在目录的 ACL 保护。", path)


def _write_initial_pwd_file(path: str, password: str):
    """把首启随机口令写成一次性文件（0600）。

    原实现只在控制台打印一次：容器里日志被滚掉、或用户没盯住控制台，就再也拿不到口令，
    唯一出路是手动删 auth.json —— 对开源使用者门槛过高。落一份 0600 的副本即可解决，
    代价是口令短暂存在于磁盘上，因此首登改密成功后立即删除（见 _remove_initial_pwd_file）。
    """
    body = (
        "汐储 TideShift · 一次性初始口令\n"
        "================================\n"
        f"用户名：admin\n"
        f"初始口令：{password}\n\n"
        "登录后系统会要求你设置新口令；改密成功后本文件会被自动删除。\n"
        "若需重新生成：python -m backend.manage reset-password\n"
    )
    _atomic_write(path, body.encode("utf-8"))


def _load_or_create_secret(path: str) -> bytes:
    """加载（或首次生成）密钥文件。P0-47：O_EXCL 锁防首启并发双写互相覆盖。"""
    if os.path.exists(path):
        with open(path, "rb") as f:
            s = f.read().strip()
        if s:
            return s
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lock_path = path + ".lock"
    lock_fd = None
    for _attempt in range(200):  # 最多等 10s
        try:
            lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 30:
                    os.remove(lock_path)
                    continue
            except OSError:
                continue
            time.sleep(0.05)
    try:
        if lock_fd is not None:
            # 双检：拿到锁后密钥可能已被上一持锁者写出
            if os.path.exists(path):
                with open(path, "rb") as f:
                    s = f.read().strip()
                if s:
                    return s
        s = secrets.token_urlsafe(32).encode()
        _atomic_write(path, s)
        return s
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                os.remove(lock_path)
            except OSError:
                pass


# ---------------- API Key 加密（Fernet） ----------------
def _fernet() -> Fernet:
    raw = _load_or_create_secret(os.path.join(_config_dir(), ".api_secret"))
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(key)


def _config_dir() -> str:
    """认证与密钥的落盘目录。

    默认是项目根的 `config/`；可用环境变量 **`ENERGY_CONFIG_DIR`** 覆盖。

    为什么需要覆盖（缺它会导致一个阻断级问题）：
      `config/auth.json` 一旦存在，服务启动就跳过 `_create_default()`，
      `ADMIN_INITIAL_PASSWORD` 被**静默忽略**，用户按 README 的
      「装依赖 → 跑 pytest → 启动」路径操作后，任何口令都登录失败。
      根因是 pytest 会把测试实例化的 AuthStore 写进**仓库工作树**。
      测试通过 `ENERGY_CONFIG_DIR` 指向临时目录即可彻底隔离。
    """
    override = (os.environ.get("ENERGY_CONFIG_DIR") or "").strip()
    if not override:
        # 兼容别名：另有一份针对同一缺陷的修复采用 AUTH_CONFIG_DIR。
        # 同时接受两个名字，避免两侧修复合并时因命名不一致而**静默复发**
        # （表现为测试重新污染工作树 → 服务忽略 ADMIN_INITIAL_PASSWORD）。
        override = (os.environ.get("AUTH_CONFIG_DIR") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")


def encrypt_key(plain: str) -> str:
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_key(token: str) -> str:
    """解密失败（密钥轮换/篡改/格式旧）一律返回空串，由上层回退环境变量。"""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, Exception):
        return ""


def mask_key(k: str) -> str:
    if not k:
        return ""
    if len(k) <= 8:
        return "****"
    return k[:3] + "****" + k[-4:]


# ---------------- JWT（HS256，标准库实现） ----------------
def _jwt_secret() -> bytes:
    return _load_or_create_secret(os.path.join(_config_dir(), ".auth_secret"))


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def jwt_encode(payload: dict, ttl_s: int = _JWT_TTL_S) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    body = {**payload, "iat": now, "exp": now + ttl_s, "jti": secrets.token_hex(8)}
    signing = (_b64u(json.dumps(header, separators=(",", ":")).encode())
               + "." + _b64u(json.dumps(body, separators=(",", ":")).encode()))
    sig = hmac.new(_jwt_secret(), signing.encode(), hashlib.sha256).digest()
    return signing + "." + _b64u(sig)


def jwt_verify(token: str) -> Optional[dict]:
    try:
        h, p, s = token.split(".")
        header = json.loads(_b64u_d(h))
        if header.get("alg") != "HS256":
            return None
        signing = f"{h}.{p}".encode()
        expect = hmac.new(_jwt_secret(), signing, hashlib.sha256).digest()
        if not hmac.compare_digest(expect, _b64u_d(s)):
            return None
        payload = json.loads(_b64u_d(p))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


# P0-F2：token 指纹绑定——JWT 签发时嵌入 User-Agent 指纹（sha256 前 16 hex），
# 中间件校验请求 UA 与签发时一致。token 被 XSS 窃取后换个浏览器/环境即失效，
# 显著降低窃取后的可用性（无状态实现，不需要服务端黑名单）。
def ua_fingerprint(ua: str) -> str:
    return hashlib.sha256((ua or "").encode("utf-8")).hexdigest()[:16]


# ---------------- 登录口令（PBKDF2） ----------------
def _hash_password(pwd: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pwd.encode(), salt, _PBKDF2_ITERS).hex()


class AuthStore:
    """config/auth.json：{username, salt, hash, must_change}"""

    def __init__(self, auto_create: bool = True):
        """auto_create=False 时不因"文件缺失"而生成默认口令。

        用途：运维命令（重置口令 / 离线巡检）不该在过程中顺手造出一份随机口令——
        那会在控制台打印一个"看起来也像口令"的值，与真正的重置结果混淆。
        """
        self.mode = auth_mode()
        self.path = os.path.join(_config_dir(), "auth.json")
        if self.mode == "env":
            self._state = self._load_env()
        elif not auto_create and not os.path.exists(self.path):
            # 无文件且不自动创建：给一份空壳，交由调用方显式 set_password 落盘
            self._state = {"username": "admin", "salt": "", "hash": "", "must_change": False}
        else:
            self._state = self._load()

    # ---------- env 模式 ----------
    def _load_env(self) -> dict:
        """env 模式：口令只来自环境变量，**不读也不写**任何口令文件。

        这样"改环境变量却不生效"这类困惑在结构上不可能发生——环境变量就是唯一真相。

        口令缺失时**拒绝启动**（AuthConfigError）：既不静默回退 persistent，
        也不生成"随重启变化、只打印一次"的随机口令——那两种做法都会让运维无从下手。
        """
        if not env_password():
            raise AuthConfigError(
                "ENERGY_AUTH_MODE=env 但环境变量 ADMIN_INITIAL_PASSWORD 为空，无法确定管理员口令，"
                "服务拒绝启动。\n"
                "  改法一：设置 ADMIN_INITIAL_PASSWORD=<你的口令> 后重启；\n"
                "  改法二：去掉 ENERGY_AUTH_MODE（回到默认 persistent，口令落盘到配置目录）。")
        log.info("认证模式: env ｜ 口令来源: ADMIN_INITIAL_PASSWORD（每次启动都生效，不落盘）")
        # salt/hash 留空：verify() 在 env 模式下不走哈希比对分支
        return {"username": "admin", "salt": "", "hash": "", "must_change": False}

    @property
    def password_managed_by_env(self) -> bool:
        """口令是否由环境变量托管（此时应用内改密无意义）。"""
        return self.mode == "env"

    @property
    def initial_pwd_file(self) -> str:
        """首启随机口令的一次性落盘路径（persistent 模式才有意义）。"""
        return os.path.join(_config_dir(), INITIAL_PWD_FILENAME)

    def auth_source_description(self) -> str:
        """一行说明"当前口令从哪来"，在口令**已就绪**的状态下打印。

        仅对非法 ENERGY_AUTH_MODE 告警是不够的——容器日志一刷就过去了，
        用户仍然会困惑"我改了环境变量为什么不生效"。把结论直接打进启动横幅。

        注意：persistent 模式"首次创建"的文案由 `_create_default()` 自行打印。
        本方法构造完成后调用时文件必然已存在，因此不在此处放"首次创建"分支——
        否则会写出一段永远走不到的代码。
        """
        if self.mode == "env":
            return ("认证模式: env ｜ 口令来源: ADMIN_INITIAL_PASSWORD"
                    "（每次启动都生效，不落盘）")
        note = "；当前它**不会生效**" if env_password() else ""
        return (f"认证模式: persistent ｜ 口令来源: 口令文件 {self.path}"
                f"（环境变量只在首次创建且无此文件时生效{note}）")

    def _load(self) -> dict:
        """读取已有口令文件；无文件则首次创建。

        注意 try 块只包住"读文件+解析"：后续的清理与日志若被 broad except 吞掉，
        会静默走到 _create_default() 重新造一份凭据，那是最危险的失败模式。
        """
        state = None
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("hash"):
                    state = d
            except Exception:
                state = None

        if state is None:
            return self._create_default()

        # 口令文件已存在 → 不走 _create_default()，ADMIN_INITIAL_PASSWORD **不生效**。
        # 这正是"跑完 pytest 再启动服务后任何口令都登录失败"的现场：残留的 auth.json
        # 若是测试生成的随机口令，用户设的初始口令会被静默忽略，且从报错里完全看不出来。
        if env_password():
            log.warning(
                "⚠️  检测到已存在的口令文件 %s —— ADMIN_INITIAL_PASSWORD 本次"
                "**不会生效**（初始口令仅在首次创建时读取）。若这不是你设置的口令，"
                "请执行 python -m backend.manage reset-password，或改用 ENERGY_AUTH_MODE=env。",
                self.path)
        # must_change=False 表示初始口令已失效：此时若还残留一次性口令文件，
        # 就是一份"长期可读的初始口令"。崩溃中断、手工改密、从别处拷来 auth.json
        # 都可能留下它，因此在每次启动时按状态纠正。
        if not state.get("must_change"):
            self._remove_initial_pwd_file()
        log.info(self.auth_source_description())
        return state

    def _create_default(self) -> dict:
        """🔴#3 修复：首启生成随机强口令（或读 ADMIN_INITIAL_PASSWORD），不再使用 admin/admin123。

        开源可用性补强：随机口令除了打印到控制台，还会以 0600 写入
        `<配置目录>/INITIAL_PASSWORD.txt`——控制台日志被刷掉/容器里看不到时仍能取到口令；
        首登改密成功后该文件自动删除（一次性凭据不长期留在磁盘）。
        must_change=true 同时由 server.py 中间件强制拦截。
        """
        salt = secrets.token_bytes(16)
        env_pwd = env_password()
        initial_pwd = env_pwd if env_pwd else secrets.token_urlsafe(10)  # ~64bit 熵
        d = {"username": "admin", "salt": salt.hex(),
             "hash": _hash_password(initial_pwd, salt), "must_change": not bool(env_pwd)}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # 用 _atomic_write 落盘：它同时把权限收敛到 0600（此前这里用普通 open()，
        # 首启生成的 auth.json 权限不受限，与后续 _save() 的 0600 不一致）
        _atomic_write(self.path, json.dumps(d, ensure_ascii=False, indent=2).encode())
        log.info("=" * 60)
        log.info("认证模式: persistent ｜ 口令来源: %s",
                 "首次创建，取 ADMIN_INITIAL_PASSWORD" if env_pwd
                 else f"首次创建，已生成随机口令并写入 {self.initial_pwd_file}")
        if env_pwd:
            log.warning("⚠️  已使用环境变量 ADMIN_INITIAL_PASSWORD 初始化管理员口令，"
                        "首次登录后请立即修改")
            log.info("    提示：若希望环境变量**每次启动都生效**（容器/CI 场景），"
                     "设 ENERGY_AUTH_MODE=env")
        else:
            log.warning("⚠️  首次启动：已生成随机管理员口令（请立即登录并修改）：")
            log.info("    用户名: admin    初始口令: %s", initial_pwd)
            _write_initial_pwd_file(self.initial_pwd_file, initial_pwd)
            log.info("    口令副本（一次性，改密成功后自动删除）：%s", self.initial_pwd_file)
        log.info("    口令文件：%s（PBKDF2-SHA256, %d 迭代）", self.path, _PBKDF2_ITERS)
        log.info("    忘记口令：python -m backend.manage reset-password")
        log.info("=" * 60)
        return d

    def _save(self):
        _atomic_write(self.path, json.dumps(self._state, ensure_ascii=False, indent=2).encode())

    @property
    def username(self) -> str:
        return self._state.get("username", "admin")

    def must_change(self) -> bool:
        return bool(self._state.get("must_change"))

    def verify(self, username: str, password: str) -> bool:
        """🔴#22 修复：compare_digest 前统一 encode 成 bytes。

        hmac.compare_digest 对含非 ASCII 字符的 str 抛 TypeError
        （实测 compare_digest('管理员','admin') → TypeError），中文用户名登录会 500。
        encode 后按字节比较，任何输入一律返回 False 而非抛异常。
        """
        try:
            user_ok = hmac.compare_digest((username or "").encode("utf-8"),
                                          self.username.encode("utf-8"))
        except (TypeError, ValueError):
            return False
        if not user_ok:
            return False
        if self.password_managed_by_env:
            # env 模式：与环境变量直接比对（无落盘哈希可比）
            try:
                return hmac.compare_digest((password or "").encode("utf-8"),
                                           env_password().encode("utf-8"))
            except (TypeError, ValueError):
                return False
        salt = bytes.fromhex(self._state.get("salt", ""))
        try:
            hash_ok = hmac.compare_digest(_hash_password(password or "", salt).encode("ascii"),
                                          str(self._state.get("hash", "")).encode("ascii"))
        except (TypeError, ValueError):
            return False
        return hash_ok

    def _remove_initial_pwd_file(self):
        """首启随机口令是一次性凭据：改密成功后立即删除，避免长期留在磁盘上。"""
        try:
            if self.initial_pwd_file and os.path.exists(self.initial_pwd_file):
                os.remove(self.initial_pwd_file)
                log.info("已删除一次性初始口令文件：%s", self.initial_pwd_file)
        except OSError as e:
            log.warning("删除一次性初始口令文件失败（请手动删除 %s）：%s",
                        self.initial_pwd_file, e)

    def set_password(self, new_password: str):
        if self.password_managed_by_env:
            raise AuthModeError(
                "当前为 env 认证模式：口令由环境变量 ADMIN_INITIAL_PASSWORD 管理，"
                "应用内改密不会生效。请修改该环境变量后重启服务。")
        salt = secrets.token_bytes(16)
        self._state.update({"salt": salt.hex(), "hash": _hash_password(new_password, salt),
                            "must_change": False})
        self._save()
        self._remove_initial_pwd_file()
