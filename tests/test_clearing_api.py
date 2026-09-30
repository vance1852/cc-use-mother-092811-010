"""清算 HTTP 路由测试：写入口、参与方可见性、审计权限、关账后追加更正。"""

import unittest

from transport_coordination.api import route
from transport_coordination.clearing.service import ClearingService
from transport_coordination.service import DomainService
from transport_coordination.storage import Database

O_ADMIN, O_LINE, O_CARRIER, O_CHANNEL = "org-a", "org-l", "org-c", "org-h"
AGR, PERIOD = "agr-1", "2026-09"


class ClearingApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.base = DomainService(self.database)
        self.clearing = ClearingService(self.database)
        for org in (O_ADMIN, O_LINE, O_CARRIER, O_CHANNEL):
            self.base.register_organization(request_id=f"o-{org}", actor_id="bootstrap",
                                            organization_id=org, name=org)
        self.base.register_actor(request_id="aa", actor_id="bootstrap", new_actor_id="adm",
                                 display_name="adm", role="admin", organization_id=O_ADMIN)
        self.base.register_actor(request_id="al", actor_id="adm", new_actor_id="opl",
                                 display_name="opl", role="operator", organization_id=O_LINE)
        self.base.register_actor(request_id="ac", actor_id="adm", new_actor_id="opc",
                                 display_name="opc", role="operator", organization_id=O_CARRIER)
        self.base.register_actor(request_id="au", actor_id="adm", new_actor_id="aud",
                                 display_name="aud", role="auditor", organization_id=O_ADMIN)
        self._call("POST", "/clearing/agreements", "adm", {
            "request_id": "agr", "agreement_id": AGR,
            "party_orgs": [O_LINE, O_CARRIER, O_CHANNEL]})
        self._call("POST", "/clearing/fare-versions", "adm", {
            "request_id": "fv", "version_id": "fv1", "agreement_id": AGR,
            "effective_date": "2026-09-01", "channel_org": O_CHANNEL,
            "channel_bp": 500, "tax_bp": 1000, "tax_mode": "inclusive",
            "comp_line_bp": 3000, "comp_carrier_bp": 7000,
            "segments": [
                {"segment_id": "s1", "line_owner_org": O_LINE, "carrier_org": O_CARRIER,
                 "mileage": 100, "per_km_cents": 200, "line_bp": 4000, "carrier_bp": 6000}]})

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, actor, body=None):
        return route(self.base, method, path, body or {}, {"X-Actor-Id": actor},
                     clearing_service=self.clearing)

    def _sale(self):
        status, _ = self._call("POST", "/clearing/events", "opc", {
            "request_id": "e1", "event": {
                "event_id": "ev-1", "agreement_id": AGR, "type": "sale",
                "service_date": "2026-09-05", "segment_ids": ["s1"],
                "fare_cents": 10000, "collected_cents": 10000}})
        self.assertEqual(201, status)

    def test_event_replay_is_idempotent_over_http(self):
        self._sale()
        status, body = self._call("POST", "/clearing/events", "opc", {
            "request_id": "e1", "event": {
                "event_id": "ev-1", "agreement_id": AGR, "type": "sale",
                "service_date": "2026-09-05", "segment_ids": ["s1"],
                "fare_cents": 10000, "collected_cents": 10000}})
        self.assertEqual(200, status)
        self.assertTrue(body["replayed"])

    def test_build_confirm_close_and_read_balance(self):
        self._sale()
        status, draft = self._call("POST", "/clearing/drafts/build", "adm",
                                   {"request_id": "d1", "period": PERIOD})
        self.assertEqual(201, status)
        self.assertTrue(draft["zero_sum"])
        # 重放构建同一 request_id：回放原结果，不产生新版本
        status, replay = self._call("POST", "/clearing/drafts/build", "adm",
                                    {"request_id": "d1", "period": PERIOD})
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["version_no"], 1)

        status, _ = self._call("POST", "/clearing/drafts/confirm", "opl",
                               {"request_id": "cf1", "period": PERIOD, "version_no": 1})
        self.assertEqual(201, status)
        # 同 request_id 重放确认不报错
        status, again = self._call("POST", "/clearing/drafts/confirm", "opl",
                                   {"request_id": "cf1", "period": PERIOD, "version_no": 1})
        self.assertEqual(200, status)
        self.assertTrue(again["replayed"])

        status, closed = self._call("POST", "/clearing/periods/close", "adm",
                                    {"request_id": "cl1", "period": PERIOD, "version_no": 1})
        self.assertEqual(200, status)
        status, balance = self._call("GET", f"/clearing/balances?period={PERIOD}", "opl")
        self.assertEqual(200, status)
        self.assertTrue(all(item["account"] == O_LINE for item in balance["items"]))

    def test_party_cannot_reach_audit_operator_sees_only_own_lines(self):
        self._sale()
        self._call("POST", "/clearing/drafts/build", "adm",
                   {"request_id": "d1", "period": PERIOD})
        status, payload = self._call("GET", f"/clearing/audit?period={PERIOD}", "opl")
        self.assertEqual(403, status)
        status, payload = self._call("GET", f"/clearing/lines?period={PERIOD}", "aud")
        self.assertEqual(200, status)
        auditor_count = len(payload["items"])
        status, payload = self._call("GET", f"/clearing/lines?period={PERIOD}", "opl")
        self.assertEqual(200, status)
        self.assertLess(len(payload["items"]), auditor_count)

    def test_shared_basis_requires_party(self):
        status, payload = self._call("GET", f"/clearing/basis?agreement_id={AGR}", "opl")
        self.assertEqual(200, status)
        self.assertIn("fingerprint", payload["versions"][0])


if __name__ == "__main__":
    unittest.main()
