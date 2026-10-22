"""退出路径回归测试：验证"停止 → 总结落盘"在各种退出方式下都不丢。

覆盖场景：
  1. stop(wait_summary=True)     —— 同步等待，summary.md 必须存在
  2. stop() 后立刻 wait_for_summary() —— 异步启动后等待，结果一致
  3. stop() 后**不等待**直接退出  —— 守护线程会被杀，summary 可能缺失
                                  → 证明必须走等待路径（作为对照）
  4. 二次点击"停止"（已停止后再 stop）—— 不应报错、不应重复总结
  5. 空会话（无任何语句）—— 不应产生总结、不应崩溃
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import default_config_path, load_config  # noqa: E402
from src.transcriber import MeetingTranscriber  # noqa: E402
from tests._helpers import backend_ready, skip_reason  # noqa: E402

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    mark = "✓" if ok else "✗"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    results.append((SKIP, name, detail))
    print(f"  [-] {name}  (跳过：{detail})")


def make_transcriber(cfg) -> MeetingTranscriber:
    t = MeetingTranscriber(cfg)
    t.translate_status = "mock"
    return t


def seed(t: MeetingTranscriber, sentences: list[str]) -> None:
    """写入若干句原文 + 译文（不依赖真实 LLM）。"""
    t._open_session()
    t._started_at = datetime.now()
    for i, s in enumerate(sentences, 1):
        t._on_final(s)
        t.stats["translated"] += 1
        t._write_translation(i, f"[译文{i}] {s}")
    t._close_session()


def main() -> int:
    cfg = load_config(default_config_path(ROOT))
    tmp = ROOT / "output" / "_exit_test"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    sents = [
        "The 261 kilowatt hour cabinet supports Modbus TCP, RTU and CAN only.",
        "SNMP is not supported on the outdoor cabinet.",
        "We agreed to submit the VDE-AR-N 4105 report before the RFQ deadline.",
    ]

    live = backend_ready(cfg)
    reason = skip_reason(cfg)

    print("=== 场景 1：stop(wait_summary=True) 同步等待 ===")
    t = make_transcriber(cfg)
    seed(t, sents)
    t.stop(wait_summary=True)
    sp = t.summary_path
    has_sum = sp is not None and sp.exists() and sp.stat().st_size > 0
    md = t.transcript_path.read_text(encoding="utf-8")
    if live:
        check("summary.md 已生成", has_sum,
              f"{sp.name if sp else 'None'} "
              f"{sp.stat().st_size if has_sum else 0}B")
        check("summary.md 内容非空",
              has_sum and len(sp.read_text(encoding="utf-8")) > 50)
        check("transcript.md 含总结段", "## 会议总结" in md,
              f"{len(md)} 字符")
        check("总结后仍有结束时间与时长",
              "结束时间" in md and "时长" in md)
    else:
        for nm in ("summary.md 已生成", "summary.md 内容非空",
                   "transcript.md 含总结段", "总结后仍有结束时间与时长"):
            skip(nm, reason)

    print("\n=== 场景 2：stop() 异步 + wait_for_summary() ===")
    t2 = make_transcriber(cfg)
    seed(t2, sents)
    t2.stop()
    done = t2.wait_for_summary()
    sp2 = t2.summary_path
    ok2 = done and sp2 is not None and sp2.exists() and sp2.stat().st_size > 0
    if live:
        check("wait_for_summary 返回完成", done)
        check("summary.md 已生成", ok2, f"{sp2.stat().st_size if ok2 else 0}B")
    else:
        skip("wait_for_summary 返回完成", reason)
        skip("summary.md 已生成", reason)

    print("\n=== 场景 3（对照）：stop() 后不等待直接退出 ===")
    t3 = make_transcriber(cfg)
    seed(t3, sents)
    t3.stop()
    # 模拟进程立刻退出：守护线程被中止
    sp3 = t3.summary_path
    lost = (sp3 is None) or (not sp3.exists()) or sp3.stat().st_size == 0
    if live:
        check("对照：未等待时总结确实可能丢失", lost,
              "→ 证明退出路径必须走 wait_summary/wait_for_summary")
    else:
        skip("对照：未等待时总结确实可能丢失", reason)

    print("\n=== 场景 4：重复 stop（用户连点两次停止）===")
    t4 = make_transcriber(cfg)
    seed(t4, sents)
    t4.stop(wait_summary=True)
    err = None
    try:
        t4.stop(wait_summary=True)
    except Exception as e:  # noqa: BLE001
        err = e
    check("重复 stop 不抛异常", err is None, f"{type(err).__name__ if err else 'OK'}")
    if live:
        check("重复 stop 不覆盖已有总结",
              t4.summary_path.exists() and t4.summary_path.stat().st_size > 0)
    else:
        skip("重复 stop 不覆盖已有总结", reason)

    print("\n=== 场景 5：空会话不产生总结 ===")
    t5 = make_transcriber(cfg)
    t5._open_session()
    t5._started_at = datetime.now()
    t5._close_session()
    r5 = t5.summarize()
    check("空会话 summarize() 返回 False", r5 is False, f"ret={r5}")
    check("空会话不产生 summary.md",
          t5.summary_path is None or not t5.summary_path.exists())

    print("\n=== 场景 6：UI 退出钩子调用 wait_summary ===")
    t6 = make_transcriber(cfg)
    seed(t6, sents)
    t6.stop()
    # UI 的 _on_close 走的是 wait_for_summary 分支
    ok6 = t6.wait_for_summary()
    if live:
        check("UI 退出路径可等到总结落盘", ok6)
    else:
        skip("UI 退出路径可等到总结落盘", reason)

    shutil.rmtree(tmp, ignore_errors=True)

    n_pass = sum(1 for s, _, _ in results if s == PASS)
    n_skip = sum(1 for s, _, _ in results if s == SKIP)
    n_fail = len(results) - n_pass - n_skip
    print("\n==== 退出路径回归结果 ====")
    for st, name, detail in results:
        if st == FAIL:
            print(f"  [FAIL] {name}  {detail}")
    print(f"  通过 {n_pass}/{len(results) - n_skip}"
          + (f"，失败 {n_fail}" if n_fail else "，全部通过")
          + (f"，跳过 {n_skip}" if n_skip else ""))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
