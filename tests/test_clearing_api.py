import json
import unittest

from transport_coordination.api import route
from transport_coordination.clearing_service import ClearingService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database


class ClearingApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        self.clearing = ClearingService(self.database)
        self._bootstrap()

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor="root"):
        return route(self.service, method, path, body or {}, {"X-Actor-Id": actor})

    def _bootstrap(self):
        self._call("POST", "/organizations",
                   {"request_id": "oh", "organization_id": "org-hub", "name": "平台"}, "bootstrap")
        self._call("POST", "/actors",
                   {"request_id": "ar", "new_actor_id": "root", "display_name": "管理员",
                    "role": "admin", "organization_id": "org-hub"}, "bootstrap")
        for rid, oid, name in (("ol", "org-lm", "线路方"), ("oo", "org-op", "运营方"),
                               ("oc", "org-ch", "渠道方")):
            self._call("POST", "/organizations",
                       {"request_id": rid, "organization_id": oid, "name": name})
        self._call("POST", "/actors",
                   {"request_id": "ao", "new_actor_id": "op", "display_name": "运营员",
                    "role": "operator", "organization_id": "org-op"})

    def _full_agreement(self):
        self._call("POST", "/agreements",
                   {"request_id": "ag", "agreement_id": "ag1", "code": "WT", "title": "协议"})
        for rid, pid, oid, kind in (
                ("pl", "lm1", "org-lm", "line_manager"),
                ("po", "op1", "org-op", "operator"),
                ("pc", "ch1", "org-ch", "channel")):
            status, _ = self._call("POST", "/parties",
                                   {"request_id": rid, "agreement_id": "ag1", "party_id": pid,
                                    "organization_id": oid, "kind": kind, "name": pid})
            assert status == 201
        self._call("POST", "/segments",
                   {"request_id": "s1", "agreement_id": "ag1", "segment_id": "segA", "code": "SEC-A",
                    "from_station": "北京", "to_station": "济南", "length_km": 400,
                    "line_manager_party_id": "lm1"})
        self._call("POST", "/services",
                   {"request_id": "sv", "agreement_id": "ag1", "service_id": "svc1", "code": "G1",
                    "name": "京沪", "kind": "passenger",
                    "items": [{"item_code": "seat", "name": "二等座"}],
                    "route": [{"ordinal": 1, "segment_id": "segA"}]})
        self._call("POST", "/tariff-versions",
                   {"request_id": "t1", "agreement_id": "ag1", "version_no": 1,
                    "effective_from": "2026-01-01", "effective_through": "2026-12-31",
                    "rules": {"segment_prices": {"SEC-A": 100}, "tax_rate_bp": 900,
                              "channel_fee_rate_bp": 300}})
        self._call("POST", "/agreements/activate",
                   {"request_id": "ac", "agreement_id": "ag1", "operating_party_id": "op1"})

    def test_unknown_clearing_route_is_404(self):
        status, payload = self._call("GET", "/nope")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_create_party_before_agreement_is_not_found(self):
        status, payload = self._call("POST", "/parties",
                                     {"request_id": "px", "agreement_id": "missing", "party_id": "zz",
                                      "organization_id": "org-op", "kind": "operator", "name": "z"})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_end_to_end_settlement_over_http(self):
        self._full_agreement()
        status, _ = self._call("POST", "/transport-facts",
                               {"request_id": "f1", "agreement_id": "ag1", "external_event_id": "E1",
                                "service_id": "svc1", "item_code": "seat", "fact_type": "sale",
                                "gross_cents": 10000, "occurred_at": "2026-09-10T09:00Z",
                                "acknowledged_at": "2026-09-10T09:05Z", "channel_party_id": "ch1"},
                               actor="op")
        self.assertEqual(201, status)
        status, payload = self._call("POST", "/periods",
                                     {"request_id": "p1", "agreement_id": "ag1", "period_key": "2026-09",
                                      "start_date": "2026-09-01", "end_date": "2026-09-30"})
        self.assertEqual(201, status)
        period_id = self.database.connection.execute(
            "SELECT period_id FROM settlement_periods").fetchone()[0]
        status, gen = self._call("POST", "/settlements/generate",
                                 {"request_id": "g1", "period_id": period_id})
        self.assertEqual(201, status)
        version_id = gen["resource_id"]

        status, report = self._call("GET", f"/settlements/report?version_id={version_id}")
        self.assertEqual(200, status)
        self.assertEqual(10000, report["shared_basis"]["conservation"]["signed_gross_cents"])

        status, balances = self._call("GET", f"/periods/balances?period_id={period_id}")
        self.assertEqual(200, status)
        self.assertIn("party_balances_cents", balances)

    def test_report_requires_version_id(self):
        status, payload = self._call("GET", "/settlements/report")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_idempotent_replay_returns_200(self):
        self._full_agreement()
        body = {"request_id": "f1", "agreement_id": "ag1", "external_event_id": "E1",
                "service_id": "svc1", "item_code": "seat", "fact_type": "sale",
                "gross_cents": 10000, "occurred_at": "2026-09-10T09:00Z",
                "acknowledged_at": "2026-09-10T09:05Z", "channel_party_id": "ch1"}
        self.assertEqual(201, self._call("POST", "/transport-facts", body, actor="op")[0])
        status, payload = self._call("POST", "/transport-facts", body, actor="op")
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])


if __name__ == "__main__":
    unittest.main()
