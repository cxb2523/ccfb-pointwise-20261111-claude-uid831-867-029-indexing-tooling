"""AV ingestion tests: media probing, rotation handling, corrupt-file skip,
missing-binary degradation, and the pre-existing text path."""

import io
import os
import struct
import subprocess

import pytest
from imageio_ffmpeg import get_ffmpeg_exe

from src.indexing import (
    MediaUnavailableError,
    document_body,
    document_summary,
    ingest_batch,
    media_available,
    probe_media,
    reset_media_capability_cache,
)
from src.indexing import media as media_mod
from src.indexing import pipeline as pipeline_mod

FFMPEG = get_ffmpeg_exe()


def run_ffmpeg(args):
    subprocess.run([FFMPEG, "-y", *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def sine_wav(tmp_path_factory):
    path = tmp_path_factory.mktemp("av") / "tone.wav"
    run_ffmpeg([
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-ac", "1", "-ar", "16000", str(path),
    ])
    return path


@pytest.fixture(scope="module")
def testsrc_mp4(tmp_path_factory):
    path = tmp_path_factory.mktemp("av") / "clip.mp4"
    run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=2",
        "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path),
    ])
    return path


# ffmpeg 7.x muxers no longer convert the "rotate" metadata tag into a
# display matrix, so rotation is written straight into the track header
# (tkhd) matrix of the synthesized mp4. The matrix sits 48 bytes into the
# tkhd atom (4 size + 4 type + 40 bytes of fields), big-endian 9x int32.
_ROTATION_MATRICES = {
    90: (0, 65536, 0, -65536, 0, 0, 0, 0, 0x40000000),
    180: (-65536, 0, 0, 0, -65536, 0, 0, 0, 0x40000000),
    270: (0, -65536, 0, 65536, 0, 0, 0, 0, 0x40000000),
}


def make_rotated(src_path, tmp_path, degrees):
    path = tmp_path / f"rot{degrees}.mp4"
    data = bytearray(src_path.read_bytes())
    tkhd = data.find(b"tkhd")
    assert tkhd != -1, "no tkhd atom in synthesized mp4"
    struct.pack_into(">9i", data, tkhd + 4 + 40, *_ROTATION_MATRICES[degrees])
    path.write_bytes(bytes(data))
    return path


class NonSeekableStream(io.BytesIO):
    """Upload streams are not seekable; the pipeline must cope."""

    def seekable(self):
        return False

    def seek(self, *args, **kwargs):
        raise io.UnsupportedOperation("stream is not seekable")


# --- probing ---------------------------------------------------------------


def test_probe_sine_audio(sine_wav):
    info = probe_media(sine_wav)
    assert abs(info["duration"] - 2.0) < 0.3
    assert info["audio_codec"] == "pcm_s16le"
    assert info["sample_rate"] == 16000
    assert info["channels"] == 1
    assert info["video_codec"] is None
    assert info["width"] is None


def test_probe_testsrc_video(testsrc_mp4):
    info = probe_media(testsrc_mp4)
    assert abs(info["duration"] - 2.0) < 0.3
    assert info["video_codec"] == "h264"
    assert info["width"] == 320
    assert info["height"] == 240
    assert info["rotate"] == 0
    assert (info["display_width"], info["display_height"]) == (320, 240)


@pytest.mark.parametrize("degrees", [90, 270])
def test_rotation_90_270_swaps_display_dimensions(testsrc_mp4, tmp_path, degrees):
    rotated = make_rotated(testsrc_mp4, tmp_path, degrees)
    info = probe_media(rotated)
    assert info["rotate"] == degrees
    # encoded fields keep the original dimensions...
    assert (info["width"], info["height"]) == (320, 240)
    # ...while the indexed display dimensions are swapped
    assert (info["display_width"], info["display_height"]) == (240, 320)


def test_rotation_180_does_not_swap(testsrc_mp4, tmp_path):
    rotated = make_rotated(testsrc_mp4, tmp_path, 180)
    info = probe_media(rotated)
    assert info["rotate"] == 180
    assert (info["width"], info["height"]) == (320, 240)
    assert (info["display_width"], info["display_height"]) == (320, 240)


# --- batch pipeline --------------------------------------------------------


def test_ingest_media_batch_via_non_seekable_stream(testsrc_mp4, monkeypatch):
    spooled = []
    real_mkstemp = pipeline_mod.tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        spooled.append(path)
        return fd, path

    monkeypatch.setattr(pipeline_mod.tempfile, "mkstemp", recording_mkstemp)

    stream = NonSeekableStream(testsrc_mp4.read_bytes())
    result = ingest_batch([{"kind": "media", "stream": stream, "filename": "clip.mp4"}])

    assert result.warnings == []
    assert len(result.documents) == 1
    doc = result.documents[0]
    assert doc["type"] == "media"
    assert doc["source"] == "clip.mp4"
    assert doc["media"]["video_codec"] == "h264"
    # temp spool file was cleaned up
    assert spooled and all(not os.path.exists(p) for p in spooled)


def test_corrupt_file_warns_and_batch_continues(tmp_path, sine_wav):
    corrupt = tmp_path / "broken.mp4"
    corrupt.write_bytes(b"this is definitely not a media file" * 100)

    result = ingest_batch([
        {"kind": "media", "stream": open(corrupt, "rb"), "filename": "broken.mp4"},
        {"kind": "media", "stream": open(sine_wav, "rb"), "filename": "tone.wav"},
        {"kind": "text", "body": "hello index", "source": "note.txt"},
    ])

    assert len(result.warnings) == 1
    assert "broken.mp4" in result.warnings[0]
    assert [d["type"] for d in result.documents] == ["media", "text"]
    assert result.documents[0]["source"] == "tone.wav"


def test_missing_binary_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(media_mod, "get_ffmpeg_exe", lambda: r"Z:\no\such\ffmpeg.exe")
    reset_media_capability_cache()
    try:
        assert media_available() is False

        with pytest.raises(MediaUnavailableError):
            ingest_batch([{
                "kind": "media",
                "stream": io.BytesIO(b"whatever"),
                "filename": "clip.mp4",
            }])

        # the text path never touches the binary
        result = ingest_batch([{"kind": "text", "body": "still works"}])
        assert result.documents[0]["body"] == "still works"
    finally:
        # leave a clean cache for other tests once monkeypatch is undone
        reset_media_capability_cache()


# --- text path & schema backward compatibility -----------------------------


def test_text_ingest_unchanged():
    result = ingest_batch([
        {"kind": "text", "body": "first", "source": "a.txt"},
        {"kind": "text", "body": "second"},
    ])
    assert result.warnings == []
    assert [d["body"] for d in result.documents] == ["first", "second"]
    assert all(d["type"] == "text" for d in result.documents)


def test_media_documents_have_no_body_and_old_queries_survive(sine_wav):
    result = ingest_batch([
        {"kind": "text", "body": "transcript"},
        {"kind": "media", "stream": open(sine_wav, "rb"), "filename": "tone.wav"},
    ])
    text_doc, media_doc = result.documents

    assert "body" not in media_doc
    # legacy iteration over mixed documents must not raise KeyError
    bodies = [document_body(d) for d in result.documents]
    assert bodies == ["transcript", None]
    summaries = [document_summary(d) for d in result.documents]
    assert summaries[0] == "transcript"
    assert "tone.wav" in summaries[1]
    assert text_doc["body"] == "transcript"
