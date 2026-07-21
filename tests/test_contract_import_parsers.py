from __future__ import annotations

import json
from io import BytesIO
import unittest

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.modules.contract_import.parsers import (
    decode_text,
    parse_pdf,
    parse_json_bytes,
    split_text_into_clauses,
)


class ContractImportParserTests(unittest.TestCase):
    def test_split_numbered_chinese_clauses(self) -> None:
        text = """采购合同

第一条 合同标的
供应方应按约定提供设备。

第二条 付款方式
验收通过后十个工作日内付款。"""

        clauses = split_text_into_clauses(text)

        self.assertEqual(3, len(clauses))
        self.assertEqual("前言", clauses[0].title)
        self.assertEqual("第一条", clauses[1].clause_no)
        self.assertEqual("合同标的", clauses[1].title)
        self.assertIn("十个工作日", clauses[2].content)

    def test_decode_gb18030_text(self) -> None:
        text = "第一条 交付\n应在约定日期交付。"
        self.assertEqual(text, decode_text(text.encode("gb18030")))

    def test_parse_structured_json(self) -> None:
        payload = {
            "contract_no": "TEST-001",
            "name": "测试采购合同",
            "contract_type_code": "PURCHASE",
            "clauses": [
                {
                    "clause_no": "第一条",
                    "title": "付款",
                    "content": "验收后付款。",
                }
            ],
        }

        parsed = parse_json_bytes(json.dumps(payload, ensure_ascii=False).encode())

        self.assertEqual("TEST-001", parsed.contract_no)
        self.assertEqual("第一条", parsed.clauses[0].clause_no)

    def test_parse_pdf_with_text_layer(self) -> None:
        pdf_data = _build_text_pdf("1. Payment Terms")

        raw_text, clauses, metadata = parse_pdf(pdf_data)

        self.assertIn("Payment Terms", raw_text)
        self.assertEqual("1.", clauses[0].clause_no)
        self.assertEqual(1, clauses[0].page_no)
        self.assertEqual(1, metadata["page_count"])


def _build_text_pdf(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)

    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): font_reference}
            )
        }
    )

    content = DecodedStreamObject()
    escaped_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped_text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)

    output = BytesIO()
    writer.write(output)
    return output.getvalue()


if __name__ == "__main__":
    unittest.main()
