import unittest
from datetime import datetime, timezone

from transport_coordination.clearing_service import ClearingService
from transport_coordination.clock import FixedClock
from transport_coordination.errors import ConflictError, PermissionDenied, ValidationError
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class ClearingFixture:
    def __init__(self):
        self.db = Database()
        clock = FixedClock(datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc))
        self.base = DomainService(self.db, clock)
        self.svc = ClearingService(self.db, clock)
        b = self.base
        b.register_organization(request_id="oh", actor_id="bootstrap", organization_id="org-hub", name="平台")
        b.register_actor(request_id="ar", actor_id="bootstrap", new_actor_id="root",
                         display_name="管理员", role="admin", organization_id="org-hub")
        for rid, oid, aid, name in (
                ("ol", "org-lm", "lm", "线路方"),
                ("oo", "org-op", "op", "运营方"),
                ("oc", "org-ch", "ch", "渠道方")):
            b.register_organization(request_id=rid, actor_id="root", organization_id=oid, name=name)
            b.register_actor(request_id="a" + aid, actor_id="root", new_actor_id=aid,
                             display_name=name + "员", role="operator", organization_id=oid)
        b.register_actor(request_id="aa", actor_id="root", new_actor_id="aud",
                         display_name="审计", role="auditor", organization_id="org-hub")
        s = self.svc
        s.create_agreement(request_id="ag", actor_id="root", agreement_id="ag1", code="WT", title="协议")
        for rid, pid, oid, kind in (
                ("pl", "lm1", "org-lm", "line_manager"),
                ("po", "op1", "org-op", "operator"),
                ("pc", "ch1", "org-ch", "channel")):
            s.register_party(request_id=rid, actor_id="root", agreement_id="ag1", party_id=pid,
                             organization_id=oid, kind=kind, name=pid)
        s.define_segment(request_id="s1", actor_id="root", agreement_id="ag1", segment_id="segA",
                         code="SEC-A", from_station="北京", to_station="济南", length_km=400,
                         line_manager_party_id="lm1")
        s.define_segment(request_id="s2", actor_id="root", agreement_id="ag1", segment_id="segB",
                         code="SEC-B", from_station="济南", to_station="南京", length_km=600,
                         line_manager_party_id="lm1")
        s.define_service(request_id="sv", actor_id="root", agreement_id="ag1", service_id="svc1",
                         code="G1", name="京沪", kind="passenger",
                         items=[{"item_code": "seat", "name": "二等座"}],
                         route=[{"ordinal": 1, "segment_id": "segA"},
                                {"ordinal": 2, "segment_id": "segB"}])
        s.create_tariff_version(request_id="t1", actor_id="root", agreement_id="ag1", version_no=1,
                                effective_from="2026-01-01", effective_through="2026-09-30",
                                rules={"segment_prices": {"SEC-A": 100, "SEC-B": 100},
                                       "tax_rate_bp": 900, "channel_fee_rate_bp": 300})
        s.activate_agreement(request_id="ac", actor_id="root", agreement_id="ag1",
                             operating_party_id="op1")

    def fact(self, rid, eid, ftype, gross, occ, ack="2026-09-10T09:05Z", related=None, channel="ch1"):
        return self.svc.record_fact(request_id=rid, actor_id="op", agreement_id="ag1",
                                    external_event_id=eid, service_id="svc1", item_code="seat",
                                    fact_type=ftype, gross_cents=gross, occurred_at=occ,
                                    acknowledged_at=ack, channel_party_id=channel,
                                    related_external_id=related)

    def period(self, key="2026-09", start="2026-09-01", end="2026-09-30"):
        self.svc.open_period(request_id="per-" + key, actor_id="root", agreement_id="ag1",
                             period_key=key, start_date=start, end_date=end)
        return self.db.connection.execute(
            "SELECT period_id FROM settlement_periods WHERE period_key=?", (key,)).fetchone()[0]

    def generate(self, rid, period_id):
        return self.svc.generate_settlement(request_id=rid, actor_id="root", period_id=period_id)

    def report(self, actor, vid):
        return self.svc.settlement_report(actor_id=actor, version_id=vid)


class SettlementWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.fx = ClearingFixture()

    def tearDown(self):
        self.fx.db.close()

    def test_full_workflow_conservation_and_late_correction(self):
        fx = self.fx
        fx.fact("f1", "E1", "sale", 10000, "2026-09-10T09:00Z")
        fx.fact("f2", "E2", "change", 500, "2026-09-11T09:00Z", related="E1")
        fx.fact("f3", "E3", "partial_refund", 3000, "2026-09-12T09:00Z",
                ack="2026-10-02T22:00Z", related="E1")
        fx.fact("f4", "E4", "suspension_compensation", 1200, "2026-09-13T09:00Z", channel=None)
        pid = fx.period()
        gen = fx.generate("g1", pid)
        vid = gen["resource_id"]
        self.assertEqual("regular", gen["kind"])
        # 成分分录代数和 == 有符号票款合计
        rep = fx.report("root", vid)
        self.assertEqual(10000 + 500 - 3000 - 1200,
                         sum(e["amount_cents"] for e in rep["entries"]))

        # 无争议收入全部确认并支付
        for e in rep["entries"]:
            if e["status"] == "projected":
                fx.svc.confirm_entry(request_id="cf-" + e["entry_id"][:8], actor_id="root",
                                     version_id=vid, entry_id=e["entry_id"])
        paid = fx.svc.pay_confirmed(request_id="pay1", actor_id="root", version_id=vid)
        self.assertGreater(paid["paid_count"], 0)
        fx.svc.close_version(request_id="cl1", actor_id="root", version_id=vid)
        fx.svc.close_period(request_id="pcl", actor_id="root", period_id=pid)

        # 关账后晚到事件 -> correction 新版本，仅追加分录，旧版本不动
        fx.fact("f5", "E5", "partial_refund", 1000, "2026-09-20T09:00Z",
                ack="2026-10-05T22:00Z", related="E1")
        gen2 = fx.generate("g2", pid)
        self.assertEqual("correction", gen2["kind"])
        self.assertEqual(1, gen2["fact_count"])
        v2 = gen2["resource_id"]
        rep2 = fx.report("root", v2)
        self.assertEqual(-1000, sum(e["amount_cents"] for e in rep2["entries"]
                                    if not e["basis"].get("carried_from_entry_id")))
        conservation = rep2["shared_basis"]["conservation"]
        self.assertTrue(conservation["balanced"])
        self.assertEqual(0, conservation["carried_in_count"])
        self.assertEqual(-1000, conservation["carried_in_cents"] + conservation["signed_gross_cents"])

        # 旧版本余额维持首次关账结果，不被晚到事件改写
        self.assertEqual(10000 + 500 - 3000 - 1200,
                         sum(e["amount_cents"] for e in fx.report("root", vid)["entries"]))

        # 任一事实只进入一个版本，绝不重复计费
        counts = self.fx.db.connection.execute(
            "SELECT COUNT(*) FROM fact_inclusions").fetchone()[0]
        distinct = self.fx.db.connection.execute(
            "SELECT COUNT(DISTINCT fact_id) FROM fact_inclusions").fetchone()[0]
        self.assertEqual(counts, distinct)
        self.assertEqual(5, counts)

    def test_dispute_freezes_only_related_entry(self):
        fx = self.fx
        fx.fact("f1", "E1", "sale", 10000, "2026-09-10T09:00Z")
        pid = fx.period()
        vid = fx.generate("g1", pid)["resource_id"]
        rep = fx.report("root", vid)
        target = next(e for e in rep["entries"] if e["account"] == "line_revenue")
        others = [e for e in rep["entries"] if e["entry_id"] != target["entry_id"]
                  and e["status"] == "projected"]

        fx.svc.raise_dispute(request_id="d1", actor_id="lm", version_id=vid,
                             entry_id=target["entry_id"], reason="里程存疑")
        for e in others:
            fx.svc.confirm_entry(request_id="cf-" + e["entry_id"][:8], actor_id="root",
                                 version_id=vid, entry_id=e["entry_id"])
        # 被争议项不能支付，其余无争议项照常支付
        with self.assertRaises(ConflictError):
            fx.svc.pay_entry(request_id="pe", actor_id="root", version_id=vid,
                             entry_id=target["entry_id"])
        fx.svc.pay_confirmed(request_id="pay", actor_id="root", version_id=vid)
        after = {e["entry_id"]: e["status"] for e in fx.report("root", vid)["entries"]}
        self.assertEqual("disputed", after[target["entry_id"]])

        # 仍有 projected 才阻止关账；争议未决本身不阻止（结转处理）
        # 此处争议项为 disputed，无 projected，可关账
        fx.svc.close_version(request_id="cl", actor_id="root", version_id=vid)

    def test_unconfirmed_entries_block_close(self):
        fx = self.fx
        fx.fact("f1", "E1", "sale", 10000, "2026-09-10T09:00Z")
        pid = fx.period()
        vid = fx.generate("g1", pid)["resource_id"]
        with self.assertRaises(ConflictError):
            fx.svc.close_version(request_id="cl", actor_id="root", version_id=vid)

    def test_dispute_carried_into_new_version_and_excluded_from_balance(self):
        fx = self.fx
        fx.fact("f1", "E1", "sale", 10000, "2026-09-10T09:00Z")
        pid = fx.period()
        v1 = fx.generate("g1", pid)["resource_id"]
        target = next(e for e in fx.report("root", v1)["entries"]
                      if e["account"] == "line_revenue")
        fx.svc.raise_dispute(request_id="d1", actor_id="lm", version_id=v1,
                             entry_id=target["entry_id"], reason="存疑")
        for e in fx.report("root", v1)["entries"]:
            if e["status"] == "projected":
                fx.svc.confirm_entry(request_id="cf-" + e["entry_id"][:8], actor_id="root",
                                     version_id=v1, entry_id=e["entry_id"])
        fx.svc.close_version(request_id="cl1", actor_id="root", version_id=v1)
        fx.svc.close_period(request_id="pcl", actor_id="root", period_id=pid)

        fx.fact("f2", "E2", "partial_refund", 1000, "2026-09-20T09:00Z",
                ack="2026-10-05T22:00Z", related="E1")
        v2 = fx.generate("g2", pid)["resource_id"]
        self.assertEqual(1, fx.report("root", v2)["version"]["version_no"] - 1)
        carried_copy = next(e for e in fx.report("root", v2)["entries"]
                            if e["entry_id"] != target["entry_id"]
                            and e["correlation"] == "E1" and e["account"] == "line_revenue"
                            and e["amount_cents"] == target["amount_cents"])
        self.assertEqual("projected", carried_copy["status"])
        cons = fx.report("root", v2)["shared_basis"]["conservation"]
        # 版本分录合计 = 晚到退运票款(-1000) + 转入争议副本
        self.assertTrue(cons["balanced"])
        self.assertEqual(1, cons["carried_in_count"])
        # 原争议项已置 carried，余额只计新版本副本，不重复
        old = self.fx.db.connection.execute(
            "SELECT status FROM settlement_entries WHERE entry_id=?", (target["entry_id"],)
        ).fetchone()[0]
        self.assertEqual("carried", old)
        bal = fx.svc.period_balances(actor_id="root", period_id=pid)
        # 结转金额在净额中只出现一次
        lm_net = bal["party_balances_cents"]["lm1"]["net"]
        manual = self.fx.db.connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM settlement_entries "
            "WHERE beneficiary_party_id='lm1' AND status!='carried' AND account!='memo'"
        ).fetchone()[0]
        self.assertEqual(manual, lm_net)

    def test_replaying_fact_and_generation_is_idempotent(self):
        fx = self.fx
        first = fx.fact("f1", "E1", "sale", 10000, "2026-09-10T09:00Z")
        replay = fx.fact("f1", "E1", "sale", 10000, "2026-09-10T09:00Z")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        pid = fx.period()
        g1 = fx.generate("g1", pid)
        g1b = fx.generate("g1", pid)
        self.assertTrue(g1b["replayed"])
        self.assertEqual(g1["resource_id"], g1b["resource_id"])
        self.assertEqual(1, self.fx.db.connection.execute(
            "SELECT COUNT(*) FROM settlement_versions").fetchone()[0])


