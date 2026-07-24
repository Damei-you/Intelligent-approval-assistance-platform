from __future__ import annotations

import unittest

from app.main import app
from app.modules.contract_import.exceptions import DemoContractCleanupError
from app.modules.contract_import.service import ContractImportService


class FakeCleanupRepository:
    """记录服务传入的合同编号，避免单元测试连接真实数据库。"""

    def __init__(self, *, deleted: bool) -> None:
        self.deleted = deleted
        self.received_contract_no: str | None = None

    def delete_contract_data(self, contract_no: str) -> dict[str, object]:
        self.received_contract_no = contract_no
        return {
            "contract_no": contract_no,
            "deleted": self.deleted,
            "deleted_documents": 1 if self.deleted else 0,
            "deleted_clauses": 50 if self.deleted else 0,
            "deleted_reviews": 1 if self.deleted else 0,
            "deleted_approvals": 1 if self.deleted else 0,
            "deleted_chat_sessions": 1 if self.deleted else 0,
            "deleted_async_jobs": 2 if self.deleted else 0,
        }


class FailingCleanupRepository:
    """模拟数据库异常，验证接口层不会泄露底层错误详情。"""

    def delete_contract_data(self, contract_no: str) -> dict[str, object]:
        raise RuntimeError(f"不应暴露的数据库错误：{contract_no}")


class DemoContractCleanupTests(unittest.TestCase):
    """验证清理服务只能删除固定示例合同，并保持重复调用幂等。"""

    def test_cleanup_uses_fixed_demo_contract_number(self) -> None:
        repository = FakeCleanupRepository(deleted=True)
        service = ContractImportService(repository)  # type: ignore[arg-type]

        result = service.delete_demo_contract()

        self.assertEqual("EVAL-STRESS-001", repository.received_contract_no)
        self.assertTrue(result.deleted)
        self.assertEqual(50, result.deleted_clauses)
        self.assertIn("全部清理", result.message)

    def test_cleanup_returns_successful_noop_when_demo_contract_is_absent(self) -> None:
        repository = FakeCleanupRepository(deleted=False)
        service = ContractImportService(repository)  # type: ignore[arg-type]

        result = service.delete_demo_contract()

        self.assertFalse(result.deleted)
        self.assertEqual(0, result.deleted_documents)
        self.assertIn("没有需要清理", result.message)

    def test_cleanup_http_route_is_registered_as_delete_only(self) -> None:
        """OpenAPI 应公开固定清理路径，且不接收任意合同编号参数。"""

        operation = app.openapi()["paths"]["/api/v1/contracts/imports/demo"]

        self.assertEqual({"delete"}, set(operation))
        self.assertEqual([], operation["delete"].get("parameters", []))

    def test_cleanup_wraps_database_error_without_leaking_details(self) -> None:
        """数据库异常应转换为稳定业务错误，不返回 SQL 或底层异常。"""

        service = ContractImportService(FailingCleanupRepository())  # type: ignore[arg-type]

        with self.assertRaises(DemoContractCleanupError) as raised:
            service.delete_demo_contract()

        self.assertEqual("DEMO_CONTRACT_CLEANUP_ERROR", raised.exception.code)
        self.assertNotIn("数据库错误：EVAL", raised.exception.message)


if __name__ == "__main__":
    unittest.main()
