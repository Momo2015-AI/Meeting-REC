"""跨服务商后端回退测试。

覆盖场景：
  1. 主后端 /models 探测成功 → active_backend = primary
  2. 主后端 404（不实现 /models）→ 自动改用 chat 探测，仍判为就绪
  3. 主后端整体不可用 → 自动切到备用后端（豆包），active_backend = doubao
  4. 翻译时主后端 500 → 自动降级到备用后端并成功返回译文
  5. 翻译时主后端 403 → 降级到同后端的下一档模型
  6. 全部后端都失败 → 返回空串，不抛异常
  7. 总结时主后端失败 → 走备用后端出总结，summary_used_backend 正确
  8. 配置层：alt_backends 能从 TOML 解析出 base_url / 密钥文件
  9-13. 熔断自愈 / 预检假阳性 / 备用后端不拖累主后端
  14-16. 降级提示去重：多句只提示一次、恢复后可再提示、停用只提示一次
  17-19. 限流（HTTP 429）专项：限流是临时状态，不得熔断/停用后端，
         冷却到期自动放行自愈，全部后端限流也不得停用翻译
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import default_config_path, load_config  # noqa: E402
from src.translate import Backend, Translator  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{'✓' if ok else '✗'}] {name}" + (f"  ({detail})" if detail else ""))


class _Handler(BaseHTTPRequestHandler):
    """按路径返回预设响应的假后端。

    注意：routes 必须挂在"每个服务器各自的 Handler 子类"上。
    若挂在基类上（类属性），多个服务器会互相覆盖路由，导致用例串味。
    """
    routes: dict[str, tuple[int, dict]] = {}

    def log_message(self, *a):  # noqa: ANN002, ANN003
        pass

    def _read_body(self) -> dict:
        """读取请求体（必须读，否则连接被重置，客户端报 IncompleteRead）。"""
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        code, payload = self.routes.get("GET " + self.path, (404, {"error": "nope"}))
        self._send(code, payload)

    def do_POST(self):  # noqa: N802
        self._read_body()
        code, payload = self.routes.get("POST " + self.path, (404, {"error": "nope"}))
        self._send(code, payload)


def make_server(routes: dict[str, tuple[int, dict]]) -> tuple[HTTPServer, str]:
    """启动一个独立路由表的假后端。"""
    handler = type("_ScopedHandler", (_Handler,), {"routes": dict(routes)})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


def main() -> int:
    print("=== 跨服务商后端回退测试 ===\n")

    # 场景 1：主后端正常
    srv, url = make_server({
        "GET /v1/models": (200, {"data": [{"id": "m-a"}]}),
        "POST /v1/chat/completions": (200, {"choices": [{"message": {"content": "你好"}}]}),
    })
    tr = Translator(base_url=url, api_key="k", model="m-a")
    ok, msg = tr.preflight()
    check("场景1 主后端探测成功", ok and tr.active_backend == "primary", msg)

    # 场景 2：主后端不实现 /models（404）→ 改用 chat 探测
    srv2, url2 = make_server({
        "GET /v1/models": (404, {"error": "not found"}),
        "POST /v1/chat/completions": (200, {"choices": [{"message": {"content": "ok"}}]}),
    })
    tr2 = Translator(base_url=url2, api_key="k", model="m-a")
    ok2, msg2 = tr2.preflight()
    check("场景2 /models 404 时改用 chat 探测", ok2, msg2)

    # 场景 3：主后端挂掉 → 切到豆包备用后端
    srv3, url3 = make_server({
        "GET /v1/models": (200, {"data": [{"id": "m-doubao"}]}),
        "POST /v1/chat/completions": (200, {"choices": [{"message": {"content": "豆包译文"}}]}),
    })
    tr3 = Translator(
        base_url="http://127.0.0.1:1/v1", api_key="k", model="m-a",
        alt_backends=[Backend(name="doubao", base_url=url3, api_key="k2", model="m-doubao")],
    )
    ok3, msg3 = tr3.preflight()
    check("场景3 主后端不可用时切到备用后端",
          ok3 and tr3.active_backend == "doubao", msg3)

    # 场景 4：翻译时主后端不可用 → 降级到备用后端成功
    #   （主后端用"连接被拒"的地址，确保翻译请求确实失败）
    tr4 = Translator(
        base_url="http://127.0.0.1:1/v1", api_key="k", model="m-a",
        alt_backends=[Backend(name="doubao", base_url=url3, api_key="k2", model="m-doubao")],
    )
    tr4.enabled = True
    out4 = tr4.translate("hello", "en")
    check("场景4 翻译自动降级到备用后端", out4 == "豆包译文", f"译文={out4!r}")

    # 场景 5：主后端 403 → 同后端下一档模型
    srv5, url5 = make_server({
        "POST /v1/chat/completions": (403, {"error": {"message": "额度不足"}}),
    })
    tr5 = Translator(base_url=url5, api_key="k", model="m-a",
                     translate_models=["m-a", "m-b"])
    tr5.enabled = True
    out5 = tr5.translate("hello", "en")
    check("场景5 主后端失败时返回空串（无备用后端）", out5 == "", f"译文={out5!r}")

    # 场景 6：主后端 403，同后端第二档模型成功
    state = {"n": 0}

    class _H2(_Handler):
        def do_POST(self):  # noqa: N802
            body = self._read_body()
            model = body.get("model", "")
            if model == "m-a":
                self._send(403, {"error": {"message": "额度不足"}})
            else:
                self._send(200, {"choices": [{"message": {"content": "降级成功"}}]})

    srv6 = HTTPServer(("127.0.0.1", 0), _H2)
    threading.Thread(target=srv6.serve_forever, daemon=True).start()
    url6 = f"http://127.0.0.1:{srv6.server_address[1]}/v1"
    tr6 = Translator(base_url=url6, api_key="k", model="m-a",
                     translate_models=["m-a", "m-b"])
    tr6.enabled = True
    out6 = tr6.translate("hello", "en")
    check("场景6 同后端内降级到下一档模型", out6 == "降级成功", f"译文={out6!r}")

    # 场景 7：总结时主后端失败 → 走备用后端
    tr7 = Translator(
        base_url=url5, api_key="k", model="m-a",
        summary_models=["m-a"],
        alt_backends=[Backend(name="doubao", base_url=url3, api_key="k2", model="m-doubao")],
    )
    tr7.enabled = True
    md7 = tr7.summarize("会议转写内容", "总结提示词")
    check("场景7 总结走备用后端",
          md7 == "豆包译文" and tr7.summary_used_backend == "doubao",
          f"used={getattr(tr7, 'summary_used_backend', '?')}")

    # 场景 8：配置层 alt_backends 解析
    cfg = load_config(default_config_path(ROOT))
    alts = cfg.translate.alt_backends
    ok8 = bool(alts) and alts[0].base_url.startswith("https://")
    check("场景8 配置解析出备用后端", ok8,
          f"name={alts[0].name if alts else '?'} url={alts[0].base_url if alts else '?'}")

    # 场景 9：熔断——主后端连续失败后被跳过，不再重复调用
    calls = {"primary": 0, "alt": 0}

    class _H3(_Handler):
        def do_POST(self):  # noqa: N802
            body = self._read_body()
            model = body.get("model", "")
            if model == "m-a":
                calls["primary"] += 1
                self._send(403, {"error": {"message": "额度不足"}})
            else:
                calls["alt"] += 1
                self._send(200, {"choices": [{"message": {"content": "豆包译文"}}]})

    srv9 = HTTPServer(("127.0.0.1", 0), _H3)
    threading.Thread(target=srv9.serve_forever, daemon=True).start()
    url9 = f"http://127.0.0.1:{srv9.server_address[1]}/v1"
    tr9 = Translator(
        base_url=url9, api_key="k", model="m-a", translate_models=["m-a"],
        alt_backends=[Backend(name="doubao", base_url=url9, api_key="k2", model="m-doubao")],
    )
    tr9.enabled = True
    notes9: list[str] = []
    tr9.events_sink = notes9.append
    for _ in range(5):
        tr9.translate("hello", "en")
    # 熔断阈值 2：第 1、2 次失败后熔断；第 3 次是"立即放行探测"（额度可能刚好恢复）；
    # 第 4、5 次应完全跳过主后端 → 主后端共调用 3 次，备用后端 5 次。
    check("场景9 主后端熔断后被跳过",
          calls["primary"] == 3 and calls["alt"] == 5,
          f"primary调用={calls['primary']} alt调用={calls['alt']} 熔断={tr9._breaker}")

    # 场景 10：主后端恢复后自动切回
    class _H4(_Handler):
        state = {"ok": False}

        def do_POST(self):  # noqa: N802
            body = self._read_body()
            if body.get("model") == "m-a":
                if self.state["ok"]:
                    self._send(200, {"choices": [{"message": {"content": "主后端译文"}}]})
                else:
                    self._send(403, {"error": {"message": "额度不足"}})
            else:
                self._send(200, {"choices": [{"message": {"content": "豆包译文"}}]})

    srv10 = HTTPServer(("127.0.0.1", 0), _H4)
    threading.Thread(target=srv10.serve_forever, daemon=True).start()
    url10 = f"http://127.0.0.1:{srv10.server_address[1]}/v1"
    tr10 = Translator(
        base_url=url10, api_key="k", model="m-a", translate_models=["m-a"],
        alt_backends=[Backend(name="doubao", base_url=url10, api_key="k2", model="m-doubao")],
    )
    tr10.enabled = True
    tr10.events_sink = lambda m: None
    for _ in range(3):
        tr10.translate("hello", "en")
    _H4.state["ok"] = True            # 额度恢复
    tr10._last_probe = None           # 强制立即放行探测
    out10 = tr10.translate("hello again", "en")
    check("场景10 主后端恢复后自动切回",
          out10 == "主后端译文" and tr10._breaker.get("primary", 0) == 0,
          f"译文={out10!r} 熔断={tr10._breaker}")

    # 场景 11：预检假阳性 —— /models 返回全量目录（含未开通模型），
    #   但实际调用 404 ModelNotOpen → 必须判定为不可用。
    class _H5(_Handler):
        def do_POST(self):  # noqa: N802
            self._read_body()
            self._send(404, {"error": {"code": "ModelNotOpen",
                                       "message": "has not activated the model"}})

    srv11 = HTTPServer(("127.0.0.1", 0), _H5)
    threading.Thread(target=srv11.serve_forever, daemon=True).start()
    url11 = f"http://127.0.0.1:{srv11.server_address[1]}/v1"
    tr11 = Translator(
        base_url=url11, api_key="k", model="m-a",
        alt_backends=[Backend(name="doubao", base_url=url11, api_key="k2",
                              model="m-doubao")],
    )
    ok11, msg11 = tr11.preflight()
    check("场景11 /models 有目录但模型未开通 → 预检判为不可用",
          (not ok11) and "未开通" in msg11 or "ModelNotOpen" in msg11,
          msg11[:150])

    # 场景 12：备用后端持续失败（未开通）不得拖停健康的主后端
    class _H6(_Handler):
        def do_POST(self):  # noqa: N802
            body = self._read_body()
            if body.get("model") == "m-a":
                self._send(200, {"choices": [{"message": {"content": "主后端译文"}}]})
            else:
                # 备用后端 404（未开通模型）→ 每次都失败
                self._send(404, {"error": {"code": "ModelNotOpen",
                                           "message": "not activated"}})

    srv12 = HTTPServer(("127.0.0.1", 0), _H6)
    threading.Thread(target=srv12.serve_forever, daemon=True).start()
    url12 = f"http://127.0.0.1:{srv12.server_address[1]}/v1"
    tr12 = Translator(
        base_url=url12, api_key="k", model="m-a", translate_models=["m-a"],
        alt_backends=[Backend(name="doubao", base_url=url12, api_key="k2",
                              model="m-doubao")],
    )
    tr12.enabled = True
    tr12.events_sink = lambda m: None
    outs12 = [tr12.translate("hello", "en") for _ in range(6)]
    # 主后端健康 → 每句都应成功翻译，翻译功能不得被备用后端的 404 拖停
    check("场景12 备用后端持续404不影响主后端翻译",
          all(o == "主后端译文" for o in outs12) and tr12.enabled,
          f"译文={outs12[:2]} enabled={tr12.enabled} "
          f"全局_fail={tr12._fail} 备用_fail={tr12._backend_fail.get('doubao')}")

    # 场景 13：预检失败不得永久停用翻译 —— 运行期仍应真实重试并成功
    #   主后端在预检阶段返回 503（模拟"启动那一刻后端抖动"），
    #   之后恢复正常。预检判不通过，但翻译功能不得被停用。
    class _H7(_Handler):
        state = {"fail": True}

        def do_POST(self):  # noqa: N802
            body = self._read_body()
            if body.get("model") == "m-a":
                if self.state["fail"]:
                    self._send(503, {"error": {"message": "服务暂时不可用"}})
                else:
                    self._send(200, {"choices": [{"message": {"content": "恢复后的译文"}}]})
            else:
                self._send(404, {"error": {"code": "ModelNotOpen",
                                           "message": "not activated"}})

    srv13 = HTTPServer(("127.0.0.1", 0), _H7)
    threading.Thread(target=srv13.serve_forever, daemon=True).start()
    url13 = f"http://127.0.0.1:{srv13.server_address[1]}/v1"
    tr13 = Translator(
        base_url=url13, api_key="k", model="m-a", translate_models=["m-a"],
        alt_backends=[Backend(name="doubao", base_url=url13, api_key="k2",
                              model="m-doubao")],
    )
    ok13, msg13 = tr13.preflight()
    check("场景13 预检失败不永久停用翻译",
          (not ok13) and tr13.enabled,
          f"ok={ok13} enabled={tr13.enabled} msg={msg13[:80]}")
    _H7.state["fail"] = False          # 后端恢复
    out13 = tr13.translate("hello", "en")
    check("场景13b 预检失败后运行期仍能翻译成功",
          out13 == "恢复后的译文", f"译文={out13!r}")

    # ---------- 场景 14：降级提示只出现一次（不逐句刷屏）----------
    # 主模型持续失败 → 降级到同后端下一档。5 句翻译应只产生 1 条降级提示。
    class _H8(_Handler):
        def do_POST(self):  # noqa: N802
            body = self._read_body()
            if body.get("model") == "m-a":
                self._send(403, {"error": {"code": "quota", "message": "额度不足"}})
            else:
                self._send(200, {"choices": [{"message": {"content": "降级译文"}}]})

    srv14 = HTTPServer(("127.0.0.1", 0), _H8)
    threading.Thread(target=srv14.serve_forever, daemon=True).start()
    url14 = f"http://127.0.0.1:{srv14.server_address[1]}/v1"
    tr14 = Translator(base_url=url14, api_key="k", model="m-a",
                      translate_models=["m-a", "m-b"])
    tr14.enabled = True
    notes14: list[str] = []
    routes14: list[tuple] = []
    tr14.events_sink = notes14.append
    tr14.route_sink = lambda d, b, m: routes14.append((d, b, m))
    outs14 = [tr14.translate("hello", "en") for _ in range(5)]
    check("场景14a 降级后仍能正常翻译",
          all(o == "降级译文" for o in outs14), f"译文={outs14[:2]}")
    check("场景14b 5 句只提示 1 次（不逐句刷屏）",
          len(notes14) == 1, f"提示条数={len(notes14)} 内容={notes14}")
    check("场景14c 降级状态通过徽标回调暴露（不靠反复弹提示）",
          len(routes14) == 1 and routes14[0][0] is True
          and tr14.route_state == (True, "primary", "m-b"),
          f"routes={routes14} state={tr14.route_state}")

    # ---------- 场景 15：恢复首选模型后再降级，可再次提示 ----------
    class _H9(_Handler):
        state = {"ok": False}

        def do_POST(self):  # noqa: N802
            body = self._read_body()
            if body.get("model") == "m-a" and not self.state["ok"]:
                self._send(403, {"error": {"code": "quota", "message": "额度不足"}})
            else:
                self._send(200, {"choices": [{"message": {"content": "首选译文"}}]})

    srv15 = HTTPServer(("127.0.0.1", 0), _H9)
    threading.Thread(target=srv15.serve_forever, daemon=True).start()
    url15 = f"http://127.0.0.1:{srv15.server_address[1]}/v1"
    tr15 = Translator(base_url=url15, api_key="k", model="m-a",
                      translate_models=["m-a", "m-b"])
    tr15.enabled = True
    notes15: list[str] = []
    tr15.events_sink = notes15.append
    tr15.translate("hello", "en")            # 降级
    _H9.state["ok"] = True                    # 首选模型恢复
    tr15._breaker["primary"] = 0
    tr15.translate("hello again", "en")       # 回到首选
    n_after_recover = len(notes15)
    tr15.translate("third", "en")
    check("场景15a 恢复到首选时提示一次",
          any("恢复" in n for n in notes15), f"notes={notes15}")
    check("场景15b 恢复后不再重复提示",
          len(notes15) == n_after_recover, f"提示条数={len(notes15)}")

    # ---------- 场景 16：全部后端失败 → 停用提示只出现一次 ----------
    tr16 = Translator(base_url="http://127.0.0.1:1/v1", api_key="k", model="m-a",
                      translate_models=["m-a"])
    tr16.enabled = True
    notes16: list[str] = []
    statuses16: list[tuple] = []
    tr16.events_sink = notes16.append
    tr16.status_sink = lambda ok, reason: statuses16.append((ok, reason))
    for _ in range(6):
        tr16.translate("hello", "en")
    check("场景16 全部失败时停用提示只出现一次",
          len([n for n in notes16 if "停用" in n]) == 1,
          f"notes={notes16}")
    check("场景16b 停用状态通过回调暴露",
          len(statuses16) == 1 and statuses16[0][0] is False,
          f"statuses={statuses16}")

    # ---------- 场景 17：限流（HTTP 429）是临时状态，不得打掉备用后端 ----------
    # 真实事故：备用后端 agnes 未实名认证 → 账号级 1 req/min 限流。
    # 旧实现把 429 当硬失败 → 熔断 + 累计到阈值 → 翻译被永久停用，
    # 整场会议再也译不出来。修复后：429 只进冷却，不熔断、不停用。
    class _H17(_Handler):
        state = {"limited": True}
        calls = {"primary": 0, "alt": 0}

        def do_POST(self):  # noqa: N802
            body = self._read_body()
            model = body.get("model", "")
            if model == "m-a":
                _H17.calls["primary"] += 1
                if _H17.state["limited"]:
                    self._send(429, {"error": {"message": "Too Many Requests"}})
                else:
                    self._send(200, {"choices": [{"message": {"content": "主后端译文"}}]})
            else:
                _H17.calls["alt"] += 1
                self._send(200, {"choices": [{"message": {"content": "备用译文"}}]})

    srv17 = HTTPServer(("127.0.0.1", 0), _H17)
    threading.Thread(target=srv17.serve_forever, daemon=True).start()
    url17 = f"http://127.0.0.1:{srv17.server_address[1]}/v1"
    tr17 = Translator(base_url=url17, api_key="k", model="m-a",
                      translate_models=["m-a", "m-a2"],
                      alt_backends=[Backend(name="alt", base_url=url17, api_key="k2",
                                            model="m-alt")])
    tr17.enabled = True
    notes17: list[str] = []
    statuses17: list[tuple] = []
    tr17.events_sink = notes17.append
    tr17.status_sink = lambda ok, reason: statuses17.append((ok, reason))
    outs17 = [tr17.translate("hello", "en") for _ in range(5)]
    check("场景17a 限流时自动降级到备用后端并继续出译文",
          all(o == "备用译文" for o in outs17), f"译文={outs17[:3]}")
    check("场景17b 限流不熔断主后端（未进入 breaker 状态）",
          tr17._breaker.get("primary", 0) == 0,
          f"breaker={tr17._breaker}")
    check("场景17c 限流不累计停用计数（翻译未被停用）",
          tr17.enabled is True and tr17._fail == 0,
          f"enabled={tr17.enabled} fail={tr17._fail}")
    check("场景17d 限流不触发停用回调",
          len(statuses17) == 0, f"statuses={statuses17}")
    check("场景17e 限流提示只出现一次（不逐句刷屏）",
          len([n for n in notes17 if "限流" in n]) == 1,
          f"限流提示={len([n for n in notes17 if '限流' in n])} notes={notes17}")
    check("场景17f 限流后同后端不再重复调用（省一次往返）",
          _H17.calls["primary"] == 1,
          f"primary调用={_H17.calls['primary']}")

    # ---------- 场景 18：限流冷却到期自动放行，配额恢复即自愈 ----------
    tr18 = Translator(base_url=url17, api_key="k", model="m-a",
                      translate_models=["m-a"],
                      alt_backends=[Backend(name="alt", base_url=url17, api_key="k2",
                                            model="m-alt")])
    tr18.enabled = True
    notes18: list[str] = []
    tr18.events_sink = notes18.append
    tr18.translate("hello", "en")          # 触发限流 → 进入冷却
    check("场景18a 限流后进入冷却状态",
          tr18._in_rate_limit("primary") is True,
          f"rate_limit_until={tr18._rate_limit_until}")
    import time as _time
    # 模拟冷却期自然到期（把冷却截止时间拨到过去）
    tr18._rate_limit_until["primary"] = _time.monotonic() - 1.0
    _H17.state["limited"] = False          # 配额窗口重开
    out18 = tr18.translate("hello again", "en")
    check("场景18b 冷却到期自动放行，配额恢复后主后端回归",
          out18 == "主后端译文",
          f"译文={out18!r} 限流记录={tr18._rate_limit_until}")
    check("场景18c 恢复后清掉限流记录（下次可重新提示）",
          "primary" not in tr18._rate_limit_until,
          f"rate_limit_until={tr18._rate_limit_until}")

    # ---------- 场景 19：全部后端限流 → 不得停用翻译 ----------
    class _H19(_Handler):
        def do_POST(self):  # noqa: N802
            self._read_body()
            self._send(429, {"error": {"message": "Too Many Requests"}})

    srv19 = HTTPServer(("127.0.0.1", 0), _H19)
    threading.Thread(target=srv19.serve_forever, daemon=True).start()
    url19 = f"http://127.0.0.1:{srv19.server_address[1]}/v1"
    tr19 = Translator(base_url=url19, api_key="k", model="m-a",
                      translate_models=["m-a"])
    tr19.enabled = True
    notes19: list[str] = []
    statuses19: list[tuple] = []
    tr19.events_sink = notes19.append
    tr19.status_sink = lambda ok, reason: statuses19.append((ok, reason))
    for _ in range(8):
        tr19.translate("hello", "en")
    check("场景19a 全部后端限流时翻译不被停用",
          tr19.enabled is True, f"enabled={tr19.enabled}")
    check("场景19b 全部后端限流不触发停用回调",
          len(statuses19) == 0, f"statuses={statuses19}")
    check("场景19c 全部后端限流提示只出现一次",
          len([n for n in notes19 if "限流" in n]) == 1,
          f"限流提示={len([n for n in notes19 if '限流' in n])}")

    # ---------- 场景 20：HTTP 429 的两种含义必须区分 ----------
    # 同一个状态码，可能是"发得太快"（临时，等窗口重开即恢复），
    # 也可能是"额度用尽"（需人工充值，等窗口重开无效）。
    # 把额度耗尽误判为限流会导致对着一个已停用的模型无限重试。
    check("场景20a 429 + rpm 提示 → 判为限流（临时）",
          Translator._is_rate_limit("agnes-2.5-flash HTTP 429：请登录platform进行实名认证以获取更高rpm"),
          "应判为限流")
    check("场景20b 429 + SetLimitExceeded → 判为额度耗尽（硬失败）",
          not Translator._is_rate_limit(
              "deepseek HTTP 429：SetLimitExceeded: Your account has reached the "
              "set usage limit, and the model service has been paused"),
          "不应判为限流")
    check("场景20c 403 额度不足 → 不判为限流",
          not Translator._is_rate_limit("m-a HTTP 403：quota：额度不足"),
          "403 额度不足不应判为限流")
    check("场景20d 429 + 通用 rate limit → 判为限流",
          Translator._is_rate_limit("m-a HTTP 429：rate limit exceeded"),
          "应判为限流")

    for srv in (srv, srv2, srv3, srv5, srv6, srv9, srv10, srv11, srv12, srv13,
                srv14, srv15, srv17, srv19):
        srv.shutdown()

    n_ok = sum(1 for r in results if r[0] == PASS)
    print(f"\n=== 结果：{n_ok}/{len(results)} 通过 ===")
    if n_ok != len(results):
        print("失败项：")
        for st, name, detail in results:
            if st == FAIL:
                print(f"  - {name}  {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
