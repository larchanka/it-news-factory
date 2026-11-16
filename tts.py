from __future__ import annotations

import asyncio
import logging
import math
import struct
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import soundfile as sf

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "am_adam"
DEFAULT_SPEED = 1.08
SAMPLE_RATE = 24000
SAMPLE_WIDTH = 2  # 16-bit PCM
CHANNELS = 1  # Mono

KOKORO_MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v0_19.onnx"
KOKORO_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
)


@dataclass
class AudioMeta:
    wav_path: str
    duration_seconds: float


def get_audio_duration(file_path: str | Path) -> float:
    """Read exact audio duration in seconds from a WAV file.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is not a valid WAV file.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"Audio file not found: {path}")

    try:
        with wave.open(str(path), "rb") as wf:
            framerate = wf.getframerate()
            nframes = wf.getnframes()
            if framerate <= 0:
                raise ValueError(f"Invalid framerate in audio file: {framerate}")
            return nframes / float(framerate)
    except wave.Error as e:
        raise ValueError(f"Failed to read WAV audio file '{path}': {e}") from e


def is_kokoro_model_available(
    models_dir: str | Path = "models",
    model_filename: str = "kokoro-v0_19.onnx",
    voices_filename: str = "voices.bin",
) -> bool:
    """Check if Kokoro ONNX model weights and voices files exist."""
    dir_path = Path(models_dir)
    model_file = dir_path / model_filename
    voices_file = dir_path / voices_filename
    alt_voices_file = dir_path / "voices.json"
    alt_v1_voices_file = dir_path / "voices-v1.0.bin"
    has_voices = voices_file.is_file() or alt_voices_file.is_file() or alt_v1_voices_file.is_file()
    return model_file.is_file() and has_voices


def _download_file(url: str, target_path: Path, timeout: float = 30.0) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_suffix(f"{target_path.suffix}.tmp")
    try:
        with (
            urllib.request.urlopen(url, timeout=timeout) as response,
            open(temp_path, "wb") as out_file,
        ):
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                out_file.write(chunk)
        temp_path.replace(target_path)
    except Exception as e:
        if temp_path.exists():
            temp_path.unlink()
        raise RuntimeError(
            f"Failed to download Kokoro model asset from '{url}' to '{target_path}': {e}.\n"
            f"Please download manually or run with mock/offline fallback."
        ) from e


def ensure_kokoro_model(
    models_dir: str | Path = "models",
    download_if_missing: bool = False,
    model_filename: str = "kokoro-v0_19.onnx",
    voices_filename: str = "voices.bin",
    model_url: str = KOKORO_MODEL_URL,
    voices_url: str = KOKORO_VOICES_URL,
) -> tuple[Path, Path]:
    """Ensure Kokoro ONNX model weights and voices files are present.

    Returns:
        tuple[Path, Path]: Tuple containing (model_path, voices_path).
    Raises:
        FileNotFoundError: If model files are missing and download_if_missing is False.
        RuntimeError: If download fails due to network/offline error.
    """
    dir_path = Path(models_dir)
    model_path = dir_path / model_filename
    voices_path = dir_path / voices_filename

    if not voices_path.is_file():
        if (dir_path / "voices-v1.0.bin").is_file():
            voices_path = dir_path / "voices-v1.0.bin"
        elif (dir_path / "voices.json").is_file():
            voices_path = dir_path / "voices.json"

    model_exists = model_path.is_file()
    voices_exists = voices_path.is_file()

    if model_exists and voices_exists:
        return model_path, voices_path

    if not download_if_missing:
        raise FileNotFoundError(
            f"Kokoro TTS model files missing in '{dir_path}'.\n"
            f"Required: '{model_filename}' and '{voices_filename}'.\n"
            f"To download automatically, call ensure_kokoro_model(download_if_missing=True),\n"
            f"or place weights manually from {model_url}."
        )

    # Download missing files
    if not model_exists:
        _download_file(model_url, model_path)
    if not voices_exists:
        _download_file(voices_url, voices_path)

    return model_path, voices_path


def _generate_mock_wav(
    output_path: Path,
    duration_seconds: float,
    sample_rate: int = SAMPLE_RATE,
) -> None:
    """Generate a clean 24kHz 16-bit mono WAV with subtle tone/silence for fallback testing."""
    num_frames = max(1, int(duration_seconds * sample_rate))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(sample_rate)

        # Generate a gentle, faint 440Hz sine wave to be measurable by tools
        # amplitude 500 out of 32767
        chunk_size = 2400
        frames_written = 0
        while frames_written < num_frames:
            current_chunk = min(chunk_size, num_frames - frames_written)
            raw_bytes = bytearray()
            for i in range(current_chunk):
                t = (frames_written + i) / float(sample_rate)
                val = int(500 * math.sin(2.0 * math.pi * 440.0 * t))
                raw_bytes.extend(struct.pack("<h", val))
            wf.writeframes(raw_bytes)
            frames_written += current_chunk


class LocalTTS:
    """Local Kokoro TTS synthesizer generating 24kHz 16-bit mono WAV narration."""

    def __init__(
        self,
        model_path: str | Path | None = None,
        voices_path: str | Path | None = None,
        voice_name: str = DEFAULT_VOICE,
        speed: float = DEFAULT_SPEED,
        fallback_mock: bool = True,
    ) -> None:
        self.model_path = Path(model_path) if model_path else None
        self.voices_path = Path(voices_path) if voices_path else None
        self.voice_name = voice_name
        self.speed = max(0.5, float(speed))
        self.fallback_mock = fallback_mock
        self._kokoro: Any | None = None

    def _has_model_files(self) -> bool:
        return bool(
            self.model_path
            and self.model_path.is_file()
            and self.voices_path
            and self.voices_path.is_file()
        )

    def _get_kokoro_session(self) -> Any:
        if self._kokoro is None:
            import kokoro_onnx

            if not self.model_path or not self.voices_path:
                raise FileNotFoundError(
                    "Model path and voices path must be specified to initialize Kokoro session."
                )
            self._kokoro = kokoro_onnx.Kokoro(str(self.model_path), str(self.voices_path))
        return self._kokoro

    def _mock_synthesize(self, text: str, dest_path: Path, speed: float) -> AudioMeta:
        logger.warning(
            "Kokoro ONNX model weights not loaded or failed. Falling back to synthetic mock audio generation."
        )
        words = text.strip().split()
        word_count = max(1, len(words))
        duration = max(0.8, (word_count / 2.5) / speed)
        _generate_mock_wav(dest_path, duration)
        measured_duration = get_audio_duration(dest_path)
        return AudioMeta(wav_path=str(dest_path), duration_seconds=measured_duration)

    def synthesize(
        self,
        text: str,
        output_wav_path: str | Path,
        voice: str | None = None,
        speed: float | None = None,
    ) -> AudioMeta:
        dest_path = Path(output_wav_path).resolve()
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        chosen_voice = voice or self.voice_name
        chosen_speed = max(0.5, float(speed if speed is not None else self.speed))

        if not self._has_model_files():
            if not self.fallback_mock:
                raise FileNotFoundError(
                    f"Kokoro model files not found: model='{self.model_path}', voices='{self.voices_path}'."
                )
            return self._mock_synthesize(text, dest_path, chosen_speed)

        try:
            kokoro_session = self._get_kokoro_session()
            samples, sample_rate = kokoro_session.create(
                text=text,
                voice=chosen_voice,
                speed=chosen_speed,
            )
            sf.write(str(dest_path), samples, sample_rate, subtype="PCM_16")
            measured_duration = get_audio_duration(dest_path)
            return AudioMeta(wav_path=str(dest_path), duration_seconds=measured_duration)
        except Exception:
            if not self.fallback_mock:
                raise
            return self._mock_synthesize(text, dest_path, chosen_speed)


def synthesize_speech(
    text: str,
    output_path: str | Path,
    voice: str | None = None,
    speed: float | None = None,
    config: AppConfig | None = None,
    fallback_mock: bool = True,
) -> float:
    """Synthesize text into a 24kHz mono WAV and return exact duration T in seconds."""
    model_path = config.tts.model_path if config else None
    voices_path = config.tts.voices_path if config else None
    default_voice = config.tts.voice if config else DEFAULT_VOICE
    default_speed = config.tts.speed if config else DEFAULT_SPEED

    chosen_voice = voice or default_voice
    chosen_speed = speed if speed is not None else default_speed

    tts = LocalTTS(
        model_path=model_path,
        voices_path=voices_path,
        voice_name=chosen_voice,
        speed=chosen_speed,
        fallback_mock=fallback_mock,
    )
    meta = tts.synthesize(text, output_path, voice=chosen_voice, speed=chosen_speed)
    return meta.duration_seconds


async def async_synthesize_speech(
    text: str,
    output_path: str | Path,
    voice: str | None = None,
    speed: float | None = None,
    config: AppConfig | None = None,
    fallback_mock: bool = True,
) -> float:
    """Asynchronously synthesize text using worker thread executor."""
    return await asyncio.to_thread(
        synthesize_speech,
        text=text,
        output_path=output_path,
        voice=voice,
        speed=speed,
        config=config,
        fallback_mock=fallback_mock,
    )
