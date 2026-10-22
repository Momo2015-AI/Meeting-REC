"""采集生命周期与"点击才开始"行为回归测试。

覆盖本次改造的关键语义：
  1. 构造 transcriber / 打开字幕窗 **不创建会话、不打开音频设备**（点击前无副作用）
  2. config.ui.autostart 默认关闭
  3. 点击「开始」→ 创建会话、进入采集中、引导层隐藏
  4. running 属性在采集中为 True、停止后为 False（不是靠线程存活猜）
  5. 停止后设备被释放，可再次「开始」开启新一场
  6. 每场会议统计独立（第二场不会误触发总结）
  7. 字幕窗初始为「待开始」，按钮文案正确

运行：python tests/lifecycle_test.py
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.transcriber import MeetingTranscriber  # noqa: E402
from src.ui import SubtitleWindow  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""))


def blocking_source(on_pcm, on_error):
    """阻塞式假音源：一直阻塞直到 stop 后由 transcriber 终止。

    与真实 LoopbackCapture 语义一致（采集在独立线程里跑，stop 后退出）。
    """
    def worker():
        while True:
            time.sleep(0.05)
    threading.Thread(target=worker, daemon=True).start()
    # 阻塞直到被 transcriber 停止
    ev = threading.Event()
    threading.Thread(target=ev.wait, daemon=True).start()
    return None


def main() -> int:
    cfg = load_config(ROOT / "config.toml")
    cfg.translate.enabled = False          # 不触发真实 LLM
    cfg.summary.enabled = False            # 不触发真实总结
    cfg.audio.save_audio = False

    # ---------- 1. 配置：默认不自动开始 ----------
    check("config.ui.autostart 默认关闭", cfg.ui.autostart is False,
          f"autostart={cfg.ui.autostart}")

    # ---------- 2. 构造阶段无副作用 ----------
    t = MeetingTranscriber(cfg)
    check("构造后未创建会话目录", t.session_dir is None, str(t.session_dir))
    check("构造后 transcript_path 为 None", t.transcript_path is None)
    check("构造后未打开音频设备", t._capture is None)
    check("构造后 running 为 False", t.running is False)

    # ---------- 3. 字幕窗初始状态 ----------
    win = SubtitleWindow(cfg, t)
    win.root.update()
    check("字幕窗初始状态为「待开始」",
          "待开始" in win.status.cget("text"), win.status.cget("text"))
    check("开始按钮初始文案正确",
          "开始" in win.btn.cget("text"), win.btn.cget("text"))
    check("初始显示中央引导层", bool(win.start_overlay.winfo_ismapped()))

    # ---------- 4. 点击「开始」 ----------
    win._toggle()
    win.root.update()
    time.sleep(0.3)
    win.root.update()
    check("点击后创建了会话目录", t.session_dir is not None, str(t.session_dir))
    check("点击后 transcript.md 已生成",
          t.transcript_path is not None and t.transcript_path.exists())
    check("点击后 running 为 True", t.running is True)
    check("点击后状态显示「采集中」",
          "采集" in win.status.cget("text"), win.status.cget("text"))
    check("点击后按钮变为「停止」",
          "停止" in win.btn.cget("text"), win.btn.cget("text"))
    check("点击后中央引导层已隐藏",
          not bool(win.start_overlay.winfo_ismapped()))
    first_dir = t.session_dir

    # ---------- 5. 再次点击「停止」 ----------
    win._toggle()
    win.root.update()
    time.sleep(0.4)
    check("停止后 running 为 False", t.running is False)
    check("停止后状态显示「已停止」",
          "已停止" in win.status.cget("text"), win.status.cget("text"))
    check("停止后按钮回到「开始」",
          "开始" in win.btn.cget("text"), win.btn.cget("text"))
    check("停止后释放了采集器引用", t._capture is None)
    check("停止后 MD 写入结束时间尾块",
          "结束时间" in (t.transcript_path.read_text(encoding="utf-8")))

    # ---------- 6. 可以再次开始（新会话） ----------
    win._toggle()
    win.root.update()
    time.sleep(0.3)
    check("可再次开始采集", t.running is True)
    check("第二次开启产生新的会话目录",
          t.session_dir is not None and t.session_dir != first_dir,
          f"{first_dir} → {t.session_dir}")
    check("新会话统计归零", t.stats["finals"] == 0, str(t.stats))

    # ---------- 7. 空会话不触发总结 ----------
    win._toggle()
    win.root.update()
    time.sleep(0.3)
    check("空会话停止后不触发总结", t.stats.get("summarized") is False,
          str(t.stats.get("summarized")))

    # ---------- 8. 统计不跨会话累积 ----------
    t2 = MeetingTranscriber(cfg)
    t2._open_session()
    t2._started_at = datetime.now()
    t2._on_final("First sentence of session A.")
    t2.stats["translated"] += 1
    t2._write_translation(1, "[译文] First sentence of session A.")
    check("第一场统计到 1 句", t2.stats["finals"] == 1)
    dir_a = t2.session_dir
    content_a = t2.transcript_path.read_text(encoding="utf-8")
    t2._open_session()          # 开启第二场
    check("第二场统计重置为 0", t2.stats["finals"] == 0, str(t2.stats))
    check("同秒内开启第二场不覆盖上一场目录",
          t2.session_dir != dir_a, f"{dir_a} → {t2.session_dir}")
    check("上一场纪要内容未被截断",
          "First sentence of session A." in content_a
          and content_a == dir_a.joinpath("transcript.md").read_text(encoding="utf-8"))

    win.root.destroy()

    print()
    print(f"==== 结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过 ====")
    if FAIL:
        for f in FAIL:
            print(f"  [FAIL] {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
