# -*- coding: utf-8 -*-
"""数据库访问层: 连接、脚本执行、批量写入。"""
from __future__ import annotations

import io
import sys
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as PgConnection

from config import DB_SCHEMA, DSN


def log(msg: str) -> None:
    print(msg, flush=True)


if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


#: 每个线程复用一个只读连接。
#: 建连约 85 ms, 而 GUI 生成勾选项文案 / 装配数据会发起上百次查询 ——
#: 不复用连接时仅打开窗口就要 ~50 s (实测), 复用后降到 1~2 s。
#: 用 thread_local 是因为 GUI 的模拟跑在独立线程, psycopg2 连接不能跨线程共用。
_LOCAL = threading.local()


def _cached_conn() -> PgConnection:
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None and conn.closed == 0:
        return conn
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET search_path TO %s, public" % DB_SCHEMA)
    _LOCAL.conn = conn
    return conn


@contextmanager
def connect(autocommit: bool = False) -> Iterator[PgConnection]:
    """事务型连接 (每次新建; 写入/DDL 用这个)。"""
    conn = psycopg2.connect(DSN)
    conn.autocommit = autocommit
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO %s, public" % DB_SCHEMA)
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_sql_file(path) -> None:
    """执行 .sql 文件中的全部语句 (psycopg2 支持多语句)。"""
    sql = open(path, "r", encoding="utf-8").read()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    log(f"[db] 已执行 {path}")


def bulk_insert(
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    page_size: int = 1000,
    on_conflict: str = "",
) -> int:
    """批量插入, 返回插入行数。on_conflict 例如 'ON CONFLICT DO NOTHING'。"""
    rows = list(rows)
    if not rows:
        return 0
    collist = ", ".join(columns)
    sql = f"INSERT INTO {DB_SCHEMA}.{table} ({collist}) VALUES %s {on_conflict}".strip()
    with connect() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, rows, page_size=page_size)
    return len(rows)


def jsonb(obj: Any):
    """包装为 JSONB 适配对象。"""
    return psycopg2.extras.Json(obj)


def fetch_all(sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
    """只读查询: 复用线程内的常驻连接 (autocommit), 避免每次建连。"""
    conn = _cached_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(sql: str, params: Sequence[Any] | None = None):
    conn = _cached_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def scalar(sql: str, params: Sequence[Any] | None = None):
    row = fetch_one(sql, params)
    return row[0] if row else None
