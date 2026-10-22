"""翻译后端：OpenAI 兼容协议（/chat/completions）。

可接入 Ollama（本地零密钥）、vLLM、LM Studio、DeepSeek/OpenAI、**火山方舟豆包**等
任何兼容服务。

设计要点：
- preflight()：启动时快速探测后端，不通则立即停用翻译，绝不拖慢识别主循环；
- translate()：单次调用带超时，失败即返回空串并累计失败次数，达到阈值自动停用；
- 多后端回退：主后端连续失败（额度不足/限流/渠道不可用）时，自动切到备用后端
  （例如豆包），跨服务商兜底，避免会议中途"哑火"。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

_LANG_NAME = {"zh": "中文", "en": "英文"}


class SummaryError(RuntimeError):
    """总结请求失败（携带后端返回的可读原因，如"额度不足"）。"""


@dataclass
class Backend:
    """一个可用的翻译/总结后端（OpenAI 兼容协议）。

    name 仅用于提示与日志，例如 "agnes"、"doubao"。
    """
    name: str = "primary"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    translate_models: list[str] = field(default_factory=list)
    summary_models: list[str] = field(default_factory=list)


class Translator:
    def __init__(self, base_url: str, api_key: str, model: str, target_lang: str = "zh",
                 timeout: float = 8.0, system_prompt: str = "", enabled: bool = True,
                 max_fail: int = 3, summary_models: Optional[list[str]] = None,
                 translate_models: Optional[list[str]] = None,
                 alt_backends: Optional[list[Backend]] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.target_lang = target_lang
        self.timeout = timeout
        self.system_prompt = system_prompt
        self.enabled = enabled
        self._fail = 0
        self._max_fail = max_fail
        # 按后端独立的失败计数：某个后端失败不该拖累其他后端的健康判断。
        # （旧实现用全局 _fail 累加，备用后端一直 404 会把主后端一起拖停。）
        self._backend_fail: dict[str, int] = {}
        self.disabled_reason = ""
        self.last_error = ""
        # 预检结果：False 只代表"启动时这一次探测没通"，不代表后端一定不可用。
        # 运行期仍会真实重试，连续失败达阈值才真正停用翻译。
        self.preflight_ok = True
        # 总结模型回退链：配置里显式给出的顺序即为优先级（首选 → 更便宜档位）。
        # 若未配置，则退回 translate.model。
        self.summary_models = [m for m in (summary_models or []) if m]
        if not self.summary_models and self.model:
            self.summary_models = [self.model]
        self.summary_used_model = ""
        self.summary_used_backend = ""
        # 翻译模型回退链：同上逻辑
        self.translate_models = [m for m in (translate_models or []) if m]
        if not self.translate_models and self.model:
            self.translate_models = [self.model]
        # 跨服务商备用后端（如豆包）。主后端整体失败时按顺序顶上。
        self.alt_backends = alt_backends or []
        self.active_backend = "primary"
        # 后端熔断：某后端连续失败到阈值后暂时跳过，不让每句都白等一个超时
        self._breaker: dict[str, int] = {}
        self._breaker_threshold = 2
        # 记录最近一次"切换到哪个备用后端"，避免同一降级逐句重复提示
        self._last_switch_note = ""
        # 主后端熔断后的自愈探测间隔（秒）
        self._recover_interval = 60.0
        self._last_probe: Optional[float] = None

    # ---------------- 后端清单 ----------------
    def _backend_chain(self) -> list[Backend]:
        """主后端 + 备用后端，按优先级排列。"""
        primary = Backend(
            name="primary",
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            translate_models=list(self.translate_models),
            summary_models=list(self.summary_models),
        )
        return [primary] + list(self.alt_backends)

    # ---------------- 启动预检 ----------------
    def preflight(self, timeout: float = 3.0) -> tuple[bool, str]:
        """探测后端可用性。返回 (ok, 说明)。

        依次探测主后端与各备用后端，任一可用即视为就绪。**全部**后端都会探测
        （不提前 return），这样用户能在启动提示里看到每个后端的真实状态，
        也便于判断"备用后端是否真的已开通"。

        预检失败**不永久停用**翻译：网络抖动/后端偶发慢都会造成假阴性，
        停用后整场会议都只有原文。因此这里只记录状态，真正的"停用"判定
        交给运行期（连续失败达阈值才停用），预检不可用时首句仍会真实尝试。
        """
        if not self.enabled:
            return False, "翻译已在配置中关闭"
        notes: list[str] = []
        first_ok = ""
        for be in self._backend_chain():
            ok, msg = self._probe(be, timeout)
            notes.append(f"{be.name}: {msg}")
            if ok and not first_ok:
                first_ok = be.name
        if first_ok:
            self.active_backend = first_ok
            self.preflight_ok = True
            return True, "；".join(notes)
        # 全部不可达 → 不停用翻译，仅提示；运行期会真实重试。
        # 理由：预检超时（3s）对慢响应后端偏紧，一次抖动不该毁掉整场会议。
        self.preflight_ok = False
        self.disabled_reason = notes[-1] if notes else "无可用后端"
        return False, ("预检未通过（" + "；".join(notes) +
                       "）；将在首句翻译时实际重试，若仍失败则降级为仅转写")

    def _probe(self, be: Backend, timeout: float) -> tuple[bool, str]:
        """探测单个后端：优先 GET /models，不支持则退回一次极小的 chat 调用。

        注意：某些服务（如火山方舟）的 GET /models 返回的是**全量模型目录**，
        包含大量当前账号并未开通的模型（例如 ModelNotOpen）。仅凭 /models 成功
        就判定"模型可用"会得到假阳性——预检通过但实际每句都 404。
        因此这里在 /models 成功后再补一次**真实的 chat 探测**（max_tokens=1，
        消耗可忽略），以实际可调用性为准。
        """
        if not be.base_url or not be.api_key:
            return False, "未配置 base_url 或密钥"
        url = f"{be.base_url.rstrip('/')}/models"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {be.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            data = json.loads(raw)
            models = [m.get("id", "") for m in data.get("data", [])]
            listed = bool(models) and be.model in models
            # 目录里有该模型 ≠ 该模型已开通/可调用 → 必须实际打一次
            ok, msg = self._probe_chat(be, timeout)
            if ok:
                if models and not listed:
                    return True, f"后端就绪（模型 {be.model} 未在目录中但可调用）"
                return True, f"后端就绪，模型 {be.model} 可用"
            if models and not listed:
                return False, f"模型 {be.model} 未在目录中，且实测调用失败：{msg}"
            return False, msg
        except urllib.error.HTTPError as e:
            # 部分兼容服务不实现 /models（404/405），此时改用 chat 探测
            if e.code in (404, 405, 501):
                return self._probe_chat(be, timeout)
            return False, f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            return self._probe_chat(be, timeout, fallback_err=f"{e.__class__.__name__}")

    def _probe_chat(self, be: Backend, timeout: float,
                    fallback_err: str = "") -> tuple[bool, str]:
        """用一次极小的 chat/completions 调用探测后端（不产生有效 token 消耗）。"""
        payload = {
            "model": be.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
        req = urllib.request.Request(
            f"{be.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {be.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                json.loads(resp.read().decode("utf-8"))
            return True, f"后端就绪，模型 {be.model} 可用"
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            return False, f"HTTP {e.code}{('：' + self._extract_api_msg(body)) if body else ''}"
        except Exception as e:  # noqa: BLE001
            return False, fallback_err or f"{e.__class__.__name__}"


    # ---------------- 总结 ----------------
    def summarize(self, transcript: str, system_prompt: str,
                  timeout: Optional[float] = None) -> str:
        """把整场转写交给 LLM 生成结构化 Markdown 总结。

        依次尝试「后端 → 模型」两级回退链（主后端各档 → 备用后端各档）：
        某档失败（余额不足/渠道不可用/超时）自动降级到下一档。
        全部失败则抛出最后一个错误，由调用方记录到事件流（不静默吞掉）。
        """
        if not self.enabled or not transcript.strip():
            return ""
        last_err: Optional[SummaryError] = None
        backends = self._backend_chain()      # 只构造一次，保证 index() 能定位
        for be in backends:
            chain = be.summary_models or [be.model]
            for idx, m in enumerate(chain):
                try:
                    out = self._summary_once(transcript, system_prompt, m,
                                             timeout=timeout, backend=be)
                except SummaryError as e:
                    last_err = e
                    nxt = chain[idx + 1] if idx + 1 < len(chain) else None
                    if nxt:
                        self.events_sink(f"总结模型 {m} 失败（{e}），降级到 {nxt}")
                        continue
                    # 本后端模型链走完 → 换下一个后端（跨服务商兜底）
                    nxt_be = self._next_backend(backends, be)
                    if nxt_be:
                        self.events_sink(f"总结模型 {m} 失败（{e}），切换到 {nxt_be.name} 后端")
                        break
                    raise
                if out.strip():
                    self.summary_used_model = m
                    self.summary_used_backend = be.name
                    self._fail = 0
                    return out
                last_err = SummaryError(f"{m} 返回空内容")
        raise last_err or SummaryError("无可用总结模型")

    def events_sink(self, msg: str) -> None:
        """回退过程中的提示。由调用方（transcriber）接到事件流。"""
        self._pending_note = msg

    def _summary_once(self, transcript: str, system_prompt: str, model: str,
                      timeout: Optional[float] = None,
                      backend: Optional[Backend] = None) -> str:
        be = backend or Backend(base_url=self.base_url, api_key=self.api_key)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",
                 "content": "以下是本次会议的逐句转写内容（英文原文 + 中文译文）：\n\n"
                            f"{transcript}\n\n请按要求输出结构化总结。"},
            ],
            "temperature": 0.3,
            "stream": False,
        }
        req = urllib.request.Request(
            f"{be.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {be.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return str(data["choices"][0]["message"]["content"]).strip()
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: BLE001
                pass
            self._fail += 1
            raise SummaryError(
                f"HTTP {e.code}：{self._extract_api_msg(body) or e.reason}") from e
        except Exception as e:  # noqa: BLE001
            self._fail += 1
            raise SummaryError(f"{e.__class__.__name__}：{e}") from e

    @staticmethod
    def _next_backend(chain: list[Backend], cur: Backend) -> Optional[Backend]:
        """取当前后端之后的下一个后端（跨服务商兜底）。"""
        for i, b in enumerate(chain):
            if b is cur:
                return chain[i + 1] if i + 1 < len(chain) else None
        return None


    @staticmethod
    def _extract_api_msg(body: str) -> str:
        """从错误 JSON 里抽出可读信息（额度不足、模型不可用等）。

        保留 error.code 前缀——像方舟的 "ModelNotOpen"（模型未开通）、
        "insufficient_user_quota"（额度不足）这类 code 直接对应可行动作，
        只显示 message 会丢失关键线索。
        """
        if not body:
            return ""
        try:
            data = json.loads(body)
            err = data.get("error")
            if isinstance(err, dict):
                code = str(err.get("code") or "").strip()
                msg = str(err.get("message") or "").strip()
                if code and msg:
                    return f"{code}：{msg}"[:300]
                return (code or msg)[:300]
            if isinstance(err, str):
                return err[:300]
        except Exception:  # noqa: BLE001
            pass
        return body[:300]

    # ---------------- 翻译 ----------------
    def translate(self, text: str, source_lang: str = "") -> str:
        """翻译单句。失败时按「后端 → 模型」两级回退链降级重试。"""
        if not self.enabled or not text or not text.strip():
            return ""
        src_name = _LANG_NAME.get(source_lang, source_lang or "原文")
        tgt_name = _LANG_NAME.get(self.target_lang, self.target_lang)
        backends = self._backend_chain()
        last_err = ""
        # 本次调用中是否有任何后端成功
        any_ok = False
        # 主后端熔断后，定期放行一次让它自愈（额度恢复后自动切回）
        self._maybe_recover_primary()
        for be in backends:
            # 熔断：该后端刚连续失败过，先跳过（避免每句都白等一个超时）
            if self._is_open(be.name):
                continue
            chain = be.translate_models or [be.model]
            for idx, m in enumerate(chain):
                out, err = self._translate_once(text, src_name, tgt_name, m, be)
                if out:
                    any_ok = True
                    self._fail = 0
                    # 只重置"正在用的这个后端"的熔断计数；
                    # 其他后端的失败计数保持不变（否则主后端熔断会被误清）
                    self._reset_breaker(be.name)
                    self._backend_fail[be.name] = 0
                    if be.name != "primary":
                        # 同一次降级只提示一次，不逐句刷屏
                        if self._last_switch_note != be.name:
                            self.events_sink(f"翻译已切换到 {be.name} 后端（{m}）")
                            self._last_switch_note = be.name
                    elif m != self.model:
                        self.events_sink(f"翻译模型降级到 {m}")
                    return out
                last_err = err
                # 失败按后端独立累计，不污染全局计数
                self._backend_fail[be.name] = self._backend_fail.get(be.name, 0) + 1
                self._trip_breaker(be.name)
                nxt = chain[idx + 1] if idx + 1 < len(chain) else None
                if nxt:
                    self.events_sink(f"翻译模型 {m} 失败（{last_err}），降级到 {nxt}")
                    continue
                nxt_be = self._next_backend(backends, be)
                if nxt_be:
                    self.events_sink(
                        f"翻译模型 {m} 失败（{last_err}），切换到 {nxt_be.name} 后端")
                    break
                break

        if any_ok:
            # 本句至少有一个后端成功过 → 不累计全局失败
            return ""

        # 所有后端都失败：连续失败达阈值才停用（避免单次抖动误伤）
        self._fail += 1
        if self._fail >= self._max_fail:
            self.enabled = False
            self.disabled_reason = last_err
        self.last_error = last_err
        return ""

    # ---------------- 后端熔断 ----------------
    def _trip_breaker(self, name: str) -> None:
        """记录某后端的一次失败。"""
        self._breaker[name] = self._breaker.get(name, 0) + 1

    def _reset_breaker(self, name: str) -> None:
        """该后端成功一次 → 清空失败计数。"""
        self._breaker[name] = 0

    def _is_open(self, name: str) -> bool:
        """熔断是否打开：同一后端连续失败达到阈值则暂时跳过。"""
        return self._breaker.get(name, 0) >= self._breaker_threshold

    def _maybe_recover_primary(self) -> None:
        """主后端熔断后，每隔一段时间放它"试一次"，成功后自动切回主后端。

        没有这一步的话，额度恢复后仍会一直用备用后端（浪费更贵的额度）。
        """
        if self._breaker.get("primary", 0) < self._breaker_threshold:
            return
        now = time.monotonic()
        # _last_probe 初值为 None → 首次熔断时立即放行一次探测（额度可能刚好恢复）；
        # 之后按 _recover_interval 周期放行，避免每句都打一次主后端。
        if self._last_probe is not None and now - self._last_probe < self._recover_interval:
            return
        self._last_probe = now
        # 放行窗口：把计数压到阈值-1，让本次请求能真正打到主后端；
        # 若这次仍失败，_trip_breaker 会把它重新推回阈值。
        self._breaker["primary"] = self._breaker_threshold - 1
        self.events_sink("正在重试主后端…")
    def _translate_once(self, text: str, src_name: str, tgt_name: str,
                        model: str, be: Backend) -> tuple[str, str]:
        """调用单个后端的单个模型。返回 (译文, 错误原因)。"""
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user",
                 "content": f"请将以下{src_name}会议发言翻译为{tgt_name}，只输出译文：\n{text}"},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        req = urllib.request.Request(
            f"{be.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {be.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out = str(data["choices"][0]["message"]["content"]).strip()
            if out:
                return out, ""
            return "", f"{model} 返回空内容"
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            return "", f"{model} HTTP {e.code}：{self._extract_api_msg(body) or e.reason}"
        except Exception as e:  # noqa: BLE001
            return "", f"{model} {e.__class__.__name__}：{e}"

