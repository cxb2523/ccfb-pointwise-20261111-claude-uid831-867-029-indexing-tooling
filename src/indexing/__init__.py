"""Batch ingestion pipeline for text and media (audio/video) documents."""

from .media import (
    MediaProbeError,
    get_ffmpeg_exe,
    media_available,
    probe_media,
    reset_media_capability_cache,
)
from .pipeline import (
    BatchResult,
    MediaUnavailableError,
    document_body,
    document_summary,
    ingest_batch,
    make_media_document,
    make_text_document,
    spool_to_tempfile,
)

__all__ = [
    "BatchResult",
    "MediaProbeError",
    "MediaUnavailableError",
    "document_body",
    "document_summary",
    "get_ffmpeg_exe",
    "ingest_batch",
    "make_media_document",
    "make_text_document",
    "media_available",
    "probe_media",
    "reset_media_capability_cache",
    "spool_to_tempfile",
]
