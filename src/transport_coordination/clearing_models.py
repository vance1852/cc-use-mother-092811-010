"""委托运输收入清算域在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Party:
    party_id: str
    agreement_id: str
    organization_id: str
    kind: str  # line_manager / operator / channel
    name: str


@dataclass(frozen=True)
class Segment:
    segment_id: str
    agreement_id: str
    code: str
    from_station: str
    to_station: str
    length_km: int
    line_manager_party_id: str


@dataclass(frozen=True)
class TariffVersion:
    tariff_version_id: str
    agreement_id: str
    version_no: int
    effective_from: str
    effective_through: str
    status: str
    rules: dict[str, Any]
    rules_hash: str


@dataclass(frozen=True)
class TransportFact:
    fact_id: str
    agreement_id: str
    external_event_id: str
    tariff_version_id: str
    service_id: str
    item_code: str
    channel_party_id: str | None
    fact_type: str
    gross_cents: int
    related_external_id: str | None
    payload: dict[str, Any]
    occurred_at: str
    acknowledged_at: str


@dataclass(frozen=True)
class SettlementPeriod:
    period_id: str
    agreement_id: str
    period_key: str
    start_date: str
    end_date: str
    status: str


@dataclass(frozen=True)
class SettlementVersion:
    version_id: str
    period_id: str
    agreement_id: str
    version_no: int
    kind: str
    status: str
    snapshot_hash: str
    entries_hash: str | None
    fact_count: int = 0


@dataclass(frozen=True)
class SettlementEntry:
    entry_id: str
    version_id: str
    entry_key: str
    fact_id: str | None
    line_no: int
    account: str
    beneficiary_party_id: str | None
    responsible_party_id: str | None
    amount_cents: int
    basis: dict[str, Any]
    correlation: str | None
    status: str


@dataclass(frozen=True)
class Dispute:
    dispute_id: str
    version_id: str
    entry_id: str
    raised_by_party_id: str
    reason: str
    status: str
    prior_status: str
