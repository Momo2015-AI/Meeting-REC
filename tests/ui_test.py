"""UI 层验证：构建悬浮字幕窗、注入事件、核对控件真实内容与几何尺寸。

覆盖：新事件协议（final/trans 带句序号）、译文插入到对应原文句正下方。

运行：python tests/ui_test.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.transcriber import MeetingTranscriber  # noqa: E402
from src.ui import SubtitleWindow  # noqa: E402


def main() -> int:
    cfg = load_config(ROOT / "config.toml")
    cfg.translate.enabled = False
    t = MeetingTranscriber(cfg)

    win = SubtitleWindow(cfg, t)
    win.root.update()

    # 模拟真实时序：两条原文先到，译文后到且乱序返回
    t.events.put(("status", "running"))
    t.events.put(("final", "11:02:31", "The capacity is 261 kWh.", 1))
    t.events.put(("final", "11:02:44", "Site acceptance test in Ghana.", 2))
    t.events.put(("partial", "the battery energy storage system"))
    # 译文乱序到达：第 2 句先返回
    t.events.put(("trans", "11:02:44", "加纳现场验收测试。", 2))
    t.events.put(("trans", "11:02:31", "容量为 261 千瓦时。", 1))

    for _ in range(20):
        win.root.update()
        time.sleep(0.05)

    body = win.text.get("1.0", "end").strip()
    partial = win.partial_lbl.cget("text")
    status = win.status.cget("text")
    geo = win.root.winfo_geometry()
    on_top = bool(win.root.attributes("-topmost"))
    lines = [ln for ln in body.splitlines() if ln.strip()]

    print("==== UI 验证结果 ====")
    print(f"  窗口几何：{geo}（topmost={on_top}）")
    print(f"  状态标签：{status}")
    print(f"  中间结果：{partial}")
    print("  文本区内容：")
    for line in lines:
        print(f"    | {line}")

    # 译文必须紧跟其对应原文句（配对正确性）
    pair_ok = False
    if len(lines) >= 4:
        pair_ok = (
            "261 kWh" in lines[0] and "容量为" in lines[1]
            and "Site acceptance" in lines[2] and "加纳现场验收" in lines[3]
        )

    ok = ("261 kWh" in body and "容量为" in body and "采集中" in status
          and "battery energy storage" in partial and on_top
          and geo.startswith(str(cfg.ui.width)) and pair_ok)
    win.root.destroy()
    print(f"  配对检查：{'通过' if pair_ok else '未通过'}")
    print(f"  结论：{'通过 —— 字幕窗渲染、事件分发与译文配对正常' if ok else '未通过'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
