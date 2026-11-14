import ctypes
import threading
import time
import wave
from collections import deque
from ctypes import wintypes
from pathlib import Path


class AudioPlaybackError(RuntimeError):
    pass


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.POINTER(ctypes.c_char)),
        ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD),
        ("dwUser", ctypes.c_size_t),
        ("dwFlags", wintypes.DWORD),
        ("dwLoops", wintypes.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_size_t),
    ]


WAVE_FORMAT_PCM = 1
WAVE_MAPPER = 0xFFFFFFFF
CALLBACK_NULL = 0
MMSYSERR_NOERROR = 0
WHDR_DONE = 0x00000001


class _PCMBuffer(object):
    def __init__(self, data, winmm, device):
        self.data = ctypes.create_string_buffer(data)
        self.header = WAVEHDR()
        self.header.lpData = ctypes.cast(
            self.data,
            ctypes.POINTER(ctypes.c_char),
        )
        self.header.dwBufferLength = len(data)
        result = winmm.waveOutPrepareHeader(
            device,
            ctypes.byref(self.header),
            ctypes.sizeof(self.header),
        )
        if result != MMSYSERR_NOERROR:
            raise AudioPlaybackError(
                "准备音频播放缓冲失败（错误码 {}）。".format(result)
            )
        self.winmm = winmm
        self.device = device
        self.prepared = True

    def done(self):
        return bool(self.header.dwFlags & WHDR_DONE)

    def close(self):
        if not self.prepared:
            return
        self.winmm.waveOutUnprepareHeader(
            self.device,
            ctypes.byref(self.header),
            ctypes.sizeof(self.header),
        )
        self.prepared = False


