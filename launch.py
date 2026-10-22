"""以完全独立的进程启动会议监听应用。

与 `python run.py` 的区别：本脚本用 DETACHED_PROCESS 创建子进程，
子进程不依附当前 shell / 终端会话 —— 后台任务被回收时不会连带杀掉应用。

用法：
    python launch.py            # 前台启动（调试用，会随本进程退出）
    python launch.py --detach   # 独立进程启动（推荐用于挂机）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
RUN = ROOT / "run.py"
LOG = ROOT / "logs" / "launcher.log"


def main() -> int:
    detach = "--detach" in sys.argv
    if not detach:
        # 前台：直接跑，方便看日志
        return subprocess.call([PY, str(RUN)], cwd=str(ROOT))

    # 独立进程：DETACHED_PROCESS(0x8) | CREATE_NEW_PROCESS_GROUP(0x200)
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    # 日志重定向：GUI 子系统程序（pythonw）没有控制台，输出必须落到文件，
    # 否则出错时无从排查。用 python.exe + 重定向可保留完整启动日志。
    # 注意：不能 close_fds=True —— 传入的 stdout 句柄会被关闭，
    # 子进程写日志时直接失败退出（表现为"报 PID 但进程秒退"）。
    LOG.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [PY, str(RUN)],
        cwd=str(ROOT),
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=open(LOG, "ab", buffering=0),
        stderr=subprocess.STDOUT,
    )
    print(f"[launch] 已以独立进程启动，PID={proc.pid}")
    print(f"[launch] 日志：{LOG}")
    print(f"[launch] 工作目录：{ROOT}")
    print("[launch] 该进程不依附本终端，关闭终端/退出本脚本不影响其运行。")
    print("[launch] 停止方式：在字幕窗点「停止」；或 kill 该 PID。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
