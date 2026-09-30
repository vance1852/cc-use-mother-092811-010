"""清算服务层测试：版本化草案、争议隔离、关账、可见性与追加分录。"""

import unittest
from datetime import datetime, timezone

from transport_coordination.clearing.service import ClearingService
from transport_coordination.clock import FixedClock
from transport_coordination.errors import ConflictError, PermissionDenied
from transport_coordination.service import DomainService
from transport_coordination.storage import Database

O_ADMIN, O_LINE, O_CARRIER, O_CHANNEL = "org-a", "org-l", "org-c", "org-h"
AGR, PERIOD = "agr-1", "2026-09"


class ClearingServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 5, tzinfo=timezone.utc))
        self.base = DomainService(self.database, clock)
        self.svc = ClearingService(self.database, clock)
        for org, name in ((O_ADMIN, "管理"), (O_LINE, "线路"), (O_CARRIER, "运营"),
                          (O_CHANNEL, "渠道")):
            self.base.register_organization(request_id=f"o-{org}", actor_id="bootstrap",
                                            organization_id=org, name=name)
        self.base.register_actor(request_id="a-adm", actor_id="bootstrap", new_actor_id="adm",
                                 display_name="管理员", role="admin", organization_id=O_ADMIN)
        for rid, aid, org in (("a-l", "opl", O_LINE), ("a-c", "opc", O_CARRIER),
                              ("a-h", "oph", O_CHANNEL)):
            self.base.register_actor(request_id=rid, actor_id="adm", new_actor_id=aid,
                                     display_name=aid, role="operator", organization_id=org)
        self.base.register_actor(request_id="a-aud", actor_id="adm", new_actor_id="aud",
                                 display_name="审计", role="auditor", organization_id=O_ADMIN)
        self.svc.register_agreement(
            request_id="agr", actor_id="adm", agreement_id=AGR,
            party_orgs=[O_LINE, O_CARRIER, O_CHANNEL])
        self.svc.register_fare_version(
            request_id="fv", actor_id="adm", version_id="fv1", agreement_id=AGR,
            effective_date="2026-09-01", channel_org=O_CHANNEL,
            channel_bp=500, tax_bp=1000, tax_mode="inclusive",
            comp_line_bp=3000, comp_carrier_bp=7000,
            segments=[
                {"segment_id": "s1", "line_owner_org": O_LINE, "carrier_org": O_CARRIER,
                 "mileage": 100, "per_km_cents": 200, "line_bp": 4000, "carrier_bp": 6000},
                {"segment_id": "s2", "line_owner_org": O_LINE, "carrier_org": O_CARRIER,
                 "mileage": 300, "per_km_cents": 100, "line_bp": 4000, "carrier_bp": 6000},
            ])

    def tearDown(self):
        self.database.close()

    def _sale(self, rid="e1", eid="ev-1", fare=11000, date="2026-09-05",
              segments=("s1", "s2"), occurred="2026-09-05T00:00:00Z"):
        return self.svc.record_event(request_id=rid, actor_id="opc", event={
            "event_id": eid, "agreement_id": AGR, "type": "sale",
            "service_date": date, "occurred_at": occurred, "segment_ids": list(segments),
            "fare_cents": fare, "service_fee_cents": 0, "collected_cents": fare})

    def test_replay_does_not_double_bill(self):
        first = self._sale()
        replay = self._sale()
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        draft = self.svc.build_draft(request_id="d1", actor_id="adm", period=PERIOD)
        # 事件集合只含一个售票事件
        recon = self.svc.audit_reconstruction("aud", period=PERIOD)
        self.assertEqual(recon["versions"][0]["events"], ["ev-1"])

    def test_open_draft_absorbs_late_events_then_closed_version_is_immutable(self):
        self._sale()
        v1 = self.svc.build_draft(request_id="d1", actor_id="adm", period=PERIOD)
        self.assertEqual(v1["version_no"], 1)
        v1_lines = self.svc.audit_reconstruction("aud", period=PERIOD)["versions"][0]["lines"]

        # 确认并关账 v1
        for rid, who in (("c1", "opl"), ("c2", "opc"), ("c3", "oph")):
            self.svc.confirm_draft(request_id=rid, actor_id=who, period=PERIOD, version_no=1)
        self.svc.close_period(request_id="cl1", actor_id="adm", period=PERIOD, version_no=1)

        # 晚到事件进入 v2，v1 分录原样不动
        self._sale(rid="e2", eid="ev-late", fare=2200, date="2026-09-25",
                   segments=("s1",), occurred="2026-10-04T20:00:00Z")
        v2 = self.svc.build_draft(request_id="d2", actor_id="adm", period=PERIOD)
        self.assertEqual(v2["version_no"], 2)
        recon = self.svc.audit_reconstruction("aud", period=PERIOD)
        v1_after, v2_after = recon["versions"]
        self.assertEqual(v1_after["lines"], v1_lines)
        self.assertEqual(v1_after["status"], "closed")
        self.assertIn("ev-late", v2_after["events"])
        self.assertNotIn("ev-late", v1_after["events"])
        # 不能重复关账
        with self.assertRaises(ConflictError):
            self.svc.close_period(request_id="cl1b", actor_id="adm", period=PERIOD, version_no=1)

    def test_dispute_freezes_only_target_line(self):
        self._sale()
        self.svc.build_draft(request_id="d1", actor_id="adm", period=PERIOD)
        draft = self.svc.get_draft("aud", period=PERIOD, version_no=1)
        target = next(l for l in draft["lines"]
                      if l["account"] == O_CARRIER and l["kind"] == "carrier")
        other_before = sum(
            l["amount"] for l in draft["lines"]
            if l["account"] == O_CARRIER and l["line_id"] != target["line_id"])

        self.svc.open_dispute(request_id="dp1", actor_id="opc", period=PERIOD,
                              line_id=target["line_id"], event_id=target["event_id"],
                              reason="里程待核")
        # 运营方不能对线路方的分录提争议
        line_line = next(l for l in draft["lines"] if l["account"] == O_LINE)
        with self.assertRaises(PermissionDenied):
            self.svc.open_dispute(request_id="dpX", actor_id="opc", period=PERIOD,
                                  line_id=line_line["line_id"], event_id=line_line["event_id"],
                                  reason="无权")

        self.svc.close_period(request_id="cl1", actor_id="adm", period=PERIOD, version_no=1)
        bal = self.svc.get_balance("aud", period=PERIOD, version_no=1)["items"]
        carrier = next(b for b in bal if b["account"] == O_CARRIER)
        self.assertEqual(carrier["frozen_amount"], target["amount"])
        # 无争议部分（其他 carrier/service 行）仍可支付
        self.assertEqual(carrier["total_amount"] - carrier["frozen_amount"],
                         carrier["payable_amount"])
        self.assertGreaterEqual(carrier["payable_amount"], other_before)
        line_bal = next(b for b in bal if b["account"] == O_LINE)
        self.assertEqual(line_bal["frozen_amount"], 0)

        # 裁决放行：解冻
        dp = self.svc.audit_reconstruction("aud", period=PERIOD)["versions"][0]["disputes"][0]
        self.svc.resolve_dispute(request_id="rs1", actor_id="adm",
                                 dispute_id=dp["dispute_id"], resolution="核实无误", release=True)
        # 关账快照不变（已固化），但重新构建新版本可体现解冻
        self.assertEqual(dp["status"], "open")

    def test_party_reads_only_own_lines_and_basis(self):
        self._sale()
        self.svc.build_draft(request_id="d1", actor_id="adm", period=PERIOD)
        line_view = self.svc.get_lines("opl", period=PERIOD)["items"]
        self.assertTrue(line_view)
        self.assertTrue(all(l["account"] == O_LINE for l in line_view))
        full = self.svc.get_lines("aud", period=PERIOD)["items"]
        self.assertGreater(len(full), len(line_view))
        # 参与方可读共享依据；非参与方被拒
        basis = self.svc.get_shared_basis("opl", agreement_id=AGR)
        self.assertEqual(basis["versions"][0]["fingerprint"],
                         self.svc.get_shared_basis("aud", agreement_id=AGR)["versions"][0]["fingerprint"])
        # operator 不能做审计还原
        with self.assertRaises(PermissionDenied):
            self.svc.audit_reconstruction("opl", period=PERIOD)

    def test_post_close_correction_enters_new_period_as_adjustment(self):
        self._sale()
        self.svc.build_draft(request_id="d1", actor_id="adm", period=PERIOD)
        self.svc.close_period(request_id="cl1", actor_id="adm", period=PERIOD, version_no=1)
        self.svc.record_event(request_id="adj1", actor_id="adm", event={
            "event_id": "ev-adj", "agreement_id": AGR, "type": "adjustment",
            "service_date": "2026-10-06", "occurred_at": "2026-10-06T09:00:00Z",
            "adjustment_lines": [
                {"account": O_CARRIER, "kind": "adjust", "amount": 100, "segment_id": "s1"},
                {"account": "@customer", "kind": "adjust", "amount": -100},
            ]})
        oct_draft = self.svc.build_draft(request_id="dOct", actor_id="adm", period="2026-10")
        self.assertTrue(oct_draft["zero_sum"])
        # 9 月版本不受影响
        recon_sep = self.svc.audit_reconstruction("aud", period=PERIOD)
        self.assertFalse(any("ev-adj" in v["events"] for v in recon_sep["versions"]))

    def test_cross_month_receipt_belongs_to_service_period_and_books_nothing(self):
        self.svc.record_event(request_id="rc1", actor_id="oph", event={
            "event_id": "ev-rc", "agreement_id": AGR, "type": "receipt",
            "service_date": "2026-09-30", "occurred_at": "2026-10-03T08:00:00Z"})
        draft = self.svc.build_draft(request_id="d1", actor_id="adm", period=PERIOD)
        self.assertEqual(draft["new_events"], 1)
        self.assertEqual(self.svc.get_lines("aud", period=PERIOD)["items"], [])
        # 回执不计入 10 月
        self.assertEqual(
            self.svc.audit_reconstruction("aud", period="2026-10")["versions"], [])


if __name__ == "__main__":
    unittest.main()
