from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from app.core.config import PROJECT_ROOT, settings
from app.modules.contract_import.parsers import (
    decode_text,
    detect_import_format,
    parse_json_bytes,
    parse_pdf,
    split_text_into_clauses,
)
from app.modules.contract_import.exceptions import (
    DemoContractCleanupError,
    DocumentParseError,
    PreviewFileMismatchError,
)
from app.modules.contract_import.repository import ContractImportRepository
from app.modules.contract_import.schemas import (
    ContractFileMetadata,
    ContractImportDetail,
    ContractImportPreviewResponse,
    ContractImportResponse,
    ContractJsonImportRequest,
    DemoContractCleanupResponse,
    ImportFormat,
    ParsedClause,
    ParsedContract,
)
from app.modules.vectorization.service import enqueue_document_vectorization


class ContractImportService:
    """组织文件解析、人工确认和最终持久化的业务流程。"""

    DEMO_CONTRACT_NO = "EVAL-STRESS-001"

    def __init__(self, repository: ContractImportRepository) -> None:
        self.repository = repository

    def preview_file(
        self,
        *,
        file_name: str,
        mime_type: str | None,
        data: bytes,
        file_metadata: ContractFileMetadata | None,
    ) -> ContractImportPreviewResponse:
        """生成前端可编辑的标准 JSON；预览阶段不保存文件和业务数据。"""

        import_format, parsed = self._parse_source_file(
            file_name=file_name,
            data=data,
            file_metadata=file_metadata,
        )
        payload = self._to_json_payload(parsed)
        warnings: list[str] = []
        if import_format == ImportFormat.PDF:
            warnings.append("PDF 当前按页解析，跨页条款可能需要人工合并。")

        return ContractImportPreviewResponse(
            preview_hash=hashlib.sha256(data).hexdigest(),
            source_format=import_format,
            file_name=Path(file_name).name,
            file_size=len(data),
            mime_type=mime_type or _default_mime_type(import_format),
            clause_count=len(payload.clauses),
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
        payload: ContractJsonImportRequest,
    ) -> ContractImportResponse:
        """确认原文件未变化后，将用户确认的条款事务写入数据库。"""

        # SHA-256 用于证明确认时上传的文件就是之前解析预览的文件。
        actual_hash = hashlib.sha256(data).hexdigest()
        if actual_hash != preview_hash:
            raise PreviewFileMismatchError("确认时的文件与预览文件不一致，请重新解析。")

        import_format = detect_import_format(file_name)
        if import_format == ImportFormat.JSON:
            parsed = self._from_json_payload(payload)
        else:
            source_metadata = ContractFileMetadata(
                contract_no=payload.contract_no,
                name=payload.name,
                contract_type_code=payload.contract_type_code,
                counterparty=payload.counterparty,
                amount=payload.amount,
                currency=payload.currency,
                document_title=payload.document_title,
            )
            source_parsed = self._from_text_file(
                import_format=import_format,
                data=data,
                metadata=source_metadata,
            )
            parsed = self._from_json_payload(payload)
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
        # 原始文件延迟到确认阶段才保存，避免用户放弃确认时留下无效文件。
        saved_path = self._save_source_file(file_name, data)
        parsed.storage_uri = _relative_storage_uri(saved_path)
        try:
            result = self.repository.save_import(parsed)
        except Exception:
            saved_path.unlink(missing_ok=True)
            raise
        return self._build_import_response(result)

    def import_json(self, payload: ContractJsonImportRequest) -> ContractImportResponse:
        parsed = self._from_json_payload(payload)
        canonical_json = json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        parsed.file_hash = hashlib.sha256(canonical_json).hexdigest()
        result = self.repository.save_import(parsed)
        return self._build_import_response(result)

    def get_import_detail(self, document_id: str) -> ContractImportDetail:
        result = self.repository.get_import_detail(document_id)
        return ContractImportDetail.model_validate(result)

    def delete_demo_contract(self) -> DemoContractCleanupResponse:
        """只清理固定演示合同，禁止调用方指定任意业务合同编号。"""

        try:
            result = self.repository.delete_contract_data(self.DEMO_CONTRACT_NO)
        except Exception as exc:
            # 数据库错误统一转换为不包含 SQL、连接串或合同正文的稳定 API 错误。
            raise DemoContractCleanupError("示例合同数据清理失败，请稍后重试。") from exc
        if result["deleted"]:
            result["message"] = (
                "示例合同及其文档、条款、风险审查、审批、问答、"
                "工作流轨迹和异步任务记录已全部清理。"
            )
        else:
            result["message"] = "数据库中没有需要清理的示例合同数据。"
        return DemoContractCleanupResponse.model_validate(result)

    def _build_import_response(self, result: dict[str, object]) -> ContractImportResponse:
        """在导入事务提交后创建异步向量任务，并合并为接口响应。"""

        # 向量任务必须放在 save_import 返回之后创建，确保 Celery Worker 不会读到尚未提交的条款。
        # 即使 Redis 不可用，合同导入也已经成功；此时记录 FAILED 状态供前端明确展示。
        vectorization = enqueue_document_vectorization(result["document_id"])
        result["vectorization_job_id"] = vectorization["job_id"]
        result["vectorization_status"] = vectorization["status"]
        result["message"] = {
            "QUEUED": "合同及条款导入成功，向量化任务已进入队列。",
            "NOT_CONFIGURED": "合同及条款导入成功；未配置 api-key 环境变量，未创建向量化任务。",
            "FAILED": "合同及条款导入成功，但向量化任务投递失败。",
        }.get(vectorization["status"], "合同及条款导入成功。")
        return ContractImportResponse.model_validate(result)

    def _from_text_file(
        self,
        *,
        import_format: ImportFormat,
        data: bytes,
        metadata: ContractFileMetadata,
    ) -> ParsedContract:
        parse_metadata: dict[str, object] = {}
        if import_format == ImportFormat.PDF:
            raw_text, clauses, parse_metadata = parse_pdf(data)
        else:
            raw_text = decode_text(data)
            clauses = split_text_into_clauses(raw_text)

        return ParsedContract(
            contract_no=metadata.contract_no,
            name=metadata.name,
            contract_type_code=metadata.contract_type_code,
            counterparty=metadata.counterparty,
            amount=metadata.amount,
            currency=metadata.currency,
            document_title=metadata.document_title or metadata.name,
            import_format=import_format,
            raw_text=raw_text,
            clauses=clauses,
            metadata=parse_metadata,
        )

    def _parse_source_file(
        self,
        *,
        file_name: str,
        data: bytes,
        file_metadata: ContractFileMetadata | None,
    ) -> tuple[ImportFormat, ParsedContract]:
        import_format = detect_import_format(file_name)
        if import_format == ImportFormat.JSON:
            return import_format, self._from_json_payload(parse_json_bytes(data))
        if file_metadata is None:
            raise DocumentParseError(
                "PDF/TXT 文件解析必须提供 contract_no、name 和 contract_type_code。"
            )
        return import_format, self._from_text_file(
            import_format=import_format,
            data=data,
            metadata=file_metadata,
        )

    def _from_json_payload(self, payload: ContractJsonImportRequest) -> ParsedContract:
        clauses = [
            ParsedClause(
                clause_no=clause.clause_no,
                title=clause.title,
                content=clause.content,
                page_no=clause.page_no,
                metadata=clause.metadata,
            )
            for clause in payload.clauses
        ]
        raw_text = "\n\n".join(clause.content for clause in clauses)
        return ParsedContract(
            contract_no=payload.contract_no,
            name=payload.name,
            contract_type_code=payload.contract_type_code,
            counterparty=payload.counterparty,
            amount=payload.amount,
            currency=payload.currency,
            document_title=payload.document_title or payload.name,
            import_format=ImportFormat.JSON,
            raw_text=raw_text,
            clauses=clauses,
            mime_type="application/json",
            metadata=payload.metadata,
        )

    def _to_json_payload(self, parsed: ParsedContract) -> ContractJsonImportRequest:
        return ContractJsonImportRequest(
            contract_no=parsed.contract_no,
            name=parsed.name,
            contract_type_code=parsed.contract_type_code,
            counterparty=parsed.counterparty,
            amount=parsed.amount,
            currency=parsed.currency,
            document_title=parsed.document_title,
            clauses=[
                {
                    "clause_no": clause.clause_no,
                    "title": clause.title,
                    "content": clause.content,
                    "page_no": clause.page_no,
                    "metadata": clause.metadata,
                }
                for clause in parsed.clauses
            ],
            metadata=parsed.metadata,
        )

    def _save_source_file(self, file_name: str, data: bytes) -> Path:
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(file_name).name)
        safe_name = safe_name or "contract"
        destination = settings.upload_dir / f"{uuid4().hex}_{safe_name}"
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
