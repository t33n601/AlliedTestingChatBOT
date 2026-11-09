"""Turn an uploaded file into (heading, body) chunks.

Markdown is split on headings, exactly like loader.py does for the knowledge/ folder.
PDF, DOCX and plain text have no reliable headings, so they are split into
paragraph groups of roughly MAX_CHARS characters.
"""
import io
import re
from pathlib import Path

MAX_CHARS = 900
SUPPORTED = {".md", ".markdown", ".txt", ".pdf", ".docx"}


def split_markdown(text):
    chunks = []
    for part in text.split("\n#"):
        lines = part.strip().lstrip("#").strip().split("\n", 1)
        if len(lines) < 2 or not lines[1].strip():
            continue
        chunks.append((lines[0].strip(), lines[1].strip()))
    return chunks


def split_paragraphs(text, title):
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paragraphs:
        if buf and len(buf) + len(p) > MAX_CHARS:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        chunks.append(buf)
    # a heading for each piece: its first line if short, otherwise "<title> · part N"
    out = []
    for i, body in enumerate(chunks, 1):
        first = body.split("\n", 1)[0].strip()
        heading = first if len(first) <= 80 and len(chunks) > 1 and "\n" in body else f"{title} · part {i}"
        out.append((heading, body))
    return out


def extract_text(filename, data: bytes):
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    if ext == ".docx":
        from docx import Document
        doc = Document(io.BytesIO(data))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    return data.decode("utf-8", errors="replace")


def file_to_chunks(filename, data: bytes):
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED:
        raise ValueError(f"Unsupported file type {ext}. Use: {', '.join(sorted(SUPPORTED))}")
    text = extract_text(filename, data).replace("\r\n", "\n")
    title = Path(filename).stem
    if ext in {".md", ".markdown"}:
        chunks = split_markdown("\n" + text)
        if chunks:
            return chunks
    return split_paragraphs(text, title)
