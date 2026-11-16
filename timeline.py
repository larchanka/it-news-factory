from __future__ import annotations

import logging
import math
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tts import get_audio_duration

if TYPE_CHECKING:
    from concat_client import ConcatClient

logger = logging.getLogger(__name__)

DEFAULT_SLOT_DURATION = 8.0
KEN_BURNS_MAX_ZOOM_INCREMENT = 0.10


# --- Data Models ---


@dataclass(frozen=True)
class VideoSlot:
    """Represents a placed user video clip on the master video track."""

    video_index: int
    path: Path
    start_time: float
    duration: float


@dataclass(frozen=True)
class ImageSlot:
    """Represents an image slot with alternating Ken Burns zoom scale keyframes."""

    slot_index: int
    image_index: int
    path: Path
    start_time: float
    duration: float
    start_scale: float
    end_scale: float


@dataclass(frozen=True)
class TimelinePlan:
    """Calculated layout for visual media conforming to audio duration T."""

    total_duration: float
    videos: list[VideoSlot]
    images: list[ImageSlot]


@dataclass(frozen=True)
class TimelineResult:
    """Result of building the shorts project timeline in Concat Desktop."""

    project_id: str
    audio_clip_id: str
    captions_id: str | None
    video_clip_ids: list[str]
    image_clip_ids: list[str]
    total_duration: float
    plan: TimelinePlan


# --- Video Duration Probing ---


def probe_video_duration(file_path: str | Path) -> float:
    """Probe video duration in seconds via ffprobe.

    Raises:
        FileNotFoundError: If the video file does not exist.
        ValueError: If duration probing fails or produces non-positive value.
    """
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Video file not found: {path}")

    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=10.0)
        output = res.stdout.strip()
        duration = float(output)
        if duration <= 0:
            raise ValueError(f"Probed duration is non-positive: {duration}")
        return duration
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        raise ValueError(f"Failed to probe duration for video '{path}': {e}") from e


# --- Mathematical Planning Helpers ---


def _plan_video_slots(
    total_duration: float,
    video_durations: Sequence[float],
    video_paths: Sequence[str | Path] | None,
) -> tuple[list[VideoSlot], float]:
    """Calculate video clip placements trimmed to total audio duration T."""
    slots: list[VideoSlot] = []
    elapsed = 0.0

    for idx, raw_dur in enumerate(video_durations):
        if elapsed >= total_duration:
            break
        remaining = total_duration - elapsed
        actual_dur = min(float(raw_dur), remaining)
        if actual_dur <= 0:
            continue

        if video_paths and idx < len(video_paths):
            v_path = Path(video_paths[idx])
        else:
            v_path = Path(f"video_{idx}.mp4")

        slots.append(
            VideoSlot(
                video_index=idx,
                path=v_path,
                start_time=elapsed,
                duration=actual_dur,
            )
        )
        elapsed += actual_dur

    return slots, elapsed


def _calculate_slot_scale(
    slot_index: int,
    duration: float,
    standard_slot_duration: float,
) -> tuple[float, float]:
    """Calculate Ken Burns zoom keyframe values (alternating in/out with pro-rated scale)."""
    norm_ratio = duration / standard_slot_duration
    scale_increment = KEN_BURNS_MAX_ZOOM_INCREMENT * norm_ratio

    if slot_index % 2 == 0:
        # Even slot: Zoom In from 1.0 -> 1.0 + increment
        start_scale = 1.0
        end_scale = round(1.0 + scale_increment, 6)
    else:
        # Odd slot: Zoom Out from 1.0 + increment -> 1.0
        start_scale = round(1.0 + scale_increment, 6)
        end_scale = 1.0

    return start_scale, end_scale


def _resolve_image_path(img_idx: int, image_paths: Sequence[str | Path] | None) -> Path:
    """Resolve Path object for an image index from asset list or default placeholder."""
    if image_paths and img_idx < len(image_paths):
        return Path(image_paths[img_idx])
    return Path(f"image_{img_idx}.jpg")


