"""翻译后端：OpenAI 兼容协议（/chat/completions）。

可接入 Ollama（本地零密钥）、vLLM、LM Studio、DeepSeek/OpenAI，以及任何
兼容 OpenAI 协议的商业推理服务。

设计要点：
- preflight()：启动时快速探测后端，不通则立即停用翻译，绝不拖慢识别主循环；
- translate()：单次调用带超时，失败即返回空串并累计失败次数，达到阈值自动停用；
- 多后端回退：主后端连续失败（额度不足/限流/渠道不可用）时，自动切到备用后端，
  跨服务商兜底，避免会议中途"哑火"。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

_LANG_NAME = {"zh": "中文", "en": "英文"}

# "1. 译文" / "12、译文" / "3：译文" 等编号行
_NUM_LINE_RE = re.compile(r"^\s*(\d{1,2})\s*[.、．:：)）\]]\s*(.+)$")

# 限流（HTTP 429）特征。
# 限流与"额度耗尽 / 模型不可用"性质不同：它是"发得太快"的**临时**状态，
# 等一会儿配额窗口就会重开。因此限流既不该计入后端熔断计数（否则一次
# 抖动就把后端长期拉黑），也不该计入"停用翻译"的失败计数（否则一场
# 会议就永久失去翻译了）。
#
# 但 HTTP 429 并不都是限流——同一个状态码可能表示"额度已用尽"，
# 那种情况需要人工充值/提额，等窗口重开并不会恢复，必须区分开：
#   · Agnes    ：请登录platform进行实名认证以获取更高rpm        → RPM 限流（临时）
#   · 火山方舟 ：SetLimitExceeded / usage limit / 额度用尽       → 额度耗尽（需人工处理）
#
# 状态码由本项目的错误格式化统一写成 "HTTP 429：..."，因此以它为准。
_RATE_LIMIT_PATTERNS = (
    "http 429",
    "too many requests",
    "rate limit",
    "ratelimit",
    "请求过于频繁",
    "触发限流",
    "达到频率上限",
    "获取更高rpm",
    "每分钟",
)

# 额度耗尽特征：命中这些说明"等窗口重开"没用，必须当成硬失败处理，
# 否则会对着一个已停用的模型无限重试，白白拖慢每一句翻译。
_QUOTA_EXHAUSTED_PATTERNS = (
    "setlimitexceeded",
    "usage limit",
    "quota",
    "额度不足",
    "额度耗尽",
    "余额不足",
    "insufficient",
    "billing",
    "account.*paused",
    "模型服务已暂停",
)


class SummaryError(RuntimeError):
    """总结请求失败（携带后端返回的可读原因，如"额度不足"）。"""


@dataclass
class Backend:
    """一个可用的翻译/总结后端（OpenAI 兼容协议）。

    name 仅用于提示与日志，例如 "primary"、"backup"。
    """
    name: str = "primary"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    translate_models: list[str] = field(default_factory=list)
    summary_models: list[str] = field(default_factory=list)
    extra_body: dict = field(default_factory=dict)   # 透传到请求体（如 {"thinking":{"type":"disabled"}}）


class Translator:
    def __init__(self, base_url: str, api_key: str, model: str, target_lang: str = "zh",
                 timeout: float = 8.0, system_prompt: str = "", enabled: bool = True,
                 max_fail: int = 3, summary_models: Optional[list[str]] = None,
                 translate_models: Optional[list[str]] = None,
                 alt_backends: Optional[list[Backend]] = None,
                 extra_body: Optional[dict] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.target_lang = target_lang
        self.timeout = timeout
        self.system_prompt = system_prompt
        self.enabled = enabled
        self.extra_body = dict(extra_body or {})
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
        # 主后端熔断后的自愈探测间隔（秒）
        self._recover_interval = 60.0
        self._last_probe: Optional[float] = None
        # ---------------- 限流（HTTP 429）冷却 ----------------
        # 限流是"发得太快"的临时状态，配额窗口重开即可恢复。它与"额度耗尽"
        # 必须区别对待：
        #   - 限流**不**计入 _fail / _backend_fail，因此不会把后端熔断、
        #     也不会累计到阈值把整个翻译永久停用；
        #   - 限流后该后端进入冷却（默认 60s），冷却期内直接跳过，
        #     避免每一句都去撞一次 429 白等一个往返；
        #   - 冷却期结束后自动放行重试，配额恢复即自愈。
        self._rate_limit_until: dict[str, float] = {}
        self._rate_limit_cooldown = 60.0
        # 限流提示同样只发一次（key 前缀 rate_limit:）
        self._rate_limited: set[str] = set()

        # ---------------- 提示去重 ----------------
        # 降级/失败提示改为「状态变化事件」：同一类问题每场只提示一次，
        # 而不是每句翻译都弹一条（主模型挂掉时那会刷屏到无法阅读）。
        # key 格式统一为 "<kind>:<backend>:<model>"，便于按后端批量清理。
        self._noted: set[str] = set()
        # 当前生效路线 (后端, 模型)；变化时才提示并刷新状态
        self._route: Optional[tuple[str, str]] = None
        # 对外暴露的路线状态：(是否降级, 后端名, 模型名)，供 UI 徽标显示
        self.route_state: Optional[tuple[bool, str, str]] = None
        # 路线变化回调 (degraded, backend, model)：由 transcriber 接到事件流，
        # 让 UI 用「常驻徽标」表达当前降级状态，而不是靠反复弹提示。
        self.route_sink: Optional[Callable[[bool, str, str], None]] = None
        # 翻译可用性变化回调 (可用?, 原因)：停用/恢复时更新 UI 徽标
        self.status_sink: Optional[Callable[[bool, str], None]] = None

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
            extra_body=dict(getattr(self, "extra_body", None) or {}),
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
        payload.update(be.extra_body)   # 推理模型：预检同样走关思考，探测更快且不吃思考配额
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
    _CHUNK_PROMPT = (
        "你是会议纪要助手。对以下会议转录片段生成简要要点小结："
        "用无序列表，每条一句话，必须保留型号、标准编号、数字、决议与责任方；"
        "不要寒暄，不要概括成大段文字，不得臆造转写中没有的信息。"
    )

    def summarize(self, transcript: str, system_prompt: str,
                  timeout: Optional[float] = None,
                  chunk_chars: int = 12000) -> str:
        """把整场转写交给 LLM 生成结构化 Markdown 总结。

        转写超过 chunk_chars 时走两段式：先逐块小结（各自走完整回退链），
        再把全部小结归并生成最终纪要——长会议不再被截断丢掉后半场。

        简短转写保持单次调用。任一块失败走「后端 → 模型」回退链后仍失败
        则抛 SummaryError，由调用方记录（不静默吞掉）。
        """
        if not self.enabled or not transcript.strip():
            return ""
        if chunk_chars <= 0 or len(transcript) <= chunk_chars:
            return self._summary_with_chain(transcript, system_prompt, timeout)

        parts = self._split_transcript(transcript, chunk_chars)
        self.events_sink(f"转写较长（{len(transcript)} 字符），分 {len(parts)} 块小结后归并总结")
        notes: list[str] = []
        for i, part in enumerate(parts, 1):
            note = self._summary_with_chain(part, self._CHUNK_PROMPT, timeout)
            notes.append(f"### 片段 {i}\n\n{note}")
        merged = "\n\n".join(notes)
        return self._summary_with_chain(
            merged,
            system_prompt + "\n\n（输入内容是按时间顺序拼接的各片段要点小结，"
            "请归并去重后输出最终纪要，结构保持不变。）",
            timeout,
        )

    @staticmethod
    def _split_transcript(text: str, chunk_chars: int) -> list[str]:
        """按行切块（句子不拆半），每块约 chunk_chars。"""
        lines = text.splitlines()
        parts: list[str] = []
        cur: list[str] = []
        size = 0
        for ln in lines:
            cur.append(ln)
            size += len(ln) + 1
            if size >= chunk_chars:
                parts.append("\n".join(cur))
                cur, size = [], 0
        if cur:
            parts.append("\n".join(cur))
        return parts

    def _summary_with_chain(self, transcript: str, system_prompt: str,
                            timeout: Optional[float]) -> str:
        """单次总结调用，含完整「后端 → 模型」回退链。"""
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
                    # ---- 限流：临时状态，换后端而不是换模型 ----
                    if self._is_rate_limit(str(e)):
                        self._enter_rate_limit(be.name, str(e))
                        nxt_be = self._next_backend(backends, be)
                        if nxt_be:
                            self._note(f"summary_rate_limit:{be.name}",
                                       f"{be.name} 总结限流（{self._brief(str(e))}），"
                                       f"切换到 {nxt_be.name} 后端")
                        else:
                            self._note(f"summary_rate_limit:{be.name}",
                                       f"{be.name} 总结限流（{self._brief(str(e))}），"
                                       f"已暂停 {int(self._rate_limit_cooldown)} 秒后重试")
                        # 限流通常是账号级配额，换模型无意义 → 直接换后端
                        break
                    nxt = chain[idx + 1] if idx + 1 < len(chain) else None
                    if nxt:
                        # 同一模型上的失败只提示一次
                        self._note(f"summary_model_fail:{be.name}:{m}",
                                   f"总结降级：{m} → {nxt}（{self._brief(str(e))}）")
                        continue
                    # 本后端模型链走完 → 换下一个后端（跨服务商兜底）
                    nxt_be = self._next_backend(backends, be)
                    if nxt_be:
                        self._note(f"summary_backend_fail:{be.name}:{m}",
                                   f"总结切到 {nxt_be.name} 后端（{m} 不可用）")
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

    # ---------------- 提示去重 / 路线状态 ----------------

    def _note(self, key: str, msg: str) -> None:
        """发出一条提示，但同一 key 只发一次。

        key 形如 "<kind>:<backend>:<model>"，同一类降级在同一后端/模型上
        只提示一次——避免主模型挂掉后每句都刷一条同样的提示。
        """
        if key in self._noted:
            return
        self._noted.add(key)
        self.events_sink(msg)

    def _clear_notes(self, prefix: str) -> None:
        """清除某一类提示的去重记录（状态恢复后允许再次提示）。"""
        for k in [k for k in self._noted if k.startswith(prefix)]:
            self._noted.discard(k)

    def _preferred_model(self, backend: str) -> str:
        """该后端的首选模型（回退链的第一档）。"""
        if backend == "primary":
            chain = self.translate_models or [self.model]
        else:
            for b in self.alt_backends:
                if b.name == backend:
                    chain = b.translate_models or [b.model]
                    break
            else:
                return ""
        return chain[0] if chain else ""

    def _set_route(self, backend: str, model: str) -> None:
        """记录当前实际生效的（后端, 模型）路线。

        路线变化时才更新状态；**降级时不发提示**（失败原因已由 _note 报过，
        避免同一次降级弹出两行），只有恢复到首选路线时才提示一次。
        """
        route = (backend, model)
        if route == self._route:
            return
        first = self._route is None
        self._route = route
        degraded = (backend != "primary") or (model != self._preferred_model(backend))
        self.route_state = (degraded, backend, model)
        # 徽标状态始终同步（含首次建立路线），否则 UI 拿不到降级信息
        if self.route_sink is not None:
            self.route_sink(degraded, backend, model)
        if first:
            # 会话刚开始的正常状态，不提示
            return
        if not degraded:
            # 恢复到首选：清掉该后端的旧降级提示记录，
            # 将来若再次降级可以重新提示一次
            self._clear_notes(f"model_fail:{backend}:")
            self._clear_notes(f"backend_fail:{backend}:")
            self._clear_notes("primary_probe:")
            self.events_sink(f"翻译已恢复到 {backend} 后端（{model}）")

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
        payload.update(getattr(be, "extra_body", None) or self.extra_body)
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
            msg = f"HTTP {e.code}：{self._extract_api_msg(body) or e.reason}"
            # 限流是临时状态，不计入停用计数（见 _is_rate_limit 的说明）
            if not self._is_rate_limit(msg):
                self._fail += 1
            raise SummaryError(msg) from e
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
    _BATCH_PROMPT_SUFFIX = (
        "\n\n输入每一句都有编号。请逐句翻译，输出行数必须与输入相同，"
        "每行格式为「编号. 译文」，编号保持不变，不要合并或拆分行，只输出译文行。"
    )

    def translate(self, text: str, source_lang: str = "",
                  context: Optional[list[str]] = None) -> str:
        """翻译单句。失败时按「后端 → 模型」两级回退链降级重试。

        context 为本句之前同场会议的最近几句原文，仅供模型理解上下文
        （统一术语、正确指代），不要求翻译输出。
        """
        if not self.enabled or not text or not text.strip():
            return ""
        trans = self._translate_with_chain([text], source_lang, [context or []])
        return trans[0] if trans else ""

    def translate_batch(self, texts: list[str], source_lang: str = "",
                        contexts: Optional[list[list[str]]] = None) -> list[str]:
        """批量翻译几句。返回与输入等长的译文列表，失败项为空串。

        合并一次请求可显著省时省额度；解析失败或漏行时，失败句自动
        逐句降级重试，尽力不丢译文。
        """
        if not self.enabled or not texts:
            return ["" for _ in texts]
        texts = [t for t in texts]  # 防御性拷贝
        ctxs = contexts or [[] for _ in texts]
        trans = self._translate_with_chain(texts, source_lang, ctxs)
        # 有漏行的句子逐句重试（走各自上下文的单句链路）
        for i, (t, out) in enumerate(zip(texts, trans)):
            if not out.strip() and t.strip():
                trans[i] = self.translate(t, source_lang, ctxs[i])
        return trans

    def _build_user_content(self, src_name: str, tgt_name: str,
                            texts: list[str], ctxs: list[list[str]]) -> str:
        """构造带上下文的用户消息。texts 单句或多句编号均可。"""
        show_ctx = bool(ctxs) and any(c for c in ctxs)
        head = ""
        if show_ctx:
            merged: list[str] = []
            for c in ctxs:
                for s in c:
                    s = s.strip()
                    if s and s not in merged:
                        merged.append(s)
            if merged:
                head = ("以下是同一会议中，需要翻译的句子之前的发言"
                        "（仅供理解上下文，请勿翻译输出）：\n"
                        + "\n".join(merged) + "\n\n")
        if len(texts) == 1:
            body = texts[0]
        else:
            body = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
            body += self._BATCH_PROMPT_SUFFIX
        return f"{head}请将以下{src_name}会议发言翻译为{tgt_name}，只输出译文：\n{body}"

    def _translate_with_chain(self, texts: list[str], source_lang: str,
                              ctxs: list[list[str]]) -> list[str]:
        """一次批量请求 + 完整「后端 → 模型」回退链。返回与 texts 等长结果。"""
        src_name = _LANG_NAME.get(source_lang, source_lang or "原文")
        tgt_name = _LANG_NAME.get(self.target_lang, self.target_lang)
        backends = self._backend_chain()
        last_err = ""
        # 主后端熔断后，定期放行一次让它自愈（额度恢复后自动切回）
        self._maybe_recover_primary()
        any_ok = False
        # 本轮所有失败是否都只是"限流"这一种临时原因。
        # 若是，则不计入停用计数（限流等配额窗口重开即恢复，不该把翻译永久停用）
        only_rate_limit = True
        # 本轮是否真的发起过请求。全部后端都被跳过（熔断/限流冷却）时，
        # 本轮没有新的错误信息，不应据此判定任何停用动作。
        made_request = False
        for be in backends:
            # 熔断：该后端刚连续失败过，先跳过（避免每批都白等一个超时）
            # 熔断跳过是**中性**的：硬失败在触发熔断时已计入计数，
            # 这里重复计会让"只剩限流后端"的情况被误判为硬故障而停用翻译。
            if self._is_open(be.name):
                continue
            # 限流冷却：该后端刚被 429 限流，冷却期内直接跳过
            # （限流是临时状态，配额窗口重开即自动放行，不做硬熔断）
            if self._in_rate_limit(be.name):
                last_err = last_err or f"{be.name} 后端限流中，稍后重试"
                continue
            chain = be.translate_models or [be.model]
            for idx, m in enumerate(chain):
                out, err = self._translate_once(texts, src_name, tgt_name, m, be, ctxs)
                made_request = True
                if out:
                    any_ok = True
                    self._fail = 0
                    # 只重置"正在用的这个后端"的熔断计数；
                    # 其他后端的失败计数保持不变（否则主后端熔断会被误清）
                    self._reset_breaker(be.name)
                    self._backend_fail[be.name] = 0
                    # 该后端已恢复可用 → 清掉它的限流记录，下次再限流可以重新提示
                    self._rate_limit_until.pop(be.name, None)
                    self._clear_notes(f"rate_limit:{be.name}")
                    # 记录生效路线：变化时才提示一次，同路线持续工作完全静默
                    self._set_route(be.name, m)
                    return out
                last_err = err
                # ---- 限流（HTTP 429）：临时状态，特殊处理 ----
                if self._is_rate_limit(err):
                    self._enter_rate_limit(be.name, err)
                    nxt_be = self._next_backend(backends, be)
                    if nxt_be:
                        self._note(f"rate_limit:{be.name}",
                                   f"{be.name} 限流（{self._brief(err)}），"
                                   f"暂停 {int(self._rate_limit_cooldown)} 秒，"
                                   f"切到 {nxt_be.name} 后端")
                    else:
                        self._note(f"rate_limit:{be.name}",
                                   f"{be.name} 限流（{self._brief(err)}），"
                                   f"暂停 {int(self._rate_limit_cooldown)} 秒后重试")
                    # 同一后端换模型无意义：限流通常是**账号级**配额
                    # （实测：agnes 三个模型共享同一 1 req/min 配额），
                    # 继续试下一个模型只会多烧请求、多刷提示。
                    break
                only_rate_limit = False
                # 失败按后端独立累计，不污染全局计数
                self._backend_fail[be.name] = self._backend_fail.get(be.name, 0) + 1
                self._trip_breaker(be.name)
                nxt = chain[idx + 1] if idx + 1 < len(chain) else None
                if nxt:
                    # 同一模型上的失败只提示一次，不逐句刷屏
                    self._note(f"model_fail:{be.name}:{m}",
                               f"翻译降级：{m} → {nxt}（{self._brief(last_err)}）")
                    continue
                nxt_be = self._next_backend(backends, be)
                if nxt_be:
                    self._note(f"backend_fail:{be.name}:{m}",
                               f"翻译切到 {nxt_be.name} 后端（{m} 不可用）")
                    break
                break

        if any_ok:
            # 本批至少有一个后端成功过 → 不累计全局失败
            return ["" for _ in texts]

        if only_rate_limit or not made_request:
            # 本轮失败全部来自限流（临时状态，配额窗口重开即可恢复），
            # 或本轮所有后端都被跳过（熔断/限流冷却，没有新错误信息）。
            # 这两种情况都**不能**把翻译永久停用 —— 否则一场会议就再也译不出来。
            self.last_error = last_err or self.last_error
            return ["" for _ in texts]

        # 所有后端都失败：连续失败达阈值才停用（避免单次抖动误伤）
        self._fail += 1
        if self._fail >= self._max_fail:
            self.enabled = False
            # 全部后端都在熔断期被跳过时，本轮没有新的错误信息，
            # 沿用上一次的真实失败原因（否则提示会是空的，看不出问题）
            reason = last_err or self.last_error or "所有后端均不可用"
            self.disabled_reason = reason
            # 停用只提示一次（后续每句失败不再重复打扰）
            self._note("translate_disabled:all",
                       f"翻译已停用（连续 {self._fail} 次失败）：{self._brief(reason)}")
            if self.status_sink is not None:
                self.status_sink(False, reason)
        self.last_error = last_err or self.last_error
        return ["" for _ in texts]

    # ---------------- 后端熔断 ----------------
    def _trip_breaker(self, name: str) -> None:
        """记录某后端的一次失败。"""
        self._breaker[name] = self._breaker.get(name, 0) + 1

    @staticmethod
    def _brief(err: str, limit: int = 60) -> str:
        """把后端返回的长错误压成一行短原因（仅供日志/徽标，不进正文）。"""
        e = " ".join((err or "").split())
        if not e:
            return "未知错误"
        code = re.search(r"HTTP\s*(\d{3})", e, re.I)
        low = e.lower()
        if any(re.search(p, low) for p in _QUOTA_EXHAUSTED_PATTERNS):
            reason = "额度用尽"
        elif any(p in low for p in _RATE_LIMIT_PATTERNS):
            reason = "限流"
        elif "modelnotopen" in low or "未开通" in e:
            reason = "模型未开通"
        elif "timeout" in low or "timed out" in low or "超时" in e:
            reason = "超时"
        elif code:
            reason = "请求失败"
        else:
            return e if len(e) <= limit else e[:limit] + "…"
        return f"HTTP {code.group(1)} {reason}" if code else reason

    @staticmethod
    def _is_rate_limit(err: str) -> bool:
        """判断错误是否为限流（HTTP 429 / Too Many Requests）。

        限流是"发得太快"的临时状态，与额度耗尽（403）、模型不可用（404）
        性质完全不同：前者等配额窗口重开就恢复，后者不会自己好。
        两者若混为一谈，一次限流就会把后端熔断掉甚至永久停用翻译。

        注意 HTTP 429 也可能表示"额度用尽"（火山方舟 SetLimitExceeded、
        Agnes 实名认证前的 rpm 限制等），必须先排除额度耗尽再判限流。
        """
        if not err:
            return False
        low = err.lower()
        # 额度耗尽优先：等配额窗口重开对它无效，必须当硬失败处理
        if any(p in low for p in _QUOTA_EXHAUSTED_PATTERNS):
            return False
        return any(p in low for p in _RATE_LIMIT_PATTERNS)

    def _enter_rate_limit(self, name: str, err: str) -> None:
        """标记后端进入限流冷却。

        冷却期内该后端被直接跳过，避免每句都去撞一次 429 白等一个往返；
        冷却结束后自动放行，配额恢复即自愈。
        """
        self._rate_limit_until[name] = time.monotonic() + self._rate_limit_cooldown
        self._rate_limited.add(name)

    def _in_rate_limit(self, name: str) -> bool:
        """该后端是否仍处于限流冷却期。冷却到期自动放行。"""
        until = self._rate_limit_until.get(name)
        if until is None:
            return False
        if time.monotonic() < until:
            return True
        # 冷却期已过 → 放行，并清掉限流记录（下次再限流可以重新提示）
        self._rate_limit_until.pop(name, None)
        self._rate_limited.discard(name)
        self._clear_notes(f"rate_limit:{name}")
        return False

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
        # 探测提示同样只发一次，避免自愈探测每分钟刷一条
        self._note("primary_probe:primary",
                   f"主后端已熔断，正在重试（每 {int(self._recover_interval)}s 放行一次）…")
    def _translate_once(self, texts: list[str], src_name: str, tgt_name: str,
                        model: str, be: Optional[Backend],
                        ctxs: Optional[list[list[str]]] = None
                        ) -> tuple[Optional[list[str]], str]:
        """调用单个后端的单个模型（单句或多句批量）。

        成功返回（与 texts 等长的译文列表, ""）；整体失败返回（None, 原因）。
        多句时按「编号行」对齐回填，漏行由上层逐句降级重试。
        """
        be = be or Backend(base_url=self.base_url, api_key=self.api_key)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user",
                 "content": self._build_user_content(src_name, tgt_name, texts, ctxs or [])},
            ],
            "temperature": 0.2,
            "stream": False,
        }
        payload.update(getattr(be, "extra_body", None) or self.extra_body)
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
            if not out:
                return None, f"{model} 返回空内容"
            results = self._parse_numbered(out, len(texts))
            if all(not r for r in results):
                # 返回了内容但没有可提取的译文行 → 视为整批失败
                return None, f"{model} 译文解析失败：{out[:80]}"
            return results, ""
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            return None, f"{model} HTTP {e.code}：{self._extract_api_msg(body) or e.reason}"
        except Exception as e:  # noqa: BLE001
            return None, f"{model} {e.__class__.__name__}：{e}"

    @staticmethod
    def _parse_numbered(out: str, n: int) -> list[str]:
        """从「编号. 译文」格式的输出中回填 n 句译文。"""
        results = [""] * n
        for line in out.splitlines():
            m = _NUM_LINE_RE.match(line.strip())
            if not m or not m.group(2).strip():
                continue
            i = int(m.group(1)) - 1
            if 0 <= i < n and not results[i]:
                results[i] = m.group(2).strip()
        # 单句、且模型没按编号回 → 整体视为该句译文（兼容旧行为）
        if n == 1 and not results[0]:
            text = out.strip()
            # 模型偶尔会自己带 "1. " 前缀
            m = _NUM_LINE_RE.match(text)
            if m and m.group(2).strip():
                text = m.group(2).strip()
            results[0] = text
        return results

