#!/usr/bin/env bash
# 储能调度系统 Web 版一键启动（Linux/macOS）
cd "$(dirname "$0")"
( sleep 2 && xdg-open http://127.0.0.1:8800 2>/dev/null || open http://127.0.0.1:8800 2>/dev/null ) &
python3 backend/server.py
