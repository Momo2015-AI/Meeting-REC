"""会议转写编排：音频采集 → Vosk 流式识别 → LLM 翻译 → 双语纪要落盘 + 事件分发。

线程模型：
- capture 线程：LoopbackCapture 回调产出 PCM；
- transcriber 线程：accept/final 识别（Vosk 调用均在此线程），落盘原文；
- translate 线程：独立消费待译队列，翻译完成后补写译文并发事件（不阻塞识别）；
- 消费方：UI 主线程或控制台，通过 self.events 队列获取 partial/final/trans/status/error。

事件协议：
- ("status", "running"|"stopped")
- ("partial", 文本)
- ("final", 时间戳, 原文, 句序号)
- ("trans", 时间戳, 译文, 句序号)
- ("error", 描述)

句序号用于把异步返回的译文精确绑定回原文句（同一秒内可能有多句，
仅靠时间戳无法可靠配对）。
"""
from __future__ import annotations

import queue
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .asr import VoskASR
from .capture import LoopbackCapture
from .translate import Backend, Translator


def build_asr(cfg, engine: str):
    """按引擎名构建识别器。

    抽成工厂是为了支持**运行时切换**：UI 的「切换引擎」按钮要在不重启
    程序的前提下换掉识别器，两套引擎必须走同一条构建路径。

    - "vosk"       → 流式逐词（libvosk.dll，无 Python ABI 约束）
    - "sensevoice" → 块级整句（funasr-onnx，需 Python ≤ 3.13）
    """
    engine = (engine or "vosk").strip().lower()
    if engine == "sensevoice":
        # 懒导入：engine=vosk 时不背 onnx 依赖
        from .asr_sensevoice import SenseVoiceStream

        return SenseVoiceStream(
            cfg.resolve(cfg.asr.sv_model_dir),
            cfg.resolve(cfg.asr.sv_vad_dir),
            sample_rate=cfg.audio.sample_rate,
            threads=cfg.asr.sv_threads,
            lang=cfg.asr.source_lang,
            max_block_s=cfg.asr.sv_max_block_s,
            textnorm=cfg.asr.sv_textnorm,
        )
    return VoskASR(
        str(cfg.resolve(cfg.asr.lib_dir)),
        str(cfg.resolve(cfg.asr.model_dir)),
        cfg.audio.sample_rate,
    )


