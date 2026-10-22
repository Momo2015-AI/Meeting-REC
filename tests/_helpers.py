"""测试公共辅助：判断当前是否具备调用真实 LLM 后端的条件。

公开仓库的用户往往没有配置密钥或使用占位端点。此时依赖真实后端的断言
无法成立，应标记为「跳过」而不是「失败」，以免误导使用者。
"""
from __future__ import annotations

from pathlib import Path

PLACEHOLDER_MARKERS = ("example.com", "your-model-name", "your-fallback-model",
                       "your-backup", "unused")


def backend_ready(cfg) -> bool:
    """返回当前配置是否指向一个可能真实可用的翻译后端。"""
    t = cfg.translate
    if not t.enabled or not t.base_url:
        return False
    low = t.base_url.lower()
    if any(m in low for m in PLACEHOLDER_MARKERS):
        return False
    if t.api_key in ("", "unused", "ollama"):
        # ollama 本地端点无需密钥，但也需要本地服务在跑；此处放行由调用方预检
        return t.api_key == "ollama"
    return True


def skip_reason(cfg) -> str:
    return ("未配置可用的翻译后端（缺少密钥或仍为占位端点），"
            "相关断言已跳过；配置好 config.local.toml 后可完整运行")
