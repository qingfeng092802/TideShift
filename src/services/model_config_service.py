# -*- coding: utf-8 -*-
"""服务层：模型供应商凭据配置（P0-02/#23 双前端统一）

🟠#23 修复：此前 backend/server.py 与 Streamlit app.py 各自实现
_load_model_config/_save_model_config，且安全改造只做了一半——
server.py 落盘加密 Key（api_key_enc），app.py 读到密文不解密 →
用户在 Web 端配置的 Key 在 Streamlit 端静默失效（安全分叉）。

现在两端统一走本模块：写盘必加密、读盘必解密、明文 Key 永不落盘。
"""
import copy
import json
import os

from src.utils.logger import get_logger

_log = get_logger("model_config_service")

_DEFAULT_LLM_PARAMS = {"temperature": 0.3, "max_tokens": 2048, "timeout": 60, "stream": False}


def _default_providers():
    return [{
        "id": "deepseek-official",
        "name": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "api_format": "OpenAI 兼容 (/v1/chat/completions)",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    }]


def load_model_config(path: str) -> dict:
    """读取模型供应商配置；自动解密 api_key_enc → api_key（内存态明文）。"""
    import auth as auth_mod  # backend/auth.py（backend 目录在 sys.path 中）

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("providers"), list) and data["providers"]:
                for _p in data["providers"]:
                    _enc = _p.pop("api_key_enc", None)
                    if _enc and not _p.get("api_key"):
                        _p["api_key"] = auth_mod.decrypt_key(_enc)
                return data
        except Exception:
            _log.warning("模型配置文件读取失败，使用默认配置", exc_info=True)
    return {"providers": _default_providers(), "active_id": "deepseek-official",
            "llm_params": dict(_DEFAULT_LLM_PARAMS)}


def save_model_config(path: str, state: dict) -> None:
    """保存模型供应商配置；api_key 以 Fernet 加密为 api_key_enc 落盘（明文 Key 永不落盘）。"""
    import auth as auth_mod

    safe = copy.deepcopy(state)
    for _p in safe.get("providers", []):
        if _p.get("api_key"):
            _p["api_key_enc"] = auth_mod.encrypt_key(_p.pop("api_key"))
        else:
            _p.pop("api_key", None)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(safe, f, ensure_ascii=False, indent=2)
