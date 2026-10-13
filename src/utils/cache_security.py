# -*- coding: utf-8 -*-
"""缓存安全模块：签名 / 密钥分离 / 纯数据反序列化 / 容量上限 / 生成互斥

缓存文件 HMAC-SHA256 签名，防篡改/投毒。签名密钥不与缓存同目录——
密钥存 config/.cache_secret（可经 ENERGY_CACHE_SECRET_FILE 覆盖），
.solve_cache/ 下的读权限拿不到密钥，伪造合法签名需要额外攻破 config/。

反序列化：**本模块不再使用 pickle**。载荷编码为 JSON，只允许基础类型
（dict/list/str/int/float/bool/None）加四类显式登记的数据载体
（numpy 数组、pandas DatetimeIndex、以及 4 个 dataclass）。
解码走精确的 (module, name) 元组白名单 + importlib 反查：既不接受
"前缀命中即放行"，也不存在任何调用任意函数/属性的原语。

历史包袱：v1.2.3 及更早版本用 pickle 写 `.pkl`，本模块已读不了
（魔数由 b"HMAC1" 改为 b"TSJ1"），旧文件一律按"缓存未命中"处理，
并由 enforce_disk_lru 淘汰。这是刻意的取舍——留着读取路径等于留着 RCE 原语。

enforce_disk_lru() 提供 mtime LRU 淘汰，防缓存目录无界膨胀。
密钥文件生成加互斥锁（O_EXCL 锁文件），防首启并发双写互相覆盖。

写入格式：b"TSJ1\\n" + hex签名 + b"\\n" + UTF-8 JSON 载荷
读取时先验签（hmac.compare_digest 防时序攻击），验签失败一律按"缓存未命中"处理。
"""
import dataclasses
import hashlib
import hmac
import importlib
import json
import os
import secrets
import time
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.utils.file_perms import harden_file_permissions
from src.utils.logger import get_logger

# 魔数从 b"HMAC1" 改为 b"TSJ1"：载荷格式由 pickle 换成 JSON，
# 旧文件必须判为未命中，否则就会走到下面那条已被删除的 pickle 分支。
_MAGIC = b"TSJ1"
_KEY_FILE = ".cache_secret"
_KEY_CACHE = None
_HARDEN_WARNED = set()      # 权限收敛失败只告警一次（按路径去重）
_log = get_logger("cache_security")

# 缓存文件后缀（读/写/LRU 淘汰共用一处口径）
CACHE_SUFFIX = ".json"
# 旧版 pickle 缓存后缀：只用于 LRU 清理，不再读取
_LEGACY_SUFFIX = ".pkl"
_CACHE_SUFFIXES = (CACHE_SUFFIX, _LEGACY_SUFFIX)

# 单个缓存文件的体积上限：实测 viz 载荷约 50KB，给 3 个数量级余量。
# 目的是让"被人塞一个超大文件"变成一次廉价的未命中，而不是一次内存分配。
_MAX_CACHE_BYTES = 64 * 1024 * 1024

# ---------------------------------------------------------------------------
# 反序列化白名单：精确 (module, name) 元组，不做前缀匹配
# ---------------------------------------------------------------------------
# 只登记真正会进缓存的 4 个数据载体（_save_disk / thermal pe 两条路径实测载荷）。
# 新增类型必须显式加到这里，且 importlib 反查后仍要复核 module/name 完全一致。
_ALLOWED_DATACLASSES: dict = {
    "src.agents.coordinator_agent.DailyReport":
        ("src.agents.coordinator_agent", "DailyReport"),
    "src.agents.storage_optimization_agent.ScheduleResult":
        ("src.agents.storage_optimization_agent", "ScheduleResult"),
    "src.agents.demand_response_agent.DRSignal":
        ("src.agents.demand_response_agent", "DRSignal"),
    "src.agents.demand_response_agent.DRResponse":
        ("src.agents.demand_response_agent", "DRResponse"),
}

