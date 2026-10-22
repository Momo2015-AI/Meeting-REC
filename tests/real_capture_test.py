"""真实端到端验证：系统放音 → WASAPI 环录 → Vosk 识别 → 纪要落盘。

通过 winsound 异步播放测试语音（走默认输出设备），环录线程应捕获到系统混音，
从而验证「采集 → 重采样 16k → 流式识别 → 落盘」整条真实链路。

运行：python tests/real_capture_test.py
"""
from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import default_config_path, load_config  # noqa: E402
from src.transcriber import MeetingTranscriber  # noqa: E402


def main() -> int:
    import winsound

    wav = ROOT / "tests" / "test.wav"
    assert wav.exists(), f"缺少测试音频 {wav}"

    cfg = load_config(default_config_path(ROOT))
    cfg.asr.model_dir = "models/vosk-model-small-en-us-0.15"
    cfg.asr.source_lang = "en"
    cfg.translate.enabled = False
    cfg.audio.save_audio = True

    t = MeetingTranscriber(cfg)
    print(f"[init] 模型已加载：{cfg.asr.model_dir}")
    t.start()
    time.sleep(1.2)

    print("[play] 播放测试语音（走系统默认输出设备）……")
    winsound.PlaySound(str(wav), winsound.SND_FILENAME | winsound.SND_ASYNC)
    deadline = time.time() + 14
    finals: list[tuple[str, str, str]] = []
    while time.time() < deadline:
        try:
            while True:
                ev = t.events.get_nowait()
                if ev[0] == "final":
                    finals.append(ev)
                    print(f"  [final] {ev[2]}")
                elif ev[0] == "error":
                    print(f"  [error] {ev[1]}")
        except Exception:
            pass
        time.sleep(0.1)
    t.stop()

    audio = t.session_dir / "meeting.wav"
    size = audio.stat().st_size if audio.exists() else 0
    with wave.open(str(audio), "rb") as w:
        frames, rate = w.getnframes(), w.getframerate()
    seconds = frames / rate
    text = " ".join(f[2] for f in finals).strip()

    print("\n==== 真实环录测试结果 ====")
    print(f"  环录音频：{audio.name}  {size} 字节 / {seconds:.1f} 秒 @ {rate}Hz")
    print(f"  识别语句数：{len(finals)}")
    print(f"  识别文本：{text or '（无）'}")
    ok = size > 44 + rate and len(finals) > 0
    print(f"  结论：{'通过 —— 真实系统音频已被环录并识别' if ok else '未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
