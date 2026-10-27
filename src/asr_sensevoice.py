"""SenseVoice-Small 块级语音识别（非流式模型 + 流式 Fsmn-VAD 切块）。

设计：
- 环录 PCM 每 100ms 一段喂入 accept()；内部用 funasr_onnx.Fsmn_vad_online
  流式检测停顿：分段（[start_ms, end_ms]，**绝对毫秒**、自评分器创建时刻起算，
  -1 表示"起/终点不在本次批次"）闭合时，从自维护的「当前纪元累计波形缓冲」
  按 ms*样点率 切出语音段，交给 SenseVoice 推理（CPU int8 RTF≈0.03，
  实测 8.3s 音频 0.2s 出结果），返回带标点的整句 → 剥标签后作为 final 上报。
- 与 VoskASR 同接口：accept(pcm)->Optional[str]（None=尚未成句）、partial()=""。
  transcriber.py 以 [asr] engine 开关切换两套引擎，管线其余部分完全复用。
- 段闭合后**整体重建** VAD 评分器（start_ms/end_ms 参考系随之归零），
  断点之后的残留音频作为下一纪元的初始缓冲继续喂——映射恒一致。
- 超长强制断句：buffer 超过 max_block_s 时用空块+is_final 让评分器回冲出句。

★ 本机可运行性（2026-10-27 逐项实测，见 README「SenseVoice 可用性排查」）：
  1) 必须 Python ≤ 3.13。onnxruntime==1.20.1 只发到 cp313；cp314 的
     onnxruntime 加载 pybind11 扩展会失败（DLL 初始化例程失败 / 0xC0000005）。
     推荐 CPython 3.12（onnxruntime 1.20.1 有 cp312 轮子）。
  2) sentencepiece **必须钉 0.2.0**：0.2.1 / 0.2.2 的 SentencePieceProcessor.Load()
     在本机一律 access violation（段错误，exit 139），且与文件是否被加密无关——
     LoadFromSerializedProto(内存 bytes) 同样崩，崩点在 pyd 内部。
     0.2.0 有 cp312 Windows 轮子且加载正常。
  3) funasr-onnx 声明 numpy<=1.26.4，但 1.26.4 无 cp313 轮子；
     实际装 numpy==2.2.6 并对 funasr-onnx 用 --no-deps 绕过（运行时无兼容问题）。

依赖（见 requirements.txt 的 SenseVoice 段）：
  funasr-onnx + onnxruntime + sentencepiece==0.2.0 + modelscope
模型：python scripts/download_sensevoice.py（含导出 onnx）。
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Optional

import numpy as np

_TAG_RE = re.compile(r"<\|[^|<>]*\|>")
_LANGS = ("auto", "zh", "en", "ja", "ko", "yue", "nospeech")


class SenseVoiceError(RuntimeError):
    pass


class SenseVoiceStream:
    """与 VoskASR 同 accept() 接口的 SenseVoice 块级识别器。"""

    def __init__(self, model_dir: str | Path, vad_dir: str | Path,
                 sample_rate: int = 16000, threads: int = 4,
                 lang: str = "auto", max_block_s: float = 6.0,
                 min_block_s: float = 0.3, textnorm: str = "woitn"):
        if sample_rate != 16000:
            raise SenseVoiceError("SenseVoice 管线固定 16 kHz 采样")
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._lang = lang if lang in _LANGS else "auto"
        # funasr_onnx 的 textnorm 只认 withitn/woitn；其他值（如 itn）会导致空输出，兜底
        self._textnorm = textnorm if textnorm in ("withitn", "woitn") else "woitn"
        self._max_block = int(float(max_block_s) * sample_rate)
        self._min_block = int(float(min_block_s) * sample_rate)

        for p in (model_dir, vad_dir):
            if not Path(p).is_dir():
                raise SenseVoiceError(f"模型目录不存在：{p}（先跑 scripts/download_sensevoice.py）")
        try:
            from funasr_onnx import SenseVoiceSmall, Fsmn_vad_online
        except Exception as e:  # noqa: BLE001
            raise SenseVoiceError(f"funasr-onnx 导入失败：{e}") from e
        try:
            self._asr = SenseVoiceSmall(str(Path(model_dir).resolve()),
                                        quantize=True,
                                        intra_op_num_threads=max(1, int(threads or 4)))
            self._vad = Fsmn_vad_online(str(Path(vad_dir).resolve()),
                                        quantize=True,
                                        intra_op_num_threads=max(1, int(threads or 4)))
        except Exception as e:  # noqa: BLE001
            raise SenseVoiceError(f"SenseVoice 模型加载失败：{e}") from e

        # 流式 VAD 状态（funasr_onnx 约定：向前端/评分器状态字典贯穿喂入）
        self._pd: dict = {}
        # 当前纪元（=评分器生命期）自累计的原始 PCM 波形，float32 [0,1)
        self._buf = np.empty(0, dtype=np.float32)
        # 已知的语音起点（ms）；-1 尚未获知
        self._start_ms = -1
        # 待消费的闭合分段 [(start_ms, end_ms), ...]
        self._segq: list[tuple[int, int]] = []
        # 已识别但尚未被取走的整句文本
        self._pending: list[str] = []

    # ---------------- 对外接口（与 VoskASR 相同） ----------------

    def accept(self, pcm: bytes) -> Optional[str]:
        """喂 16kHz/单声道/16-bit PCM。返回 None=尚未成句；str=完整一句文本。"""
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        with self._lock:
            self._feed(audio)
            return self._take()

    def partial(self) -> str:
        """SenseVoice 块级模式没有 partial；提前量交给 VAD 断句速度。"""
        return ""

    def final(self) -> str:
        """流结束时冲刷残余：返回最后一句（无则空串）。

        VAD 只在检测到后续静音时才闭合分段，因此最后一段语音要等到
        is_final 冲刷才会吐出——不调用本方法会丢掉会议的最后一句。
        """
        with self._lock:
            try:
                self._segq.extend(self._vad_step(np.empty(0, dtype=np.float32), is_final=True))
                self._drain()
            except Exception:  # noqa: BLE001
                pass
            return self._pending[0] if self._pending else ""

    def drain_finals(self) -> list[str]:
        """取走所有已就绪的整句（含 final 冲刷结果）。"""
        with self._lock:
            try:
                self._segq.extend(self._vad_step(np.empty(0, dtype=np.float32), is_final=True))
                self._drain()
            except Exception:  # noqa: BLE001
                pass
            out, self._pending = self._pending, []
            return out

    def reset(self) -> None:
        with self._lock:
            self._pd = {}
            self._buf = np.empty(0, dtype=np.float32)
            self._start_ms = -1
            self._segq.clear()
            self._pending.clear()

    def close(self) -> None:
        pass

    def __enter__(self) -> "SenseVoiceStream":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------------- 内部：VAD 状态机 ----------------

    def _take(self) -> Optional[str]:
        """取走一句已识别文本（队列空则 None）。"""
        if not self._pending:
            return None
        return self._pending.pop(0)

    def _feed(self, audio: np.ndarray) -> None:
        """喂入波形并把闭合分段转成整句文本。"""
        if audio.size:
            self._buf = np.concatenate((self._buf, audio))
        self._segq.extend(self._vad_step(audio, is_final=False))

        # 超长强制断句（长句无停顿也照发，保证翻译时效）
        if not self._segq and self._buf.shape[0] >= self._max_block:
            self._segq.extend(self._vad_step(np.empty(0, dtype=np.float32), is_final=True))
            if not self._segq:
                # 空块回冲失效（罕见）：丢弃过长的缓冲防内存累积
                self._reset_epoch(self._buf.shape[0], keep_tail=False)
                return
        self._drain()

    def _drain(self) -> None:
        """把待处理分段逐个识别成整句（识别期间可能再产生新分段）。"""
        while self._segq:
            start_ms, end_ms = self._segq.pop(0)
            self._pending.append(self._emit(start_ms, end_ms))

    def _vad_step(self, chunk: np.ndarray, is_final: bool) -> list[tuple[int, int]]:
        """喂一段波形；返回本次闭合的分段列表。

        funasr_onnx 的在线输出有两类：
          [start_ms, -1]  语音起点（本批未结束）→ 只记录起点；
          [-1, end_ms]    语音终点（起点在更早的批次里）→ 与已记起点配对成闭合段。
        """
        pd = self._pd
        pd.setdefault("in_cache", [])
        pd["is_final"] = bool(is_final)
        try:
            raw = self._vad(np.ascontiguousarray(chunk), param_dict=pd)
        except Exception:  # noqa: BLE001
            # 空音频/纯噪声：funasr_onnx 抛 ONNXRuntimeError → 无断句
            pd["is_final"] = False
            return []
        pd["is_final"] = False

        segs: list[tuple[int, int]] = []
        for batch in raw or []:
            for seg in batch or []:
                if len(seg) < 2:
                    continue
                a, b = int(seg[0]), int(seg[1])
                if a > 0 and b < 0:
                    self._start_ms = a          # 本段起点获知、尚未结束
                elif b > 0:
                    start = a if a > 0 else self._start_ms
                    segs.append((start if start > 0 else 0, b))
        return segs

    def _emit(self, start_ms: int, end_ms: int) -> str:
        """切出 [start_ms, end_ms] 语音段送识别，并切纪元。"""
        begin = int(start_ms * 16)
        end = min(int(end_ms * 16), self._buf.shape[0])
        text = ""
        if end - begin >= self._min_block:
            seg = self._buf[begin:end]
            try:
                # 注意：funasr_onnx 只接受裸 ndarray / 文件路径；包成 list 会走文件分支
                res = self._asr(np.ascontiguousarray(seg),
                                language=self._lang, textnorm=self._textnorm)
                text = _TAG_RE.sub("", str(res[0]).strip()) if res else ""
            except Exception:  # noqa: BLE001
                text = ""
        # 纪元切换：断点之后的残留音频带入下一纪元（尾部静音通常几十~几百毫秒，
        # 必须保留——VAD 需要它来确认"下一句还没开始"）。
        self._segq.extend(self._reset_epoch(end, keep_tail=True))
        return text

    def _reset_epoch(self, cut_samples: int, keep_tail: bool) -> list[tuple[int, int]]:
        """重建评分器；keep_tail 时把断点后的残留音频带入下一纪元。

        返回残留音频回灌时新产生的分段（若有）。
        """
        tail = self._buf[int(cut_samples):] if keep_tail else np.empty(0, dtype=np.float32)
        self._pd = {}
        self._buf = np.ascontiguousarray(tail)
        self._start_ms = -1
        if not tail.size:
            return []
        try:
            # 残留是上一纪元已过度尾部（通常为尾静音），直接给新纪元过一遍状态
            return self._vad_step(np.ascontiguousarray(tail), is_final=False)
        except Exception:  # noqa: BLE001
            return []
