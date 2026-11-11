"""Batch ingestion pipeline shared by text and media documents.

Document schema (backward compatible):
- text:  {"id", "type": "text", "source", "body"}
- media: {"id", "type": "media", "source", "media": {...probe info...}}

Media documents intentionally have no "body" field; readers must use
document_body()/doc.get("body") instead of doc["body"] so old queries
iterating mixed batches do not crash on missing keys.
"""

from __future__ import annotations

import itertools
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field

from .media import MediaProbeError, media_available, probe_media

logger = logging.getLogger(__name__)

TEXT_TYPE = "text"
MEDIA_TYPE = "media"

_id_counter = itertools.count(1)


class MediaUnavailableError(RuntimeError):
    """Raised when media ingestion is requested but the ffmpeg binary is missing."""


@dataclass
class BatchResult:
    documents: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def make_text_document(body, source=None, doc_id=None):
    return {
        "id": doc_id if doc_id is not None else f"doc-{next(_id_counter)}",
        "type": TEXT_TYPE,
        "source": source,
        "body": body,
    }


def make_media_document(source, media_info, doc_id=None):
    # NOTE: no "body" key on purpose — media has no text body.
    return {
        "id": doc_id if doc_id is not None else f"doc-{next(_id_counter)}",
        "type": MEDIA_TYPE,
        "source": source,
        "media": dict(media_info),
    }


def document_body(doc):
    """Body of a document, or None for media docs and legacy docs without one."""
    return doc.get("body")


def document_summary(doc):
    """One-line summary safe for every document type, old or new."""
    body = doc.get("body")
    if body is not None:
        return body[:80]
    media = doc.get("media") or {}
    duration = media.get("duration")
    suffix = f" ({duration:.1f}s)" if isinstance(duration, (int, float)) else ""
    return f"[media] {doc.get('source')}{suffix}"


def spool_to_tempfile(stream, suffix=""):
    """Spool a (possibly non-seekable) upload stream to a temp file.

    Streams in chunks via shutil.copyfileobj — the file is never read into
    memory wholesale. The caller owns the returned path and must delete it.
    """
    fd, path = tempfile.mkstemp(prefix="ingest_", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as tmp:
            shutil.copyfileobj(stream, tmp, length=1024 * 1024)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def ingest_batch(items):
    """Ingest a batch of text/media items, reusing one pipeline for both.

    Each item is a dict:
    - text:  {"kind": "text", "body": str, "source": optional}
    - media: {"kind": "media", "stream": file-like, "filename": optional}

    A corrupt media file is logged as a warning and skipped without
    aborting the batch. A missing ffmpeg binary raises
    MediaUnavailableError; the text path never touches the binary.
    """
    result = BatchResult()
    for item in items:
        kind = item.get("kind") or item.get("type")
        if kind == TEXT_TYPE:
            result.documents.append(
                make_text_document(item.get("body", ""), source=item.get("source"))
            )
        elif kind == MEDIA_TYPE:
            result.documents.append(_ingest_media_item(item, result.warnings))
        else:
            raise ValueError(f"unknown batch item kind: {kind!r}")
    # Drop slots left empty by skipped (corrupt) media files.
    result.documents = [doc for doc in result.documents if doc is not None]
    return result


def _ingest_media_item(item, warnings):
    if not media_available():
        raise MediaUnavailableError(
            "media ingestion is unavailable: the bundled ffmpeg binary is "
            "missing or not executable (text ingestion is unaffected)"
        )

    stream = item["stream"]
    filename = item.get("filename") or "upload"
    suffix = os.path.splitext(filename)[1]

    # Upload streams are not seekable and ffmpeg only probes paths, so spool
    # to a temp file first; the temp file is always cleaned up afterwards.
    tmp_path = spool_to_tempfile(stream, suffix=suffix)
    try:
        media_info = probe_media(tmp_path)
    except MediaProbeError as exc:
        message = f"skipping corrupt media file {filename!r}: {exc}"
        logger.warning(message)
        warnings.append(message)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            logger.warning("could not remove temp spool file %s", tmp_path)

    return make_media_document(filename, media_info)
