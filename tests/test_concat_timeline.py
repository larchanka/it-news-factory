from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from concat_client import ConcatClient, MockConcatServer
from timeline import (
    TimelineResult,
    build_timeline,
    calculate_ken_burns_slots,
    calculate_timeline_plan,
)


def test_calculate_ken_burns_slots_alternating_zoom_and_terminal_prorated() -> None:
    """Verify Ken Burns zoom in/out alternating directions and terminal slot scale formula."""
    total_duration = 20.0  # 8.0 + 8.0 + 4.0
    video_durations: list[float] = []
    num_images = 3

    plan = calculate_timeline_plan(
        total_duration=total_duration,
        video_durations=video_durations,
        num_images=num_images,
        slot_duration=8.0,
    )

    assert plan.total_duration == pytest.approx(20.0)
    assert len(plan.videos) == 0
    assert len(plan.images) == 3

    # Slot 0 (even): 8.0s, zoom in 1.0 -> 1.10
    s0 = plan.images[0]
    assert s0.slot_index == 0
    assert s0.image_index == 0
    assert s0.start_time == pytest.approx(0.0)
    assert s0.duration == pytest.approx(8.0)
    assert s0.start_scale == pytest.approx(1.0)
    assert s0.end_scale == pytest.approx(1.10)

    # Slot 1 (odd): 8.0s, zoom out 1.10 -> 1.0
    s1 = plan.images[1]
    assert s1.slot_index == 1
    assert s1.image_index == 1
    assert s1.start_time == pytest.approx(8.0)
    assert s1.duration == pytest.approx(8.0)
    assert s1.start_scale == pytest.approx(1.10)
    assert s1.end_scale == pytest.approx(1.0)

    # Slot 2 (even, terminal): 4.0s (d_final = 20 - 16 = 4.0)
    # Pro-rated scale increment = 0.10 * (4.0 / 8.0) = 0.05
    # Since slot 2 is even: 1.0 -> 1.05
    s2 = plan.images[2]
    assert s2.slot_index == 2
    assert s2.image_index == 2
    assert s2.start_time == pytest.approx(16.0)
    assert s2.duration == pytest.approx(4.0)
    assert s2.start_scale == pytest.approx(1.0)
    assert s2.end_scale == pytest.approx(1.05)


def test_calculate_ken_burns_slots_terminal_slot_odd() -> None:
    """Verify terminal slot scale when terminal slot index is odd."""
    # 8.0s (even, 0) + 3.0s (odd, 1) = 11.0s
    total_duration = 11.0
    plan = calculate_timeline_plan(
        total_duration=total_duration,
        video_durations=[],
        num_images=2,
        slot_duration=8.0,
    )

    assert len(plan.images) == 2
    s1 = plan.images[1]
    assert s1.slot_index == 1
    assert s1.duration == pytest.approx(3.0)
    # Odd slot: start_scale = 1.0 + (0.10 * 3.0 / 8.0) = 1.0375, end_scale = 1.0
    expected_scale = 1.0 + (0.10 * 3.0 / 8.0)
    assert s1.start_scale == pytest.approx(expected_scale)
    assert s1.end_scale == pytest.approx(1.0)


def test_calculate_timeline_plan_exact_duration_match() -> None:
    """Verify that total visual timeline duration matches audio duration T exactly."""
    durations = [1.5, 7.9, 8.0, 16.0, 37.3, 45.0]
    for t in durations:
        plan = calculate_timeline_plan(
            total_duration=t,
            video_durations=[],
            num_images=4,
            slot_duration=8.0,
        )
        total_visual = sum(s.duration for s in plan.images)
        assert total_visual == pytest.approx(t)


def test_calculate_timeline_plan_image_looping() -> None:
    """Verify that image indices cycle modulo available images."""
    plan = calculate_timeline_plan(
        total_duration=32.0,  # 4 slots of 8s
        video_durations=[],
        num_images=2,  # Only 2 images available
        slot_duration=8.0,
    )
    assert [s.image_index for s in plan.images] == [0, 1, 0, 1]