def _plan_image_slots(
    remaining_duration: float,
    start_time_offset: float,
    num_images: int,
    image_paths: Sequence[str | Path] | None,
    slot_duration: float,
) -> list[ImageSlot]:
    """Calculate 8.0s image slots looping through assets with alternating Ken Burns zooms."""
    if remaining_duration <= 0.0:
        return []

    if slot_duration <= 0:
        raise ValueError(f"Slot duration must be positive, got {slot_duration}")

    if num_images <= 0:
        raise ValueError(
            "At least one image asset is required to fill remaining timeline duration."
        )

    num_slots = math.ceil(remaining_duration / slot_duration)
    slots: list[ImageSlot] = []
    elapsed = start_time_offset

    for i in range(num_slots):
        is_terminal = i == (num_slots - 1)
        slot_dur = remaining_duration - (i * slot_duration) if is_terminal else slot_duration

        img_idx = i % num_images
        img_path = _resolve_image_path(img_idx, image_paths)
        start_scale, end_scale = _calculate_slot_scale(i, slot_dur, slot_duration)

        slots.append(
            ImageSlot(
                slot_index=i,
                image_index=img_idx,
                path=img_path,
                start_time=elapsed,
                duration=slot_dur,
                start_scale=start_scale,
                end_scale=end_scale,
            )
        )
        elapsed += slot_dur

    return slots


def calculate_timeline_plan(
    total_duration: float,
    video_durations: Sequence[float],
    num_images: int,
    image_paths: Sequence[str | Path] | None = None,
    video_paths: Sequence[str | Path] | None = None,
    slot_duration: float = DEFAULT_SLOT_DURATION,
) -> TimelinePlan:
    """Calculate exact video and image timeline slots bounded by total audio duration T."""
    if total_duration <= 0:
        raise ValueError(f"Total duration must be positive, got {total_duration}")
    if slot_duration <= 0:
        raise ValueError(f"Slot duration must be positive, got {slot_duration}")

    videos, videos_elapsed = _plan_video_slots(total_duration, video_durations, video_paths)
    remaining_for_images = max(0.0, total_duration - videos_elapsed)
    images = _plan_image_slots(
        remaining_duration=remaining_for_images,
        start_time_offset=videos_elapsed,
        num_images=num_images,
        image_paths=image_paths,
        slot_duration=slot_duration,
    )

    return TimelinePlan(
        total_duration=total_duration,
        videos=videos,
        images=images,
    )


def calculate_ken_burns_slots(
    remaining_duration: float,
    num_images: int,
    slot_duration: float = DEFAULT_SLOT_DURATION,
    start_time_offset: float = 0.0,
    image_paths: Sequence[str | Path] | None = None,
) -> list[ImageSlot]:
    """Convenience helper to compute Ken Burns image slots for a given duration."""
    return _plan_image_slots(
        remaining_duration=remaining_duration,
        start_time_offset=start_time_offset,
        num_images=num_images,
        image_paths=image_paths,
        slot_duration=slot_duration,
    )


# --- Concat Automation Timeline Builder ---


def _lookup_override_duration(
    v_path: str | Path,
    idx: int,
    video_durations: dict[str | Path, float] | Sequence[float] | None,
) -> float | None:
    """Check if duration override exists in dictionary or sequence."""
    if isinstance(video_durations, dict):
        if v_path in video_durations:
            return float(video_durations[v_path])
        path_obj = Path(v_path)
        if path_obj in video_durations:
            return float(video_durations[path_obj])
    elif isinstance(video_durations, (list, tuple)) and idx < len(video_durations):
        return float(video_durations[idx])
    return None


def _resolve_video_durations(
    video_paths: Sequence[str | Path] | None,
    video_durations: dict[str | Path, float] | Sequence[float] | None,
) -> list[float]:
    """Resolve duration for each video path from overrides or ffprobe."""
    if not video_paths:
        return []

    resolved: list[float] = []
    for idx, v_path in enumerate(video_paths):
        override = _lookup_override_duration(v_path, idx, video_durations)
        resolved.append(override if override is not None else probe_video_duration(v_path))

    return resolved


class _MediaImporter:
    """Caches imported media IDs to avoid redundant RPC calls for looped assets."""

    def __init__(self, client: ConcatClient) -> None:
        self.client = client
        self.cache: dict[str, str] = {}

    def get_or_import(self, path: str | Path) -> str:
        key = str(Path(path).resolve())
        if key not in self.cache:
            self.cache[key] = self.client.media_import(key)
        return self.cache[key]


