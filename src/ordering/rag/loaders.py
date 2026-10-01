"""Document loading and parsing (Markdown, plain text, HTML, PDF) with front-matter metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import frontmatter

SUPPORTED = {".md", ".markdown", ".txt", ".html", ".htm", ".pdf"}
DOC_TYPES = {"ordering_policy", "vendor_rules", "replenishment_guideline", "payment_policy", "operations", "other"}


@dataclass
class LoadedDocument:
    path: Path
    text: str
    title: str
    doc_type: str
    tenant_id: str | None
    source_uri: str
    declared_version: str | None
    effective_date: date | None
    metadata: dict[str, Any] = field(default_factory=dict)


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        if tag in ("h1", "h2", "h3"):
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        if tag in ("p", "li", "br", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _read_body(path: Path) -> tuple[str, dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown", ".txt"):
        post = frontmatter.load(str(path))
        return post.content, dict(post.metadata)
    if suffix in (".html", ".htm"):
        parser = _HTMLText()
        parser.feed(path.read_text(encoding="utf-8", errors="replace"))
        return "".join(parser.parts), {}
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "") for page in reader.pages]
        meta = {k.lstrip("/").lower(): str(v) for k, v in (reader.metadata or {}).items()}
        return "\n\n".join(pages), {"title": meta.get("title")} if meta.get("title") else {}
    raise ValueError(f"unsupported file type: {suffix}")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace(chr(0xA0), " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _infer_tenant(path: Path, root: Path) -> str | None:
    try:
        first = path.relative_to(root).parts[0]
    except (ValueError, IndexError):
        return None
    return None if first in ("shared", "_archive") else first


def load_document(path: Path, root: Path) -> LoadedDocument:
    body, meta = _read_body(path)
    text = normalize_text(body)
    heading = re.search(r"^#\s+(.+)$", text, flags=re.MULTILINE)
    title = str(meta.get("title") or (heading.group(1).strip() if heading else path.stem.replace("-", " ").title()))
    doc_type = str(meta.get("doc_type", "other"))
    if doc_type not in DOC_TYPES:
        doc_type = "other"
    tenant = meta.get("tenant") or _infer_tenant(path, root)
    effective = meta.get("effective_date")
    if isinstance(effective, str):
        effective = date.fromisoformat(effective)
    source_uri = str(meta.get("source_uri") or path.relative_to(root).as_posix())
    known = {"title", "doc_type", "tenant", "effective_date", "source_uri", "version"}
    return LoadedDocument(
        path=path,
        text=text,
        title=title,
        doc_type=doc_type,
        tenant_id=str(tenant) if tenant else None,
        source_uri=source_uri,
        declared_version=str(meta["version"]) if meta.get("version") is not None else None,
        effective_date=effective if isinstance(effective, date) else None,
        metadata={k: str(v) for k, v in meta.items() if k not in known},
    )


def discover(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED)