class MeetingTranscriber:
    def __init__(self, cfg, pcm_source: Optional[Callable] = None):
        self.cfg = cfg
        engine = str(getattr(cfg.asr, "engine", "vosk") or "vosk").lower()
        self.engine = engine
        self.asr = build_asr(cfg, engine)
        self.translator = Translator(
            base_url=cfg.translate.base_url,
            api_key=cfg.translate.api_key,
            model=cfg.translate.model,
            target_lang=cfg.translate.target_lang,
            timeout=cfg.translate.timeout,
            system_prompt=cfg.translate.system_prompt,
            enabled=cfg.translate.enabled and cfg.translate.backend != "none",
            summary_models=list(cfg.summary.fallback_models),
            translate_models=list(cfg.translate.fallback_models),
            extra_body=dict(cfg.translate.extra_body),
            alt_backends=[
                Backend(
                    name=b.name,
                    base_url=b.base_url,
                    api_key=b.api_key,
                    model=b.model,
                    translate_models=list(b.fallback_models) or ([b.model] if b.model else []),
                    summary_models=list(b.fallback_models) or ([b.model] if b.model else []),
                    extra_body=dict(b.extra_body),
                )
                for b in cfg.translate.alt_backends
                if b.enabled and b.base_url and b.api_key
            ],
        )
        self.events: "queue.Queue[tuple]" = queue.Queue()
        # 翻译层的降级提示必须在**构造时**就接到事件流。
        # 旧实现只在 summarize() 里绑定，导致会议进行中的模型/后端降级提示
        # 全部被丢弃（Translator.events_sink 默认只写 _pending_note，无人读取）。
        self.translator.events_sink = self._on_translator_note
        # 路线变化（降级/恢复）→ 常驻徽标状态，不靠反复弹窗表达
        self.translator.route_sink = self._on_route_change
        # 翻译被停用 → 徽标转为「仅转写」
        self.translator.status_sink = self._on_translator_status
        # 采集器延迟创建：只有用户点「开始」后才真正打开系统音频设备。
        # pcm_source 注入（测试用）优先，此时完全不需要真实设备。
        self._pcm_source = pcm_source
        self._capture: Optional[LoopbackCapture] = None
        self._stop = threading.Event()
        self._active = False
        self._thread: Optional[threading.Thread] = None
        self._trans_q: "queue.Queue[tuple]" = queue.Queue()
        self._trans_thread: Optional[threading.Thread] = None
        self._wav: Optional[wave.Wave_write] = None
        self._write_lock = threading.Lock()
        self.session_dir: Optional[Path] = None
        self._closed = False                      # 幂等守卫：stop() 重入不重复收尾
        self.transcript_path: Optional[Path] = None
        self.summary_path: Optional[Path] = None
        self._last_partial_push = 0.0
        self._started_at: Optional[datetime] = None
        self._seq_line: dict[int, int] = {}
        self._summary_thread: Optional[threading.Thread] = None
        self._recent_finals: list[str] = []      # 最近几句原文，供翻译上下文
        self.stats = {"finals": 0, "translated": 0, "pcm_bytes": 0, "summarized": False}
        self.translate_status = ""

    # ---------------- 翻译层事件桥接 ----------------

    def _on_translator_note(self, msg: str) -> None:
        """翻译层降级/限流提示 → 只写控制台日志，不进字幕正文。

        正文区只保留「原文 + 译文」；降级状态由顶部常驻徽标
        （⚠ 降级 backend/model）表达。事件仍投递（第三位 silent=True），
        供测试与排查消费。
        """
        print(f"[translate] {msg}")
        self.events.put(("note", msg, True))

    def _on_route_change(self, degraded: bool, backend: str, model: str) -> None:
        """生效路线变化 → 事件流（UI 更新顶部徽标，不刷屏）。"""
        self.events.put(("route", degraded, backend, model))

    def _on_translator_status(self, ok: bool, reason: str) -> None:
        """翻译可用性变化 → 事件流（UI 更新徽标）。"""
        self.events.put(("trstatus", ok, reason))

    # ---------------- 引擎切换 ----------------

    def switch_engine(self, engine: str) -> tuple[bool, str]:
        """运行时切换识别引擎。返回 (是否成功, 说明)。

        约束：采集进行中不允许切换——识别器实例正被采集线程调用，
        换掉会撕裂状态。UI 会在采集期间禁用该按钮，这里再兜一层。

        切换失败（依赖缺失、模型缺失、ABI 不兼容等）时**保持原引擎不变**，
        并把失败原因原样带回给 UI 提示，绝不留下半损坏状态。
        """
        engine = (engine or "").strip().lower()
        if engine not in ("vosk", "sensevoice"):
            return False, f"未知引擎：{engine}"
        if self.running:
            return False, "采集中无法切换引擎，请先停止"
        if engine == self.engine:
            return True, f"当前已是 {engine} 引擎"

        try:
            new_asr = build_asr(self.cfg, engine)
        except Exception as e:  # noqa: BLE001
            return False, f"切换到 {engine} 失败：{e}"

        # 构建成功才替换；旧引擎此刻释放资源
        old = self.asr
        self.asr = new_asr
        self.engine = engine
        self.cfg.asr.engine = engine
        try:
            old.close()
        except Exception:  # noqa: BLE001
            pass
        return True, f"已切换到 {engine} 引擎"

    # ---------------- 会话生命周期 ----------------
    def _open_session(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_root = self.cfg.resolve(self.cfg.output.dir)
        out_root.mkdir(parents=True, exist_ok=True)
        # 目录名精确到秒：同一秒内"停止→再开始"会撞名并覆盖上一场纪要。
        # 这里加序号后缀保证唯一，绝不覆盖已有会话。
        session_dir = out_root / f"meeting_{stamp}"
        idx = 2
        while session_dir.exists():
            session_dir = out_root / f"meeting_{stamp}_{idx}"
            idx += 1
        self.session_dir = session_dir
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_path = self.session_dir / "transcript.md"
        self.summary_path = self.session_dir / "summary.md"
        # 每场会议独立统计：否则上一场说过话会让本场误判"有内容"而触发空总结
        self.stats = {"finals": 0, "translated": 0, "pcm_bytes": 0, "summarized": False}
        self._seq_line.clear()
        self._recent_finals.clear()
        asr_engine = str(getattr(self.cfg.asr, "engine", "vosk"))
        with self._write_lock:
            with open(self.transcript_path, "w", encoding="utf-8") as f:
                f.write("# 会议双语纪要\n\n")
                f.write(f"- 开始时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n")
                f.write(f"- 识别语言：{self.cfg.asr.source_lang} → 翻译目标："
                        f"{self.cfg.translate.target_lang}\n")
                f.write(f"- 识别引擎：{asr_engine}（"
                        f"{self.cfg.asr.model_dir if asr_engine != 'sensevoice' else self.cfg.asr.sv_model_dir}）\n")
                f.write(f"- 翻译后端：{self.translate_status or '未探测'}\n")
                if self.cfg.audio.save_audio:
                    f.write("- 原始录音：`meeting.wav`（16 kHz / 16 bit / 单声道）\n")
                f.write(f"- 总结：{'停止采集后由 LLM 自动生成' if self.cfg.summary.enabled else '未启用'}\n\n")
                f.write("## 逐句转写\n\n---\n\n")
        if self.cfg.audio.save_audio:
            wav_path = self.session_dir / "meeting.wav"
            self._wav = wave.open(str(wav_path), "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(self.cfg.audio.sample_rate)

    def _close_session(self) -> None:
        if self._wav is not None:
            try:
                self._wav.close()
            finally:
                self._wav = None
        if self.transcript_path is not None and self._started_at is not None:
            dur = (datetime.now() - self._started_at).total_seconds()
            with self._write_lock:
                with open(self.transcript_path, "a", encoding="utf-8") as f:
                    f.write("\n---\n\n")
                    f.write(f"- 结束时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n")
                    f.write(f"- 时长：{dur / 60:.1f} 分钟；完整语句 {self.stats['finals']} 句；"
                            f"已翻译 {self.stats['translated']} 句\n")

    # ---------------- 停止后的总结 ----------------
    def _build_summary_input(self) -> str:
        """从已落盘的 MD 提取转写正文（原文+译文），供 LLM 总结。"""
        if self.transcript_path is None or not self.transcript_path.exists():
            return ""
        try:
            text = self.transcript_path.read_text(encoding="utf-8")
        except OSError:
            return ""
        # 只取"## 逐句转写"之后的正文，去掉头部元信息
        marker = "## 逐句转写"
        body = text.split(marker, 1)[1] if marker in text else text
        # 去掉尾部统计块
        if "\n---\n\n- 结束时间" in body:
            body = body.split("\n---\n\n- 结束时间", 1)[0]
        # 去掉分隔线与空行：只剩 "---" 说明本场没有任何转写内容
        lines = [ln for ln in body.splitlines() if ln.strip() and ln.strip() != "---"]
        body = "\n".join(lines)
        # 不再截断：超长转写由 Translator.summarize 分块小结后归并（见 max_chars）
        return body.strip()

    def _write_summary(self, summary_md: str) -> None:
        """把总结写入 transcript.md 末尾，并另存 summary.md。"""
        if self.transcript_path is None:
            return
        with self._write_lock:
            with open(self.transcript_path, "a", encoding="utf-8") as f:
                f.write("\n---\n\n## 会议总结\n\n")
                f.write(summary_md.rstrip() + "\n")
            if self.summary_path is not None:
                header = (f"# 会议总结\n\n"
                          f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
                          f"- 来源：{self.transcript_path.name}\n\n")
                with open(self.summary_path, "w", encoding="utf-8") as f:
                    f.write(header + summary_md.rstrip() + "\n")

    def _write_summary_failure(self, reason: str) -> None:
        """总结失败时在 MD 里留痕（不静默丢失），并给出补救指引。"""
        if self.transcript_path is None or not self.transcript_path.exists():
            return
        try:
            text = self.transcript_path.read_text(encoding="utf-8")
        except OSError:
            return
        if "## 会议总结" in text:      # 已有总结（成功或旧记录），不覆盖
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._write_lock:
            with open(self.transcript_path, "a", encoding="utf-8") as f:
                f.write("\n---\n\n## 会议总结\n\n")
                f.write(f"> ⚠️ 总结生成失败（{stamp}）\n>\n")
                f.write(f"> 原因：{reason}\n>\n")
                f.write("> 转写正文完整保留在上方，未受影响。\n"
                        "> 可在额度恢复后运行 `python summarize.py <会话目录>` 补生成。\n")

    def summarize(self) -> bool:
        """生成会议总结。返回是否成功。同步调用（供停止流程使用）。"""
        if not self.cfg.summary.enabled or not self.translator.enabled:
            return False
        content = self._build_summary_input()
        if not content.strip():
            return False
        self.events.put(("status", "summarizing"))
        # 翻译层的回退提示已在构造时接到事件流（见 __init__），
        # 这里无需重复绑定。
        try:
            md = self.translator.summarize(
                content, self.cfg.summary.system_prompt,
                timeout=self.cfg.summary.timeout,
                chunk_chars=self.cfg.summary.max_chars,
            )
        except Exception as e:  # noqa: BLE001
            reason = str(e)
            self.events.put(("error", f"总结生成失败：{reason}"))
            self.stats["summary_error"] = reason
            self._write_summary_failure(reason)
            return False
        if not md.strip():
            self.events.put(("error", "总结生成失败：后端未返回内容"))
            self._write_summary_failure("后端未返回内容")
            return False
        self._write_summary(md)
        self.stats["summarized"] = True
        used = getattr(self.translator, "summary_used_model", "")
        if used:
            self.events.put(("note", f"总结由模型 {used} 生成"))
        self.events.put(("summary", md))
        return True

    def start(self) -> None:
        """开始采集。必须在用户明确点击「开始」后调用。"""
        if self.running:
            return
        self._closed = False
        self._stop.clear()
        self._active = True
        self._open_session()
        self._started_at = datetime.now()
        self._thread = threading.Thread(target=self._run, name="transcriber", daemon=True)
        self._thread.start()
        self._trans_thread = threading.Thread(target=self._run_translate, name="translator", daemon=True)
        self._trans_thread.start()
        self.events.put(("status", "running"))

    def stop(self, wait_summary: bool = False) -> None:
        """停止采集。

        总结在独立线程异步进行，不阻塞调用方（UI 不会被卡住）。
        wait_summary=True 时同步等待总结落盘后再返回——进程退出前必须走这条路径，
        否则守护线程会随进程一起被杀，summary 丢失。

        幂等：重复调用 stop()（双击停止、Esc+窗口关闭连发）只收尾一次，
        不重复写"结束时间"尾块。
        """
        if self._closed:
            self.events.put(("status", "stopped"))
            return
        self._closed = True
        self._active = False
        self._stop.set()
        # 让阻塞中的采集循环退出，并释放系统音频设备句柄
        if self._capture is not None:
            self._capture.stop()
        self._trans_q.put(("__stop__",))
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if self._trans_thread is not None:
            self._trans_thread.join(timeout=8)
            self._trans_thread = None
        self._capture = None
        # 从未开始过 → 没有任何会话可收尾，直接返回
        if self._started_at is None:
            self.events.put(("status", "stopped"))
            return
        # 冲刷识别器残余：解码器/VAD 要等到「确认没有后续语音」才吐出最后一句，
        # 不冲刷就会丢掉会议的最后一句（SenseVoice 尤其明显）。
        self._flush_asr_tail()
        # 收尾：关闭 WAV、补写"结束时间/时长/句数"尾块
        self._close_session()
        self.events.put(("status", "stopped"))
        # 总结触发条件：以"本场是否已有转写内容"为准。
        # 采集线程可能已异常退出但内容已落盘（此时仍应总结）；
        # 反之本场一句未说（不应总结）。
        has_content = self.stats["finals"] > 0
        if has_content and self.cfg.summary.enabled and self.translator.enabled:
            self._summary_thread = threading.Thread(
                target=self._run_summary, name="summarizer", daemon=True)
            self._summary_thread.start()
            if wait_summary:
                self._summary_thread.join(timeout=self.cfg.summary.timeout + 15)
                self._summary_thread = None

    def _flush_asr_tail(self) -> None:
        """停止采集时冲刷识别器残余，把最后一句补进转写文档。"""
        drain = getattr(self.asr, "drain_finals", None)
        if drain is None:
            return
        try:
            texts = drain() or []
        except Exception:  # noqa: BLE001
            return
        for text in texts:
            if text and text.strip():
                self._on_final(text.strip())

    def _run_summary(self) -> None:
        try:
            self.summarize()
        except Exception as e:  # noqa: BLE001
            self.events.put(("error", f"总结生成失败：{e}"))

    def wait_for_summary(self, timeout: float | None = None) -> bool:
        """等待总结线程结束。返回是否已完成（含"无需总结"的情况）。"""
        th = self._summary_thread
        if th is None:
            return True
        th.join(timeout=timeout if timeout is not None else self.cfg.summary.timeout + 15)
        done = not th.is_alive()
        if done:
            self._summary_thread = None
        return done

    @property
    def running(self) -> bool:
        """是否正在采集。

        用显式标志而非线程存活判断：注入的采集源可能在后台线程里跑，
        线程存活与否不等于"会话是否处于采集状态"。
        """
        return self._active

    # ---------------- 主循环 ----------------
    def _run(self) -> None:
        def on_pcm(pcm: bytes) -> None:
            if self._stop.is_set():
                return
            self.stats["pcm_bytes"] += len(pcm)
            if self._wav is not None:
                self._wav.writeframes(pcm)
            text = self.asr.accept(pcm)
            if text and text.strip():
                self._on_final(text.strip())
                return
            now = time.monotonic()
            if now - self._last_partial_push >= self.cfg.asr.partial_interval:
                self._last_partial_push = now
                part = self.asr.partial()
                if part.strip():
                    self.events.put(("partial", part))

        def on_error(exc: Exception) -> None:
            # 只报告，不终止：由下面的重连循环决定是否恢复（设备热拔插场景）
            self.events.put(("error", f"音频采集异常：{exc}"))

        # 采集源：真实场景用 LoopbackCapture（阻塞式，stop() 时退出）；
        # 测试场景可注入 pcm_source（约定为阻塞式，同样需在 stop 后返回）。
        if self._pcm_source is not None:
            self._pcm_source(on_pcm, on_error)
            return

        attempts = max(1, int(getattr(self.cfg.audio, "reconnect_attempts", 5) or 5))
        delay = float(getattr(self.cfg.audio, "reconnect_delay", 3.0) or 3.0)
        attempt = 0
        while not self._stop.is_set():
            # 每次重连都重新解析默认输出设备——耳机拔插后设备名/句柄都会变
            self._capture = LoopbackCapture(
                sample_rate=self.cfg.audio.sample_rate, chunk_ms=self.cfg.audio.chunk_ms
            )
            try:
                self._capture.start(on_pcm, on_error)
            except RuntimeError as e:
                # 设备不可用（如没有输出设备）——提示后延时重试
                self.events.put(("error", str(e)))
            self._capture = None
            if self._stop.is_set():
                break
            attempt += 1
            if attempt >= attempts:
                self.events.put(("error", f"音频重连 {attempt} 次失败，停止本场采集"))
                self._stop.set()
                break
            self.events.put(("note",
                             f"音频中断，{delay:.0f}s 后重连（{attempt}/{attempts}）…"))
            self._stop.wait(delay)   # 可被 stop() 立即打断

    def _on_final(self, text: str) -> None:
        now = datetime.now()
        ts = now.strftime("%H:%M:%S")
        self.stats["finals"] += 1
        seq = self.stats["finals"]
        # 先写原文并登记占位行号，译文到达后原地回填，保证一一对应
        with self._write_lock:
            with open(self.transcript_path, "a", encoding="utf-8") as f:
                f.write(f"- **[{ts}]** {text}\n")
                f.flush()
                self._seq_line[seq] = f.tell()
        # 携带此前几句原文作上下文（统一术语、正确指代），与句序号一起入队
        n_ctx = int(getattr(self.cfg.translate, "context_sentences", 0) or 0)
        ctx = list(self._recent_finals[-n_ctx:]) if n_ctx > 0 else []
        self.events.put(("final", ts, text, seq))
        if self.translator.enabled:
            self._trans_q.put((ts, text, seq, ctx))
        self._recent_finals.append(text)
        if len(self._recent_finals) > 8:
            self._recent_finals = self._recent_finals[-8:]

    # ---------------- 翻译线程 ----------------
    def _run_translate(self) -> None:
        """翻译消费线程：支持批量凑批（batch_size/batch_wait），不阻塞识别。"""
        bsize = max(1, int(getattr(self.cfg.translate, "batch_size", 1) or 1))
        bwait = float(getattr(self.cfg.translate, "batch_wait", 0.4) or 0.0)
        while True:
            item = self._trans_q.get()
            if item[0] == "__stop__":
                return
            batch = [item]
            if bsize > 1:
                # 短窗口内凑批：来句快（多人讨论）时合并请求省钱省时；
                # 窗口结束或队伍空了立即开翻，不牺牲延迟
                deadline = time.monotonic() + bwait
                while len(batch) < bsize:
                    remain = deadline - time.monotonic()
                    if remain <= 0:
                        break
                    try:
                        nxt = self._trans_q.get(timeout=remain)
                    except queue.Empty:
                        break
                    if nxt[0] == "__stop__":
                        self._trans_q.put(nxt)   # 挪到队尾，下一轮退出
                        break
                    batch.append(nxt)
            texts = [b[1] for b in batch]
            ctxs = [b[3] for b in batch]
            try:
                if len(texts) == 1:
                    trans_list = [self.translator.translate(
                        texts[0], self.cfg.asr.source_lang, ctxs[0])]
                else:
                    trans_list = self.translator.translate_batch(
                        texts, self.cfg.asr.source_lang, ctxs)
            except Exception as e:  # noqa: BLE001
                self.events.put(("error", f"翻译失败：{e}"))
                continue
            for (ts, _text, seq, _ctx), trans in zip(batch, trans_list):
                if not trans:
                    continue
                self.stats["translated"] += 1
                with self._write_lock:
                    self._write_translation(seq, trans)
                self.events.put(("trans", ts, trans, seq))

    def _write_translation(self, seq: int, trans: str) -> None:
        """把译文写回对应原文句之后（原文与译文始终成对相邻）。"""
        if self.transcript_path is None:
            return
        line = f"  - 译文：{trans}\n"
        with open(self.transcript_path, "r+", encoding="utf-8") as f:
            content = f.read()
            lines = content.splitlines(keepends=True)
            # 找到该 seq 对应的原文行位置（按原文行顺序定位）
            orig_idx = None
            seen = 0
            for i, ln in enumerate(lines):
                if ln.startswith("- **["):
                    seen += 1
                    if seen == seq:
                        orig_idx = i
                        break
            if orig_idx is None:
                f.seek(0, 2)
                f.write(line)
                return
            # 已有译文行则替换，否则插入
            j = orig_idx + 1
            if j < len(lines) and lines[j].startswith("  - 译文："):
                lines[j] = line
            else:
                lines.insert(j, line)
            f.seek(0)
            f.write("".join(lines))
            f.truncate()
