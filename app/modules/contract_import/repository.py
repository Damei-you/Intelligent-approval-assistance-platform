from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from app.core.database import open_connection
from app.modules.contract_import.exceptions import (
    ContractTypeNotFoundError,
    ImportRecordNotFoundError,
)
from app.modules.contract_import.schemas import ParsedContract


class ContractImportRepository:
    """使用 psycopg 执行合同导入所需的参数化 SQL。"""

    def save_import(self, contract: ParsedContract) -> dict[str, Any]:
        """在一个事务中保存合同、文档和全部条款。"""

        with open_connection() as connection:
            # 正常离开 transaction 代码块会提交；任何异常都会自动回滚全部写入。
            with connection.transaction():
                contract_type = connection.execute(
                    """
                    SELECT id
                    FROM contract_types
                    WHERE code = %s AND enabled = TRUE
                    """,
                    (contract.contract_type_code,),
                ).fetchone()
                if contract_type is None:
                    raise ContractTypeNotFoundError(
                        f"合同类型 {contract.contract_type_code} 不存在或未启用。"
                    )

                contract_row = connection.execute(
                    """
                    SELECT id
                    FROM contracts
                    WHERE contract_no = %s
                    FOR UPDATE
                    """,
                    (contract.contract_no,),
                ).fetchone()

                if contract_row is None:
                    contract_row = connection.execute(
                        """
                        INSERT INTO contracts (
                            contract_no, name, contract_type_id, counterparty,
                            amount, currency, status
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, 'PARSING')
                        RETURNING id
                        """,
                        (
                            contract.contract_no,
                            contract.name,
                            contract_type["id"],
                            contract.counterparty,
                            contract.amount,
                            contract.currency,
                        ),
                    ).fetchone()
                else:
                    connection.execute(
                        """
                        UPDATE contracts
                        SET name = %s,
                            contract_type_id = %s,
                            counterparty = %s,
                            amount = %s,
                            currency = %s,
                            status = 'PARSING'
                        WHERE id = %s
                        """,
                        (
                            contract.name,
                            contract_type["id"],
                            contract.counterparty,
                            contract.amount,
                            contract.currency,
                            contract_row["id"],
                        ),
                    )

                revision_row = connection.execute(
                    """
                    SELECT COALESCE(MAX(revision_no), 0) + 1 AS next_revision
                    FROM documents
                    WHERE contract_id = %s AND document_type = 'CONTRACT'
                    """,
                    (contract_row["id"],),
                ).fetchone()
                revision_no = revision_row["next_revision"]

                connection.execute(
                    """
                    UPDATE documents
                    SET is_current = FALSE
                    WHERE contract_id = %s
                      AND document_type = 'CONTRACT'
                      AND is_current = TRUE
                    """,
                    (contract_row["id"],),
                )

                document_metadata = {
                    **contract.metadata,
                    "import_format": contract.import_format.value,
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
                        'CONTRACT', %s, %s, %s, TRUE,
                        %s, %s, %s, %s, 'PARSED', %s, %s
                    )
                    RETURNING id
                    """,
                    (
                        contract_row["id"],
                        contract.document_title,
                        revision_no,
                        contract.storage_uri,
                        contract.file_name,
                        contract.mime_type,
                        contract.file_hash,
                        contract.raw_text,
                        # psycopg 的 Jsonb 适配器负责把 Python 字典安全转换成 PostgreSQL JSONB。
                        Jsonb(document_metadata),
                    ),
                ).fetchone()

                chunk_rows = [
                    (
                        document_row["id"],
                        index,
                        clause.clause_no,
                        clause.title,
                        clause.content,
                        clause.page_no,
                        Jsonb(clause.metadata),
                    )
                    for index, clause in enumerate(contract.clauses)
                ]
                # executemany 通过同一个游标批量插入条款，避免逐条创建数据库往返。
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO document_chunks (
                            document_id, chunk_index, chunk_type, clause_no,
                            title, content, page_no, metadata
                        )
                        VALUES (%s, %s, 'CONTRACT_CLAUSE', %s, %s, %s, %s, %s)
                        """,
                        chunk_rows,
                    )

                connection.execute(
                    "UPDATE contracts SET status = 'READY' WHERE id = %s",
                    (contract_row["id"],),
                )

                return {
                    "contract_id": contract_row["id"],
                    "document_id": document_row["id"],
                    "contract_no": contract.contract_no,
                    "revision_no": revision_no,
                    "import_format": contract.import_format,
                    "parse_status": "PARSED",
                    "clause_count": len(contract.clauses),
                    "vectorized": False,
                }

    def get_import_detail(self, document_id: str) -> dict[str, Any]:
        with open_connection() as connection:
            row = connection.execute(
                """
                SELECT
                    d.id AS document_id,
                    c.id AS contract_id,
                    c.contract_no,
                    c.name AS contract_name,
                    ct.code AS contract_type_code,
                    d.title AS document_title,
                    d.revision_no,
                    d.is_current,
                    d.file_name,
                    d.mime_type,
                    d.parse_status,
                    COUNT(dc.id)::INTEGER AS clause_count,
                    COUNT(dc.embedding)::INTEGER AS vectorized_clause_count,
                    d.created_at
                FROM documents d
                JOIN contracts c ON c.id = d.contract_id
                JOIN contract_types ct ON ct.id = c.contract_type_id
                LEFT JOIN document_chunks dc ON dc.document_id = d.id
                WHERE d.id = %s AND d.document_type = 'CONTRACT'
                GROUP BY d.id, c.id, ct.code
                """,
                (document_id,),
            ).fetchone()
        if row is None:
            raise ImportRecordNotFoundError(f"未找到文档 {document_id} 的导入记录。")
        return dict(row)

    def delete_contract_data(self, contract_no: str) -> dict[str, Any]:
        """按精确合同编号事务删除合同及全部数据库关联记录。"""

        with open_connection() as connection:
            # 合同主记录上的 FOR UPDATE 锁可避免清理期间并发创建新修订版本。
            # transaction() 正常结束时提交，任一步失败都会回滚，防止只删除部分数据。
            with connection.transaction():
                contract = connection.execute(
                    """
                    SELECT id
                    FROM contracts
                    WHERE contract_no = %s
                    FOR UPDATE
                    """,
                    (contract_no,),
                ).fetchone()
                if contract is None:
                    return {
                        "contract_no": contract_no,
                        "deleted": False,
                        "deleted_documents": 0,
                        "deleted_clauses": 0,
                        "deleted_reviews": 0,
                        "deleted_approvals": 0,
                        "deleted_chat_sessions": 0,
                        "deleted_async_jobs": 0,
                    }

                contract_id = contract["id"]
                document_rows = connection.execute(
                    "SELECT id FROM documents WHERE contract_id = %s",
                    (contract_id,),
                ).fetchall()
                review_rows = connection.execute(
                    "SELECT id FROM review_runs WHERE contract_id = %s",
                    (contract_id,),
                ).fetchall()
                counts = connection.execute(
                    """
                    SELECT
                        (
                            SELECT COUNT(*)::INTEGER
                            FROM documents
                            WHERE contract_id = %s
                        ) AS deleted_documents,
                        (
                            SELECT COUNT(*)::INTEGER
                            FROM document_chunks dc
                            JOIN documents d ON d.id = dc.document_id
                            WHERE d.contract_id = %s
                        ) AS deleted_clauses,
                        (
                            SELECT COUNT(*)::INTEGER
                            FROM review_runs
                            WHERE contract_id = %s
                        ) AS deleted_reviews,
                        (
                            SELECT COUNT(*)::INTEGER
                            FROM approval_instances
                            WHERE contract_id = %s
                        ) AS deleted_approvals,
                        (
                            SELECT COUNT(*)::INTEGER
                            FROM chat_sessions
                            WHERE contract_id = %s
                        ) AS deleted_chat_sessions
                    """,
                    (
                        contract_id,
                        contract_id,
                        contract_id,
                        contract_id,
                        contract_id,
                    ),
                ).fetchone()

                # async_jobs.resource_id 没有外键，数据库无法自动级联。
                # 这里同时覆盖合同、所有文档及所有审查任务，避免清理后留下孤立任务记录。
                resource_ids = [
                    contract_id,
                    *(row["id"] for row in document_rows),
                    *(row["id"] for row in review_rows),
                ]
                deleted_jobs = connection.execute(
                    """
                    DELETE FROM async_jobs
                    WHERE resource_id = ANY(%s::uuid[])
                    RETURNING id
                    """,
                    (resource_ids,),
                ).fetchall()

                # 审批、问答和审查都同时引用合同、文档或风险项。先按业务层级显式删除，
                # 可以避免 PostgreSQL 在级联删除 documents 时先遇到仍引用条款的证据外键。
                connection.execute(
                    "DELETE FROM approval_instances WHERE contract_id = %s",
                    (contract_id,),
                )
                connection.execute(
                    "DELETE FROM chat_sessions WHERE contract_id = %s",
                    (contract_id,),
                )
                connection.execute(
                    "DELETE FROM review_runs WHERE contract_id = %s",
                    (contract_id,),
                )

                # 正常业务数据的条款引用已随问答和审查删除。以下三条继续清理可能存在的
                # 跨流程历史引用，确保示例合同的条款和向量可以完整移除。
                connection.execute(
                    """
                    DELETE FROM finding_evidence
                    WHERE chunk_id IN (
                        SELECT dc.id
                        FROM document_chunks dc
                        JOIN documents d ON d.id = dc.document_id
                        WHERE d.contract_id = %s
                    )
                    """,
                    (contract_id,),
                )
                connection.execute(
                    """
                    DELETE FROM chat_message_citations
                    WHERE chunk_id IN (
                        SELECT dc.id
                        FROM document_chunks dc
                        JOIN documents d ON d.id = dc.document_id
                        WHERE d.contract_id = %s
                    )
                    """,
                    (contract_id,),
                )
                connection.execute(
                    """
                    DELETE FROM retrieval_hits
                    WHERE chunk_id IN (
                        SELECT dc.id
                        FROM document_chunks dc
                        JOIN documents d ON d.id = dc.document_id
                        WHERE d.contract_id = %s
                    )
                    """,
                    (contract_id,),
                )

                # 剩余 documents 和 document_chunks 通过 contracts 的外键级联统一清理。
                connection.execute(
                    "DELETE FROM contracts WHERE id = %s",
                    (contract_id,),
                )

                return {
                    "contract_no": contract_no,
                    "deleted": True,
                    **dict(counts),
                    "deleted_async_jobs": len(deleted_jobs),
                }
