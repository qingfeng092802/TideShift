# -*- coding: utf-8 -*-
"""缓存安全模块：签名 / 密钥分离 / 受限反序列化 / 容量上限 / 生成互斥

缓存文件 HMAC-SHA256 签名，防篡改/投毒（pickle 反序列化 RCE 防线）。
签名密钥不再与密文同目录——密钥存 config/.cache_secret（可经
       ENERGY_CACHE_SECRET_FILE 环境变量覆盖），.solve_cache/ 下的读权限
       拿不到密钥，伪造合法签名需要额外攻破 config/。
       同时 pickle.loads 换受限 Unpickler（模块白名单），删除任意类的反序列化原语。
enforce_disk_lru() 提供 mtime LRU 淘汰，防缓存目录无界膨胀。
密钥文件生成加互斥锁（O_EXCL 锁文件），防首启并发双写互相覆盖。

写入格式：b"HMAC1\\n" + hex签名 + b"\\n" + pickle 载荷
读取时先验签（hmac.compare_digest 防时序攻击），验签失败一律按"缓存未命中"处理。
"""
import hashlib
import hmac
import os
import pickle
import secrets
import time

from src.utils.logger import get_logger

_MAGIC = b"HMAC1"
_KEY_FILE = ".cache_secret"
_KEY_CACHE = None
_log = get_logger("cache_security")

# 受限反序列化白名单：只允许数据载体类，任何可执行/系统类一律拒绝
_ALLOWED_MODULES = (
    "numpy", "pandas", "src.", "builtins", "collections", "datetime",
)


class _RestrictedUnpickler(pickle.Unpickler):
    """只允许白名单模块内的类被反序列化，阻断任意代码执行原语。"""

    def find_class(self, module, name):
        if any(module == m or module.startswith(m) for m in _ALLOWED_MODULES):
            # builtins 只放行基础类型
            if module == "builtins" and name not in (
                    "set", "list", "dict", "tuple", "frozenset", "bytes", "str",
                    "int", "float", "bool", "complex", "range", "slice", "object"):
                raise pickle.UnpicklingError(f"blocked builtin: {name}")
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"blocked module: {module}")


def _secret_key_path(cache_dir: str) -> str:
    """密钥文件路径：默认放 config/（与缓存密文分离），可环境变量覆盖。"""
    override = os.getenv("ENERGY_CACHE_SECRET_FILE")
    if override:
        return override
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(project_root, "config", _KEY_FILE)


def _secret_key(cache_dir: str) -> bytes:
    """加载（或首次生成）缓存签名密钥。密钥不入库、不入 git（config/ 已排除）。

    O_EXCL 锁文件保证首启并发时只有一个线程/进程生成密钥，
    其余等待锁释放后读取，避免两份密钥互相 O_TRUNC 覆盖导致旧缓存全部失效。
    """
    global _KEY_CACHE
    if _KEY_CACHE is not None:
        return _KEY_CACHE
    os.makedirs(cache_dir, exist_ok=True)
    key_path = _secret_key_path(cache_dir)
    key_dir = os.path.dirname(key_path)
    os.makedirs(key_dir, exist_ok=True)
    lock_path = key_path + ".lock"

    try:
        with open(key_path, "rb") as f:
            raw = f.read().strip()
        if len(raw) >= 32:
            _KEY_CACHE = raw
            return _KEY_CACHE
    except OSError:
        pass

    # 获取互斥锁（O_CREAT|O_EXCL 原子性；持锁进程异常退出时锁文件随 stale 检测清理）
    lock_fd = None
    for _attempt in range(200):  # 最多等 10s
        try:
            lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            try:
                # stale 锁：超过 30s 视为持锁者已死，强占
                if time.time() - os.path.getmtime(lock_path) > 30:
                    os.remove(lock_path)
                    continue
            except OSError:
                continue
            time.sleep(0.05)
    raw = secrets.token_hex(32).encode("ascii")
    try:
        if lock_fd is not None:
            # 双检：拿到锁后密钥可能已被上一持锁者写出
            try:
                with open(key_path, "rb") as f:
                    existing = f.read().strip()
                if len(existing) >= 32:
                    _KEY_CACHE = existing
                    return _KEY_CACHE
            except OSError:
                pass
            # 0600：仅当前用户可读写（Windows 上 best-effort）
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
        else:
            # 等锁超时：退化为本进程自用密钥（旧缓存失效但不阻塞启动）
            _log.warning("缓存密钥锁等待超时，本进程使用临时密钥（旧缓存将失效）")
            return raw
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                os.remove(lock_path)
            except OSError:
                pass
    _KEY_CACHE = raw
    return _KEY_CACHE


def save_signed(cache_dir: str, filename: str, obj) -> bool:
    """签名写入缓存对象。成功返回 True，失败（IO 等）返回 False 且不留半截文件。"""
    try:
        os.makedirs(cache_dir, exist_ok=True)
        payload = pickle.dumps(obj)
        sig = hmac.new(_secret_key(cache_dir), payload, hashlib.sha256).hexdigest()
        tmp = os.path.join(cache_dir, filename + ".tmp")
        with open(tmp, "wb") as f:
            f.write(_MAGIC + b"\n" + sig.encode("ascii") + b"\n" + payload)
        os.replace(tmp, os.path.join(cache_dir, filename))  # 原子替换，防写一半
        return True
    except Exception:
        _log.warning("缓存写入失败: %s", filename, exc_info=True)
        return False


def load_signed(cache_dir: str, filename: str):
    """验签读取缓存对象。文件不存在/被篡改/格式异常 → 返回 None（调用方按未命中重算）。"""
    path = os.path.join(cache_dir, filename)
    try:
        if not (os.path.exists(path) and os.path.getsize(path) > 0):
            return None
        with open(path, "rb") as f:
            raw = f.read()
        head, sig, payload = raw.split(b"\n", 2)
        if head != _MAGIC:
            return None
        expect = hmac.new(_secret_key(cache_dir), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig.decode("ascii"), expect):
            return None  # 验签失败：缓存被篡改或密钥已更换
        # 受限反序列化——白名单外的类直接判未命中，不再给任意类执行机会
        import io as _io
        return _RestrictedUnpickler(_io.BytesIO(payload)).load()
    except Exception:
        return None


def enforce_disk_lru(cache_dir: str, max_files: int = 50, max_total_mb: float = 1024.0) -> int:
    """按 mtime LRU 淘汰磁盘缓存，返回删除的文件数。

    缓存键含浮点参数组合，理论无穷多；不淘汰则反复调参可撑爆磁盘。
    """
    try:
        entries = []
        total = 0
        for name in os.listdir(cache_dir):
            if not name.endswith(".pkl"):
                continue
            p = os.path.join(cache_dir, name)
            try:
                st = os.stat(p)
                entries.append((st.st_mtime, st.st_size, p))
                total += st.st_size
            except OSError:
                continue
        entries.sort(reverse=True)  # 新的在前
        limit_bytes = int(max_total_mb * 1024 * 1024)
        removed = 0
        kept = 0
        for mtime, size, path in entries:
            kept += 1
            if kept > max_files or total > limit_bytes:
                try:
                    os.remove(path)
                    total -= size
                    removed += 1
                except OSError:
                    pass
        if removed:
            _log.info("磁盘缓存 LRU 淘汰 %d 个文件（上限 %d 个 / %dMB）", removed, max_files, int(max_total_mb))
        return removed
    except OSError:
        return 0
