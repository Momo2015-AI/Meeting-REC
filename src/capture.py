"""Windows WASAPI Loopback 系统音频采集（soundcard 实现）。

抓取的是系统混音输出（扬声器环录），而非麦克风，
因此腾讯会议 / Zoom / WebEx / 本地播放等一切系统声音均可覆盖。
"""
from __future__ import annotations

import threading
import warnings
from typing import Callable, Optional, Tuple

try:  # 环录偶发断点属正常现象，屏蔽以免刷屏
    from soundcard.mediafoundation import SoundcardRuntimeWarning

    warnings.filterwarnings("ignore", category=SoundcardRuntimeWarning)
except Exception:  # noqa: BLE001
    pass


def list_devices() -> dict:
    """列出全部输出设备及其对应的 Loopback 环录设备。"""
    import soundcard as sc

    speakers, loopbacks = [], []
    for spk in sc.all_speakers():
        speakers.append((spk.name, str(spk.id)))
        try:
            lb = sc.get_microphone(str(spk.id), include_loopback=True)
            loopbacks.append((lb.name, str(lb.id)))
        except Exception:
            pass
    return {"speakers": speakers, "loopbacks": loopbacks}


class LoopbackCapture:
    """对默认输出设备做环录，输出 16kHz 单声道 16-bit PCM 字节流。

    生命周期刻意做成「延迟打开 + 阻塞采集」：
    - 构造时不碰音频设备（只记参数），避免程序一启动就占用系统输出设备；
    - open() 才真正解析设备句柄；
    - start() 在**当前线程**里跑采集循环，直到 stop() 被调用。
      这样调用方的"工作线程是否存活"就等价于"是否正在采集"，
      不会出现线程秒退导致状态判断失真的问题。
    """

    def __init__(self, sample_rate: int = 16000, chunk_ms: int = 100,
                 speaker_id: Optional[str] = None):
        self.sample_rate = sample_rate
        self.chunk_ms = chunk_ms
        self.speaker_id = speaker_id
        self._running = threading.Event()
        self._mic = None
        self._device_name = ""

    @property
    def device_name(self) -> str:
        return self._device_name

    @property
    def opened(self) -> bool:
        return self._mic is not None

    def open(self) -> str:
        """打开环录设备（幂等）。返回设备名。"""
        if self._mic is not None:
            return self._device_name
        import soundcard as sc

        try:
            if self.speaker_id:
                mic = sc.get_microphone(self.speaker_id, include_loopback=True)
            else:
                default = sc.default_speaker()
                mic = sc.get_microphone(str(default.id), include_loopback=True)
        except Exception as e:
            raise RuntimeError(
                f"无法打开环录设备：{e}\n请用 --list-devices 检查系统输出设备是否存在。"
            ) from e
        self._mic = mic
        self._device_name = mic.name
        return self._device_name

    def start(self, on_pcm: Callable[[bytes], None],
              on_error: Optional[Callable[[Exception], None]] = None) -> None:
        """阻塞式采集：在当前线程运行采集循环，直到 stop() 被调用。

        阻塞语义是刻意的——调用方（转写线程）存活即代表正在采集。
        """
        import numpy as np

        self.open()
        frames = max(1, int(self.sample_rate * self.chunk_ms / 1000))
        self._running.set()
        try:
            with self._mic.recorder(samplerate=self.sample_rate, channels=1) as rec:
                while self._running.is_set():
                    data = rec.record(numframes=frames)  # float32, shape (frames, 1)
                    mono = data.reshape(-1)
                    pcm = (mono * 32767.0).clip(-32768, 32767).astype("<i2").tobytes()
                    on_pcm(pcm)
        except Exception as e:  # noqa: BLE001
            if on_error is not None:
                on_error(e)
        finally:
            self._running.clear()

    def stop(self) -> None:
        """请求采集循环退出。下一帧（≤chunk_ms）后返回。"""
        self._running.clear()
