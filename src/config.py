"""配置加载：TOML 格式，相对路径统一相对于 config 文件所在目录解析。"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AudioCfg:
    sample_rate: int = 16000
    chunk_ms: int = 100
    save_audio: bool = True


@dataclass
class AsrCfg:
    lib_dir: str = "models/lib"
    model_dir: str = "models/vosk-model-small-cn-0.22"
    source_lang: str = "zh"
    partial_interval: float = 0.3


@dataclass
class BackendCfg:
    """备用后端配置（跨服务商兜底，如火山方舟豆包）。"""
    name: str = "alt"
    base_url: str = ""
    api_key_file: str = ""
    api_key: str = ""
    api_key_env: str = ""
    model: str = ""
    fallback_models: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class TranslateCfg:
    enabled: bool = True
    backend: str = "openai_compatible"
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    api_key_env: str = ""        # 从环境变量读取 key（优先于 api_key）
    api_key_file: str = ""       # 从文件读取 key（相对路径基于配置目录）
    model: str = "qwen2.5:7b-instruct"
    target_lang: str = "en"
    timeout: float = 8.0
    fallback_models: list[str] = field(default_factory=list)
    # 备用后端列表：主后端整体失败时按顺序顶上（如豆包免费额度兜底）
    alt_backends: list[BackendCfg] = field(default_factory=list)
    system_prompt: str = (
        "你是资深的储能行业会议同声传译。要求：译文准确、专业术语统一、"
        "表达符合目标语言商务习惯；只输出译文本身，不要解释，不要加引号。"
    )


@dataclass
class SummaryCfg:
    enabled: bool = True
    timeout: float = 60.0
    max_chars: int = 12000
    language: str = "zh"
    fallback_models: list[str] = field(default_factory=list)
    system_prompt: str = (
        "你是一位资深的储能行业项目经理助理，负责为会议纪要撰写结构化总结。"
        "严格按 Markdown 结构输出：## 会议概要 / ## 核心要点 / ## 决议事项 / "
        "## 待办事项 / ## 风险与关注点。只依据转写内容，不得臆造。"
    )


@dataclass
class UiCfg:
    enabled: bool = True
    width: int = 900
    height: int = 200
    opacity: float = 0.92
    always_on_top: bool = True
    font_size: int = 13
    autostart: bool = False       # 不自动采集：必须点击「开始」才监听系统音频


@dataclass
class OutputCfg:
    dir: str = "output"


@dataclass
class Config:
    base_dir: Path
    audio: AudioCfg
    asr: AsrCfg
    translate: TranslateCfg
    summary: SummaryCfg
    ui: UiCfg
    output: OutputCfg

    def resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (self.base_dir / path).resolve()


def _from_dict(dc_cls, data: dict):
    fields = dc_cls.__dataclass_fields__
    return dc_cls(**{k: v for k, v in data.items() if k in fields})


def load_config(path: str | Path) -> Config:
    cfg_path = Path(path).resolve()
    if not cfg_path.is_file():
        raise FileNotFoundError(f"配置文件不存在：{cfg_path}")
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    t_raw = raw.get("translate", {})
    cfg = Config(
        base_dir=cfg_path.parent,
        audio=_from_dict(AudioCfg, raw.get("audio", {})),
        asr=_from_dict(AsrCfg, raw.get("asr", {})),
        translate=_from_dict(TranslateCfg, t_raw),
        summary=_from_dict(SummaryCfg, raw.get("summary", {})),
        ui=_from_dict(UiCfg, raw.get("ui", {})),
        output=_from_dict(OutputCfg, raw.get("output", {})),
    )
    # 备用后端（[[translate.alt_backends]] 数组表）
    cfg.translate.alt_backends = [
        _from_dict(BackendCfg, b) for b in t_raw.get("alt_backends", [])
    ]
    _resolve_api_key(cfg)
    for b in cfg.translate.alt_backends:
        _resolve_backend_key(cfg, b)
    return cfg


def _resolve_api_key(cfg: Config) -> None:
    """按 api_key_file > api_key_env > api_key 的优先级解析最终密钥。"""
    t = cfg.translate
    t.api_key = _resolve_key(cfg.base_dir, t.api_key_file, t.api_key_env, t.api_key)


def _resolve_backend_key(cfg: Config, b: BackendCfg) -> None:
    """解析备用后端密钥（规则同主后端）。"""
    b.api_key = _resolve_key(cfg.base_dir, b.api_key_file, b.api_key_env, b.api_key)


def _resolve_key(base_dir: Path, key_file: str, key_env: str, fallback: str) -> str:
    if key_file:
        key_path = Path(key_file)
        if not key_path.is_absolute():
            key_path = (base_dir / key_path).resolve()
        try:
            text = key_path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    if key_env:
        import os

        env_val = os.environ.get(key_env, "").strip()
        if env_val:
            return env_val
    return fallback
