"""SenseVoice 引擎冒烟：用 tests/test.wav 验证「切块→识别」全链。

不落盘任何会话，只打印识别结果与耗时，判断 funasr_onnx 集成是否正确。

    python scripts/probe_sensevoice.py [tests/test.wav]
"""
from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.asr_sensevoice import SenseVoiceStream  # noqa: E402


def main() -> int:
    wav_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tests/test.wav")
    with wave.open(str(wav_path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, \
            f"测试音频须为 16kHz 单声道：{w.getframerate()} / {w.getnchannels()}"
        pcm = w.readframes(w.getnframes())

    t0 = time.time()
    engine = SenseVoiceStream(Path("models/sensevoice-small"), Path("models/fsmn-vad"),
                              threads=4, lang="auto")
    print(f"[init] 模型加载 {time.time() - t0:.1f}s  音频 {len(pcm)/32000:.1f}s")

    # 模拟环形缓存 100ms 一片喂入（与 LoopbackCapture 节奏一致）
    chunk = 3200          # 0.2s @16k*2B
    finals: list[str] = []
    t0 = time.time()
    for i in range(0, len(pcm), chunk):
        out = engine.accept(pcm[i:i + chunk])
        if out:
            finals.append(out)
            print(f"[{time.time() - t0:5.1f}s] final → {out}")
    print(f"\n[done] 识别 {time.time() - t0:.1f}s / 音频 {len(pcm)/32000:.1f}s "
          f"(RTF {(time.time() - t0)/(len(pcm)/32000):.2f})")
    print("[finals]", finals if finals else "（无成句——检查 VAD 配置）")
    return 0 if finals else 1


if __name__ == "__main__":
    raise SystemExit(main())