# JSON 载荷里的类型标签。普通 dict 只要含任一保留键就会被包成 __ts_map__，
# 因此标签不会与业务数据撞车（撞车时走 map 分支，仍是精确往返）。
_T_TYPE = "__ts_type__"
_T_ARRAY = "__ts_array__"
_T_INDEX = "__ts_index__"
_T_TUPLE = "__ts_tuple__"
_T_MAP = "__ts_map__"
_RESERVED_KEYS = (_T_TYPE, _T_ARRAY, _T_INDEX, _T_TUPLE, _T_MAP)

# numpy 标量类型 → 内置类型（JSON 不认识 np.float64）
_NP_SCALARS = (np.floating, np.integer, np.bool_)


class CacheDecodeError(ValueError):
    """缓存载荷不是本模块写出的纯数据（非法标签 / 白名单外的类 / 越界值）"""


def _resolve_dataclass(type_name: str):
    """按精确 (module, name) 反查 dataclass 类型；不在白名单内一律拒绝。"""
    entry = _ALLOWED_DATACLASSES.get(type_name)
    if entry is None:
        raise CacheDecodeError(f"blocked type: {type_name}")
    mod_name, cls_name = entry
    module = importlib.import_module(mod_name)   # 白名单内的固定模块名，非用户输入
    cls = getattr(module, cls_name)
    # 复核：getattr 拿到的必须就是登记的那个类（防止模块内重绑定/替身）
    if not (isinstance(cls, type) and cls.__module__ == mod_name and cls.__name__ == cls_name):
        raise CacheDecodeError(f"type mismatch: {type_name}")
    if not dataclasses.is_dataclass(cls):
        raise CacheDecodeError(f"not a dataclass: {type_name}")
    return cls


def _encode(obj: Any) -> Any:
    """把缓存对象编码成纯 JSON 结构。不认识的类型直接报错（写盘侧失败即不写）。"""
    if obj is None or isinstance(obj, (str, bool, int, float)):
        return obj
    if isinstance(obj, _NP_SCALARS):
        return obj.item()
    if isinstance(obj, np.ndarray):
        if obj.dtype.kind in ("O", "V"):     # object / 结构化 dtype 不是纯数据
            raise CacheDecodeError(f"array dtype not allowed: {obj.dtype}")
        return {_T_ARRAY: {"dtype": str(obj.dtype), "shape": list(obj.shape),
                           "data": obj.ravel().tolist()}}
    if isinstance(obj, pd.DatetimeIndex):
        return {_T_INDEX: [t.isoformat() for t in obj]}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        key = f"{type(obj).__module__}.{type(obj).__name__}"
        if key not in _ALLOWED_DATACLASSES:
            raise CacheDecodeError(f"dataclass not registered: {key}")
        return {_T_TYPE: key,
                "fields": {f.name: _encode(getattr(obj, f.name))
                           for f in dataclasses.fields(obj)}}
    if isinstance(obj, tuple):
        return {_T_TUPLE: [_encode(x) for x in obj]}
    if isinstance(obj, (list, set, frozenset)):
        return [_encode(x) for x in obj]
    if isinstance(obj, dict):
        # 非字符串键或含保留键 → 包成 __ts_map__，保证标签无歧义
        if any(not isinstance(k, str) or k in _RESERVED_KEYS for k in obj):
            return {_T_MAP: [[_encode(k), _encode(v)] for k, v in obj.items()]}
        return {k: _encode(v) for k, v in obj.items()}
    raise CacheDecodeError(f"unsupported type: {type(obj).__name__}")


