"""补生成会议总结：额度恢复后，为已有会话重新生成总结。

用法：
    python summarize.py                      # 列出所有缺总结的会话
    python summarize.py output/meeting_2026…  # 指定会话目录（补生成）
    python summarize.py --all                # 补生成所有缺总结的会话
    python summarize.py --session <dir> --model agnes-2.5-flash
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.transcriber import MeetingTranscriber  # noqa: E402


def find_sessions(cfg) -> list[Path]:
    out_dir = cfg.resolve(cfg.output.dir)
    if not out_dir.is_dir():
        return []
    return sorted((d for d in out_dir.iterdir()
                   if d.is_dir() and (d / "transcript.md").exists()),
                  key=lambda p: p.name)


def needs_summary(sess: Path) -> bool:
    """判断会话是否缺总结：没有总结段，或总结段是失败留痕。"""
    txt = (sess / "transcript.md").read_text(encoding="utf-8")
    if "## 会议总结" not in txt:
        return True
    return "总结生成失败" in txt.split("## 会议总结", 1)[1][:400]


def summarize_session(cfg, sess: Path, model: str | None = None) -> bool:
    t = MeetingTranscriber(cfg)
    t.translate_status = "手动补生成"
    t.transcript_path = (sess / "transcript.md").resolve()
    t.summary_path = (sess / "summary.md").resolve()
    t.session_dir = sess.resolve()

    # 统计原句数与译文数（用于 MD 尾部统计与提示）
    txt = t.transcript_path.read_text(encoding="utf-8")
    t.stats["finals"] = txt.count("- **[")
    t.stats["translated"] = txt.count("  - 译文：")

    if model:
        # 手动指定模型：放在回退链首位
        t.translator.summary_models = [model] + [
            m for m in t.translator.summary_models if m != model]

    # 失败留痕的会话：先把占位段移除，避免"已有总结则不覆盖"的判断挡住写入
    if "## 会议总结" in txt:
        head = txt.split("## 会议总结", 1)[0].rstrip()
        head = head.split("\n---\n\n- 结束时间", 1)[0].rstrip()
        tail = ""
        if "\n---\n\n- 结束时间" in txt:
            tail = "\n---\n\n" + txt.split("\n---\n\n- 结束时间", 1)[1]
            tail = "- 结束时间" + tail
        with open(t.transcript_path, "w", encoding="utf-8") as f:
            f.write(head + tail + "\n")

    ok = t.summarize()
    used = getattr(t.translator, "summary_used_model", "")
    err = t.stats.get("summary_error", "")
    print(f"[{'OK' if ok else 'FAIL'}] {sess.name}"
          + (f"  模型={used}" if ok else f"  原因={err[:120]}"))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="补生成会议总结")
    ap.add_argument("session", nargs="?", help="会话目录（默认列出待补会话）")
    ap.add_argument("--all", action="store_true", help="补生成所有缺总结的会话")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--model", default=None, help="指定模型（放在回退链首位）")
    args = ap.parse_args()

    cfg = load_config(args.config)

    if args.session:
        sess = Path(args.session)
        if not sess.is_dir():
            sess = cfg.resolve(cfg.output.dir) / args.session
        if not (sess / "transcript.md").exists():
            print(f"找不到会话：{sess}")
            return 1
        return 0 if summarize_session(cfg, sess, args.model) else 1

    sessions = find_sessions(cfg)
    todo = [s for s in sessions if needs_summary(s)]
    if not todo:
        print("所有会话都已有总结，无需补生成。")
        return 0
    print(f"共 {len(sessions)} 个会话，其中 {len(todo)} 个缺总结：")
    for s in todo:
        print(f"  - {s.name}")
    if not args.all:
        print("\n加 --all 批量补生成，或指定会话目录："
              f"python summarize.py {todo[0].name}")
        return 0

    print()
    n_ok = 0
    for s in todo:
        if summarize_session(cfg, s, args.model):
            n_ok += 1
    print(f"\n完成 {n_ok}/{len(todo)}")
    return 0 if n_ok == len(todo) else 1


if __name__ == "__main__":
    raise SystemExit(main())
