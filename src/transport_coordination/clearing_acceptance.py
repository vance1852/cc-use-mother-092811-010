"""委托运输收入清算域的离线端到端验收。

在临时 SQLite 数据库中登记三方协议、版本化运价与跨线径路，回放联程销售、
改签、部分退运、停运赔付与跨月回执，完成草案复算、按职责确认、争议冻结、
无争议支付、关账与晚到更正，最后核对金额守恒、幂等、可见性与审计链。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clearing_service import ClearingService
from .clock import FixedClock
from .service import DomainService
from .storage import Database


def _seed(base: DomainService, clearing: ClearingService) -> None:
    base.register_organization(request_id="oh", actor_id="bootstrap", organization_id="org-hub", name="清算平台")
    base.register_actor(request_id="ar", actor_id="bootstrap", new_actor_id="root",
                        display_name="管理员", role="admin", organization_id="org-hub")
    for rid, oid, aid, name in (
            ("ol", "org-lm", "lm", "京沪高铁公司"),
            ("oo", "org-op", "op", "客运段"),
            ("oc", "org-ch", "ch", "代售网")):
        base.register_organization(request_id=rid, actor_id="root", organization_id=oid, name=name)
        base.register_actor(request_id="a" + aid, actor_id="root", new_actor_id=aid,
                            display_name=name + "经办人", role="operator", organization_id=oid)
    base.register_actor(request_id="aa", actor_id="root", new_actor_id="aud",
                        display_name="监管审计", role="auditor", organization_id="org-hub")
    clearing.create_agreement(request_id="ag", actor_id="root", agreement_id="ag1",
                              code="WT-2026", title="委托运输清算协议")
    for rid, pid, oid, kind in (
            ("pl", "lm1", "org-lm", "line_manager"),
            ("po", "op1", "org-op", "operator"),
            ("pc", "ch1", "org-ch", "channel")):
        clearing.register_party(request_id=rid, actor_id="root", agreement_id="ag1", party_id=pid,
                                organization_id=oid, kind=kind, name=pid)
    clearing.define_segment(request_id="s1", actor_id="root", agreement_id="ag1", segment_id="segA",
                            code="SEC-A", from_station="北京南", to_station="济南西", length_km=400,
                            line_manager_party_id="lm1")
    clearing.define_segment(request_id="s2", actor_id="root", agreement_id="ag1", segment_id="segB",
                            code="SEC-B", from_station="济南西", to_station="南京南", length_km=600,
                            line_manager_party_id="lm1")
    clearing.define_service(request_id="sv", actor_id="root", agreement_id="ag1", service_id="svc1",
                            code="G1", name="京沪G1", kind="passenger",
                            items=[{"item_code": "seat", "name": "二等座"}],
                            route=[{"ordinal": 1, "segment_id": "segA"},
                                   {"ordinal": 2, "segment_id": "segB"}])
    clearing.create_tariff_version(request_id="t1", actor_id="root", agreement_id="ag1", version_no=1,
                                   effective_from="2026-01-01", effective_through="2026-12-31",
                                   rules={"segment_prices": {"SEC-A": 100, "SEC-B": 100},
                                          "tax_rate_bp": 900, "channel_fee_rate_bp": 300})
    clearing.activate_agreement(request_id="ac", actor_id="root", agreement_id="ag1",
                                operating_party_id="op1")


def run() -> dict[str, object]:
    """执行完整清算链并返回可核对结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "clearing_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc))
        base = DomainService(database, clock)
        clearing = ClearingService(database, clock)
        _seed(base, clearing)

        def fact(rid, eid, ftype, gross, occ, ack, related=None, channel="ch1"):
            return clearing.record_fact(
                request_id=rid, actor_id="op", agreement_id="ag1", external_event_id=eid,
                service_id="svc1", item_code="seat", fact_type=ftype, gross_cents=gross,
                occurred_at=occ, acknowledged_at=ack, channel_party_id=channel,
                related_external_id=related)

        fact("f1", "E-SALE", "sale", 10000, "2026-09-10T09:00Z", "2026-09-10T09:05Z")
        fact("f2", "E-CHG", "change", 500, "2026-09-11T09:00Z", "2026-09-11T09:20Z",
             related="E-SALE")
        fact("f3", "E-REF", "partial_refund", 3000, "2026-09-12T09:00Z", "2026-10-02T22:00Z",
             related="E-SALE")
        fact("f4", "E-SUSP", "suspension_compensation", 1200, "2026-09-13T09:00Z",
             "2026-09-13T10:00Z", channel=None)

        clearing.open_period(request_id="per", actor_id="root", agreement_id="ag1",
                             period_key="2026-09", start_date="2026-09-01", end_date="2026-09-30")
        period_id = database.connection.execute(
            "SELECT period_id FROM settlement_periods").fetchone()[0]
        first = clearing.generate_settlement(request_id="gen", actor_id="root", period_id=period_id)
        replay = clearing.generate_settlement(request_id="gen", actor_id="root", period_id=period_id)
        version_id = first["resource_id"]

        report = clearing.settlement_report(actor_id="root", version_id=version_id)
        signed_total = 10000 + 500 - 3000 - 1200
        entries_sum = sum(e["amount_cents"] for e in report["entries"])

        disputed = next(e for e in report["entries"] if e["account"] == "line_revenue")
        clearing.raise_dispute(request_id="d1", actor_id="lm", version_id=version_id,
                               entry_id=disputed["entry_id"], reason="区段里程复核")
        for entry in report["entries"]:
            if entry["status"] == "projected" and entry["entry_id"] != disputed["entry_id"]:
                clearing.confirm_entry(request_id="cf-" + entry["entry_id"][:8], actor_id="root",
                                       version_id=version_id, entry_id=entry["entry_id"])
        paid = clearing.pay_confirmed(request_id="pay", actor_id="root", version_id=version_id)
        clearing.close_version(request_id="close", actor_id="root", version_id=version_id)
        clearing.close_period(request_id="pclose", actor_id="root", period_id=period_id)

        # 关账后晚到的 9 月票务事件（10 月才回执）→ correction 新版本追加分录。
        fact("f5", "E-LATE", "partial_refund", 1000, "2026-09-20T09:00Z", "2026-10-05T22:00Z",
             related="E-SALE")
        correction = clearing.generate_settlement(request_id="gen2", actor_id="root",
                                                  period_id=period_id)

        channel_view = clearing.settlement_report(actor_id="ch", version_id=version_id)
        audited = clearing.audit_period(actor_id="aud", period_id=period_id)
        audit_valid, event_count = base.verify_audit()

        result = {
            "status": "ok",
            "audit_valid": audit_valid,
            "audit_events": event_count,
            "first_replayed": first["replayed"],
            "generate_replayed": replay["replayed"],
            "conservation_ok": entries_sum == signed_total,
            "entries_sum_cents": entries_sum,
            "paid_count": paid["paid_count"],
            "disputed_frozen": disputed["entry_id"] in {
                e["entry_id"] for e in report["entries"]},
            "correction_kind": correction["kind"],
            "correction_fact_count": correction["fact_count"],
            "carried_count": correction["carried_count"],
            "channel_isolated": all(
                e["beneficiary_party_id"] == "ch1" or e["responsible_party_id"] == "ch1"
                for e in channel_view["entries"]) and not any(
                e["account"] == "line_revenue" for e in channel_view["entries"]),
            "audit_versions": [v["version_no"] for v in audited["versions"]],
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并以退出码反映守恒与审计是否通过。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"] and result["conservation_ok"]
          and result["generate_replayed"] and not result["first_replayed"]
          and result["correction_kind"] == "correction" and result["channel_isolated"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