def _apply_video_clips(
    client: ConcatClient,
    importer: _MediaImporter,
    videos: Sequence[VideoSlot],
) -> list[str]:
    """Place video clips onto the master video track."""
    clip_ids: list[str] = []
    for slot in videos:
        media_id = importer.get_or_import(slot.path)
        clip_id = client.clip_add(
            track="video",
            media_id=media_id,
            start_time=slot.start_time,
            duration=slot.duration,
        )
        clip_ids.append(clip_id)
    return clip_ids


def _apply_image_clips(
    client: ConcatClient,
    importer: _MediaImporter,
    images: Sequence[ImageSlot],
) -> list[str]:
    """Place image slots onto the video track and attach Ken Burns zoom keyframes."""
    clip_ids: list[str] = []
    for slot in images:
        media_id = importer.get_or_import(slot.path)
        clip_id = client.clip_add(
            track="video",
            media_id=media_id,
            start_time=slot.start_time,
            duration=slot.duration,
        )
        client.keyframe_set(
            clip_id=clip_id,
            property="scale",
            time=0.0,
            value=slot.start_scale,
        )
        client.keyframe_set(
            clip_id=clip_id,
            property="scale",
            time=slot.duration,
            value=slot.end_scale,
        )
        clip_ids.append(clip_id)
    return clip_ids


def build_timeline(
    client: ConcatClient,
    audio_path: str | Path,
    image_paths: Sequence[str | Path],
    video_paths: Sequence[str | Path] | None = None,
    project_name: str = "short",
    output_resolution: tuple[int, int] | list[int] = (1080, 1920),
    fps: int = 30,
    timeline_background: str = "blur",
    audio_duration: float | None = None,
    video_durations: dict[str | Path, float] | Sequence[float] | None = None,
    language: str = "en",
    slot_duration: float = DEFAULT_SLOT_DURATION,
    **kwargs: Any,
) -> TimelineResult:
    """Build an automated vertical shorts project conforming to master audio duration T.

    Steps:
    1. Read exact audio duration T (master clock).
    2. Initialize project in 1080x1920 9:16 vertical mode with blurred background.
    3. Import audio into project and add to audio track.
    4. Call captions_add for whisper.cpp subtitle stamping.
    5. Place user video clips first, trimming them if their sum exceeds T.
    6. Fill remaining time with 8.0s image slots with alternating Ken Burns zoom keyframes.
    7. Save project state and return complete TimelineResult.
    """
    total_audio_duration = (
        float(audio_duration) if audio_duration is not None else get_audio_duration(audio_path)
    )
    if total_audio_duration <= 0:
        raise ValueError(f"Audio duration must be positive, got {total_audio_duration}")

    v_durations = _resolve_video_durations(video_paths, video_durations)
    plan = calculate_timeline_plan(
        total_duration=total_audio_duration,
        video_durations=v_durations,
        num_images=len(image_paths),
        image_paths=image_paths,
        video_paths=video_paths,
        slot_duration=slot_duration,
    )

    proj_id = client.project_create(name=project_name)
    width, height = int(output_resolution[0]), int(output_resolution[1])
    client.project_set_video(
        width=width,
        height=height,
        fps=fps,
        timeline_background=timeline_background,
        **kwargs,
    )

    importer = _MediaImporter(client)
    audio_media_id = importer.get_or_import(audio_path)
    audio_clip_id = client.clip_add(
        track="audio",
        media_id=audio_media_id,
        start_time=0.0,
        duration=total_audio_duration,
    )

    captions_resp = client.captions_add(audio_clip_id, language=language)
    captions_id = (
        str(captions_resp.get("captions_id"))
        if isinstance(captions_resp, dict) and "captions_id" in captions_resp
        else None
    )

    video_clip_ids = _apply_video_clips(client, importer, plan.videos)
    image_clip_ids = _apply_image_clips(client, importer, plan.images)

    client.project_save()

    return TimelineResult(
        project_id=proj_id,
        audio_clip_id=audio_clip_id,
        captions_id=captions_id,
        video_clip_ids=video_clip_ids,
        image_clip_ids=image_clip_ids,
        total_duration=total_audio_duration,
        plan=plan,
    )