def _decode(obj: Any) -> Any:
    """把 JSON 结构还原成缓存对象。任何非法标签/白名单外类型都抛错（调用方按未命中处理）。"""
    if obj is None or isinstance(obj, (str, bool, int, float)):
        return obj
    if isinstance(obj, list):
        return [_decode(x) for x in obj]
    if not isinstance(obj, dict):
        raise CacheDecodeError(f"unexpected json node: {type(obj).__name__}")

    if _T_ARRAY in obj:
        spec = obj[_T_ARRAY]
        dt = np.dtype(spec["dtype"])
        if dt.kind in ("O", "V"):
            raise CacheDecodeError(f"array dtype not allowed: {dt}")
        arr = np.array(spec["data"], dtype=dt)
        return arr.reshape(tuple(spec["shape"]))
    if _T_INDEX in obj:
        return pd.DatetimeIndex(pd.to_datetime(list(obj[_T_INDEX])))
    if _T_TUPLE in obj:
        return tuple(_decode(x) for x in obj[_T_TUPLE])
    if _T_MAP in obj:
        return {_decode(k): _decode(v) for k, v in obj[_T_MAP]}
    if _T_TYPE in obj:
        cls = _resolve_dataclass(obj[_T_TYPE])
        fields = obj.get("fields")
        if not isinstance(fields, dict):
            raise CacheDecodeError("dataclass payload missing fields")
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(fields) - known
        if unknown:      # 字段对不上说明载荷与当前代码不同版，宁可未命中
            raise CacheDecodeError(f"unknown fields for {cls.__name__}: {sorted(unknown)}")
        return cls(**{k: _decode(v) for k, v in fields.items()})
    # 普通 dict（编码侧已保证不含保留键）
    return {k: _decode(v) for k, v in obj.items()}


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

    密钥文件是本模块的安全根：读得到它就能伪造合法签名，所以生成后与读取命中时
    都收敛一次 ACL（Windows 上 chmod 只切只读位，必须走 icacls）。
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
            _harden_key_file(key_path)
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
                    _harden_key_file(key_path)
                    _KEY_CACHE = existing
                    return _KEY_CACHE
            except OSError:
                pass
            # 0600：仅当前用户可读写（Windows 上 chmod 无效，随后 icacls 收敛）
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            _harden_key_file(key_path)
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


def _harden_key_file(key_path: str) -> None:
    """把密钥文件收敛到"仅当前用户可读"。失败只告警，不阻断（与 backend/auth.py 同口径）。

    告警按路径去重：每次进程启动只提示一次，避免反复调用刷屏。
    """
    if key_path in _HARDEN_WARNED:
        return
    if not harden_file_permissions(key_path):
        _HARDEN_WARNED.add(key_path)
        _log.warning("无法把缓存签名密钥 %s 的访问权限收敛到当前用户"
                     "（Windows 下应装/可用 icacls）。该文件目前依赖所在目录的 ACL 保护。",
                     key_path)


def _is_safe_cache_name(filename: str) -> bool:
    """文件名必须是一个纯名字，不能借它跳出 cache_dir。

    为什么要有这道闸（P1.5，路径穿越）：`save_signed`/`load_signed` 只做
    `os.path.join(cache_dir, filename)`，而 filename 的一个来源是**请求参数**——
    `backend/server.py` 的页面级缓存键把 `SolveReq.date` 原样拼进文件名：
        ck = f"pe_{fp8}_{selected_date}_{soc_min}_{soc_max}_{rated_power}_{use_ml}"
    `pd.to_datetime` 接受 `'2024-07-15/../../..'` 这种带分隔符的字符串（它照样
    解析成 2024-07-15），于是 `os.path.normpath` 会把写入点挪到 `.solve_cache`
    之外——两个普通鉴权请求就能在仓库树里落一个**带真实 HMAC 签名**的文件；
    换成 `'x/../../.solve_cache/pe_<fp8>_<受害日期>'` 则正好压到另一天的缓存
    条目上，读侧命中即把毒条目当成那天结果返回（跨条目投毒）。

    判据取"与 basename 相同"而不是黑名单某个分隔符：Windows 上 `\\`、`/`、
    盘符 `C:`、UNC 前缀都能改变解析结果，逐条列举必然漏；basename 相等这一条
    同时覆盖它们，且 `os.altsep` 为空（POSIX）时也不会误伤。
    """
    if not filename or not isinstance(filename, str):
        return False
    # "." / ".." 是目录本身，不是文件；basename 判据对它们无感
    # （`os.path.basename("..")` 就是 `".."`），所以单独挡一次。
    if filename in (".", ".."):
        return False
    if filename != os.path.basename(filename):
        return False
    # 显式再挡一次分隔符与空字节：basename 在个别平台上对尾部分隔符的行为
    # 有差异（"a/" 的 basename 是 ""），这里不依赖那个细节。
    if os.sep in filename or (os.altsep and os.altsep in filename):
        return False
    if "\x00" in filename:
        return False
    return True


