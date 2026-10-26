"""把会话目录里的 meeting.wav 用 whisper.cpp 重新识别，生成 transcript_whisper.md。

Vosk small 模型识别快但准确率有限；本脚本用 whisper（whisper.cpp 预编译
binaries + 量化 ggml 模型）对留档 WAV 做离线重识别，用于：
1. 校正在线转写的错句；
2. 会议结束后回溯补全漏掉的句子。

用法：
    python scripts/rescore_whisper.py output/meeting_20261022_173134
    python scripts/rescore_whisper.py output/xxx/meeting.wav
    python scripts/rescore_whisper.py output/xxx --lang zh

依赖（外部）：whisper-cli.exe 与 ggml 模型文件，路径可用参数指定，
默认寻找 ../meeting-translate/（本机已有的 whisper.cpp 环境）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # 项目根
SIBLING = Path.home() / "Documents" / "Default Project" / "meeting-translate"  # whisper.cpp 环境

NOISE_PREFIX = ("whisper_", "load_backend", "read_audio", "main:", "system_info")


def find_cli(cli_arg: str) -> Path:
    if cli_arg:
        p = Path(cli_arg)
        if not p.is_file():
            sys.exit(f"whisper-cli 不存在：{p}")
        return p
    for cand in (SIBLING / "wc2/Release/whisper-cli.exe", SIBLING / "wc-bin/Release/whisper-cli.exe"):
        if cand.is_file():
            return cand
    sys.exit("未找到 whisper-cli.exe，请用 --whisper-cli 指定路径")


def find_model(model_arg: str) -> Path:
    if model_arg:
        p = Path(model_arg)
        if not p.is_file():
            sys.exit(f"模型文件不存在：{p}")
        return p
    for cand in sorted((SIBLING / "models_local").glob("ggml-*.bin")):
        if "q8" in cand.name or "q5" in cand.name:
            return cand
    for cand in sorted((SIBLING / "models_local").glob("ggml-*.bin")):
        return cand
    sys.exit("未找到 ggml 模型，请用 --model 指定路径")


def transcribe(cli: Path, model: Path, wav: Path, lang: str, threads: int,
               timeout: float) -> str:
    """跑一次 whisper-cli，返回清洗后的文本行（保留 [mm:ss -->] 时间戳）。"""
    r = subprocess.run(
        [str(cli), "-m", str(model), "-l", lang, "-np", "-t", str(threads), str(wav.resolve())],
        capture_output=True, cwd=str(wav.parent), timeout=timeout,
        encoding="utf-8", errors="replace",
    )
    lines = []
    for raw in (r.stdout.splitlines() + r.stderr.splitlines()):
        ln = raw.strip()
        if not ln or ln.startswith(NOISE_PREFIX):
            continue
        lines.append(raw.rstrip())
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="whisper.cpp 离线重识别会话 WAV")
    ap.add_argument("target", help="会话目录或 meeting.wav 路径")
    ap.add_argument("--whisper-cli", default="", help="whisper-cli.exe 路径")
    ap.add_argument("--model", default="", help="ggml 模型路径")
    ap.add_argument("--lang", default="auto", help="语言：auto / zh / en")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=900.0,
                    help="单次识别超时秒数（长会议酌情增大）")
    args = ap.parse_args()

    cli = find_cli(args.whisper_cli)
    model = find_model(args.model)

    target = Path(args.target)
    if target.is_dir():
        wav = target / "meeting.wav"
    else:
        wav = target
    if not wav.is_file():
        sys.exit(f"未找到录音文件：{wav}")
    session_dir = wav.parent

    print(f"[rescore] whisper-cli = {cli}")
    print(f"[rescore] model      = {model.name}")
    print(f"[rescore] wav        = {wav}")

    text = transcribe(cli, model, wav, args.lang, args.threads, args.timeout)
    if not text.strip():
        sys.exit("whisper 未输出任何文本（音频可能为空）")

    out = session_dir / "transcript_whisper.md"
    out.write_text(
        "# Whisper 离线重识别纪要\n\n"
        f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"- 来源：{wav}\n"
        f"- 模型：{model.name}\n"
        f"- 语言：{args.lang}\n\n"
        "## 转写\n\n"
        + text + "\n",
        encoding="utf-8",
    )
    print(f"[rescore] 写入 {out}")
    print(text[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
