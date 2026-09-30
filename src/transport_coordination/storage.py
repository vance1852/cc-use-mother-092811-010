"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
-- ===== 委托运输收入清算子系统（全部只追加，关账后不改写历史行）=====
CREATE TABLE IF NOT EXISTS clearing_agreements (
    agreement_id TEXT PRIMARY KEY,
    currency TEXT NOT NULL,
    party_orgs_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clearing_fare_versions (
    version_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    channel_org TEXT NOT NULL,
    channel_bp INTEGER NOT NULL CHECK(channel_bp >= 0 AND channel_bp <= 10000),
    tax_bp INTEGER NOT NULL CHECK(tax_bp >= 0 AND tax_bp <= 10000),
    tax_mode TEXT NOT NULL CHECK(tax_mode IN ('inclusive', 'exclusive')),
    comp_line_bp INTEGER NOT NULL CHECK(comp_line_bp >= 0 AND comp_line_bp <= 10000),
    comp_carrier_bp INTEGER NOT NULL CHECK(comp_carrier_bp >= 0 AND comp_carrier_bp <= 10000),
    segments_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(agreement_id, version_id)
);
CREATE TABLE IF NOT EXISTS clearing_events (
    event_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('sale','change','refund','cancel_payout','receipt','adjustment')),
    service_date TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    ref_event_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    drafted_version INTEGER,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clearing_events_period ON clearing_events(service_date);
CREATE TABLE IF NOT EXISTS clearing_lines (
    line_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    period TEXT NOT NULL,
    segment_id TEXT NOT NULL DEFAULT '',
    account TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL,
    ref_line_id TEXT NOT NULL DEFAULT '',
    rules_fp TEXT NOT NULL DEFAULT '',
    settled_version INTEGER,
    PRIMARY KEY(line_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_clearing_lines_period ON clearing_lines(period);
CREATE INDEX IF NOT EXISTS idx_clearing_lines_event ON clearing_lines(event_id);
CREATE TABLE IF NOT EXISTS clearing_drafts (
    period TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    status TEXT NOT NULL CHECK(status IN ('open','confirmed','closed')),
    event_ids_json TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    totals_json TEXT NOT NULL,
    zero_sum INTEGER NOT NULL CHECK(zero_sum IN (0,1)),
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    confirmed_by_json TEXT NOT NULL DEFAULT '[]',
    closed_by TEXT,
    closed_at TEXT,
    PRIMARY KEY(period, version_no)
);
CREATE TABLE IF NOT EXISTS clearing_disputes (
    dispute_id TEXT PRIMARY KEY,
    period TEXT NOT NULL,
    line_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    account TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','released','upheld')),
    reason TEXT NOT NULL,
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT,
    resolution TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_disputes_line ON clearing_disputes(line_id, status);
CREATE TABLE IF NOT EXISTS clearing_balances (
    period TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    account TEXT NOT NULL,
    total_amount INTEGER NOT NULL,
    frozen_amount INTEGER NOT NULL,
    payable_amount INTEGER NOT NULL,
    closed_at TEXT NOT NULL,
    PRIMARY KEY(period, version_no, account)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