def test_video_first_placement_and_trimming_when_videos_exceed_t() -> None:
    """Verify user videos are placed first and trimmed if T_videos >= T."""
    # Video 1 is 15s, Video 2 is 20s. Total video = 35s. But T = 25s.
    plan = calculate_timeline_plan(
        total_duration=25.0,
        video_durations=[15.0, 20.0],
        num_images=3,
        slot_duration=8.0,
    )

    assert len(plan.videos) == 2
    assert plan.videos[0].start_time == pytest.approx(0.0)
    assert plan.videos[0].duration == pytest.approx(15.0)

    assert plan.videos[1].start_time == pytest.approx(15.0)
    assert plan.videos[1].duration == pytest.approx(10.0)  # Trimmed to 10s (25 - 15)

    # All duration exhausted by videos; no images required
    assert len(plan.images) == 0
    total_visual = sum(v.duration for v in plan.videos)
    assert total_visual == pytest.approx(25.0)


def test_video_first_placement_with_remaining_image_slots() -> None:
    """Verify user video placed first and remaining time filled by image slots."""
    # Video is 6.0s, T = 22.0s -> T_rem = 16.0s (two 8.0s slots)
    plan = calculate_timeline_plan(
        total_duration=22.0,
        video_durations=[6.0],
        num_images=2,
        slot_duration=8.0,
    )

    assert len(plan.videos) == 1
    assert plan.videos[0].start_time == pytest.approx(0.0)
    assert plan.videos[0].duration == pytest.approx(6.0)

    assert len(plan.images) == 2
    assert plan.images[0].start_time == pytest.approx(6.0)
    assert plan.images[0].duration == pytest.approx(8.0)
    assert plan.images[1].start_time == pytest.approx(14.0)
    assert plan.images[1].duration == pytest.approx(8.0)

    total_visual = plan.videos[0].duration + sum(s.duration for s in plan.images)
    assert total_visual == pytest.approx(22.0)


def test_build_timeline_executes_rpc_and_sets_captions_and_keyframes(tmp_path: Path) -> None:
    """Verify build_timeline creates project, imports media, sets tracks, keyframes, and captions."""
    audio_file = tmp_path / "narration.wav"
    audio_file.write_bytes(b"dummy_wav")

    img1 = tmp_path / "img1.png"
    img1.write_bytes(b"img1")
    img2 = tmp_path / "img2.png"
    img2.write_bytes(b"img2")

    video1 = tmp_path / "clip.mp4"
    video1.write_bytes(b"video")

    client = ConcatClient(mock=True)
    client.connect()

    result = build_timeline(
        client=client,
        audio_path=audio_file,
        image_paths=[img1, img2],
        video_paths=[video1],
        audio_duration=20.0,
        video_durations=[4.0],
        project_name="test_short",
        output_resolution=(1080, 1920),
    )

    assert isinstance(result, TimelineResult)
    assert result.total_duration == pytest.approx(20.0)
    assert result.audio_clip_id.startswith("mock_clip_")
    assert len(result.video_clip_ids) == 1
    assert len(result.image_clip_ids) == 2  # 16s remaining -> 8s + 8s
    assert result.captions_id is not None


def test_build_timeline_live_mock_server(tmp_path: Path) -> None:
    """Verify full timeline construction over live MockConcatServer socket."""
    with MockConcatServer() as server:
        client = ConcatClient(port=server.port, token=server.token)
        client.connect()

        audio_file = tmp_path / "narration.wav"
        audio_file.write_bytes(b"dummy_wav")
        img_file = tmp_path / "img.jpg"
        img_file.write_bytes(b"dummy_img")

        result = client.create_shorts_project(
            audio_path=audio_file,
            image_paths=[img_file],
            audio_duration=12.0,  # 8.0s + 4.0s
            project_name="server_test",
        )

        assert result.total_duration == pytest.approx(12.0)
        assert len(result.image_clip_ids) == 2

        # Check recorded requests in MockConcatServer
        methods_called = [req["body"]["method"] for req in server._requests]
        assert "project_create" in methods_called
        assert "project_set_video" in methods_called
        assert "media_import" in methods_called
        assert "clip_add" in methods_called
        assert "captions_add" in methods_called
        assert "keyframe_set" in methods_called
        assert "project_save" in methods_called


def test_probe_video_duration_file_not_found() -> None:
    """Verify probe_video_duration raises FileNotFoundError for missing file."""
    with pytest.raises(FileNotFoundError, match="Video file not found"):
        from timeline import probe_video_duration

        probe_video_duration("/nonexistent/video.mp4")


