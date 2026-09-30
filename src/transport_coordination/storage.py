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
-- 委托运输收入清算域：参与方在协议内各扮演一种清算职责。
CREATE TABLE IF NOT EXISTS clearing_parties (
    party_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    kind TEXT NOT NULL CHECK(kind IN ('line_manager','operator','channel')),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(agreement_id, organization_id)
);
CREATE TABLE IF NOT EXISTS clearing_agreements (
    agreement_id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','active')),
    operating_party_id TEXT REFERENCES clearing_parties(party_id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    activated_at TEXT
);
-- 运行区段归属于唯一线路管理方，跨线列车在径路中顺序串联多个区段。
CREATE TABLE IF NOT EXISTS clearing_segments (
    segment_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL,
    code TEXT NOT NULL,
    from_station TEXT NOT NULL,
    to_station TEXT NOT NULL,
    length_km INTEGER NOT NULL CHECK(length_km > 0),
    line_manager_party_id TEXT NOT NULL REFERENCES clearing_parties(party_id),
    created_at TEXT NOT NULL,
    UNIQUE(agreement_id, code)
);
CREATE TABLE IF NOT EXISTS clearing_services (
    service_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('passenger','freight')),
    created_at TEXT NOT NULL,
    UNIQUE(agreement_id, code)
);
CREATE TABLE IF NOT EXISTS clearing_service_items (
    service_id TEXT NOT NULL REFERENCES clearing_services(service_id),
    item_code TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY(service_id, item_code)
);
CREATE TABLE IF NOT EXISTS clearing_service_route (
    service_id TEXT NOT NULL REFERENCES clearing_services(service_id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
    segment_id TEXT NOT NULL REFERENCES clearing_segments(segment_id),
    PRIMARY KEY(service_id, ordinal)
);
-- 运价版本：税率口径、渠道扣费、各区段里程单价的版本化文档，生效区间按业务发生日选取。
CREATE TABLE IF NOT EXISTS clearing_tariff_versions (
    tariff_version_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    effective_from TEXT NOT NULL,
    effective_through TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
    rules_json TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(agreement_id, version_no)
);
-- 运输事实只追加、按外部事件号幂等；tariff 在记账时按业务发生日固化，跨月回执不改历史口径。
CREATE TABLE IF NOT EXISTS transport_facts (
    fact_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL,
    external_event_id TEXT NOT NULL,
    tariff_version_id TEXT NOT NULL REFERENCES clearing_tariff_versions(tariff_version_id),
    service_id TEXT NOT NULL REFERENCES clearing_services(service_id),
    item_code TEXT NOT NULL,
    channel_party_id TEXT REFERENCES clearing_parties(party_id),
    fact_type TEXT NOT NULL CHECK(fact_type IN ('sale','change','partial_refund','suspension_compensation')),
    gross_cents INTEGER NOT NULL CHECK(gross_cents >= 0),
    related_external_id TEXT,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(agreement_id, external_event_id)
);
CREATE TABLE IF NOT EXISTS settlement_periods (
    period_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL,
    period_key TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    UNIQUE(agreement_id, period_key)
);
CREATE TABLE IF NOT EXISTS settlement_versions (
    version_id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL REFERENCES settlement_periods(period_id),
    agreement_id TEXT NOT NULL,
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    kind TEXT NOT NULL CHECK(kind IN ('regular','adjustment','correction')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','closed')),
    rule_snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    entries_hash TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    UNIQUE(period_id, version_no)
);
-- 任一运输事实在整个期间生命周期内只能进入一个版本，重放绝不重复计费。
CREATE TABLE IF NOT EXISTS fact_inclusions (
    fact_id TEXT PRIMARY KEY REFERENCES transport_facts(fact_id),
    version_id TEXT NOT NULL REFERENCES settlement_versions(version_id),
    included_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_entries (
    entry_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES settlement_versions(version_id),
    entry_key TEXT NOT NULL,
    fact_id TEXT REFERENCES transport_facts(fact_id),
    line_no INTEGER NOT NULL CHECK(line_no >= 1),
    account TEXT NOT NULL,
    beneficiary_party_id TEXT REFERENCES clearing_parties(party_id),
    responsible_party_id TEXT REFERENCES clearing_parties(party_id),
    amount_cents INTEGER NOT NULL,
    basis_json TEXT NOT NULL,
    correlation TEXT,
    status TEXT NOT NULL CHECK(status IN ('memo','projected','payable','paid','disputed','carried')),
    carried_from_entry_id TEXT REFERENCES settlement_entries(entry_id),
    confirmed_at TEXT,
    paid_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(version_id, entry_key)
);
CREATE TABLE IF NOT EXISTS settlement_disputes (
    dispute_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES settlement_versions(version_id),
    entry_id TEXT NOT NULL REFERENCES settlement_entries(entry_id),
    raised_by_party_id TEXT NOT NULL REFERENCES clearing_parties(party_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','withdrawn','carried')),
    prior_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    withdrawn_at TEXT
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
