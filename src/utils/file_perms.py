# -*- coding: utf-8 -*-
"""文件权限收敛（公共模块）

存在理由：`os.chmod(0o600)` 只在 POSIX 上是权限边界。**Windows 上 `os.chmod`
只能切换只读位**，实测 `0o600` 写完后 `stat` 仍是 `0o666`——也就是说在 Windows 上
"0600 的一次性口令文件/密钥文件"这个说法不成立，文件对同机其他用户依然可读。

原先这段逻辑只长在 backend/auth.py 里，于是 `src/utils/cache_security.py` 生成的
缓存签名密钥（`config/.cache_secret`）漏掉了它，成为"同机另一用户可读密钥 → 伪造
HMAC 签名"这条链路的起点。下沉为 src/utils 公共实现后，所有保管秘密的写入方共用一份口径。

Windows 上额外用 `icacls` 显式收紧 ACL：
    icacls <file> /inheritance:r /grant:r "<user>:F"
切断继承、只给当前用户完全控制（保留 F 而非 R，否则后续 `os.replace` 会因为
无权删除目标文件而失败）。

`icacls` 不可用或执行失败时返回 False（调用方记录告警），不阻断主流程。
"""
import os
import subprocess

from src.utils.logger import get_logger

log = get_logger(__name__)


def harden_file_permissions(path: str) -> bool:
    """把文件访问收敛到"仅当前用户"。返回是否**真正生效**（而非尽力而为）。"""
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
