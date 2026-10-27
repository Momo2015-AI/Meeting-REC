"""Vosk 流式语音识别：ctypes 直绑 libvosk.dll。

不依赖 vosk Python 包（其依赖链在 Python 3.14 上不完整），
仅需 models/lib 下的四个 DLL：libvosk.dll / libstdc++-6.dll /
libgcc_s_seh-1.dll / libwinpthread-1.dll。
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
from typing import Optional


class VoskError(RuntimeError):
    pass


class VoskASR:
    """libvosk.dll 薄封装。KaldiRecognizer 非线程安全，内部加锁串行化。"""

    def __init__(self, lib_dir: str, model_dir: str, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._rec: Optional[ctypes.c_void_p] = None
        self._model: Optional[ctypes.c_void_p] = None

        lib_dir_abs = os.path.abspath(lib_dir)
        dll_path = os.path.join(lib_dir_abs, "libvosk.dll")
        if not os.path.isfile(dll_path):
            raise VoskError(f"未找到 {dll_path}，请按 README 放置 Vosk 运行时 DLL")
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(lib_dir_abs)
        try:
            self._lib = ctypes.CDLL(dll_path)
        except OSError as e:
            raise VoskError(f"加载 libvosk.dll 失败：{e}") from e

        L = self._lib
        L.vosk_model_new.restype = ctypes.c_void_p
        L.vosk_model_new.argtypes = [ctypes.c_char_p]
        L.vosk_recognizer_new.restype = ctypes.c_void_p
        L.vosk_recognizer_new.argtypes = [ctypes.c_void_p, ctypes.c_float]
        L.vosk_recognizer_set_words.restype = None
        L.vosk_recognizer_set_words.argtypes = [ctypes.c_void_p, ctypes.c_int]
        L.vosk_recognizer_accept_waveform.restype = ctypes.c_int
        L.vosk_recognizer_accept_waveform.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        L.vosk_recognizer_result.restype = ctypes.c_char_p
        L.vosk_recognizer_result.argtypes = [ctypes.c_void_p]
        L.vosk_recognizer_partial_result.restype = ctypes.c_char_p
        L.vosk_recognizer_partial_result.argtypes = [ctypes.c_void_p]
        L.vosk_recognizer_final_result.restype = ctypes.c_char_p
        L.vosk_recognizer_final_result.argtypes = [ctypes.c_void_p]
        L.vosk_recognizer_reset.restype = None
        L.vosk_recognizer_reset.argtypes = [ctypes.c_void_p]
        L.vosk_model_free.restype = None
        L.vosk_model_free.argtypes = [ctypes.c_void_p]
        L.vosk_recognizer_free.restype = None
        L.vosk_recognizer_free.argtypes = [ctypes.c_void_p]

        if not os.path.isdir(model_dir):
            raise VoskError(f"模型目录不存在：{model_dir}（按 README 下载并解压模型）")
        self._model = L.vosk_model_new(model_dir.encode("utf-8"))
        if not self._model:
            raise VoskError(f"模型加载失败：{model_dir}")
        self._rec = L.vosk_recognizer_new(self._model, float(sample_rate))
        if not self._rec:
            raise VoskError("识别器创建失败")
        L.vosk_recognizer_set_words(self._rec, 0)

    def accept(self, pcm: bytes) -> Optional[str]:
        """喂入 16kHz/单声道/16-bit PCM。返回 None=语句未结束；返回字符串=已完整一句。"""
        with self._lock:
            ended = self._lib.vosk_recognizer_accept_waveform(self._rec, pcm, len(pcm))
            if ended:
                raw = self._lib.vosk_recognizer_result(self._rec)
            else:
                return None
        try:
            return json.loads(raw.decode("utf-8", "replace")).get("text", "")
        except Exception:
            return ""

    def partial(self) -> str:
        with self._lock:
            raw = self._lib.vosk_recognizer_partial_result(self._rec)
        try:
            return json.loads(raw.decode("utf-8", "replace")).get("partial", "")
        except Exception:
            return ""

    def final(self) -> str:
        with self._lock:
            raw = self._lib.vosk_recognizer_final_result(self._rec)
        try:
            return json.loads(raw.decode("utf-8", "replace")).get("text", "")
        except Exception:
            return ""

    def drain_finals(self) -> list[str]:
        """流结束时冲刷残余，返回最后一句（无则空列表）。

        与 SenseVoiceStream.drain_finals 同名同义：VAD/解码器都要等到
        「确认没有后续语音」才会吐出最后一句，不冲刷就会丢掉会议最后一句。
        """
        text = self.final()
        return [text] if text.strip() else []

    def reset(self) -> None:
        with self._lock:
            self._lib.vosk_recognizer_reset(self._rec)

    def close(self) -> None:
        with self._lock:
            if self._rec:
                self._lib.vosk_recognizer_free(self._rec)
                self._rec = None
            if self._model:
                self._lib.vosk_model_free(self._model)
                self._model = None

    def __enter__(self) -> "VoskASR":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
