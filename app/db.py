"""SQLite 连接、建库初始化与写事务控制。"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

# 数据库文件路径，可用环境变量 ROYALTY_DB_PATH 覆盖（Docker 中指向 /data）。
DB_PATH = os.environ.get("ROYALTY_DB_PATH", "./royalty.db")

# 进程内写锁：所有写事务串行执行，保证"并发入账与关账"严格按数据库提交顺序决定归属。
_write_lock = threading.RLock()

SCHEMA = """
PRAGMA journal_mode = WAL;

-- 作品：阶梯费率 + 作者分成
CREATE TABLE IF NOT EXISTS works (
    work_id            TEXT PRIMARY KEY,
    title              TEXT NOT NULL DEFAULT '',
    payload_hash       TEXT NOT NULL,               -- 幂等指纹
    cumulative_copies  INTEGER NOT NULL DEFAULT 0,  -- 按入账序号累计的销量（退款不回退）
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_tiers (
    work_id    TEXT NOT NULL REFERENCES works(work_id),
    tier_index INTEGER NOT NULL,
    threshold  INTEGER NOT NULL,   -- 累计销量达到该册数起适用本档费率（首档必须为 0）
    rate       TEXT NOT NULL,      -- 非负费率，存规范化十进制字符串
    PRIMARY KEY (work_id, tier_index)
);

CREATE TABLE IF NOT EXISTS work_authors (
    work_id   TEXT NOT NULL REFERENCES works(work_id),
    author_id TEXT NOT NULL,
    ratio     TEXT NOT NULL,       -- 分成比例（百分数），同一作品合计必须为 100
    PRIMARY KEY (work_id, author_id)
);

-- 全局入账序号：销售与退款共用，决定累计口径与关账归属
CREATE TABLE IF NOT EXISTS entries (
    entry_seq  INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_type TEXT NOT NULL CHECK (entry_type IN ('SALE', 'REFUND')),
    ref_id     TEXT NOT NULL,      -- sale_id 或 refund_id
    batch_id   INTEGER REFERENCES batches(batch_id),  -- NULL = 尚未关账
    UNIQUE (entry_type, ref_id)
);

CREATE TABLE IF NOT EXISTS sales (
    entry_seq       INTEGER PRIMARY KEY REFERENCES entries(entry_seq),
    sale_id         TEXT NOT NULL UNIQUE,
    work_id         TEXT NOT NULL REFERENCES works(work_id),
    business_time   TEXT NOT NULL,
    copies          INTEGER NOT NULL CHECK (copies > 0),
    price_cents     INTEGER NOT NULL CHECK (price_cents >= 0),
    base_copies     INTEGER NOT NULL,   -- 入账前累计销量
    royalty_cents   INTEGER NOT NULL,   -- 本笔版税合计（分）
    refunded_copies INTEGER NOT NULL DEFAULT 0,
    payload_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

-- 销售的分档计提明细（段），退款从最后一段起反向冲回
CREATE TABLE IF NOT EXISTS sale_segments (
    sale_id        TEXT NOT NULL REFERENCES sales(sale_id),
    seg_index      INTEGER NOT NULL,
    tier_index     INTEGER NOT NULL,
    threshold      INTEGER NOT NULL,  -- 该档门槛（快照）
    rate           TEXT NOT NULL,
    copies         INTEGER NOT NULL,
    per_copy_cents INTEGER NOT NULL,  -- 每册版税（分，四舍五入到分）
    start_no       INTEGER NOT NULL,  -- 该段覆盖的累计册次区间 [start_no, end_no]
    end_no         INTEGER NOT NULL,
    amount_cents   INTEGER NOT NULL,
    PRIMARY KEY (sale_id, seg_index)
);

CREATE TABLE IF NOT EXISTS sale_splits (
    sale_id      TEXT NOT NULL REFERENCES sales(sale_id),
    author_id    TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (sale_id, author_id)
);

CREATE TABLE IF NOT EXISTS refunds (
    entry_seq     INTEGER PRIMARY KEY REFERENCES entries(entry_seq),
    refund_id     TEXT NOT NULL UNIQUE,
    sale_id       TEXT NOT NULL REFERENCES sales(sale_id),
    copies        INTEGER NOT NULL CHECK (copies > 0),
    royalty_cents INTEGER NOT NULL,   -- 负数：冲回的版税
    payload_hash  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- 退款冲回的分段明细（金额为负）
CREATE TABLE IF NOT EXISTS refund_segments (
    refund_id      TEXT NOT NULL REFERENCES refunds(refund_id),
    seg_index      INTEGER NOT NULL,
    tier_index     INTEGER NOT NULL,
    rate           TEXT NOT NULL,
    copies         INTEGER NOT NULL,
    per_copy_cents INTEGER NOT NULL,
    amount_cents   INTEGER NOT NULL,
    PRIMARY KEY (refund_id, seg_index)
);

CREATE TABLE IF NOT EXISTS refund_splits (
    refund_id    TEXT NOT NULL REFERENCES refunds(refund_id),
    author_id    TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,    -- 负数：冲回的分账
    PRIMARY KEY (refund_id, author_id)
);

-- 关账批次与其不可改报表快照
CREATE TABLE IF NOT EXISTS batches (
    batch_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    closed_at           TEXT NOT NULL,
    sales_count         INTEGER NOT NULL DEFAULT 0,
    refunds_count       INTEGER NOT NULL DEFAULT 0,
    total_royalty_cents INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS batch_items (
    batch_id      INTEGER NOT NULL REFERENCES batches(batch_id),
    item_seq      INTEGER NOT NULL,
    entry_type    TEXT NOT NULL,
    ref_id        TEXT NOT NULL,
    entry_seq     INTEGER NOT NULL,
    work_id       TEXT NOT NULL,
    copies        INTEGER NOT NULL,
    royalty_cents INTEGER NOT NULL,
    splits_json   TEXT NOT NULL,   -- 分账快照
    detail_json   TEXT NOT NULL,   -- 分段/价格等明细快照
    PRIMARY KEY (batch_id, item_seq)
);

CREATE TABLE IF NOT EXISTS batch_author_totals (
    batch_id     INTEGER NOT NULL REFERENCES batches(batch_id),
    author_id    TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    PRIMARY KEY (batch_id, author_id)
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db() -> None:
    """建库建表（幂等），服务启动时调用。"""
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)
    with _write_lock:
        conn = connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()


@contextmanager
def write_tx():
    """写事务：进程内串行 + BEGIN IMMEDIATE，提交顺序即入账/关账的归属顺序。"""
    with _write_lock:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


@contextmanager
def read_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
