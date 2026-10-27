"""降级徽标端到端验证：模拟主模型持续失败 → 检查 UI 顶部徽标是否只更新一次、
提示是否只出现一次、控制台 route 事件是否正确。

不依赖真实音频设备（pcm_source 注入），不依赖真实 LLM（本地 mock HTTP 服务）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import default_config_path, load_config  # noqa: E402
from src.transcriber import MeetingTranscriber  # noqa: E402
from src.ui import SubtitleWindow  # noqa: E402

FAIL = {"n": 0}


class Handler(BaseHTTPRequestHandler):
    """按请求里的 model 字段决定成败：m-bad 恒定 403，m-good 恒定 200。"""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        model = body.get("model", "")
        if model == "m-bad":
            payload = json.dumps(
                {"error": {"message": "quota：额度不足"}}
            ).encode()
            self.send_response(403)
        else:
            payload = json.dumps(
                {"choices": [{"message": {"content": "备用模型译文"}}]}
            ).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def main() -> int:
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    cfg = load_config(default_config_path(ROOT))
    cfg.translate.enabled = True
    cfg.translate.base_url = f"http://127.0.0.1:{port}/v1"
    cfg.translate.api_key = "k"
    # 主模型不可用 → 触发模型降级；备用后端可用
    cfg.translate.model = "m-bad"
    cfg.translate.fallback_models = ["m-bad", "m-good"]
    cfg.translate.alt_backends = []
    cfg.asr.engine = "vosk"

    tr = MeetingTranscriber(cfg, pcm_source=lambda on_pcm, on_error: None)
    win = SubtitleWindow(cfg, tr)

    # 把 translator 的后端接到 mock（主模型路径先失败，再走 fallback）
    tr.translator.base_url = f"http://127.0.0.1:{port}/v1"
    tr.translator.model = "m-bad"
    tr.translator.translate_models = ["m-bad", "m-good"]

    notes: list[str] = []
    routes: list[tuple] = []
    statuses: list[tuple] = []

    def pump(seconds: float = 3.0):
        """抽干事件流，分类收集。"""
        end = time.time() + seconds
        while time.time() < end:
            try:
                ev = tr.events.get(timeout=0.05)
            except Exception:
                continue
            if ev[0] == "note":
                notes.append(ev[1])
            elif ev[0] == "route":
                routes.append(ev[1:])
            elif ev[0] == "trstatus":
                statuses.append(ev[1:])

    # 连续翻译 8 句（主模型必失败 → 应稳定落到 m-good）
    for i in range(8):
        tr.translator.translate(f"测试句子 {i}", "zh")
        pump(0.05)

    pump(1.0)
    # 事件流里可能混着 note/route；UI 徽标需要 UI 侧消费，这里直接断言 translator 状态
    print("=== 提示条数 ===")
    print(f"  提示条数={len(notes)}")
    for n in notes:
        print(f"    note: {n}")
    print("=== 路线变化 ===")
    print(f"  routes={routes}")
    print(f"  route_state={tr.translator.route_state}")

    ok = True
    # 1) 同一降级原因只提示一次
    if len(notes) != 1:
        print(f"[FAIL] 期望提示 1 条，实际 {len(notes)} 条 —— 仍在刷屏")
        ok = False
    else:
        print("[PASS] 8 句翻译只产生 1 条降级提示（不刷屏）")
    # 2) 降级状态通过 route 事件暴露
    if not routes or not routes[-1][0]:
        print(f"[FAIL] 未暴露降级状态：{routes}")
        ok = False
    else:
        print(f"[PASS] 降级状态通过 route 事件暴露：{routes[-1]}")
    # 3) route_state 一致
    st = tr.translator.route_state
    if not st or not st[0]:
        print(f"[FAIL] route_state 未标记降级：{st}")
        ok = False
    else:
        print(f"[PASS] route_state 标记为降级：{st}")

    # 4) UI 徽标：构造一次 route 事件，看徽标文本是否更新
    try:
        tr.events.put(("route", True, "primary", "m-good"))
        win.root.update_idletasks()
        win._poll()
        win.root.update_idletasks()
        lbl = win.degrade_lbl.cget("text")
        if "降级" in str(lbl):
            print(f"[PASS] UI 降级徽标显示：{lbl!r}")
        else:
            print(f"[FAIL] UI 降级徽标未显示：{lbl!r}")
            ok = False
        # 恢复后徽标应清空
        tr.events.put(("route", False, "primary", "m-bad"))
        win.root.update_idletasks()
        win._poll()
        win.root.update_idletasks()
        lbl2 = win.degrade_lbl.cget("text")
        if not str(lbl2):
            print("[PASS] 恢复首选路线后 UI 徽标已清空")
        else:
            print(f"[FAIL] 恢复后徽标未清空：{lbl2!r}")
            ok = False
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] UI 徽标验证异常：{type(e).__name__}: {e}")
        ok = False

    srv.shutdown()
    try:
        win.root.destroy()
    except Exception:
        pass
    print("\n==== 结果：" + ("全部通过 ====" if ok else "存在失败 ===="))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
