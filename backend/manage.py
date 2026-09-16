#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""汐储 TideShift · 运维命令行（无第三方依赖，仅标准库）

解决的痛点：此前"忘记管理员口令"只能手动找到并删除 `config/auth.json`，
使用者既要知道路径、又得理解这套机制。本命令提供明确的恢复入口。

用法（在项目根目录执行）：

    python -m backend.manage show-state                  # 查看认证状态（只读，无副作用）
    python -m backend.manage reset-password              # 重置为新的随机口令并打印
    python -m backend.manage reset-password --password 'YourStrongPass'   # 指定新口令

注意：`ENERGY_AUTH_MODE=env` 时口令由环境变量托管，本命令不做修改（会明确提示改法）。
"""
import argparse
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 与 backend/server.py 保持一致的导入方式：把项目根与 backend 都挂上 sys.path，
# 并以模块名 `auth` 导入，确保拿到的是同一个模块对象（而非并存的 backend.auth 副本）。
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import auth as auth_mod  # noqa: E402
from src.utils.config import CONFIG  # noqa: E402,F401  (触发配置加载，行为与运行时一致)


def _read_auth_file(path: str):
    """只读地读取口令文件；不存在或损坏返回 None（不产生任何副作用）。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def cmd_show_state(_args) -> int:
    mode = auth_mod.auth_mode()
    cfg_dir = auth_mod._config_dir()
    auth_path = os.path.join(cfg_dir, "auth.json")
    init_path = os.path.join(cfg_dir, auth_mod.INITIAL_PWD_FILENAME)

    state = _read_auth_file(auth_path)
    env_pwd = auth_mod.env_password()

    print("=" * 62)
    print("汐储 TideShift · 认证状态")
    print("=" * 62)
    print(f"认证模式            : {mode}"
          + ("（口令由环境变量管理，不落盘）" if mode == "env" else "（口令哈希落盘）"))
    print(f"配置目录            : {cfg_dir}")
    if os.environ.get("ENERGY_CONFIG_DIR", "").strip():
        print("                      ↑ 来自环境变量 ENERGY_CONFIG_DIR")
    elif os.environ.get("AUTH_CONFIG_DIR", "").strip():
        print("                      ↑ 来自环境变量 AUTH_CONFIG_DIR（兼容别名）")

    if mode == "env":
        print(f"ADMIN_INITIAL_PASSWORD: {'已设置' if env_pwd else '**未设置——将无法登录**'}")
        print("口令文件            : 不使用（env 模式不读写任何口令文件）")
    else:
        print(f"口令文件            : {auth_path}")
        print(f"  是否存在          : {'是' if os.path.exists(auth_path) else '否'}")
        if state:
            print(f"  用户名            : {state.get('username', 'admin')}")
            print(f"  需首次改密        : {'是' if state.get('must_change') else '否'}")
        print(f"ADMIN_INITIAL_PASSWORD: "
              + ("已设置" if env_pwd else "未设置"))
        if env_pwd and os.path.exists(auth_path):
            print("                      ⚠️ 口令文件已存在，该环境变量**不会生效**"
                  "（只在首次创建时读取）")
            print("                      → 想让环境变量每次启动都生效：设 ENERGY_AUTH_MODE=env")
    print(f"一次性初始口令文件  : {init_path}")
    print(f"  是否存在          : {'是（首登改密后会自动删除）' if os.path.exists(init_path) else '否'}")
    print("-" * 62)
    print("忘记口令时：python -m backend.manage reset-password")
    print("=" * 62)
    return 0


def cmd_reset_password(args) -> int:
    mode = auth_mod.auth_mode()
    if mode == "env":
        print("当前为 env 认证模式：口令由环境变量 ADMIN_INITIAL_PASSWORD 管理，"
              "不落盘、本命令无法修改。", file=sys.stderr)
        print("请修改该环境变量后重启服务（或改回 ENERGY_AUTH_MODE=persistent 再执行本命令）。",
              file=sys.stderr)
        return 2

    # auto_create=False：不因"口令文件不存在"而先造一份随机口令，
    # 否则控制台会同时出现两个口令（自动生成的 + 本次重置的），使用者无从分辨。
    store = auth_mod.AuthStore(auto_create=False)
    if args.password:
        new_pwd = args.password
    else:
        new_pwd = secrets.token_urlsafe(max(12, args.length))[:max(12, args.length)]

    store.set_password(new_pwd)          # 内部会删除一次性口令文件
    print("=" * 62)
    print("管理员口令已重置")
    print("=" * 62)
    print(f"用户名    : {store.username}")
    print(f"新口令    : {new_pwd}")
    print(f"口令文件  : {store.path}")
    print("-" * 62)
    print("请立即登录并妥善保存；此口令仅在此处显示一次。")
    print("=" * 62)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.manage",
        description="汐储 TideShift 运维命令（口令恢复 / 状态查看）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_state = sub.add_parser("show-state", help="查看认证模式、口令文件与一次性口令文件状态（只读）")
    p_state.set_defaults(func=cmd_show_state)

    p_reset = sub.add_parser("reset-password", help="重置管理员口令")
    p_reset.add_argument("--password", default="", help="指定新口令；省略则生成随机强口令")
    p_reset.add_argument("--length", type=int, default=16, help="随机口令长度（默认 16，最小 12）")
    p_reset.set_defaults(func=cmd_reset_password)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
