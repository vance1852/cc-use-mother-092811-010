import unittest

from transport_coordination.clearing import (
    ACCOUNT_CHANNEL_FEE,
    ACCOUNT_COMPENSATION,
    ACCOUNT_LINE_REVENUE,
    ACCOUNT_OPERATOR_REVENUE,
    ACCOUNT_TAX,
    build_entries,
    conservation_check,
    largest_remainder,
)


def route_two():
    return [
        {"segment_code": "A", "from_station": "s1", "to_station": "s2", "length_km": 400,
         "line_manager_party_id": "lm1", "unit_price_cents_per_km": 100},
        {"segment_code": "B", "from_station": "s2", "to_station": "s3", "length_km": 600,
         "line_manager_party_id": "lm2", "unit_price_cents_per_km": 100},
    ]


RULES = {"tax_rate_bp": 900, "channel_fee_rate_bp": 300}


class LargestRemainderTest(unittest.TestCase):
    def test_distributes_without_loss_or_gain(self):
        for target in range(0, 50):
            shares = largest_remainder(target, [400, 300, 300])
            self.assertEqual(target, sum(shares))

    def test_all_or_nothing_weights(self):
        self.assertEqual([10, 0], largest_remainder(10, [1, 0]))

    def test_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            largest_remainder(-1, [1, 2])
        with self.assertRaises(ValueError):
            largest_remainder(10, [0, 0])


class SaleConservationTest(unittest.TestCase):
    def test_sale_components_sum_to_gross(self):
        entries = build_entries(fact_type="sale", signed_gross=10000, item_code="seat",
                                operator_party_id="op", channel_party_id="ch",
                                route=route_two(), rules=RULES)
        conservation_check(entries, 10000)
        # 税 9%、渠道费 3%，其余 88% 在两段 4:6 权重间分摊。
        amounts = {e["account"]: e["amount_cents"] for e in entries}
        self.assertEqual(900, amounts[ACCOUNT_TAX])
        self.assertEqual(300, amounts[ACCOUNT_CHANNEL_FEE])
        line = [e for e in entries if e["account"] == ACCOUNT_LINE_REVENUE]
        self.assertEqual(8800, sum(e["amount_cents"] for e in line))
        self.assertEqual([3520, 5280], [e["amount_cents"] for e in line])

    def test_line_split_uses_largest_remainder_when_unequal_weights(self):
        route = [
            {"segment_code": "A", "from_station": "s1", "to_station": "s2", "length_km": 3,
             "line_manager_party_id": "lm1", "unit_price_cents_per_km": 1},
            {"segment_code": "B", "from_station": "s2", "to_station": "s3", "length_km": 3,
             "line_manager_party_id": "lm2", "unit_price_cents_per_km": 1},
            {"segment_code": "C", "from_station": "s3", "to_station": "s4", "length_km": 3,
             "line_manager_party_id": "lm1", "unit_price_cents_per_km": 1},
        ]
        entries = build_entries(fact_type="sale", signed_gross=100, item_code="x",
                                operator_party_id="op", channel_party_id=None,
                                route=route, rules={"tax_rate_bp": 0})
        line = [e["amount_cents"] for e in entries if e["account"] == ACCOUNT_LINE_REVENUE]
        self.assertEqual(100, sum(line))

    def test_refund_negates_and_still_conserves(self):
        entries = build_entries(fact_type="partial_refund", signed_gross=-3000, item_code="seat",
                                operator_party_id="op", channel_party_id="ch",
                                route=route_two(), rules=RULES)
        conservation_check(entries, -3000)
        self.assertTrue(all(e["amount_cents"] <= 0 for e in entries))

    def test_change_positive_conserves(self):
        entries = build_entries(fact_type="change", signed_gross=500, item_code="seat",
                                operator_party_id="op", channel_party_id="ch",
                                route=route_two(), rules=RULES)
        conservation_check(entries, 500)

    def test_suspension_compensation_excludes_tax_and_fee(self):
        entries = build_entries(fact_type="suspension_compensation", signed_gross=-1200,
                                item_code="seat", operator_party_id="op", channel_party_id="ch",
                                route=route_two(), rules=RULES)
        conservation_check(entries, -1200)
        accounts = {e["account"] for e in entries}
        self.assertNotIn(ACCOUNT_TAX, accounts)
        self.assertNotIn(ACCOUNT_CHANNEL_FEE, accounts)
        self.assertIn(ACCOUNT_COMPENSATION, accounts)
        self.assertEqual(-1200, sum(e["amount_cents"] for e in entries
                                    if e["account"] == ACCOUNT_COMPENSATION))

    def test_without_channel_no_fee_entry(self):
        entries = build_entries(fact_type="sale", signed_gross=10000, item_code="seat",
                                operator_party_id="op", channel_party_id=None,
                                route=route_two(), rules=RULES)
        self.assertNotIn(ACCOUNT_CHANNEL_FEE, {e["account"] for e in entries})
        conservation_check(entries, 10000)

    def test_item_specific_rate_override(self):
        rules = {"tax_rate_bp": 900, "tax_rate_by_item": {"vip": 0}}
        entries = build_entries(fact_type="sale", signed_gross=10000, item_code="vip",
                                operator_party_id="op", channel_party_id=None,
                                route=route_two(), rules=rules)
        self.assertEqual(0, next(e["amount_cents"] for e in entries
                                 if e["account"] == ACCOUNT_TAX))
        self.assertIn(ACCOUNT_OPERATOR_REVENUE, {e["account"] for e in entries})

    def test_zero_delta_is_memo(self):
        entries = build_entries(fact_type="change", signed_gross=0, item_code="seat",
                                operator_party_id="op", channel_party_id=None,
                                route=route_two(), rules=RULES)
        self.assertEqual("memo", entries[0]["account"])
        conservation_check(entries, 0)


if __name__ == "__main__":
    unittest.main()
