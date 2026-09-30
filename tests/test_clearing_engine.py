"""清算引擎纯函数测试：零和、确定性、联程/改签/退运/赔付/追加。"""

import unittest

from transport_coordination.clearing.engine import (
    CUSTOMER_ACCOUNT,
    TAX_ACCOUNT,
    Agreement,
    FareVersion,
    SegmentRule,
    SettlementError,
    TransportEvent,
    allocate,
    settle_event,
)

AGREEMENT = Agreement("agr", frozenset({"L", "C", "H"}))


def version(tax_mode: str = "inclusive") -> FareVersion:
    return FareVersion(
        "v1", "agr", "2026-09-01",
        (
            SegmentRule("a", "L", "C", 100, 200, 4000, 6000),
            SegmentRule("b", "L", "C", 300, 100, 4000, 6000),
        ),
        channel_org="H", channel_bp=500, tax_bp=1000, tax_mode=tax_mode,
        comp_line_bp=3000, comp_carrier_bp=7000,
    )


def total(lines):
    return sum(line.amount for line in lines)


class AllocateTest(unittest.TestCase):
    def test_largest_remainder_conserves_total_and_is_deterministic(self):
        first = allocate(100, {"x": 1, "y": 1, "z": 1})
        second = allocate(100, {"z": 1, "y": 1, "x": 1})
        self.assertEqual(sum(first.values()), 100)
        self.assertEqual(first, second)  # 与键顺序无关，可复算

    def test_weights_zero_rejected(self):
        with self.assertRaises(SettlementError):
            allocate(10, {"x": 0})


class SaleTest(unittest.TestCase):
    def test_sale_zero_sum_and_collected_guard(self):
        event = TransportEvent(
            event_id="s1", agreement_id="agr", type="sale",
            service_date="2026-09-05", occurred_at="2026-09-05T00:00:00Z",
            segment_ids=("a", "b"), fare_cents=11000, collected_cents=11000)
        lines = settle_event(event, {"agr": AGREEMENT}, [version()])
        self.assertEqual(total(lines), 0)
        # 收讫金额与税口径不符必须拒绝（防止表外漏计）
        bad = TransportEvent(
            event_id="s2", agreement_id="agr", type="sale",
            service_date="2026-09-05", occurred_at="x",
            segment_ids=("a", "b"), fare_cents=11000, collected_cents=9999)
        with self.assertRaises(SettlementError):
            settle_event(bad, {"agr": AGREEMENT}, [version()])

    def test_exclusive_tax_added_on_top(self):
        event = TransportEvent(
            event_id="s3", agreement_id="agr", type="sale",
            service_date="2026-09-05", occurred_at="x",
            segment_ids=("a",), fare_cents=10000, collected_cents=11000)
        lines = settle_event(event, {"agr": AGREEMENT}, [version("exclusive")])
        self.assertEqual(total(lines), 0)
        tax = sum(line.amount for line in lines if line.account == TAX_ACCOUNT)
        self.assertEqual(tax, 1000)

    def test_deterministic_line_ids(self):
        event = TransportEvent(
            event_id="s4", agreement_id="agr", type="sale",
            service_date="2026-09-05", occurred_at="x",
            segment_ids=("a", "b"), fare_cents=11000, collected_cents=11000)
        first = settle_event(event, {"agr": AGREEMENT}, [version()])
        second = settle_event(event, {"agr": AGREEMENT}, [version()])
        self.assertEqual([l.line_id for l in first], [l.line_id for l in second])


