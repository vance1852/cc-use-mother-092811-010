"""委托运输收入清算的离线端到端验收。

在临时 SQLite 库中走完一条完整链：多方协议与运价版本、跨线联程售票、改签精确冲回、
部分退运、停运赔付、跨月回执、按职责确认、争议只冻结单条分录、关账、晚到事件进入
新版本、关账后以追加分录跨月更正，最后由监管角色还原任一期间并校验审计哈希链。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..clock import FixedClock
from ..storage import Database
from .service import ClearingService
from ..service import DomainService

# 参与方组织
O_ADMIN = "org-admin"
O_LINE = "org-line"      # 线路管理方
O_CARRIER = "org-carrier"  # 客货运营方
O_CHANNEL = "org-channel"  # 代售渠道

AGREEMENT = "agr-001"
FARE_V1 = "fare-2026-09"
PERIOD = "2026-09"


def _bootstrap(base: DomainService) -> None:
    for org, name in ((O_ADMIN, "清算管理机构"), (O_LINE, "线路管理方"),
                     (O_CARRIER, "客货运营方"), (O_CHANNEL, "代售渠道")):
        base.register_organization(request_id=f"org-{org}", actor_id="bootstrap",
                                   organization_id=org, name=name)
    base.register_actor(request_id="actor-adm", actor_id="bootstrap", new_actor_id="adm",
                        display_name="管理员", role="admin", organization_id=O_ADMIN)
    base.register_actor(request_id="actor-l", actor_id="adm", new_actor_id="opl",
                        display_name="线路经办", role="operator", organization_id=O_LINE)
    base.register_actor(request_id="actor-c", actor_id="adm", new_actor_id="opc",
                        display_name="运营经办", role="operator", organization_id=O_CARRIER)
    base.register_actor(request_id="actor-h", actor_id="adm", new_actor_id="oph",
                        display_name="渠道经办", role="operator", organization_id=O_CHANNEL)
    base.register_actor(request_id="actor-aud", actor_id="adm", new_actor_id="aud",
                        display_name="监管审计", role="auditor", organization_id=O_ADMIN)


def _rules(svc: ClearingService) -> None:
    svc.register_agreement(
        request_id="agr", actor_id="adm", agreement_id=AGREEMENT,
        party_orgs=[O_LINE, O_CARRIER, O_CHANNEL])
    svc.register_fare_version(
        request_id="fare", actor_id="adm", version_id=FARE_V1, agreement_id=AGREEMENT,
        effective_date="2026-09-01", channel_org=O_CHANNEL,
        channel_bp=500, tax_bp=1000, tax_mode="inclusive",
        comp_line_bp=3000, comp_carrier_bp=7000,
        segments=[
            # 区段A：100 公里 × 200 分/公里；区段B：300 公里 × 100 分/公里（联程按里程权重拆票）
            {"segment_id": "seg-a", "line_owner_org": O_LINE, "carrier_org": O_CARRIER,
             "mileage": 100, "per_km_cents": 200, "line_bp": 4000, "carrier_bp": 6000},
            {"segment_id": "seg-b", "line_owner_org": O_LINE, "carrier_org": O_CARRIER,
             "mileage": 300, "per_km_cents": 100, "line_bp": 4000, "carrier_bp": 6000},
        ])


def _events(svc: ClearingService) -> None:
    # 联程售票：运费 11000（价内税口径下收讫=运费+服务费）
    sale = {
        "event_id": "ev-sale-1", "agreement_id": AGREEMENT, "type": "sale",
        "service_date": "2026-09-05", "segment_ids": ["seg-a", "seg-b"],
        "fare_cents": 11000, "service_fee_cents": 0, "collected_cents": 11000,
    }
    svc.record_event(request_id="ev1", actor_id="opc", event=sale)
    # 重放同一 request_id：返回原回执，绝不重复计费
    replay = svc.record_event(request_id="ev1", actor_id="opc", event=sale)
    assert replay["replayed"] is True

    # 改签：原票逐行精确冲回 + 新票 + 改签手续费 200
    svc.record_event(request_id="ev2", actor_id="opc", event={
        "event_id": "ev-change-1", "agreement_id": AGREEMENT, "type": "change",
        "service_date": "2026-09-12", "occurred_at": "2026-09-06T09:00:00Z",
        "segment_ids": ["seg-a", "seg-b"], "ref_event_id": "ev-sale-1",
        "fare_cents": 12100, "service_fee_cents": 0, "collected_cents": 12100,
        "change_fee_cents": 200,
    })

    # 部分退运：退一半运费（按原票各收入行权重精确冲回），退票费 50
    svc.record_event(request_id="ev3", actor_id="opc", event={
        "event_id": "ev-refund-1", "agreement_id": AGREEMENT, "type": "refund",
        "service_date": "2026-09-12", "occurred_at": "2026-09-08T10:00:00Z",
        "segment_ids": ["seg-a", "seg-b"], "ref_event_id": "ev-change-1",
        "ratio_bp": 5000, "refund_fee_cents": 50,
    })

    # 停运赔付 1000，线路/运营按 30/70 分担
    svc.record_event(request_id="ev4", actor_id="opc", event={
        "event_id": "ev-cancel-1", "agreement_id": AGREEMENT, "type": "cancel_payout",
        "service_date": "2026-09-15", "segment_ids": ["seg-a", "seg-b"],
        "compensation_cents": 1000,
    })

    # 跨月回执：9 月运输、10 月才收到回执，仅凭证，不产生金额
    svc.record_event(request_id="ev5", actor_id="oph", event={
        "event_id": "ev-receipt-1", "agreement_id": AGREEMENT, "type": "receipt",
        "service_date": "2026-09-30", "occurred_at": "2026-10-03T08:00:00Z",
    })


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "clearing_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))
        base = DomainService(database, clock)
        svc = ClearingService(database, clock)
        _bootstrap(base)
        _rules(svc)
        _events(svc)

        # 1) 生成 v1 草案：所有事件分录零和
        v1 = svc.build_draft(request_id="draft-v1", actor_id="adm", period=PERIOD)
        assert v1["zero_sum"] is True
        assert v1["version_no"] == 1
        totals_v1 = v1["totals"]
        assert sum(bucket["total_amount"] for bucket in totals_v1.values()) == 0
        # 跨月回执不产生任何分录，但计入事件集合
        v1_snapshot = svc.audit_reconstruction("aud", period=PERIOD)["versions"][0]
        assert "ev-receipt-1" in v1_snapshot["events"]

        # 2) 运营方对自己一条 carrier 分录提争议：只冻结该行
        draft = svc.get_draft("aud", period=PERIOD, version_no=1)
        carrier_line = next(line for line in draft["lines"]
                            if line["account"] == O_CARRIER and line["kind"] == "carrier")
        svc.open_dispute(request_id="dp1", actor_id="opc", period=PERIOD,
                         line_id=carrier_line["line_id"], event_id=carrier_line["event_id"],
                         reason="区段里程口径待核")

        # 3) 三方按职责确认，无争议收入继续支付；管理员关账 v1
        svc.confirm_draft(request_id="cf-l", actor_id="opl", period=PERIOD, version_no=1)
        svc.confirm_draft(request_id="cf-c", actor_id="opc", period=PERIOD, version_no=1)
        svc.confirm_draft(request_id="cf-h", actor_id="oph", period=PERIOD, version_no=1)
        closed_v1 = svc.close_period(request_id="close-v1", actor_id="adm",
                                    period=PERIOD, version_no=1)
        balances_v1 = closed_v1["balances"]
        crr = balances_v1[O_CARRIER]
        assert crr["frozen_amount"] == carrier_line["amount"], crr
        assert crr["payable_amount"] == crr["total_amount"] - crr["frozen_amount"]
        # 无争议的线路方、渠道方冻结额为 0，照常支付
        assert balances_v1[O_LINE]["frozen_amount"] == 0
        assert balances_v1[O_CHANNEL]["frozen_amount"] == 0

        # 4) 月末后晚到的 9 月票务事件：不推翻 v1，进入 v2
        late_sale = {
            "event_id": "ev-sale-late", "agreement_id": AGREEMENT, "type": "sale",
            "service_date": "2026-09-25", "occurred_at": "2026-10-04T20:00:00Z",
            "segment_ids": ["seg-a"], "fare_cents": 2200,
            "service_fee_cents": 0, "collected_cents": 2200,
        }
        svc.record_event(request_id="ev6", actor_id="opc", event=late_sale)
        svc.record_event(request_id="ev6", actor_id="opc", event=late_sale)  # 重放不重复计费
        v2 = svc.build_draft(request_id="draft-v2", actor_id="adm", period=PERIOD)
        assert v2["version_no"] == 2 and v2["zero_sum"] is True
        closed_v2 = svc.close_period(request_id="close-v2", actor_id="adm", period=PERIOD)
        assert closed_v2["version_no"] == 2

        # 5) 关账后更正：10 月以零和追加分录进入新版本，历史版本不动
        svc.record_event(request_id="ev7", actor_id="adm", event={
            "event_id": "ev-adjust-1", "agreement_id": AGREEMENT, "type": "adjustment",
            "service_date": "2026-10-06", "occurred_at": "2026-10-06T09:00:00Z",
            "adjustment_lines": [
                {"account": O_CARRIER, "kind": "adjust", "amount": 100, "segment_id": "seg-a"},
                {"account": "@customer", "kind": "adjust", "amount": -100},
            ],
        })
        oct_draft = svc.build_draft(request_id="draft-oct", actor_id="adm", period="2026-10")
        assert oct_draft["zero_sum"] is True

        # 6) 参与方可见性：线路方只读自己的明细，拿不到审计还原；监管可读全貌
        line_view = svc.get_lines("opl", period=PERIOD)
        assert line_view["items"] and all(item["account"] == O_LINE for item in line_view["items"])
        admin_view = svc.get_lines("aud", period=PERIOD)
        assert len(admin_view["items"]) > len(line_view["items"])
        recon = svc.audit_reconstruction("aud", period=PERIOD)
        assert [v["version_no"] for v in recon["versions"]] == [1, 2]
        v1_audit = recon["versions"][0]
        assert v1_audit["status"] == "closed"
        assert any(d["status"] == "open" for d in v1_audit["disputes"])
        assert sum(line["amount"] for line in v1_audit["lines"]) == 0
        assert AGREEMENT in v1_audit["basis"]  # 规则指纹可复算依据
        # 最新关账余额可还原
        balance_latest = svc.get_balance("aud", period=PERIOD)
        assert balance_latest["version_no"] == 2

        valid, event_count = base.verify_audit()
        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "v1_balances": balances_v1,
            "v2_balances": closed_v2["balances"],
            "v1_line_count": len(v1_audit["lines"]),
            "v2_line_count": len(recon["versions"][1]["lines"]),
            "frozen_carrier_cents": crr["frozen_amount"],
            "basis_fingerprint": v1_audit["basis"][AGREEMENT],
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