class VisibilityTest(unittest.TestCase):
    def setUp(self):
        self.fx = ClearingFixture()

    def tearDown(self):
        self.fx.db.close()

    def test_channel_sees_only_own_entries_but_shared_basis(self):
        fx = self.fx
        fx.fact("f1", "E1", "sale", 10000, "2026-09-10T09:00Z")
        pid = fx.period()
        vid = fx.generate("g1", pid)["resource_id"]
        rep = fx.svc.settlement_report(actor_id="ch", version_id=vid)
        self.assertTrue(rep["entries"])
        self.assertTrue(all(e["beneficiary_party_id"] == "ch1" or e["responsible_party_id"] == "ch1"
                            for e in rep["entries"]))
        self.assertFalse(any(e["account"] == "line_revenue" for e in rep["entries"]))
        # 共享依据仍可复算：含运价、区段、票款守恒汇总
        self.assertIn("tariffs", rep["shared_basis"])
        self.assertEqual(10000, rep["shared_basis"]["conservation"]["signed_gross_cents"])

    def test_auditor_sees_everything_and_can_reconstruct(self):
        fx = self.fx
        fx.fact("f1", "E1", "sale", 10000, "2026-09-10T09:00Z")
        pid = fx.period()
        vid = fx.generate("g1", pid)["resource_id"]
        audited = fx.svc.audit_period(actor_id="aud", period_id=pid)
        self.assertEqual(1, len(audited["versions"]))
        self.assertIn("rule_snapshot", audited["versions"][0])
        self.assertEqual(
            sum(e["amount_cents"] for e in audited["versions"][0]["entries"]),
            audited["versions"][0]["entry_sum_cents"])

    def test_auditor_required_for_full_reconstruction(self):
        fx = self.fx
        pid = fx.period()
        with self.assertRaises(PermissionDenied):
            fx.svc.audit_period(actor_id="op", period_id=pid)

    def test_responsible_party_confirms_own_entry(self):
        fx = self.fx
        fx.fact("f1", "E1", "sale", 10000, "2026-09-10T09:00Z")
        pid = fx.period()
        vid = fx.generate("g1", pid)["resource_id"]
        fee = next(e for e in fx.report("root", vid)["entries"]
                   if e["responsible_party_id"] == "ch1")
        # 线路方不能确认渠道费分录
        with self.assertRaises(PermissionDenied):
            fx.svc.confirm_entry(request_id="x", actor_id="lm", version_id=vid,
                                 entry_id=fee["entry_id"])
        fx.svc.confirm_entry(request_id="ok", actor_id="ch", version_id=vid,
                             entry_id=fee["entry_id"])

    def test_participant_can_recompute_entries_from_shared_basis_only(self):
        from transport_coordination.clearing import build_entries, conservation_check

        fx = self.fx
        fx.fact("f1", "E1", "sale", 10000, "2026-09-10T09:00Z")
        fx.fact("f2", "E2", "partial_refund", 2500, "2026-09-15T09:00Z",
                ack="2026-10-03T22:00Z", related="E1")
        pid = fx.period()
        vid = fx.generate("g1", pid)["resource_id"]
        basis = fx.svc.settlement_report(actor_id="ch", version_id=vid)["shared_basis"]

        operator = next(p["party_id"] for p in basis["parties"] if p["kind"] == "operator")
        segments = {s["segment_id"]: s for s in basis["segments"]}
        services = {s["service_id"]: s for s in basis["services"]}
        negative = {"partial_refund", "suspension_compensation"}

        recomputed = {}
        for fact in basis["facts"]:
            rules = basis["tariffs"][fact["tariff_version_id"]]["rules"]
            service = services[fact["service_id"]]
            route = []
            for step in sorted(service["route"], key=lambda r: r["ordinal"]):
                seg = segments[step["segment_id"]]
                route.append({"segment_code": seg["code"], "from_station": seg["from_station"],
                              "to_station": seg["to_station"], "length_km": seg["length_km"],
                              "line_manager_party_id": seg["line_manager_party_id"],
                              "unit_price_cents_per_km": rules["segment_prices"][seg["code"]]})
            signed = -fact["gross_cents"] if fact["fact_type"] in negative else fact["gross_cents"]
            entries = build_entries(fact_type=fact["fact_type"], signed_gross=signed,
                                    item_code=fact["item_code"], operator_party_id=operator,
                                    channel_party_id=fact["channel_party_id"], route=route,
                                    rules=rules)
            conservation_check(entries, signed)
            for line_no, entry in enumerate(entries, start=1):
                recomputed[f"{fact['external_event_id']}:{line_no}"] = (
                    entry["account"], entry["beneficiary_party_id"],
                    entry["responsible_party_id"], entry["amount_cents"])

        official = {e["entry_key"]: (e["account"], e["beneficiary_party_id"],
                                     e["responsible_party_id"], e["amount_cents"])
                    for e in fx.report("root", vid)["entries"] if e["account"] != "memo"}
        self.assertEqual(official, recomputed)