def save_signed(cache_dir: str, filename: str, obj) -> bool:
    """签名写入缓存对象。成功返回 True，失败（IO/不可编码/非法文件名）返回 False 且不留半截文件。"""
    if not _is_safe_cache_name(filename):
        _log.warning("拒绝写入非法缓存文件名（疑似路径穿越）: %r", filename)
        return False
    try:
        os.makedirs(cache_dir, exist_ok=True)
        # JSON 而非 pickle：载荷只能是纯数据，写入侧就把不可序列化的对象挡在门外
        payload = json.dumps(_encode(obj), ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        sig = hmac.new(_secret_key(cache_dir), payload, hashlib.sha256).hexdigest()
        tmp = os.path.join(cache_dir, filename + ".tmp")
        with open(tmp, "wb") as f:
            f.write(_MAGIC + b"\n" + sig.encode("ascii") + b"\n" + payload)
        os.replace(tmp, os.path.join(cache_dir, filename))  # 原子替换，防写一半
        return True
    except Exception:
        _log.warning("缓存写入失败: %s", filename, exc_info=True)
        return False


def load_signed(cache_dir: str, filename: str) -> Optional[Any]:
    """验签读取缓存对象。文件不存在/被篡改/格式异常 → 返回 None（调用方按未命中重算）。

    顺序固定：魔数 → 体积上限 → 验签 → 纯数据解码。
    先验签后解码，任何未通过验签的字节都不会进入解码器。
    非法文件名（含分隔符/穿越片段）直接按未命中返回 None，见 _is_safe_cache_name。
    """
    if not _is_safe_cache_name(filename):
        _log.warning("拒绝读取非法缓存文件名（疑似路径穿越）: %r", filename)
        return None
    path = os.path.join(cache_dir, filename)
    try:
        if not (os.path.exists(path) and os.path.getsize(path) > 0):
            return None
        if os.path.getsize(path) > _MAX_CACHE_BYTES:
            _log.warning("缓存文件超过 %dMB 上限，按未命中处理: %s",
                         _MAX_CACHE_BYTES // (1024 * 1024), filename)
            return None
        with open(path, "rb") as f:
            raw = f.read()
        head, sig, payload = raw.split(b"\n", 2)
        if head != _MAGIC:
            # 含 v1.2.3 及更早的 pickle 缓存（b"HMAC1"）：不再解析，按未命中重算
            return None
        expect = hmac.new(_secret_key(cache_dir), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig.decode("ascii"), expect):
            return None  # 验签失败：缓存被篡改或密钥已更换
        return _decode(json.loads(payload.decode("utf-8")))
    except Exception:
        return None


def enforce_disk_lru(cache_dir: str, max_files: int = 50, max_total_mb: float = 1024.0) -> int:
    """按 mtime LRU 淘汰磁盘缓存，返回删除的文件数。

    缓存键含浮点参数组合，理论无穷多；不淘汰则反复调参可撑爆磁盘。
    同时清理旧版 `.pkl`（本模块已不读，留着只占磁盘）。
    """
    try:
        entries = []
        total = 0
        for name in os.listdir(cache_dir):
            if not name.endswith(_CACHE_SUFFIXES):
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
        # 这里的 mtime 只用于上面的排序（entries.sort），淘汰循环本身不需要它，
        # 故命名带下划线前缀表明"刻意不用"。
        for _mtime, size, path in entries:
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
