"""下载 Vosk 语音模型到 models/ 目录。

模型体积较大（约 140MB），未随仓库入库，需首次运行时执行本脚本。

用法:
    python scripts/download_models.py            # 下载英文模型（默认）
    python scripts/download_models.py --cn       # 下载中文模型
    python scripts/download_models.py --all      # 两者都下
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(ROOT, "models")

MODELS = {
    "en": {
        "dir": "vosk-model-small-en-us-0.15",
        "url": "https://huggingface.co/ambind/vosk-model-small-en-us-0.15/resolve/main/vosk-model-small-en-us-0.15_c_.zip",
    },
    "cn": {
        "dir": "vosk-model-small-cn",
        "url": "https://huggingface.co/guloooovoooo/vosk-model-small-cn/resolve/main/vosk-model-small-cn.zip",
    },
}


def _progress(count: int, block: int, total: int) -> None:
    if total <= 0:
        return
    done = min(100, count * block * 100 // total)
    sys.stdout.write(f"\r  下载中... {done}%")
    sys.stdout.flush()


def download(lang: str) -> bool:
    spec = MODELS[lang]
    target_dir = os.path.join(MODELS_DIR, spec["dir"])
    if os.path.isdir(target_dir) and os.listdir(target_dir):
        print(f"[跳过] {spec['dir']} 已存在")
        return True

    os.makedirs(MODELS_DIR, exist_ok=True)
    zip_path = os.path.join(MODELS_DIR, f"{spec['dir']}.zip")
    print(f"[下载] {lang} -> {spec['dir']}")
    try:
        urllib.request.urlretrieve(spec["url"], zip_path, _progress)
        print()
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] {exc}")
        return False

    print("[解压] ...")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(MODELS_DIR)
    os.remove(zip_path)

    # 兼容部分压缩包会多套一层同名目录
    inner = os.path.join(target_dir, spec["dir"])
    if os.path.isdir(inner):
        for item in os.listdir(inner):
            shutil.move(os.path.join(inner, item), os.path.join(target_dir, item))
        os.rmdir(inner)

    print(f"[完成] {target_dir}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 Vosk 语音模型")
    parser.add_argument("--cn", action="store_true", help="下载中文模型")
    parser.add_argument("--all", action="store_true", help="下载全部模型")
    parser.add_argument("--en", action="store_true", help="下载英文模型（默认）")
    args = parser.parse_args()

    langs = []
    if args.all:
        langs = ["en", "cn"]
    elif args.cn:
        langs = ["cn"]
    else:
        langs = ["en"]

    ok = all(download(lang) for lang in langs)
    if not ok:
        print("\n部分模型下载失败，请检查网络后重试。")
        return 1
    print("\n全部模型就绪。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
