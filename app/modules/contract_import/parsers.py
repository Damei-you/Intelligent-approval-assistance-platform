from __future__ import annotations

import io
import re
from pathlib import Path

from pydantic import ValidationError
from pypdf import PdfReader

from app.modules.contract_import.exceptions import (
    DocumentParseError,
    UnsupportedFileTypeError,
)
from app.modules.contract_import.schemas import (
    ContractJsonImportRequest,
    ImportFormat,
    ParsedClause,
)


SUPPORTED_EXTENSIONS = {
    ".pdf": ImportFormat.PDF,
    ".txt": ImportFormat.TXT,
    ".json": ImportFormat.JSON,
}

CLAUSE_HEADING_PATTERN = re.compile(
    r"(?m)^[ \t]*(?P<number>"
    r"第[〇零一二三四五六七八九十百千万两\d]+条"
    r"|(?:\d+(?:\.\d+)*|[一二三四五六七八九十]+)[、.．]"
    r")(?P<title>[^\r\n]*)"
)


def detect_import_format(file_name: str) -> ImportFormat:
    extension = Path(file_name).suffix.lower()
    try:
        return SUPPORTED_EXTENSIONS[extension]
    except KeyError as exc:
        raise UnsupportedFileTypeError(
            f"不支持文件扩展名 {extension or '<无>'}，仅支持 PDF、TXT 和 JSON。"
        ) from exc


def decode_text(data: bytes) -> str:
    """按常见中文文本编码依次尝试解码 TXT 内容。"""

    # UTF-8 是默认选择，GB18030 用于兼容部分 Windows 导出的中文合同文本。
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = data.decode(encoding)
            if text.strip():
                return normalize_text(text)
        except UnicodeDecodeError:
            continue
    raise DocumentParseError("TXT 文件无法按 UTF-8 或 GB18030 编码解析。")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def split_text_into_clauses(
    text: str,
    *,
    page_no: int | None = None,
    max_chunk_chars: int = 4000,
) -> list[ParsedClause]:
    """通过“第一条”或“1.”等标题识别合同条款。"""

    normalized = normalize_text(text)
    if not normalized:
        return []

    matches = list(CLAUSE_HEADING_PATTERN.finditer(normalized))
    if not matches:
        return _split_unstructured_text(
            normalized,
            page_no=page_no,
            max_chunk_chars=max_chunk_chars,
        )

    clauses: list[ParsedClause] = []
    preamble = normalized[: matches[0].start()].strip()
    if preamble:
        clauses.extend(
            _split_unstructured_text(
                preamble,
                page_no=page_no,
                max_chunk_chars=max_chunk_chars,
                title="前言",
            )
        )

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        content = normalized[match.start() : end].strip()
        title = match.group("title").strip(" \t:：、.．") or None
        clauses.append(
            ParsedClause(
                clause_no=match.group("number").strip(),
                title=title,
                content=content,
                page_no=page_no,
            )
        )
    return clauses


def _split_unstructured_text(
    text: str,
    *,
    page_no: int | None,
    max_chunk_chars: int,
    title: str | None = None,
) -> list[ParsedClause]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        pieces = [
            paragraph[start : start + max_chunk_chars]
            for start in range(0, len(paragraph), max_chunk_chars)
        ]
        for piece in pieces:
            candidate = f"{current}\n\n{piece}".strip() if current else piece
            if current and len(candidate) > max_chunk_chars:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)

    return [
        ParsedClause(content=chunk, title=title, page_no=page_no)
        for chunk in chunks
    ]


def parse_pdf(data: bytes) -> tuple[str, list[ParsedClause], dict[str, int]]:
    """使用 pypdf 提取 PDF 文本层，并按页切分条款。"""

    try:
        # PdfReader 只提取已有文本层，不执行 OCR，因此扫描版 PDF 会在下方返回明确错误。
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise DocumentParseError("PDF 已加密，无法读取。")

        page_texts: list[str] = []
        clauses: list[ParsedClause] = []
        for page_index, page in enumerate(reader.pages, start=1):
            page_text = normalize_text(page.extract_text() or "")
            if not page_text:
                continue
            page_texts.append(page_text)
            clauses.extend(split_text_into_clauses(page_text, page_no=page_index))
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError(f"PDF 解析失败：{exc}") from exc

    raw_text = "\n\n".join(page_texts)
    if not raw_text or not clauses:
        raise DocumentParseError("PDF 中未提取到文本，扫描件暂不支持，请先进行 OCR。")
    return raw_text, clauses, {"page_count": len(reader.pages)}


def parse_json_bytes(data: bytes) -> ContractJsonImportRequest:
    try:
        return ContractJsonImportRequest.model_validate_json(data)
    except (ValidationError, ValueError, UnicodeDecodeError) as exc:
        raise DocumentParseError(f"JSON 内容不符合合同条款导入结构：{exc}") from exc