class PCMStreamPlayer(object):
    """使用 Windows waveOut 连续播放 PCM，避免逐段播放产生间隙。"""

    CHUNK_SECONDS = 1.5
    WRITE_WAIT_TIMEOUT_SECONDS = 5.0
    DEVICE_STALL_TIMEOUT_SECONDS = 4.0

    def __init__(
        self,
        prebuffer_seconds=0.35,
        max_buffer_seconds=8.0,
        muted=False,
    ):
        self.prebuffer_seconds = max(0.1, float(prebuffer_seconds))
        self.max_buffer_seconds = max(
            self.prebuffer_seconds,
            float(max_buffer_seconds),
        )
        self.muted = bool(muted)
        self._condition = threading.Condition()
        self._chunks = deque()
        self._queued_bytes = 0
        self._format = None
        self._bytes_per_second = 0
        self._input_finished = False
        self._aborted = False
        self._closed = False
        self._started = False
        self._paused = False
        self._error = None
        self._done = threading.Event()
        self._device = wintypes.HANDLE()
        self._device_open = False
        self._winmm = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def started(self):
        with self._condition:
            return self._started

    @property
    def error(self):
        with self._condition:
            return self._error

    @property
    def done(self):
        return self._done.is_set()

    def write(self, data, sample_rate, channels=1, sample_width=2):
        payload = bytes(data or b"")
        if not payload:
            return
        format_info = (int(sample_rate), int(channels), int(sample_width))
        if min(format_info) <= 0:
            raise AudioPlaybackError("PCM 音频格式无效。")
        with self._condition:
            if self._aborted or self._closed:
                raise AudioPlaybackError("音频播放已经结束。")
            if self._error is not None:
                raise AudioPlaybackError(self._error)
            if self._format is None:
                self._format = format_info
                self._bytes_per_second = (
                    format_info[0] * format_info[1] * format_info[2]
                )
            elif format_info != self._format:
                raise AudioPlaybackError("同一次回答中的 PCM 音频格式不一致。")
            self._chunks.append((payload, format_info))
            self._queued_bytes += len(payload)
            self._condition.notify_all()
            maximum = int(
                self.max_buffer_seconds * self._bytes_per_second
            )
            wait_deadline = time.monotonic() + self.WRITE_WAIT_TIMEOUT_SECONDS
            while (
                self._queued_bytes > maximum
                and not self._aborted
                and not self._closed
                and self._error is None
            ):
                remaining = wait_deadline - time.monotonic()
                if remaining <= 0:
                    message = "音频播放缓冲区长时间未消费。"
                    self._error = message
                    self._condition.notify_all()
                    raise AudioPlaybackError(message)
                self._condition.wait(timeout=min(0.2, remaining))
            if self._error is not None:
                raise AudioPlaybackError(self._error)

    def finish(self):
        with self._condition:
            self._input_finished = True
            self._condition.notify_all()

    def pause(self):
        with self._condition:
            if self._aborted or self._closed:
                return
            self._paused = True
            winmm = self._winmm
            device = self._device
            device_open = self._device_open
        if device_open and winmm is not None:
            result = winmm.waveOutPause(device)
            if result != MMSYSERR_NOERROR:
                raise AudioPlaybackError(
                    "暂停音频播放失败（错误码 {}）。".format(result)
                )

    def resume(self):
        with self._condition:
            if self._aborted or self._closed:
                return
            self._paused = False
            winmm = self._winmm
            device = self._device
            device_open = self._device_open
        if device_open and winmm is not None:
            result = winmm.waveOutRestart(device)
            if result != MMSYSERR_NOERROR:
                raise AudioPlaybackError(
                    "继续音频播放失败（错误码 {}）。".format(result)
                )

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise AudioPlaybackError("等待语音播放完成超时。")
        with self._condition:
            if self._error is not None:
                raise AudioPlaybackError(self._error)

    def abort(self):
        with self._condition:
            if self._closed:
                return
            self._aborted = True
            self._chunks.clear()
            self._queued_bytes = 0
            self._condition.notify_all()
        if self._device_open and self._winmm is not None:
            try:
                self._winmm.waveOutReset(self._device)
            except OSError:
                pass

    def _run(self):
        active = deque()
        last_progress_at = time.monotonic()

        def reap_and_check():
            nonlocal last_progress_at
            reaped = self._reap_buffers(active)
            if reaped or not active or self._paused:
                last_progress_at = time.monotonic()
            head_seconds = (
                active[0].header.dwBufferLength / float(self._bytes_per_second)
                if active and self._bytes_per_second else 0
            )
            if (
                active
                and not self._paused
                and time.monotonic() - last_progress_at
                > max(self.DEVICE_STALL_TIMEOUT_SECONDS, head_seconds + 1.0)
            ):
                raise AudioPlaybackError(
                    "音频设备在播放中途停止响应，已停止卡住的播放。"
                )

        try:
            while True:
                with self._condition:
                    if self._aborted:
                        break
                    reap_and_check()
                    # Bound the device queue too, not just the producer queue.
                    active_bytes = sum(item.header.dwBufferLength for item in active)
                    if self._paused or (
                        active and active_bytes >= self.max_buffer_seconds * self._bytes_per_second
                    ):
                        self._condition.wait(timeout=0.01)
                        continue
                    if not self._started:
                        if not self._chunks:
                            if self._input_finished:
                                break
                            self._condition.wait(timeout=0.1)
                            continue
                        prebuffer_bytes = int(
                            self.prebuffer_seconds * self._bytes_per_second
                        )
                        if (
                            self._queued_bytes < prebuffer_bytes
                            and not self._input_finished
                        ):
                            self._condition.wait(timeout=0.1)
                            continue
                        self._started = True
                    if self._chunks:
                        data, format_info = self._chunks.popleft()
                        self._queued_bytes -= len(data)
                        self._condition.notify_all()
                    elif self._input_finished and not active:
                        break
                    else:
                        self._condition.wait(timeout=0.05)
                        reap_and_check()
                        continue
                if not active:
                    last_progress_at = time.monotonic()
                active.append(self._write_buffer(data, format_info))
                reap_and_check()
        except Exception as error:
            with self._condition:
                self._error = str(error)
        finally:
            if (
                (self._aborted or self._error is not None)
                and self._device_open
                and self._winmm is not None
            ):
                try:
                    self._winmm.waveOutReset(self._device)
                except OSError:
                    pass
            deadline = time.monotonic() + max(5.0, self.max_buffer_seconds + 3.0)
            while active and time.monotonic() < deadline:
                if not self._reap_buffers(active):
                    time.sleep(0.01)
            if active and self._device_open and self._winmm is not None:
                try:
                    self._winmm.waveOutReset(self._device)
                except OSError:
                    pass
                self._reap_buffers(active)
            if active:
                with self._condition:
                    if self._error is None:
                        self._error = (
                            "音频播放缓冲区未能正常回收，已停止卡住的播放。"
                        )
            for item in active:
                try:
                    item.close()
                except OSError:
                    pass
            if self._device_open and self._winmm is not None:
                try:
                    self._winmm.waveOutClose(self._device)
                except OSError:
                    pass
            with self._condition:
                self._closed = True
                self._condition.notify_all()
            self._done.set()

    def _open_device(self, format_info):
        if self._device_open:
            return
        winmm = ctypes.WinDLL("winmm")
        winmm.waveOutOpen.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.UINT,
            ctypes.POINTER(WAVEFORMATEX),
            ctypes.c_size_t,
            ctypes.c_size_t,
            wintypes.DWORD,
        ]
        winmm.waveOutOpen.restype = wintypes.UINT
        winmm.waveOutPrepareHeader.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(WAVEHDR),
            wintypes.UINT,
        ]
        winmm.waveOutPrepareHeader.restype = wintypes.UINT
        winmm.waveOutWrite.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(WAVEHDR),
            wintypes.UINT,
        ]
        winmm.waveOutWrite.restype = wintypes.UINT
        winmm.waveOutUnprepareHeader.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(WAVEHDR),
            wintypes.UINT,
        ]
        winmm.waveOutUnprepareHeader.restype = wintypes.UINT
        winmm.waveOutReset.argtypes = [wintypes.HANDLE]
        winmm.waveOutReset.restype = wintypes.UINT
        winmm.waveOutPause.argtypes = [wintypes.HANDLE]
        winmm.waveOutPause.restype = wintypes.UINT
        winmm.waveOutRestart.argtypes = [wintypes.HANDLE]
        winmm.waveOutRestart.restype = wintypes.UINT
        winmm.waveOutClose.argtypes = [wintypes.HANDLE]
        winmm.waveOutClose.restype = wintypes.UINT
        winmm.waveOutSetVolume.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        winmm.waveOutSetVolume.restype = wintypes.UINT

        sample_rate, channels, sample_width = format_info
        block_align = channels * sample_width
        wave_format = WAVEFORMATEX(
            WAVE_FORMAT_PCM,
            channels,
            sample_rate,
            sample_rate * block_align,
            block_align,
            sample_width * 8,
            0,
        )
        result = winmm.waveOutOpen(
            ctypes.byref(self._device),
            WAVE_MAPPER,
            ctypes.byref(wave_format),
            0,
            0,
            CALLBACK_NULL,
        )
        if result != MMSYSERR_NOERROR:
            raise AudioPlaybackError(
                "无法打开 Windows 音频输出（错误码 {}）。".format(result)
            )
        self._winmm = winmm
        self._device_open = True
    def _write_buffer(self, data, format_info):
        self._open_device(format_info)
        if self.muted:
            data = bytes(len(data))
        item = _PCMBuffer(
            data,
            self._winmm,
            self._device,
        )
        result = self._winmm.waveOutWrite(
            self._device,
            ctypes.byref(item.header),
            ctypes.sizeof(item.header),
        )
        if result != MMSYSERR_NOERROR:
            item.close()
            raise AudioPlaybackError(
                "写入音频播放队列失败（错误码 {}）。".format(result)
            )
        return item

    @staticmethod
    def _reap_buffers(active):
        reaped = 0
        while active and active[0].done():
            active.popleft().close()
            reaped += 1
        return reaped


def write_pcm_wav(
    output_path,
    pcm,
    sample_rate,
    channels=1,
    sample_width=2,
):
    """把连续 PCM 数据归档成可重播的标准 WAV 文件。"""
    payload = bytes(pcm or b"")
    sample_rate = int(sample_rate)
    channels = int(channels)
    sample_width = int(sample_width)
    if not payload:
        raise AudioPlaybackError("没有可归档的语音数据。")
    if min(sample_rate, channels, sample_width) <= 0:
        raise AudioPlaybackError("归档音频格式无效。")
    block_align = channels * sample_width
    payload = payload[: len(payload) - (len(payload) % block_align)]
    if not payload:
        raise AudioPlaybackError("语音数据长度无效。")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(payload)
    return output_path
