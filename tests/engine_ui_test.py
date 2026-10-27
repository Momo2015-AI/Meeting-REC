"""引擎切换按钮的 UI 层验证：真实 tkinter 窗口 + 真实切换动作。

覆盖：
- 工具栏存在引擎下拉框，显示当前引擎名；
- 下拉框选项覆盖两个引擎；
- 采集中下拉框被禁用（不可切换）；
- 停止后下拉框恢复可用；
- 触发切换后，下拉框显示与转写器实际引擎**保持一致**（成功回填、失败回滚）；
- 切换失败时错误原因写入提示区，且不残留不一致状态。

运行：python tests/engine_ui_test.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import default_config_path, load_config  # noqa: E402
from src.transcriber import MeetingTranscriber  # noqa: E402
from src.ui import ENGINES, SubtitleWindow  # noqa: E402

_results: list[tuple[bool, str]] = []


def check(cond: bool, name: str, detail: str = "") -> None:
    _results.append((bool(cond), name))
    print(f"  {'✓' if cond else '✗'} {name}" + (f" — {detail}" if detail else ""))


def pump(win: SubtitleWindow, rounds: int = 25) -> None:
    for _ in range(rounds):
        win.root.update()
        time.sleep(0.04)


def main() -> int:
    cfg = load_config(default_config_path(ROOT))
    cfg.translate.enabled = False
    cfg.asr.engine = "vosk"

    t = MeetingTranscriber(cfg)
    win = SubtitleWindow(cfg, t)
    win.root.update()

    print("==== 引擎切换按钮 UI 验证 ====")

    # ---------- 控件存在性与初值 ----------
    labels = list(win.engine_btn.cget("values"))
    check(len(labels) == len(ENGINES), "下拉框列出全部引擎", str(labels))
    current = win.engine_var.get()
    expected = ENGINES["vosk"][0]
    check(current == expected, "下拉框初值等于当前引擎", f"{current}（期望 {expected}）")

    # ---------- 采集中禁用 ----------
    # 注意：t.running 读的是 _active 标志（真实采集态），不是 UI 的 status 事件。
    # 这里两者都要置位：_active 让转写器判定"正在采集"，事件让 UI 刷新控件状态。
    t._active = True
    t.events.put(("status", "running"))
    pump(win)
    check(str(win.engine_btn.cget("state")) == "disabled",
          "采集中下拉框被禁用", str(win.engine_btn.cget("state")))

    # 采集中尝试切换 → 被拒绝且状态一致
    before = t.engine
    win.engine_var.set(ENGINES["sensevoice"][0])
    win._on_engine_pick()
    pump(win)
    check(t.engine == before, "采集中切换被拒绝，引擎未变", t.engine)
    check(win.engine_var.get() == ENGINES[before][0],
          "采集中切换后下拉框回滚", win.engine_var.get())

    # ---------- 停止后恢复 ----------
    t._active = False
    t.events.put(("status", "stopped"))
    pump(win)
    check(str(win.engine_btn.cget("state")) == "readonly",
          "停止后下拉框恢复可用", str(win.engine_btn.cget("state")))

    # ---------- 成功切换（若本机具备 SenseVoice 条件） ----------
    try:
        import funasr_onnx  # noqa: F401
        import onnxruntime  # noqa: F401
        import sentencepiece  # noqa: F401
        sv_ready = (cfg.resolve(cfg.asr.sv_model_dir).is_dir()
                    and cfg.resolve(cfg.asr.sv_vad_dir).is_dir())
    except Exception:  # noqa: BLE001
        sv_ready = False

    if sv_ready:
        win.engine_var.set(ENGINES["sensevoice"][0])
        win._on_engine_pick()
        # 切换在后台线程执行（要加载模型），结果经队列回主线程。
        # 等「队列被消费完 + 引擎已切换」才算完成——只看 t.engine 变化会有竞态：
        # 引擎在后台线程里已换，但 UI 的收尾回调还没跑。
        for _ in range(200):
            pump(win, rounds=1)
            if t.engine == "sensevoice" and win._engine_result.empty():
                body = win.text.get("1.0", "end")
                if "✓" in body:
                    break
            time.sleep(0.05)
        check(t.engine == "sensevoice", "成功切换到 sensevoice", t.engine)
        check(win.engine_var.get() == ENGINES["sensevoice"][0],
              "下拉框显示与实际引擎一致", win.engine_var.get())
        body = win.text.get("1.0", "end")
        check("✓" in body, "成功提示已写入提示区",
              body.strip().splitlines()[-1] if body.strip() else "")

        # 切回 vosk
        win.engine_var.set(ENGINES["vosk"][0])
        win._on_engine_pick()
        for _ in range(200):
            pump(win, rounds=1)
            if t.engine == "vosk" and win._engine_result.empty():
                break
            time.sleep(0.05)
        check(t.engine == "vosk", "成功切回 vosk", t.engine)
        check(win.engine_var.get() == ENGINES["vosk"][0],
              "下拉框显示与实际引擎一致", win.engine_var.get())
    else:
        print("  - 跳过成功切换验证（本机不具备 SenseVoice 运行条件）")

    # ---------- 失败路径：切到不可用的引擎 ----------
    # 故意把模型目录指向不存在的路径 → 构建必失败
    t.cfg.asr.sv_model_dir = "models/__不存在的模型目录__"
    win.engine_var.set(ENGINES["sensevoice"][0])
    win._on_engine_pick()
    for _ in range(60):
        pump(win, rounds=1)
        time.sleep(0.05)
        if "✗" in win.text.get("1.0", "end"):
            break
    body = win.text.get("1.0", "end")
    check("✗" in body, "失败原因已写入提示区")
    check(t.engine in ENGINES, "失败后引擎状态未损坏", t.engine)
    check(win.engine_var.get() == ENGINES[t.engine][0],
          "失败后下拉框与实际引擎一致", f"{win.engine_var.get()} vs {t.engine}")

    win.root.destroy()

    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print(f"\n==== 结论：{passed}/{total} 项通过 ====")
    for ok, name in _results:
        if not ok:
            print(f"  失败：{name}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
