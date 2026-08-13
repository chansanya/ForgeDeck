"""统一读取不同 SQLAlchemy 方言的 DML 影响行数。"""

from __future__ import annotations

from typing import Protocol, cast


class _RowcountResult(Protocol):
    rowcount: int


def affected_rows(result: object) -> int:
    """Return the DBAPI row count for a SQLAlchemy DML result."""

    return cast(_RowcountResult, result).rowcount
