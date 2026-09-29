"""Shared readers for Markdown evidence and cloud chunks inside ontology packs."""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from typing import Any, Iterator


CLOUD_CHUNKS_PATH = "cloud/chunks.jsonl"


@dataclass(frozen=True)
class CloudChunk:
    id: str
    title: str
    content: str
    metadata: dict[str, Any]
    raw: dict[str, Any]

    @property
    def path(self) -> str:
        return f"{CLOUD_CHUNKS_PATH}#{self.id}" if self.id else CLOUD_CHUNKS_PATH


def iter_markdown_documents(archive: zipfile.ZipFile) -> Iterator[tuple[str, str]]:
    for info in archive.infolist():
        if info.filename.startswith("documents/") and info.filename.endswith(".md"):
            yield info.filename, archive.read(info.filename).decode("utf-8-sig", errors="replace")


def iter_cloud_chunks(archive: zipfile.ZipFile) -> Iterator[CloudChunk]:
    if CLOUD_CHUNKS_PATH not in archive.namelist():
        return
    for line in archive.read(CLOUD_CHUNKS_PATH).decode("utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        chunk_id = str(raw.get("chunk_id") or raw.get("id") or "").strip()
        yield CloudChunk(
            id=chunk_id,
            title=str(raw.get("title") or raw.get("heading") or chunk_id or "Evidence chunk"),
            content=str(raw.get("content") or raw.get("text") or ""),
            metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
            raw=raw,
        )
