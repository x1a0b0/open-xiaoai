import threading
import time

from config import APP_CONFIG
from xiaozhi.event import EventManager
from xiaozhi.ref import set_vad
from xiaozhi.services.audio.stream import MyAudio
from xiaozhi.services.audio.vad.silero import Silero
from xiaozhi.services.protocols.typing import AudioConfig
from xiaozhi.utils.base import get_env

# Performance tuning constants
SLEEP_INTERVAL_PAUSED = 0.05  # Sleep when VAD is paused (50ms)
SLEEP_INTERVAL_ACTIVE = 0.005  # Sleep during active detection (5ms)
BYTES_PER_SAMPLE = 2  # 16-bit audio = 2 bytes per sample


class _VAD:
    def __init__(self):
        set_vad(self)

        config = APP_CONFIG.get("vad", {})

        # 参数设置
        self.sample_rate = 16000
        self.frame_size = 512
        self.threshold = config.get("threshold", 0.01)
        self.min_speech_duration = config.get("min_speech_duration", 250)
        self.min_silence_duration = config.get("min_silence_duration", 500)

        # 状态变量
        self.paused = True
        self.thread = None
        self.speech_count = 0
        self.silence_count = 0

        self.audio = None
        self.stream = None

        # 暂存的语音片段
        self.silence_frames = bytearray()  # 静音片段
        self.speech_frames = bytearray()  # 语音片段
        self.target = None  # 检测目标 speech/silence

    def _reset_state(self):
        """重置状态"""
        self.speech_count = 0
        self.silence_count = 0
        self.speech_frames = bytearray()
        self.silence_frames = bytearray()

    def start(self):
        """启动VAD检测器"""
        if not get_env("CLI"):
            return

        self._initialize_audio_stream()

        # 启动检测线程
        self.paused = False
        self.thread = threading.Thread(target=self._detection_loop, daemon=True)
        self.thread.start()

    def pause(self):
        """暂停VAD检测"""
        if not get_env("CLI"):
            return

        self.paused = True
        self._reset_state()
        self.stream.stop_stream()

    def resume(self, target: str):
        """恢复VAD检测"""
        if not get_env("CLI"):
            return

        self.paused = False
        self.target = target
        self.stream.start_stream()

    def _handle_speech_frame(self, frames):
        """处理语音帧"""
        self.speech_count += len(frames)
        self.silence_count = 0

        if self.target == "speech":
            if not self.speech_frames:
                # 加入静音片段（潜在的语音片段）
                self.speech_frames += self.silence_frames

        # 加入语音片段
        self.speech_frames += frames

        speech_bytes = bytes(self.speech_frames)

        if (
            self.target == "speech"
            and self.speech_count > self.min_speech_duration * self.sample_rate / 1000
        ):
            self.pause()
            EventManager.on_speech(speech_bytes)

    def _handle_silence_frame(self, frames):
        """处理静音帧"""
        self.silence_count += len(frames)
        self.speech_count = 0

        if self.target == "speech":
            if not self.speech_frames:
                # 如果之前没有语音片段，则将当前帧加入静音片段
                self.silence_frames += frames
                # 确保静音片段长度不超过 1s
                max_len = BYTES_PER_SAMPLE * self.sample_rate
                if len(self.silence_frames) > max_len:
                    del self.silence_frames[:-max_len]
            else:
                # 如果之前有语音片段，则将当前帧加入语音片段
                self.speech_frames += frames

        if (
            self.target == "silence"
            and self.silence_count > self.min_silence_duration * self.sample_rate / 1000
        ):
            self.pause()
            EventManager.on_silence()

    def _initialize_audio_stream(self):
        """初始化独立的音频流"""
        try:
            # 创建 PyAudio 实例
            self.audio = MyAudio.create()
            # 创建输入流
            self.stream = self.audio.open(
                format=AudioConfig.FORMAT,
                channels=1,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.frame_size,
                start=True,
            )
            return True
        except Exception:
            return False

    def _close_audio_stream(self):
        """关闭音频流"""
        try:
            if self.stream:
                self.stream.stop_stream()
                self.stream.close()
                self.stream = None

            if self.audio:
                self.audio.terminate()
                self.audio = None

        except Exception:
            pass

    def _detection_loop(self):
        """VAD检测主循环"""
        while True:
            # 如果暂停或者音频流未初始化，则跳过
            if self.paused or not self.stream:
                time.sleep(SLEEP_INTERVAL_PAUSED)  # Reduced CPU usage when paused
                continue

            # 读取缓冲区音频数据
            frames = self.stream.read(self.frame_size)
            if len(frames) != self.frame_size * BYTES_PER_SAMPLE:
                time.sleep(SLEEP_INTERVAL_ACTIVE)  # Shorter sleep for faster response
                continue

            # 检测是否是语音
            speech_prob = Silero.vad(frames, self.sample_rate) or 0
            is_speech = speech_prob >= self.threshold
            if is_speech:
                self._handle_speech_frame(frames)
            else:
                self._handle_silence_frame(frames)

            # Small sleep to prevent CPU saturation while maintaining responsiveness
            time.sleep(SLEEP_INTERVAL_ACTIVE)


VAD = _VAD()
