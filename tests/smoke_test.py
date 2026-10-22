"""无硬件冒烟测试：Vosk 真实识别 / 翻译 HTTP 链路 / 管线端到端。

运行：
    python tests/smoke_test.py
    python tests/smoke_test.py --wav path/to/sample.wav
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.asr import VoskASR  # noqa: E402
from src.config import load_config  # noqa: E402
from src.transcriber import MeetingTranscriber  # noqa: E402
from src.translate import Translator  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def check(name: str, fn) -> None:
    try:
        detail = fn() or ""
        RESULTS.append(("PASS", name, detail))
        print(f"[PASS] {name} :: {detail}")
    except Exception as e:  # noqa: BLE001
        RESULTS.append(("FAIL", name, f"{type(e).__name__}: {e}"))
        print(f"[FAIL] {name} :: {type(e).__name__}: {e}")


def read_wav_pcm(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1, "仅支持单声道"
        assert w.getsampwidth() == 2, "仅支持 16-bit"
        return w.readframes(w.getnframes()), w.getframerate()


def synth_pcm(seconds: float = 3.0, rate: int = 16000) -> bytes:
    buf = bytearray()
    for i in range(int(seconds * rate)):
        v = int(6000 * math.sin(2 * math.pi * 220 * i / rate))
        buf += struct.pack("<h", v)
    return bytes(buf)


# ---------------------------------------------------------------- 用例

def case_asr_real_audio(wav_path: Path, model_dir: str) -> str:
    pcm, rate = read_wav_pcm(wav_path)
    assert rate == 16000, f"采样率应为 16000，实际 {rate}"
    asr = VoskASR(str(ROOT / "models" / "lib"), str(ROOT / "models" / model_dir), 16000)
    try:
        step = 3200
        finals: list[str] = []
        for off in range(0, len(pcm), step):
            text = asr.accept(pcm[off:off + step])
            if text:
                finals.append(text)
        tail = asr.final()
        if tail:
            finals.append(tail)
        joined = " ".join(finals).strip()
        assert joined, "未识别出任何文本（识别链路可能异常）"
        return f"识别文本：{joined[:110]}"
    finally:
        asr.close()


def case_translator_http() -> str:
    captured: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            captured.update(body)
            payload = json.dumps(
                {"choices": [{"message": {"content": "MOCK-TRANSLATION-OK"}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        tr = Translator(base_url=f"http://127.0.0.1:{port}/v1", api_key="k",
                        model="mock-model", target_lang="en", timeout=5)
        out = tr.translate("测试翻译", "zh")
        assert out == "MOCK-TRANSLATION-OK", f"返回值异常：{out!r}"
        assert captured["model"] == "mock-model", "model 字段未透传"
        assert captured["messages"][0]["role"] == "system", "system prompt 未透传"
        return f"HTTP 翻译链路正常，model={captured['model']}，messages={len(captured['messages'])}"
    finally:
        srv.shutdown()


def case_pipeline() -> str:
    cfg = load_config(ROOT / "config.toml")
    cfg.translate.enabled = False          # 无真实 LLM 时跳过翻译
    cfg.audio.save_audio = True
    pcm = synth_pcm(2.0)

    def fake_source(on_pcm, on_error):
        def worker():
            for off in range(0, len(pcm), 3200):
                on_pcm(pcm[off:off + 3200])
                time.sleep(0.02)
        threading.Thread(target=worker, daemon=True).start()

    t = MeetingTranscriber(cfg, pcm_source=fake_source)
    t.start()
    time.sleep(1.2)
    t.stop()
    assert t.transcript_path is not None and t.transcript_path.exists(), "transcript.md 未生成"
    wav = t.session_dir / "meeting.wav"
    assert wav.exists() and wav.stat().st_size > 44, "meeting.wav 未生成或为空"
    body = t.transcript_path.read_text(encoding="utf-8")
    assert "会议双语纪要" in body, "纪要头部缺失"
    return (f"纪要={t.session_path.name if hasattr(t, 'session_path') else t.transcript_path.name}，"
            f"WAV={wav.stat().st_size} 字节，stats={t.stats}")


def case_devices() -> str:
    try:
        from src.capture import list_devices
    except Exception as e:  # noqa: BLE001
        return f"（跳过：soundcard 不可用 {e}）"
    try:
        info = list_devices()
    except Exception as e:  # noqa: BLE001
        return f"（跳过：本机无音频设备 {e}）"
    return f"输出设备 {len(info['speakers'])} 个，环录设备 {len(info['loopbacks'])} 个"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", default=str(ROOT / "tests" / "test.wav"))
    ap.add_argument("--model", default="vosk-model-small-en-us-0.15")
    args = ap.parse_args()

    wav = Path(args.wav)
    if wav.exists():
        check("ASR 真实音频识别", lambda: case_asr_real_audio(wav, args.model))
    else:
        print(f"[SKIP] ASR 真实音频识别：缺少测试音频 {wav}")

    check("翻译 HTTP 链路", case_translator_http)
    check("管线端到端（转写+落盘）", case_pipeline)
    check("音频设备枚举", case_devices)

    passed = sum(1 for r in RESULTS if r[0] == "PASS")
    print(f"\n==== 结果：{passed}/{len(RESULTS)} 通过 ====")
    for status, name, detail in RESULTS:
        print(f"  {status}  {name}  {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
