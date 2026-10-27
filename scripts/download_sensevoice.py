"""SenseVoice 识别模型一键下载（ModelScope 国内直连，无需爬墙）。

下载两套模型到项目 models/ 目录（仓库只有 .pt，首次需本机导出 ONNX）：
- iic/SenseVoiceSmall                          → models/sensevoice-small（约 934 MB）
- iic/speech_fsmn_vad_zh-cn-16k-common-pytorch → models/fsmn-vad（约 1 MB，流式切句用）

用法：
    python scripts/download_sensevoice.py            # 下载 .pt 模型
    python -c "from funasr import AutoModel;         \
               AutoModel(model='models/sensevoice-small', disable_update=True) \
               .export(type='onnx', quantize=True);  \
               AutoModel(model='models/fsmn-vad', disable_update=True) \
               .export(type='onnx', quantize=True)"  # 导出 ONNX（需 funasr+torch，仅一次）
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REPOS = [
    ("iic/SenseVoiceSmall", "models/sensevoice-small"),
    ("iic/speech_fsmn_vad_zh-cn-16k-common-pytorch", "models/fsmn-vad"),
]


def main() -> int:
    try:
        from modelscope import snapshot_download
    except ImportError:
        sys.exit("请先安装：.venv\\Scripts\\python.exe -m pip install modelscope onnxruntime==1.20.1 funasr-onnx sentencepiece==0.2.0")
    for repo, rel in REPOS:
        dst = ROOT / rel
        print(f"[download] {repo} -> {dst}")
        p = snapshot_download(repo)
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(p, dst, dirs_exist_ok=True)
        print(f"[ok] {dst}")
    print("完成。config.toml 里把 [asr] engine 改为 \"sensevoice\" 即生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
