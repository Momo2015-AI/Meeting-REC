"""引擎切换验证：运行时在 Vosk / SenseVoice 之间切换的正确性。

覆盖：
- build_asr 工厂按引擎名构建正确的识别器；
- switch_engine 成功路径：换掉实例、更新 cfg/engine 字段、释放旧资源；
- switch_engine 失败路径：未知引擎 / 采集中 → 拒绝且**不破坏现有状态**；
- 切换后两个引擎都能真正吃 PCM 并产出 final（不依赖真实音频设备）；
- 末句冲刷：drain_finals 能取回最后一句（SenseVoice 尤其关键）。

运行：python tests/engine_switch_test.py
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import default_config_path, load_config  # noqa: E402
from src.transcriber import MeetingTranscriber, build_asr  # noqa: E402

PASS, FAIL = "✓", "✗"
_results: list[tuple[bool, str]] = []


def check(cond: bool, name: str, detail: str = "") -> None:
    _results.append((bool(cond), name))
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail else ""))


def sensevoice_ready(cfg) -> bool:
    """本机是否具备跑通 SenseVoice 的条件（依赖 + 模型）。"""
    try:
        import funasr_onnx  # noqa: F401
        import onnxruntime  # noqa: F401
        import sentencepiece  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return (cfg.resolve(cfg.asr.sv_model_dir).is_dir()
            and cfg.resolve(cfg.asr.sv_vad_dir).is_dir())


def main() -> int:
    cfg = load_config(default_config_path(ROOT))
    cfg.translate.enabled = False
    cfg.asr.engine = "vosk"

    print("==== 引擎切换验证 ====")

    # ---------- 1. 工厂按名构建 ----------
    print("\n[1] build_asr 工厂")
    try:
        vosk = build_asr(cfg, "vosk")
        check(True, "构建 vosk 引擎")
        check(type(vosk).__name__ == "VoskASR", "vosk 返回 VoskASR", type(vosk).__name__)
        vosk.close()
    except Exception as e:  # noqa: BLE001
        check(False, "构建 vosk 引擎", str(e))

    sv_ready = sensevoice_ready(cfg)
    if sv_ready:
        try:
            sv = build_asr(cfg, "sensevoice")
            check(type(sv).__name__ == "SenseVoiceStream", "构建 sensevoice 引擎",
                  type(sv).__name__)
            sv.close()
        except Exception as e:  # noqa: BLE001
            check(False, "构建 sensevoice 引擎", str(e))
    else:
        print(f"  - 跳过 sensevoice 构建（本机缺依赖或模型）")

    # ---------- 2. 切换的成功 / 失败路径 ----------
    print("\n[2] switch_engine 语义")
    t = MeetingTranscriber(cfg)
    original = t.asr
    check(t.engine == "vosk", "初始引擎为 vosk", t.engine)

    ok, msg = t.switch_engine("不存在的引擎")
    check(not ok, "未知引擎被拒绝", msg)
    check(t.asr is original, "拒绝后识别器实例未变")

    ok, msg = t.switch_engine("vosk")
    check(ok and t.asr is original, "切换到当前引擎为幂等操作", msg)

    if sv_ready:
        ok, msg = t.switch_engine("sensevoice")
        check(ok, "切换到 sensevoice", msg)
        check(t.engine == "sensevoice", "engine 字段已更新", t.engine)
        check(cfg.asr.engine == "sensevoice", "cfg.asr.engine 已更新", cfg.asr.engine)
        check(t.asr is not original, "识别器实例已替换")
        ok, msg = t.switch_engine("vosk")
        check(ok and t.engine == "vosk", "切回 vosk", msg)
    else:
        print("  - 跳过成功路径切换（本机不具备 SenseVoice 条件）")

    # ---------- 3. 采集中拒绝切换 ----------
    print("\n[3] 采集中拒绝切换")
    t2 = MeetingTranscriber(cfg)
    before = t2.asr
    t2._active = True          # 模拟采集进行中
    ok, msg = t2.switch_engine("sensevoice")
    check(not ok, "采集中切换被拒绝", msg)
    check(t2.asr is before, "拒绝后识别器实例未变")
    t2._active = False

    # ---------- 4. 两引擎实际吃 PCM ----------
    print("\n[4] 切换后的引擎实际识别")
    wav = ROOT / "tests" / "test.wav"
    if wav.is_file():
        with wave.open(str(wav), "rb") as w:
            pcm = w.readframes(w.getnframes())

        got: list[str] = []
        t3 = MeetingTranscriber(cfg)
        t3.switch_engine("vosk")
        chunk = 3200
        for i in range(0, len(pcm), chunk):
            out = t3.asr.accept(pcm[i:i + chunk])
            if out:
                got.append(out)
        for text in (t3.asr.drain_finals() or []):
            got.append(text)
        check(bool(got), "vosk 引擎识别出文本", " ".join(got)[:70])

        if sv_ready:
            t4 = MeetingTranscriber(cfg)
            ok, msg = t4.switch_engine("sensevoice")
            if ok:
                sv_got: list[str] = []
                for i in range(0, len(pcm), chunk):
                    out = t4.asr.accept(pcm[i:i + chunk])
                    if out:
                        sv_got.append(out)
                for text in (t4.asr.drain_finals() or []):
                    sv_got.append(text)
                check(bool(sv_got), "sensevoice 引擎识别出文本", " ".join(sv_got)[:70])
                # 末句冲刷：整段识别应包含尾部内容（"zero one eight zero three"）
                joined = " ".join(sv_got)
                check("eight" in joined.lower(),
                      "末句冲刷保留了尾部内容（不再丢句）", joined[-60:])
            else:
                check(False, "切换到 sensevoice", msg)
    else:
        print("  - 跳过识别验证（缺少 tests/test.wav）")

    # ---------- 汇总 ----------
    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print(f"\n==== 结论：{passed}/{total} 项通过 ====")
    if passed != total:
        for ok, name in _results:
            if not ok:
                print(f"  失败：{name}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
