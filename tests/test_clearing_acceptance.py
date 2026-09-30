import unittest

from transport_coordination.clearing.acceptance import run


class ClearingAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        # 关账 v1/v2 余额各自守恒
        for key in ("v1_balances", "v2_balances"):
            self.assertEqual(
                0, sum(bucket["total_amount"] for bucket in result[key].values()))
        # 争议确实冻结了运营方一条分录，且无争议收入仍可支付
        self.assertGreater(result["frozen_carrier_cents"], 0)


if __name__ == "__main__":
    unittest.main()