def test_probe_video_duration_invalid_file(tmp_path: Path) -> None:
    """Verify probe_video_duration raises ValueError when ffprobe fails."""
    bad_file = tmp_path / "corrupt.mp4"
    bad_file.write_text("not a video")
    with pytest.raises(ValueError, match="Failed to probe duration"):
        from timeline import probe_video_duration

        probe_video_duration(bad_file)


def test_calculate_timeline_plan_invalid_duration_raises() -> None:
    """Verify non-positive total duration raises ValueError."""
    with pytest.raises(ValueError, match="Total duration must be positive"):
        calculate_timeline_plan(total_duration=0.0, video_durations=[], num_images=1)


def test_calculate_timeline_plan_invalid_slot_duration_raises() -> None:
    """Verify non-positive slot duration raises ValueError."""
    with pytest.raises(ValueError, match="Slot duration must be positive"):
        calculate_timeline_plan(
            total_duration=10.0, video_durations=[], num_images=1, slot_duration=0.0
        )
    with pytest.raises(ValueError, match="Slot duration must be positive"):
        calculate_timeline_plan(
            total_duration=10.0, video_durations=[], num_images=1, slot_duration=-5.0
        )


def test_calculate_timeline_plan_missing_images_with_positive_remainder_raises() -> None:
    """Verify zero images raises ValueError when remaining time needs to be filled."""
    with pytest.raises(ValueError, match="At least one image asset is required"):
        calculate_timeline_plan(total_duration=10.0, video_durations=[], num_images=0)


def test_calculate_ken_burns_slots_convenience_helper() -> None:
    """Verify calculate_ken_burns_slots convenience helper returns valid slots."""
    slots = calculate_ken_burns_slots(
        remaining_duration=16.0,
        num_images=2,
        slot_duration=8.0,
        start_time_offset=4.0,
    )
    assert len(slots) == 2
    assert slots[0].start_time == pytest.approx(4.0)
    assert slots[0].duration == pytest.approx(8.0)
    assert slots[1].start_time == pytest.approx(12.0)
    assert slots[1].duration == pytest.approx(8.0)


def test_build_timeline_caches_media_imports(tmp_path: Path) -> None:
    """Verify media_import is only called once per unique file path across looped image slots."""
    audio_file = tmp_path / "audio.wav"
    audio_file.write_bytes(b"wav")
    img_file = tmp_path / "single_img.png"
    img_file.write_bytes(b"img")

    imported_paths: list[str] = []

    class TrackingConcatClient(ConcatClient):
        def media_import(self, file_path: str | Path, **kwargs: Any) -> str:
            imported_paths.append(str(Path(file_path).resolve()))
            return super().media_import(file_path, **kwargs)

    client = TrackingConcatClient(mock=True)
    client.connect()

    # 32s with 1 image -> 4 slots of 8.0s all using the same image
    result = build_timeline(
        client=client,
        audio_path=audio_file,
        image_paths=[img_file],
        audio_duration=32.0,
    )

    assert len(result.image_clip_ids) == 4
    # Image should only have been imported ONCE despite 4 slots
    img_imports = [p for p in imported_paths if "single_img.png" in p]
    assert len(img_imports) == 1


def test_build_timeline_invalid_audio_duration_raises(tmp_path: Path) -> None:
    """Verify non-positive audio duration raises ValueError."""
    client = ConcatClient(mock=True)
    client.connect()
    with pytest.raises(ValueError, match="Audio duration must be positive"):
        build_timeline(
            client=client,
            audio_path=tmp_path / "audio.wav",
            image_paths=[tmp_path / "img.jpg"],
            audio_duration=-5.0,
        )


def test_build_timeline_reads_audio_duration_from_real_wav(tmp_path: Path) -> None:
    """Verify audio duration is read from actual WAV file if not passed explicitly."""
    from tts import LocalTTS

    wav_path = tmp_path / "speech.wav"
    tts = LocalTTS(fallback_mock=True)
    tts.synthesize("Test speech", output_wav_path=wav_path)

    client = ConcatClient(mock=True)
    client.connect()

    img = tmp_path / "img.png"
    img.write_bytes(b"img")

    result = build_timeline(
        client=client,
        audio_path=wav_path,
        image_paths=[img],
    )
    assert result.total_duration > 0.0
    assert len(result.image_clip_ids) >= 1
