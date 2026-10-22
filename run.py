"""meeting-listener 主入口：会议实时监听与翻译（本地管线）。

用法：
    python run.py                      # 悬浮字幕窗模式
    python run.py --no-ui              # 无界面模式（控制台输出）
    python run.py --no-ui --duration 60
    python run.py --list-devices       # 列出输出/环录设备
"""
from __future__ import annotations

import argparse
import io
import os
import queue as queue_mod
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _ensure_stdout() -> None:
    """保证 print() 不会因缺少控制台而崩溃。

    pythonw.exe 是 GUI 子系统程序，没有 stdout/stderr；此时 print() 会抛
    AttributeError 导致进程立刻退出（表现为"启动了但马上消失"）。
    这里把标准流替换为 devnull 句柄，使无控制台启动也能正常工作。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:  # noqa: BLE001
                setattr(sys, name, io.StringIO())


_ensure_stdout()

from src.config import load_config  # noqa: E402
from src.transcriber import MeetingTranscriber  # noqa: E402


def _resolve_config_path(explicit: str) -> Path:
    """选择要加载的配置文件。

    优先级：命令行显式指定 > 本机私有配置 config.local.toml > 通用模板 config.toml。
    config.local.toml 用于存放本机专有的端点/模型/密钥路径，不进入版本库。
    """
    if explicit:
        return Path(explicit).resolve()
    root = Path(__file__).resolve().parent
    local = root / "config.local.toml"
    if local.is_file():
        return local
    return root / "config.toml"


def main() -> int:
    ap = argparse.ArgumentParser(description="会议实时监听与翻译（本地管线）")
    ap.add_argument("--config", default="", help="配置文件路径（默认自动选择 config.local.toml > config.toml）")
    ap.add_argument("--no-ui", action="store_true", help="无界面模式（控制台输出）")
    ap.add_argument("--duration", type=float, default=0, help="运行秒数，0=持续直到 Ctrl+C")
    ap.add_argument("--list-devices", action="store_true", help="列出音频输出与环录设备")
    args = ap.parse_args()

    if args.list_devices:
        from src.capture import list_devices

        info = list_devices()
        print("== 输出设备（环录来源） ==")
        for name, _id in info["speakers"]:
            print(f"  [输出] {name}")
        print("== Loopback 环录设备 ==")
        for name, _id in info["loopbacks"]:
            print(f"  [环录] {name}")
        return 0

    cfg = load_config(_resolve_config_path(args.config))
    print(f"[init] 配置文件：{_resolve_config_path(args.config).name}")
    print(f"[init] 识别模型：{cfg.asr.model_dir}")
    print(f"[init] 语种方向：{cfg.asr.source_lang.upper()} → {cfg.translate.target_lang.upper()}")
    print(f"[init] 翻译后端：{cfg.translate.backend} {cfg.translate.base_url} "
          f"model={cfg.translate.model} (enabled={cfg.translate.enabled})")
    print(f"[init] 停止后自动总结：{'开启' if cfg.summary.enabled else '关闭'}")
    print(f"[init] 采集方式：手动点击「开始」后才监听系统音频")
    t = MeetingTranscriber(cfg)

    # 翻译后端预检：放到后台线程，避免拖慢窗口弹出。
    # 超时给 8s（默认 3s 对慢响应后端偏紧）；即便预检失败也不会永久停用翻译，
    # 运行期首句仍会真实重试（见 Translator.preflight 注释）。
    def _preflight() -> None:
        ok, msg = t.translator.preflight(timeout=8.0)
        t.translate_status = msg
        t.events.put(("preflight", ok, msg))
        print(f"[init] 翻译预检：{msg}")
        if not ok:
            print("[warn] 翻译预检未通过，将在首句翻译时实际重试（识别不受影响）")
        else:
            if "ModelNotOpen" in msg:
                print("[hint] 火山方舟部分模型尚未开通："
                      "打开 https://console.volcengine.com/ark → 开通管理 → 开通模型"
                      "（开通即送免费额度），然后运行 python scan_ark_all.py")
            print(f"[init] 当前生效后端：{t.translator.active_backend}")

    threading.Thread(target=_preflight, name="preflight", daemon=True).start()

    if args.no_ui:
        t.start()
        print("[run] 无界面模式已启动，Ctrl+C 停止；输出目录："
              f"{t.session_dir}")
        try:
            deadline = time.time() + args.duration if args.duration > 0 else None
            while True:
                _drain_console(t)
                if deadline is not None and time.time() > deadline:
                    break
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n[run] 收到中断，正在收尾……")
        finally:
            if cfg.summary.enabled and t.translator.enabled and t.running:
                print("\n[run] 正在生成会议总结（读取 MD → LLM → 写回）……")
            t.stop(wait_summary=True)
            if cfg.summary.enabled and t.translator.enabled:
                print(f"[run] 总结已写入：{t.transcript_path}")
        return 0

    from src.ui import SubtitleWindow

    SubtitleWindow(cfg, t).run()
    return 0


def _drain_console(t: MeetingTranscriber) -> None:
    try:
        while True:
            ev = t.events.get_nowait()
            if ev[0] == "final":
                print(f"[{ev[1]}] {ev[2]}")
            elif ev[0] == "trans":
                print(f"         → {ev[2]}")
            elif ev[0] == "partial":
                print(f"\r… {ev[1][:70]:<70}", end="", flush=True)
            elif ev[0] == "summary":
                print("\n===== 会议总结 =====")
                print(ev[1])
                print("====================")
            elif ev[0] == "error":
                print(f"\n[error] {ev[1]}")
            elif ev[0] == "note":
                print(f"\n[note] {ev[1]}")
    except queue_mod.Empty:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