class TariffAndRuleTest(unittest.TestCase):
    def setUp(self):
        self.fx = ClearingFixture()

    def tearDown(self):
        self.fx.db.close()

    def test_tariff_pinned_by_occurrence_date_across_month_ack(self):
        fx = self.fx
        # 为 10 月准备不同单价版本
        fx.svc.create_tariff_version(request_id="t2", actor_id="root", agreement_id="ag1",
                                     version_no=2, effective_from="2026-10-01",
                                     effective_through="2026-12-31",
                                     rules={"segment_prices": {"SEC-A": 200, "SEC-B": 200},
                                            "tax_rate_bp": 0, "channel_fee_rate_bp": 0})
        fx.fact("f1", "E1", "sale", 10000, "2026-09-30T23:00Z", ack="2026-10-05T22:00Z")
        row = self.fx.db.connection.execute(
            "SELECT tariff_version_id FROM transport_facts WHERE external_event_id='E1'").fetchone()
        version_no = self.fx.db.connection.execute(
            "SELECT version_no FROM clearing_tariff_versions WHERE tariff_version_id=?",
            (row["tariff_version_id"],)).fetchone()[0]
        self.assertEqual(1, version_no)

    def test_overlapping_tariff_rejected(self):
        with self.assertRaises(ConflictError):
            self.fx.svc.create_tariff_version(request_id="t2", actor_id="root", agreement_id="ag1",
                                              version_no=2, effective_from="2026-06-01",
                                              effective_through="2026-12-31",
                                              rules={"segment_prices": {"SEC-A": 1, "SEC-B": 1}})

    def test_invalid_rules_rejected(self):
        with self.assertRaises(ValidationError):
            self.fx.svc.create_tariff_version(request_id="tb", actor_id="root", agreement_id="ag1",
                                              version_no=9, effective_from="2027-01-01",
                                              effective_through="2027-12-31",
                                              rules={"tax_rate_bp": 900})


if __name__ == "__main__":
    unittest.main()
