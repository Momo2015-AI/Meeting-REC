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
    reconnect_attempts: int = 5      # 设备断开（拔耳机/切默认设备）后自动重连次数
    reconnect_delay: float = 3.0     # 每次重连等待秒数


@dataclass
class AsrCfg:
    engine: str = "vosk"          # "vosk"（流式逐词）| "sensevoice"（块级整句，更准更快断句）
    lib_dir: str = "models/lib"
    model_dir: str = "models/vosk-model-small-cn-0.22"
    source_lang: str = "zh"
    partial_interval: float = 0.3
    # sensevoice 专属参数
    sv_model_dir: str = "models/sensevoice-small"
    sv_vad_dir: str = "models/fsmn-vad"
    sv_threads: int = 4           # ONNX 推理线程数
    sv_max_block_s: float = 6.0   # 单块最长秒数：超长强制断句（不等停顿）
    sv_textnorm: str = "woitn"    # 只认 "woitn"（原样，默认）| "withitn"（数字规范化）


@dataclass
class BackendCfg:
    """备用后端配置（跨服务商兜底）。"""
    name: str = "alt"
    base_url: str = ""
    api_key_file: str = ""
    api_key: str = ""
    api_key_env: str = ""
    model: str = ""
    fallback_models: list[str] = field(default_factory=list)
    enabled: bool = True
    extra_body: dict = field(default_factory=dict)   # 透传到请求体的附加字段（如火山方舟 thinking）


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
        "你是专业的会议同声传译。要求：译文准确、专业术语统一、"
        "表达符合目标语言商务习惯；只输出译文本身，不要解释，不要加引号。"
    )
    context_sentences: int = 3      # 翻译时携带的前几句原文作上下文（0=关闭）
    batch_size: int = 3             # 翻译批处理：一次请求最多合并几句（1=逐句）
    batch_wait: float = 0.4         # 凑批最长等待秒数（队列空闲时不再等）
    extra_body: dict = field(default_factory=dict)   # 主后端请求体附加字段（火山方舟关闭思考等）


@dataclass
class SummaryCfg:
    enabled: bool = True
    timeout: float = 60.0
    max_chars: int = 12000          # 单块送入 LLM 的转写字符数上限；超出时自动分块小结+归并总结
    language: str = "zh"
    fallback_models: list[str] = field(default_factory=list)
    system_prompt: str = (
        "你是专业的会议纪要助理，负责依据转写内容撰写结构化总结。"
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
class RescoreCfg:
    """whisper.cpp 离线重识别（scripts/rescore_whisper.py 的 UI 集成配置）。"""
    enabled: bool = True
    whisper_cli: str = ""           # 留空自动寻找本机 whisper.cpp 环境
    model: str = ""                 # 留空自动选择量化模型
    lang: str = "auto"


@dataclass
class Config:
    base_dir: Path
    audio: AudioCfg
    asr: AsrCfg
    translate: TranslateCfg
    summary: SummaryCfg
    ui: UiCfg
    output: OutputCfg
    rescore: RescoreCfg

    def resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (self.base_dir / path).resolve()


def _from_dict(dc_cls, data: dict):
    fields = dc_cls.__dataclass_fields__
    return dc_cls(**{k: v for k, v in data.items() if k in fields})


def default_config_path(root: str | Path | None = None) -> Path:
    """返回默认配置文件路径。

    优先级：本机私有配置 `config.local.toml` > 通用模板 `config.toml`。
    config.local.toml 用于存放本机专有的端点/模型/密钥路径，不进入版本库。
    """
    base = Path(root) if root else Path(__file__).resolve().parent.parent
    local = base / "config.local.toml"
    if local.is_file():
        return local
    return base / "config.toml"


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
        rescore=_from_dict(RescoreCfg, raw.get("rescore", {})),
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
