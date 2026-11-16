"""
Turns a walkaround video into evenly spaced frames for the Studio 360° viewer.

Frames are written as lossless PNGs. The caller then saves them through the
normal upload pipeline, so spin frames get the same sizing and logo as photos.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

ALLOWED_VIDEO_EXTENSIONS = frozenset({'.mp4', '.mov', '.m4v', '.webm', '.avi'})
FRAME_COUNT_CHOICES = (24, 36, 48, 72)
DEFAULT_FRAME_COUNT = 36

# Phones record 4K walkarounds; nothing in Studio needs more than this, and it
# keeps the intermediate PNGs small.
MAX_FRAME_WIDTH = 1920
# Fastest PNG compression: the files are temporary and re-encoded straight away.
PNG_COMPRESSION = 1
# Roughly ten minutes at 30 fps. Guards against decoding absurdly long files.
MAX_SOURCE_FRAMES = 18_000


class SpinVideoError(ValueError):
    """The uploaded video can't be turned into a 360° spin."""


def is_allowed_video(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_VIDEO_EXTENSIONS


def spaced_indices(total_frames: int, count: int) -> list[int]:
    """Return `count` frame indices spread evenly over ``[0, total_frames)``.

    The end point is excluded on purpose: a full walkaround finishes where it
    started, so its last frame would duplicate the first one.
    """
    if count <= 0:
        raise ValueError('count must be positive')
    if total_frames < count:
        raise ValueError(f'cannot pick {count} frames from {total_frames}')
    return [i * total_frames // count for i in range(count)]


def extract_spin_frames(
    video_path: Path,
    output_dir: Path,
    name_prefix: str,
    count: int = DEFAULT_FRAME_COUNT,
) -> list[Path]:
    """Write `count` evenly spaced frames of the video to `output_dir`.

    Returns the frame paths in playback order. On failure nothing is left
    behind in `output_dir`.
    """
    total = _count_frames(video_path)
    if total < count:
        raise SpinVideoError(
            f'The video has only {total} frames but {count} are needed. '
            'Record a longer walkaround or choose fewer frames.'
        )

    wanted = spaced_indices(total, count)
    written: list[Path] = []
    capture = _open_video(video_path)
    try:
        # Frames are read sequentially rather than by seeking: seeking with
        # CAP_PROP_POS_FRAMES lands on keyframes for many phone codecs, which
        # would give duplicated or out-of-order angles.
        for index in range(total):
            if not capture.grab():
                break
            if index != wanted[len(written)]:
                continue
            ok, frame = capture.retrieve()
            if not ok:
                break
            out_path = output_dir / f'{name_prefix}_{len(written):03d}.png'
            _write_png(out_path, frame)
            written.append(out_path)
            if len(written) == count:
                break
    except Exception:
        _remove_files(written)
        raise
    finally:
        capture.release()

    if len(written) < count:
        _remove_files(written)
        raise SpinVideoError('The video could not be fully decoded. Try exporting it again as MP4.')
    return written


def _open_video(video_path: Path) -> cv2.VideoCapture:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        capture.release()
        raise SpinVideoError('The file is not a readable video.')
    return capture


def _count_frames(video_path: Path) -> int:
    # CAP_PROP_FRAME_COUNT comes from container metadata, which is missing for
    # WebM and often wrong for variable-frame-rate phone video, so count by
    # walking the stream instead.
    capture = _open_video(video_path)
    total = 0
    try:
        while total < MAX_SOURCE_FRAMES and capture.grab():
            total += 1
    finally:
        capture.release()

    if total == 0:
        raise SpinVideoError('The video contains no frames.')
    if total >= MAX_SOURCE_FRAMES:
        raise SpinVideoError('The video is too long. Trim it to a single walkaround of the car.')
    return total


def _write_png(path: Path, frame: np.ndarray) -> None:
    height, width = frame.shape[:2]
    if width > MAX_FRAME_WIDTH:
        scale = MAX_FRAME_WIDTH / width
        frame = cv2.resize(frame, (MAX_FRAME_WIDTH, round(height * scale)), interpolation=cv2.INTER_AREA)
    if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_PNG_COMPRESSION, PNG_COMPRESSION]):
        raise OSError(f'Could not write frame to {path}')


def _remove_files(paths: list[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
