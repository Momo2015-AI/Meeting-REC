"""新功能验证：英文识别→中文翻译、MD 文档结构、停止后总结、清屏。

覆盖需求：
1. 英→中语种配置生效（英文模型 + 目标语言 zh）
2. 所有记录转存为 MD（transcript.md 含头部元信息 + 逐句双语 + 总结）
3. 点击"停止"后对 MD 生成总结并写回
4. 清屏只清界面显示，不删 MD 内容

运行：python tests/feature_test.py
"""
from __future__ import annotations

import sys
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


def main() -> int:
    cfg = load_config(ROOT / "config.toml")

    # ---------- 1. 英→中配置 ----------
    check("识别模型为英文模型", cfg.asr.model_dir.endswith("en-us-0.15"), cfg.asr.model_dir)
    check("识别语言为 en", cfg.asr.source_lang == "en", cfg.asr.source_lang)
    check("翻译目标语言为 zh", cfg.translate.target_lang == "zh", cfg.translate.target_lang)
    check("总结功能已启用", cfg.summary.enabled is True)
    check("英文模型目录存在", (ROOT / cfg.asr.model_dir).is_dir(), cfg.asr.model_dir)

    t = MeetingTranscriber(cfg)
    ok, msg = t.translator.preflight()
    check("翻译后端预检通过", ok, msg)
    t.translate_status = msg

    # ---------- 2. MD 文档结构 ----------
    t._open_session()
    t._started_at = datetime.now()
    md_path = t.transcript_path
    check("生成 transcript.md", md_path is not None and md_path.exists(), str(md_path))
    check("生成 summary.md 路径", t.summary_path is not None)

    # 模拟英文会议：英文原文 + 中文译文
    en_sentences = [
        "The EVE 261 kilowatt hour outdoor cabinet only supports Modbus TCP, RTU and CAN protocols, SNMP is not supported.",
        "Today's meeting focuses on the Ghana 100 megawatt solar plus energy storage bidding strategy.",
        "VDE-AR-N 4105 is the German grid connection standard and VDE-AR-E 2510-2 is the storage safety standard.",
        "We need to confirm the interface solution on the gateway side before the RFQ clarification deadline.",
    ]
    zh_translations = [
        "EVE 261 千瓦时户外柜仅支持 Modbus TCP、RTU 和 CAN 协议，不支持 SNMP。",
        "今天的会议重点是加纳 100 兆瓦光伏加储能项目的投标策略。",
        "VDE-AR-N 4105 是德国并网标准，VDE-AR-E 2510-2 是储能安全标准。",
        "我们需要在 RFQ 澄清截止日期前确认网关侧的接口方案。",
    ]
    for en in en_sentences:
        t._on_final(en)
    # 直接回填译文（模拟翻译线程），走真实写回逻辑
    for seq, zh in enumerate(zh_translations, start=1):
        t.stats["translated"] += 1
        t._write_translation(seq, zh)
        t.events.put(("trans", "12:00:00", zh, seq))

    t._close_session()
    md_text = md_path.read_text(encoding="utf-8")
    check("MD 含头部元信息", "## 逐句转写" in md_text and "识别模型" in md_text)
    check("MD 含全部英文原文", all(en[:25] in md_text for en in en_sentences))
    check("MD 含全部中文译文", all(zh in md_text for zh in zh_translations))
    check("MD 原文译文成对相邻",
          md_text.count("  - 译文：") == len(zh_translations)
          and md_text.count("- **[") == len(en_sentences),
          f"译文行数={md_text.count('  - 译文：')} 原文行数={md_text.count('- **[')}")
    check("MD 记录录音文件", "meeting.wav" in md_text)

    # ---------- 3. 停止后生成总结 ----------
    t2 = MeetingTranscriber(cfg)
    t2.translator.enabled = True
    t2._open_session()
    t2._started_at = datetime.now()
    t2._on_final("We agreed that the 261 kilowatt hour cabinet will be certified for the German market next quarter.")
    t2._on_final("Action item: the certification team will submit the VDE-AR-N 4105 test report by the end of March.")
    t2._close_session()

    ok_sum = t2.summarize()
    summary_md = ""
    for ev in list(t2.events.queue):
        if ev[0] == "summary":
            summary_md = ev[1]
    check("总结生成成功", ok_sum and bool(summary_md), f"{len(summary_md)} 字符")
    if summary_md:
        for sec in ("会议概要", "核心要点", "决议事项", "待办事项", "风险与关注点"):
            check(f"总结含章节「{sec}」", sec in summary_md)
        check("总结保留型号与标准号",
              "261" in summary_md or "VDE-AR-N 4105" in summary_md)
        md2 = t2.transcript_path.read_text(encoding="utf-8")
        check("总结写回 transcript.md", "## 会议总结" in md2 and summary_md[:40] in md2)
        check("单独生成 summary.md",
              t2.summary_path.exists() and "会议总结" in t2.summary_path.read_text(encoding="utf-8"))

    # ---------- 4. 清屏只清界面 ----------
    cfg2 = load_config(ROOT / "config.toml")
    cfg2.translate.enabled = False
    t3 = MeetingTranscriber(cfg2)
    # 显式开启会话（不再依赖 autostart：现在必须显式 start 才会创建会话/落盘）
    t3._open_session()
    t3._started_at = datetime.now()
    win = SubtitleWindow(cfg2, t3)
    win.root.update()

    t3.events.put(("status", "running"))
    t3.events.put(("final", "12:00:00", "The EVE 261 kWh cabinet.", 1))
    t3.events.put(("trans", "12:00:00", "EVE 261 千瓦时柜体。", 1))
    for _ in range(20):
        win.root.update()
        time.sleep(0.05)

    before = win.text.get("1.0", "end")
    md_before = t3.transcript_path.read_text(encoding="utf-8") if t3.transcript_path.exists() else ""
    check("清屏前界面有内容", "261" in before, before.strip().replace("\n", " | ")[:60])

    win._clear_screen()
    win.root.update()
    after = win.text.get("1.0", "end").strip()
    check("清屏后界面为空", after == "", repr(after))
    check("清屏后 partial 标签已清", win.partial_lbl.cget("text") == "")

    # 清屏后新内容仍能正常显示
    t3.events.put(("final", "12:00:30", "Second sentence after clear.", 2))
    for _ in range(10):
        win.root.update()
        time.sleep(0.05)
    after2 = win.text.get("1.0", "end")
    check("清屏后可继续显示新内容", "Second sentence" in after2)

    # MD 文件不受清屏影响
    md_after = t3.transcript_path.read_text(encoding="utf-8") if t3.transcript_path.exists() else ""
    check("清屏不删除 MD 内容",
          md_after == md_before and len(md_after) > 0,
          f"清屏前 {len(md_before)} 字符 / 清屏后 {len(md_after)} 字符（完全一致）")

    # 清屏之后产生的新记录，依然要写入 MD（"清屏不清文字"的完整含义）
    t3._on_final("A new sentence recorded after the screen was cleared.")
    t3._write_translation(2, "清屏之后新记录的一句话。")
    md_final = t3.transcript_path.read_text(encoding="utf-8")
    check("清屏后的新记录仍写入 MD",
          "A new sentence recorded after the screen was cleared." in md_final
          and "清屏之后新记录的一句话。" in md_final)
    check("MD 记录数随清屏继续累积",
          md_final.count("- **[") == md_before.count("- **[") + 1,
          f"{md_before.count('- **[')} → {md_final.count('- **[')}")

    win.root.destroy()

    print(f"\n==== 结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过 ====")
    if FAIL:
        print("  失败项：" + "; ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
