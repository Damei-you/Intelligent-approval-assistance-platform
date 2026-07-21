from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from app.core.config import PROJECT_ROOT, settings
from app.modules.contract_import.exceptions import (
    DocumentParseError,
    PreviewFileMismatchError,
)
from app.modules.contract_import.parsers import (
    decode_text,
    detect_import_format,
    parse_pdf,
    split_text_into_clauses,
)
from app.modules.contract_import.schemas import ImportFormat, ParsedClause
from app.modules.policy_import.repository import PolicyImportRepository
from app.modules.policy_import.schemas import (
    ParsedPolicy,
    PolicyFileMetadata,
    PolicyImportDetail,
    PolicyImportPreviewResponse,
    PolicyImportResponse,
    PolicyJsonImportRequest,
)
from app.modules.vectorization.service import enqueue_document_vectorization


class PolicyImportService:
    """组织制度解析预览、人工确认、持久化和异步向量化。"""

    def __init__(self, repository: PolicyImportRepository) -> None:
        self.repository = repository

    def preview_file(
        self,
        *,
        file_name: str,
        mime_type: str | None,
        data: bytes,
        file_metadata: PolicyFileMetadata | None,
    ) -> PolicyImportPreviewResponse:
        """只解析并返回可编辑 JSON，预览阶段不保存文件或数据库记录。"""

        import_format, parsed = self._parse_source_file(
            file_name=file_name,
            data=data,
            file_metadata=file_metadata,
        )
        payload = self._to_json_payload(parsed)
        warnings: list[str] = []
        if import_format == ImportFormat.PDF:
            warnings.append("PDF 当前按页解析，跨页制度章节可能需要人工合并。")
        return PolicyImportPreviewResponse(
            preview_hash=hashlib.sha256(data).hexdigest(),
            source_format=import_format,
            file_name=Path(file_name).name,
            file_size=len(data),
            mime_type=mime_type or _default_mime_type(import_format),
            section_count=len(payload.sections),
            payload=payload,
            warnings=warnings,
            persisted=False,
        )

    def confirm_file(
        self,
        *,
        file_name: str,
        mime_type: str | None,
        data: bytes,
        preview_hash: str,
        payload: PolicyJsonImportRequest,
    ) -> PolicyImportResponse:
        """校验原文件哈希，并把用户确认后的制度章节事务写入数据库。"""

        # 确认阶段重新计算 SHA-256，防止预览后原文件被替换。
        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != preview_hash:
            raise PreviewFileMismatchError("确认时的制度文件与预览文件不一致，请重新解析。")

        import_format = detect_import_format(file_name)
        parsed = self._from_json_payload(payload)
        if import_format != ImportFormat.JSON:
            source_parsed = self._from_text_file(
                import_format=import_format,
                data=data,
                metadata=PolicyFileMetadata(
                    policy_no=payload.policy_no,
                    title=payload.title,
                    version=payload.version,
                    issuer=payload.issuer,
                    effective_date=payload.effective_date,
                ),
            )
            parsed.import_format = import_format
            parsed.raw_text = source_parsed.raw_text
            parsed.metadata = {
                **source_parsed.metadata,
                **payload.metadata,
                "confirmed_from_preview": True,
            }

        parsed.file_name = Path(file_name).name
        parsed.mime_type = mime_type or _default_mime_type(import_format)
        parsed.file_hash = actual_hash
        # 与合同一致，只有人工确认后才保存原始制度文件。
        saved_path = self._save_source_file(file_name, data)
        parsed.storage_uri = _relative_storage_uri(saved_path)
        try:
            result = self.repository.save_import(parsed)
        except Exception:
            saved_path.unlink(missing_ok=True)
            raise
        return self._build_import_response(result)

    def import_json(self, payload: PolicyJsonImportRequest) -> PolicyImportResponse:
        parsed = self._from_json_payload(payload)
        canonical_json = json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        parsed.file_hash = hashlib.sha256(canonical_json).hexdigest()
        return self._build_import_response(self.repository.save_import(parsed))

    def get_import_detail(self, document_id: str) -> PolicyImportDetail:
        return PolicyImportDetail.model_validate(
            self.repository.get_import_detail(document_id)
        )

    def _parse_source_file(
        self,
        *,
        file_name: str,
        data: bytes,
        file_metadata: PolicyFileMetadata | None,
    ) -> tuple[ImportFormat, ParsedPolicy]:
        import_format = detect_import_format(file_name)
        if import_format == ImportFormat.JSON:
            try:
                payload = PolicyJsonImportRequest.model_validate_json(data)
            except (ValidationError, ValueError, UnicodeDecodeError) as exc:
                raise DocumentParseError(
                    f"JSON 内容不符合制度依据导入结构：{exc}"
                ) from exc
            return import_format, self._from_json_payload(payload)
        if file_metadata is None:
            raise DocumentParseError(
                "PDF/TXT 制度解析必须提供 policy_no 和 title。"
            )
        return import_format, self._from_text_file(
            import_format=import_format,
            data=data,
            metadata=file_metadata,
        )

    def _from_text_file(
        self,
        *,
        import_format: ImportFormat,
        data: bytes,
        metadata: PolicyFileMetadata,
    ) -> ParsedPolicy:
        parse_metadata: dict[str, object] = {}
        if import_format == ImportFormat.PDF:
            raw_text, sections, parse_metadata = parse_pdf(data)
        else:
            raw_text = decode_text(data)
            sections = split_text_into_clauses(raw_text)
        return ParsedPolicy(
            policy_no=metadata.policy_no,
            title=metadata.title,
            version=metadata.version,
            issuer=metadata.issuer,
            effective_date=metadata.effective_date,
            import_format=import_format,
            raw_text=raw_text,
            sections=sections,
            metadata=parse_metadata,
        )

    def _from_json_payload(self, payload: PolicyJsonImportRequest) -> ParsedPolicy:
        sections = [
            ParsedClause(
                clause_no=section.section_no,
                title=section.title,
                content=section.content,
                page_no=section.page_no,
                metadata=section.metadata,
            )
            for section in payload.sections
        ]
        return ParsedPolicy(
            policy_no=payload.policy_no,
            title=payload.title,
            version=payload.version,
            issuer=payload.issuer,
            effective_date=payload.effective_date,
            import_format=ImportFormat.JSON,
            raw_text="\n\n".join(section.content for section in sections),
            sections=sections,
            mime_type="application/json",
            metadata=payload.metadata,
        )

    def _to_json_payload(self, parsed: ParsedPolicy) -> PolicyJsonImportRequest:
        return PolicyJsonImportRequest(
            policy_no=parsed.policy_no,
            title=parsed.title,
            version=parsed.version,
            issuer=parsed.issuer,
            effective_date=parsed.effective_date,
            sections=[
                {
                    "section_no": section.clause_no,
                    "title": section.title,
                    "content": section.content,
                    "page_no": section.page_no,
                    "metadata": section.metadata,
                }
                for section in parsed.sections
            ],
            metadata=parsed.metadata,
        )

    def _build_import_response(self, result: dict[str, object]) -> PolicyImportResponse:
        """制度事务提交后才投递向量任务，避免 Worker 读取未提交章节。"""

        vectorization = enqueue_document_vectorization(result["document_id"])
        result["vectorization_job_id"] = vectorization["job_id"]
        result["vectorization_status"] = vectorization["status"]
        result["message"] = {
            "QUEUED": "制度依据导入成功，向量化任务已进入队列。",
            "NOT_CONFIGURED": "制度依据导入成功；未配置 api-key 环境变量，未创建向量化任务。",
            "FAILED": "制度依据导入成功，但向量化任务投递失败。",
        }.get(vectorization["status"], "制度依据导入成功。")
        return PolicyImportResponse.model_validate(result)

    def _save_source_file(self, file_name: str, data: bytes) -> Path:
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(file_name).name)
        destination = settings.upload_dir / f"{uuid4().hex}_{safe_name or 'policy'}"
        destination.write_bytes(data)
        return destination


def _relative_storage_uri(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _default_mime_type(import_format: ImportFormat) -> str:
    return {
        ImportFormat.PDF: "application/pdf",
        ImportFormat.TXT: "text/plain",
        ImportFormat.JSON: "application/json",
    }[import_format]
