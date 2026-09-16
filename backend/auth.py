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
import time
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

_JWT_TTL_S = 12 * 3600
_PBKDF2_ITERS = 200_000


def _atomic_write(path: str, data: bytes):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows 下 0600 语义受限，尽力而为


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

    def __init__(self):
        self.path = os.path.join(_config_dir(), "auth.json")
        self._state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("hash"):
                    # 口令文件已存在 → 不走 _create_default()，ADMIN_INITIAL_PASSWORD **不生效**。
                    # 这正是"跑完 pytest 再启动服务后任何口令都登录失败"的现场：
                    # 残留的 auth.json 若是测试生成的随机口令，用户设的初始口令会被静默忽略，
                    # 且从报错信息里完全看不出来。此处显式告警，给出可执行的恢复动作。
                    if os.getenv("ADMIN_INITIAL_PASSWORD", "").strip():
                        log.warning(
                            "⚠️  检测到已存在的口令文件 %s —— ADMIN_INITIAL_PASSWORD 本次"
                            "**不会生效**（初始口令仅在首次创建时读取）。若这不是你设置的口令，"
                            "请停止服务、删除该文件后重新启动。", self.path)
                    return d
            except Exception:
                pass
        return self._create_default()

    def _create_default(self) -> dict:
        """🔴#3 修复：首启生成随机强口令（或读 ADMIN_INITIAL_PASSWORD），不再使用 admin/admin123。

        随机口令仅在控制台一次性打印，未读控制台则需删除 config/auth.json 重新生成。
        must_change=true 同时由 server.py 中间件强制拦截。
        """
        salt = secrets.token_bytes(16)
        env_pwd = os.getenv("ADMIN_INITIAL_PASSWORD", "")
        initial_pwd = env_pwd if env_pwd else secrets.token_urlsafe(10)  # ~64bit 熵
        d = {"username": "admin", "salt": salt.hex(),
             "hash": _hash_password(initial_pwd, salt), "must_change": not bool(env_pwd)}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        log.info("=" * 60)
        if env_pwd:
            log.warning("⚠️  已使用环境变量 ADMIN_INITIAL_PASSWORD 初始化管理员口令，"
                  "首次登录后请立即修改")

        else:
            log.warning("⚠️  首次启动：已生成随机管理员口令（请立即记录并登录修改）：")
            log.info(f"    用户名: admin    初始口令: {initial_pwd}")
        log.info(f"    口令文件：config/auth.json（PBKDF2-SHA256, {_PBKDF2_ITERS} 迭代）")
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
        salt = bytes.fromhex(self._state.get("salt", ""))
        try:
            user_ok = hmac.compare_digest((username or "").encode("utf-8"),
                                          self.username.encode("utf-8"))
            hash_ok = hmac.compare_digest(_hash_password(password or "", salt).encode("ascii"),
                                          str(self._state.get("hash", "")).encode("ascii"))
        except (TypeError, ValueError):
            return False
        return user_ok and hash_ok

    def set_password(self, new_password: str):
        salt = secrets.token_bytes(16)
        self._state.update({"salt": salt.hex(), "hash": _hash_password(new_password, salt),
                            "must_change": False})
        self._save()
