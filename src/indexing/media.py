"""Media probing on top of the ffmpeg binary bundled with imageio-ffmpeg.

No system ffmpeg/ffprobe installation is required: imageio-ffmpeg ships a
static ffmpeg executable. It does not ship ffprobe, so probing is done by
running ``ffmpeg -hide_banner -i <path>`` and parsing the stream report it
writes to stderr.
"""

from __future__ import annotations

import logging
import re
import subprocess

import imageio_ffmpeg

logger = logging.getLogger(__name__)


class MediaProbeError(RuntimeError):
    """Raised when a media file cannot be probed (corrupt or unsupported)."""


def get_ffmpeg_exe() -> str:
    """Return the path of the ffmpeg binary bundled with imageio-ffmpeg."""
    return imageio_ffmpeg.get_ffmpeg_exe()


_capability_cache = None


def media_available() -> bool:
    """Whether the bundled ffmpeg binary is present and executable.

    Probed once at first use and cached for the process lifetime; use
    reset_media_capability_cache() to re-probe.
    """
    global _capability_cache
    if _capability_cache is None:
        try:
            proc = subprocess.run(
                [get_ffmpeg_exe(), "-hide_banner", "-version"],
                capture_output=True,
                timeout=15,
            )
            _capability_cache = proc.returncode == 0
        except Exception as exc:  # missing binary, permission error, timeout...
            logger.warning("media capability probe failed: %s", exc)
            _capability_cache = False
        if not _capability_cache:
            logger.warning("bundled ffmpeg binary unavailable; media ingestion disabled")
    return _capability_cache


def reset_media_capability_cache() -> None:
    """Forget the cached capability bit (mainly for tests)."""
    global _capability_cache
    _capability_cache = None


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\S+: Video: (\w+)[^\n]*?\b(\d{2,5})x(\d{2,5})\b")
_AUDIO_RE = re.compile(r"Stream #\S+: Audio: (\w+)[^\n]*?\b(\d+) Hz, ([^,\n]+)")
_ROTATE_TAG_RE = re.compile(r"rotate\s*:\s*(-?\d+)")
_DISPLAYMATRIX_RE = re.compile(r"displaymatrix: rotation of (-?\d+(?:\.\d+)?) degrees")

_CHANNELS_BY_NAME = {
    "mono": 1,
    "stereo": 2,
    "2.1": 3,
    "3.0": 3,
    "quad": 4,
    "4.0": 4,
    "5.0": 5,
    "5.1": 6,
    "6.1": 7,
    "7.1": 8,
}


def _parse_channels(layout: str):
    layout = layout.strip()
    if layout in _CHANNELS_BY_NAME:
        return _CHANNELS_BY_NAME[layout]
    match = re.match(r"(\d+) channels", layout)
    if match:
        return int(match.group(1))
    return None


def _parse_rotation(output: str) -> int:
    """Extract display rotation normalized to {0, 90, 180, 270}.

    The ``rotate`` metadata tag and the displaymatrix side data use opposite
    sign conventions for the same physical rotation; both are normalized to
    the tag convention here.
    """
    tag = _ROTATE_TAG_RE.search(output)
    if tag:
        return int(tag.group(1)) % 360
    side_data = _DISPLAYMATRIX_RE.search(output)
    if side_data:
        return int(round(-float(side_data.group(1)))) % 360
    return 0


def probe_media(path) -> dict:
    """Probe a media file and return its metadata.

    Returns a dict with duration (seconds), video_codec/audio_codec,
    sample_rate, channels, rotate, and two dimension sets that must not be
    conflated: width/height are the original encoded dimensions, while
    display_width/display_height have width and height swapped when the
    rotation is 90/270 (180 does not swap).
    """
    try:
        proc = subprocess.run(
            [get_ffmpeg_exe(), "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaProbeError(f"ffmpeg binary unavailable: {exc}") from exc

    # ffmpeg -i with no output file exits non-zero even on success, so the
    # report content, not the return code, decides whether probing worked.
    output = proc.stderr or ""
    if "Stream #" not in output:
        raise MediaProbeError(f"not a readable media file: {path}")

    info = {
        "duration": None,
        "video_codec": None,
        "audio_codec": None,
        "width": None,
        "height": None,
        "display_width": None,
        "display_height": None,
        "rotate": 0,
        "sample_rate": None,
        "channels": None,
    }

    duration = _DURATION_RE.search(output)
    if duration:
        hours, minutes, seconds = duration.groups()
        info["duration"] = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    video = _VIDEO_RE.search(output)
    if video:
        info["video_codec"] = video.group(1)
        info["width"] = int(video.group(2))
        info["height"] = int(video.group(3))

    audio = _AUDIO_RE.search(output)
    if audio:
        info["audio_codec"] = audio.group(1)
        info["sample_rate"] = int(audio.group(2))
        info["channels"] = _parse_channels(audio.group(3))

    rotate = _parse_rotation(output)
    info["rotate"] = rotate
    if info["width"] is not None:
        if rotate in (90, 270):
            info["display_width"] = info["height"]
            info["display_height"] = info["width"]
        else:  # 0 and 180 keep the encoded orientation
            info["display_width"] = info["width"]
            info["display_height"] = info["height"]

    return info
