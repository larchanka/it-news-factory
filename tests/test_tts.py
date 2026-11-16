import wave
from pathlib import Path

import pytest

from config import AppConfig
from tts import (
    AudioMeta,
    LocalTTS,
    async_synthesize_speech,
    ensure_kokoro_model,
    get_audio_duration,
    is_kokoro_model_available,
    synthesize_speech,
)


def create_test_wav(
    path: Path,
    duration_seconds: float = 1.0,
    sample_rate: int = 24000,
    channels: int = 1,
    sample_width: int = 2,
) -> Path:
    num_frames = int(duration_seconds * sample_rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        # Write 16-bit silent PCM samples
        wf.writeframes(b"\x00" * (num_frames * channels * sample_width))
    return path


def test_get_audio_duration_valid(tmp_path: Path):
    wav_path = create_test_wav(tmp_path / "sample.wav", duration_seconds=2.5)
    duration = get_audio_duration(wav_path)
    assert pytest.approx(duration, rel=1e-3) == 2.5


def test_get_audio_duration_file_not_found(tmp_path: Path):
    non_existent = tmp_path / "does_not_exist.wav"
    with pytest.raises(FileNotFoundError):
        get_audio_duration(non_existent)


def test_get_audio_duration_invalid_file(tmp_path: Path):
    corrupt_file = tmp_path / "corrupt.wav"
    corrupt_file.write_bytes(b"not a valid wav file content")
    with pytest.raises(ValueError):
        get_audio_duration(corrupt_file)


def test_local_tts_mock_synthesis_headers_and_duration(tmp_path: Path):
    output_path = tmp_path / "audio" / "narration.wav"
    tts = LocalTTS(fallback_mock=True)

    meta = tts.synthesize(
        "Artificial intelligence is transforming open source software development.", output_path
    )

    assert isinstance(meta, AudioMeta)
    assert Path(meta.wav_path).resolve() == output_path.resolve()
    assert meta.duration_seconds > 0

    # Verify WAV header specifications: 24kHz, 16-bit, mono
    with wave.open(str(output_path), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 24000
        nframes = wf.getnframes()
        expected_duration = nframes / 24000.0
        assert pytest.approx(meta.duration_seconds, rel=1e-3) == expected_duration


def test_local_tts_speed_scaling(tmp_path: Path):
    text = "The quick brown fox jumps over the lazy dog and explores the open source universe."
    tts = LocalTTS(fallback_mock=True)

    meta_normal = tts.synthesize(text, tmp_path / "normal.wav", speed=1.0)
    meta_fast = tts.synthesize(text, tmp_path / "fast.wav", speed=2.0)

    assert meta_normal.duration_seconds > meta_fast.duration_seconds
    assert pytest.approx(meta_fast.duration_seconds * 2.0, rel=0.1) == meta_normal.duration_seconds


def test_synthesize_speech_convenience_function(tmp_path: Path):
    output_path = tmp_path / "speech.wav"
    text = "Technology news delivered instantly to your feed."

    duration = synthesize_speech(text, output_path, voice="af_bella", speed=1.2)

    assert isinstance(duration, float)
    assert duration > 0
    assert output_path.is_file()
    assert pytest.approx(duration, rel=1e-3) == get_audio_duration(output_path)


def test_synthesize_speech_with_app_config(tmp_path: Path):
    output_path = tmp_path / "config_speech.wav"
    cfg = AppConfig()
    cfg.tts.voice = "am_adam"
    cfg.tts.speed = 1.5

    duration = synthesize_speech(
        "Testing voice synthesis with application configuration.", output_path, config=cfg
    )

    assert duration > 0
    assert output_path.is_file()


@pytest.mark.asyncio
async def test_async_synthesize_speech(tmp_path: Path):
    output_path = tmp_path / "async_speech.wav"
    text = "Async speech synthesis running in worker thread executor."

    duration = await async_synthesize_speech(text, output_path, speed=1.1)

    assert isinstance(duration, float)
    assert duration > 0
    assert output_path.is_file()
    assert pytest.approx(duration, rel=1e-3) == get_audio_duration(output_path)


def test_is_kokoro_model_available(tmp_path: Path):
    models_dir = tmp_path / "models"
    assert not is_kokoro_model_available(models_dir)

    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "kokoro-v0_19.onnx").write_bytes(b"dummy onnx")
    assert not is_kokoro_model_available(models_dir)

    (models_dir / "voices.bin").write_bytes(b"dummy voices")
    assert is_kokoro_model_available(models_dir)


def test_ensure_kokoro_model_missing_no_download_raises(tmp_path: Path):
    models_dir = tmp_path / "empty_models"
    with pytest.raises(FileNotFoundError, match="Kokoro TTS model files missing"):
        ensure_kokoro_model(models_dir=models_dir, download_if_missing=False)


def test_ensure_kokoro_model_already_present(tmp_path: Path):
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    model_file = models_dir / "kokoro-v0_19.onnx"
    voices_file = models_dir / "voices.bin"
    model_file.write_bytes(b"model data")
    voices_file.write_bytes(b"voice data")

    m_path, v_path = ensure_kokoro_model(models_dir=models_dir, download_if_missing=False)
    assert m_path == model_file
    assert v_path == voices_file


def test_ensure_kokoro_model_download_failure_handling(tmp_path: Path, monkeypatch):
    import urllib.request
    from urllib.error import URLError

    def mock_urlopen(*args, **kwargs):
        raise URLError("Network unreachable (offline)")

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    models_dir = tmp_path / "models_download_fail"
    with pytest.raises(RuntimeError, match="Failed to download Kokoro model"):
        ensure_kokoro_model(models_dir=models_dir, download_if_missing=True)


def test_ensure_kokoro_model_download_success(tmp_path: Path, monkeypatch):
    import io
    import urllib.request

    class MockResponse:
        def __init__(self, data: bytes):
            self._io = io.BytesIO(data)

        def read(self, *args, **kwargs):
            return self._io.read(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    def mock_urlopen(url, *args, **kwargs):
        if str(url).endswith(".onnx"):
            return MockResponse(b"downloaded onnx content")
        return MockResponse(b"downloaded voices content")

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    models_dir = tmp_path / "models_download_ok"
    m_path, v_path = ensure_kokoro_model(models_dir=models_dir, download_if_missing=True)

    assert m_path.is_file()
    assert v_path.is_file()
    assert m_path.read_bytes() == b"downloaded onnx content"
    assert v_path.read_bytes() == b"downloaded voices content"


def test_local_tts_missing_model_strict_mode_raises(tmp_path: Path):
    non_existent = tmp_path / "models" / "kokoro.onnx"
    tts = LocalTTS(
        model_path=non_existent, voices_path=tmp_path / "voices.bin", fallback_mock=False
    )
    with pytest.raises(FileNotFoundError, match="Kokoro model files not found"):
        tts.synthesize("Some text", tmp_path / "out.wav")


def test_local_tts_real_kokoro_session_invocation(tmp_path: Path, monkeypatch):
    import numpy as np

    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    model_path = models_dir / "kokoro-v0_19.onnx"
    voices_path = models_dir / "voices.bin"
    model_path.write_bytes(b"model-weights")
    voices_path.write_bytes(b"voice-data")

    output_path = tmp_path / "real_out.wav"

    created_calls = []

    class MockKokoroInstance:
        def __init__(self, model_p: str, voices_p: str):
            self.model_p = model_p
            self.voices_p = voices_p

        def create(self, text: str, voice: str, speed: float = 1.0, **kwargs):
            created_calls.append({"text": text, "voice": voice, "speed": speed})
            # Generate 1.5 seconds of 24kHz audio (36000 samples)
            samples = np.zeros(36000, dtype=np.float32)
            return samples, 24000

    import kokoro_onnx

    monkeypatch.setattr(kokoro_onnx, "Kokoro", MockKokoroInstance)

    tts = LocalTTS(
        model_path=model_path,
        voices_path=voices_path,
        voice_name="af_bella",
        speed=1.08,
        fallback_mock=False,
    )

    meta = tts.synthesize("Hello world", output_path, voice="af_bella", speed=1.08)

    assert len(created_calls) == 1
    assert created_calls[0]["text"] == "Hello world"
    assert created_calls[0]["voice"] == "af_bella"
    assert pytest.approx(created_calls[0]["speed"]) == 1.08

    assert pytest.approx(meta.duration_seconds, rel=1e-3) == 1.5
    assert output_path.is_file()

    with wave.open(str(output_path), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 24000