class ChangeRefundCancelTest(unittest.TestCase):
    def _sale_lines(self):
        sale = TransportEvent(
            event_id="sale", agreement_id="agr", type="sale",
            service_date="2026-09-05", occurred_at="x",
            segment_ids=("a", "b"), fare_cents=11000, collected_cents=11000)
        return settle_event(sale, {"agr": AGREEMENT}, [version()])

    def test_change_exactly_reverses_original_then_books_new(self):
        originals = self._sale_lines()
        change = TransportEvent(
            event_id="chg", agreement_id="agr", type="change",
            service_date="2026-09-06", occurred_at="x",
            segment_ids=("a", "b"), ref_event_id="sale",
            fare_cents=12100, collected_cents=12100, change_fee_cents=200)
        lines = settle_event(change, {"agr": AGREEMENT}, [version()],
                             {"sale": originals})
        self.assertEqual(total(lines), 0)
        # 冲回行逐行等于原行取负
        reversals = [line for line in lines if line.ref_line_id]
        self.assertEqual(len(reversals), len(originals))
        original_by_id = {line.line_id: line.amount for line in originals}
        for line in reversals:
            self.assertEqual(line.amount, -original_by_id[line.ref_line_id])
        # 手续费 200：运营方合计 +200，客户 -200
        fees = [line for line in lines if line.kind == "change_fee"]
        self.assertEqual(sum(l.amount for l in fees if l.account != CUSTOMER_ACCOUNT), 200)

    def test_partial_refund_uses_current_effective_income_only(self):
        originals = self._sale_lines()
        refund = TransportEvent(
            event_id="rf", agreement_id="agr", type="refund",
            service_date="2026-09-07", occurred_at="x",
            segment_ids=("a", "b"), ref_event_id="sale",
            ratio_bp=5000, refund_fee_cents=50)
        lines = settle_event(refund, {"agr": AGREEMENT}, [version()],
                             {"sale": originals})
        self.assertEqual(total(lines), 0)
        # 冲回的收入行合计应为原票正向收入的一半
        income_kinds = {"line", "carrier", "channel", "tax"}
        original_income = sum(l.amount for l in originals
                              if l.kind in income_kinds and l.amount > 0)
        reversed_income = -sum(l.amount for l in lines
                               if l.kind in income_kinds and l.amount < 0)
        self.assertEqual(reversed_income, original_income // 2)

    def test_refund_cannot_exceed_original(self):
        originals = self._sale_lines()
        refund = TransportEvent(
            event_id="rf2", agreement_id="agr", type="refund",
            service_date="2026-09-07", occurred_at="x",
            segment_ids=("a", "b"), ref_event_id="sale",
            refund_fare_cents=10 ** 9)
        with self.assertRaises(SettlementError):
            settle_event(refund, {"agr": AGREEMENT}, [version()], {"sale": originals})

    def test_cancel_payout_zero_sum_party_bears(self):
        event = TransportEvent(
            event_id="cp", agreement_id="agr", type="cancel_payout",
            service_date="2026-09-15", occurred_at="x",
            segment_ids=("a", "b"), compensation_cents=1000)
        lines = settle_event(event, {"agr": AGREEMENT}, [version()])
        self.assertEqual(total(lines), 0)
        customer = sum(l.amount for l in lines if l.account == CUSTOMER_ACCOUNT)
        self.assertEqual(customer, 1000)  # 赔付流向客户
        borne = -sum(l.amount for l in lines if l.account in ("L", "C"))
        self.assertEqual(borne, 1000)    # 线路+运营承担

    def test_receipt_books_nothing(self):
        event = TransportEvent(
            event_id="rc", agreement_id="agr", type="receipt",
            service_date="2026-09-30", occurred_at="2026-10-03T00:00:00Z")
        self.assertEqual(settle_event(event, {"agr": AGREEMENT}, [version()]), [])


class AdjustmentTest(unittest.TestCase):
    def test_adjustment_zero_sum_and_party_scoped(self):
        event = TransportEvent(
            event_id="adj", agreement_id="agr", type="adjustment",
            service_date="2026-10-06", occurred_at="x",
            adjustment_lines=(
                {"account": "C", "kind": "adjust", "amount": 100},
                {"account": CUSTOMER_ACCOUNT, "kind": "adjust", "amount": -100},
            ))
        lines = settle_event(event, {"agr": AGREEMENT}, [version()])
        self.assertEqual(total(lines), 0)

    def test_adjustment_rejects_outsider(self):
        event = TransportEvent(
            event_id="adj2", agreement_id="agr", type="adjustment",
            service_date="2026-10-06", occurred_at="x",
            adjustment_lines=(
                {"account": "OUTSIDER", "kind": "adjust", "amount": 100},
                {"account": CUSTOMER_ACCOUNT, "kind": "adjust", "amount": -100},
            ))
        with self.assertRaises(SettlementError):
            settle_event(event, {"agr": AGREEMENT}, [version()])

    def test_adjustment_must_be_balanced(self):
        event = TransportEvent(
            event_id="adj3", agreement_id="agr", type="adjustment",
            service_date="2026-10-06", occurred_at="x",
            adjustment_lines=({"account": "C", "kind": "adjust", "amount": 100},))
        with self.assertRaises(SettlementError):
            settle_event(event, {"agr": AGREEMENT}, [version()])


if __name__ == "__main__":
    unittest.main()
