from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from app.core.database import open_connection
from app.modules.contract_import.exceptions import ImportRecordNotFoundError
from app.modules.policy_import.schemas import ParsedPolicy


class PolicyImportRepository:
    """使用 psycopg 事务保存制度文档、章节和修订版本。"""

    def save_import(self, policy: ParsedPolicy) -> dict[str, Any]:
        with open_connection() as connection:
            # 制度文档与全部章节在同一个事务（Transaction）中写入，任一步失败都会整体回滚。
            with connection.transaction():
                current_rows = connection.execute(
                    """
                    SELECT id, revision_no
                    FROM documents
                    WHERE document_type = 'POLICY'
                      AND metadata ->> 'policy_no' = %s
                    FOR UPDATE
                    """,
                    (policy.policy_no,),
                ).fetchall()
                revision_no = max(
                    (row["revision_no"] for row in current_rows),
                    default=0,
                ) + 1

                connection.execute(
                    """
                    UPDATE documents
                    SET is_current = FALSE
                    WHERE document_type = 'POLICY'
                      AND metadata ->> 'policy_no' = %s
                      AND is_current = TRUE
                    """,
                    (policy.policy_no,),
                )

                document_metadata = {
                    **policy.metadata,
                    "policy_no": policy.policy_no,
                    "version": policy.version,
                    "issuer": policy.issuer,
                    "effective_date": (
                        policy.effective_date.isoformat()
                        if policy.effective_date
                        else None
                    ),
                    "import_format": policy.import_format.value,
                    "vectorized": False,
                }
                document_row = connection.execute(
                    """
                    INSERT INTO documents (
                        document_type, contract_id, title, revision_no, is_current,
                        storage_uri, file_name, mime_type, file_hash, parse_status,
                        raw_text, metadata
                    )
                    VALUES (
                        'POLICY', NULL, %s, %s, TRUE,
                        %s, %s, %s, %s, 'PARSED', %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        policy.title,
                        revision_no,
                        policy.storage_uri,
                        policy.file_name,
                        policy.mime_type,
                        policy.file_hash,
                        policy.raw_text,
                        # Jsonb 适配器把 Python 字典安全转换成 PostgreSQL JSONB 参数。
                        Jsonb(document_metadata),
                    ),
                ).fetchone()

                rows = [
                    (
                        document_row["id"],
                        index,
                        section.clause_no,
                        section.title,
                        section.content,
                        section.page_no,
                        Jsonb(section.metadata),
                    )
                    for index, section in enumerate(policy.sections)
                ]
                # executemany 复用游标批量写入章节，减少数据库往返次数。
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO document_chunks (
                            document_id, chunk_index, chunk_type, clause_no,
                            title, content, page_no, metadata
                        )
                        VALUES (%s, %s, 'POLICY_SECTION', %s, %s, %s, %s, %s)
                        """,
                        rows,
                    )

                return {
                    "document_id": document_row["id"],
                    "policy_no": policy.policy_no,
                    "title": policy.title,
                    "revision_no": revision_no,
                    "import_format": policy.import_format,
                    "parse_status": "PARSED",
                    "section_count": len(policy.sections),
                    "vectorized": False,
                }

    def get_import_detail(self, document_id: str) -> dict[str, Any]:
        with open_connection() as connection:
            row = connection.execute(
                """
                SELECT
                    d.id AS document_id,
                    d.metadata ->> 'policy_no' AS policy_no,
                    d.title,
                    COALESCE(d.metadata ->> 'version', 'V1.0') AS version,
                    d.metadata ->> 'issuer' AS issuer,
                    NULLIF(d.metadata ->> 'effective_date', '')::DATE AS effective_date,
                    d.revision_no,
                    d.is_current,
                    d.file_name,
                    d.mime_type,
                    d.parse_status,
                    COUNT(dc.id)::INTEGER AS section_count,
                    COUNT(dc.embedding)::INTEGER AS vectorized_section_count,
                    d.created_at
                FROM documents d
                LEFT JOIN document_chunks dc ON dc.document_id = d.id
                WHERE d.id = %s AND d.document_type = 'POLICY'
                GROUP BY d.id
                """,
                (document_id,),
            ).fetchone()
        if row is None:
            raise ImportRecordNotFoundError(f"未找到制度文档 {document_id} 的导入记录。")
        return dict(row)
