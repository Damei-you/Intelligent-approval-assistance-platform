from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from app.core.config import settings


@contextmanager
def open_connection() -> Iterator[Connection]:
    """提供自动关闭的 psycopg 数据库连接。

    contextmanager 让调用方可以使用 ``with``。离开代码块时无论成功还是异常，
    都会关闭连接，避免连接泄漏；事务的提交和回滚由调用方显式控制。
    """

    # dict_row 使查询结果可以通过 row["字段名"] 读取，比按位置读取更容易理解。
    connection = psycopg.connect(settings.database_url, row_factory=dict_row)
    try:
        yield connection
    finally:
        connection.close()
