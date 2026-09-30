"""委托运输收入清算的确定性记账引擎（纯函数，便于各方复算）。

金额一律使用整数「分」。调用方传入有符号票款 signed_gross：销售、改签为正
（净流入），部分退运、停运赔付为负（净流出）。任一运输事实展开为一组成分
分录，成分金额代数和严格等于 signed_gross；运营方作为剩余项吸收全部按比例
舍入。金额守恒因此由构造方式保证，而不是事后凑平。
"""

from __future__ import annotations

from typing import Any

# 费率以基点（bp）表达：10000bp = 100%。
BASIS_POINTS = 10000

# 成分账户口径。
ACCOUNT_TAX = "tax"
ACCOUNT_CHANNEL_FEE = "channel_fee"
ACCOUNT_LINE_REVENUE = "line_revenue"
ACCOUNT_OPERATOR_REVENUE = "operator_revenue"
ACCOUNT_COMPENSATION = "compensation_cost"
ACCOUNT_MEMO = "memo"


def _rate(rules: dict[str, Any], item_code: str, key: str, override_key: str) -> int:
    overrides = rules.get(override_key, {}) or {}
    if item_code in overrides:
        return int(overrides[item_code])
    return int(rules.get(key, 0))


def largest_remainder(target: int, weights: list[int]) -> list[int]:
    """把非负整数 target 按非负整数权重无遗漏分摊，余数按最大余数法分配（并列按序号）。"""

    if target < 0:
        raise ValueError("分摊目标不能为负")
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("权重之和必须为正")
    if not weights:
        return []
    quotients = [target * w // total_weight for w in weights]
    remainder = target - sum(quotients)
    if remainder:
        leftovers = [target * w - quotients[i] * total_weight for i, w in enumerate(weights)]
        order = sorted(range(len(weights)), key=lambda i: (-leftovers[i], i))
        for i in range(remainder):
            quotients[order[i]] += 1
    return quotients


def _entry(account: str, amount: int, *, beneficiary: str | None, responsible: str | None,
           basis: dict[str, Any]) -> dict[str, Any]:
    return {
        "account": account,
        "amount_cents": amount,
        "beneficiary_party_id": beneficiary,
        "responsible_party_id": responsible,
        "basis": basis,
    }


def _compensation_entries(signed_gross: int, operator_party_id: str,
                          route: list[dict[str, Any]], weights: list[int]) -> list[dict[str, Any]]:
    amount = abs(signed_gross)
    line_amounts = largest_remainder(amount, weights)
    entries: list[dict[str, Any]] = []
    for index, (seg, share) in enumerate(zip(route, line_amounts)):
        if share == 0:
            continue
        entries.append(_entry(
            ACCOUNT_COMPENSATION, signed_gross // abs(signed_gross) * share,
            beneficiary=None, responsible=seg["line_manager_party_id"],
            basis={"segment_code": seg["segment_code"], "length_km": seg["length_km"],
                   "weight": weights[index], "gross_cents": amount},
        ))
    residual = amount - sum(line_amounts)
    if residual or not entries:
        entries.append(_entry(
            ACCOUNT_COMPENSATION, signed_gross // abs(signed_gross) * residual,
            beneficiary=None, responsible=operator_party_id,
            basis={"gross_cents": amount, "residual": True},
        ))
    return entries


def build_entries(*, fact_type: str, signed_gross: int, item_code: str,
                  operator_party_id: str, channel_party_id: str | None,
                  route: list[dict[str, Any]], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """根据运价规则与跨线径路，把一个运输事实展开为守恒的成分分录。

    route 中每一项为有序径路片段：segment_code/from_station/to_station/
    length_km/line_manager_party_id/unit_price_cents_per_km，权重为
    里程与单价的乘积。
    """

    if signed_gross == 0:
        return [_entry(ACCOUNT_MEMO, 0, beneficiary=operator_party_id,
                       responsible=operator_party_id,
                       basis={"reason": "zero_delta", "fact_type": fact_type})]
    if not route:
        raise ValueError("跨线径路不能为空")

    sign = 1 if signed_gross > 0 else -1
    amount = abs(signed_gross)
    weights = [int(seg["length_km"]) * int(seg["unit_price_cents_per_km"]) for seg in route]
    if sum(weights) <= 0:
        raise ValueError("径路权重之和必须为正")

    if fact_type == "suspension_compensation":
        return _compensation_entries(signed_gross, operator_party_id, route, weights)

    tax_bp = _rate(rules, item_code, "tax_rate_bp", "tax_rate_by_item")
    tax_amount = amount * tax_bp // BASIS_POINTS
    fee_bp = 0
    fee_amount = 0
    if channel_party_id is not None:
        fee_bp = _rate(rules, item_code, "channel_fee_rate_bp", "channel_fee_rate_by_item")
        fee_amount = amount * fee_bp // BASIS_POINTS

    line_target = amount - tax_amount - fee_amount
    if line_target < 0:
        raise ValueError("税费与渠道费合计超过票款，运价口径无效")
    line_amounts = largest_remainder(line_target, weights)

    entries = [_entry(
        ACCOUNT_TAX, sign * tax_amount, beneficiary=None, responsible=operator_party_id,
        basis={"gross_cents": amount, "tax_rate_bp": tax_bp},
    )]
    if channel_party_id is not None:
        entries.append(_entry(
            ACCOUNT_CHANNEL_FEE, sign * fee_amount,
            beneficiary=channel_party_id, responsible=channel_party_id,
            basis={"gross_cents": amount, "channel_fee_rate_bp": fee_bp},
        ))
    for index, (seg, share) in enumerate(zip(route, line_amounts)):
        entries.append(_entry(
            ACCOUNT_LINE_REVENUE, sign * share,
            beneficiary=seg["line_manager_party_id"], responsible=seg["line_manager_party_id"],
            basis={"segment_code": seg["segment_code"], "from_station": seg["from_station"],
                   "to_station": seg["to_station"], "length_km": seg["length_km"],
                   "unit_price_cents_per_km": seg["unit_price_cents_per_km"],
                   "weight": weights[index], "gross_cents": amount},
        ))
    operator_residual = amount - tax_amount - fee_amount - sum(line_amounts)
    entries.append(_entry(
        ACCOUNT_OPERATOR_REVENUE, sign * operator_residual,
        beneficiary=operator_party_id, responsible=operator_party_id,
        basis={"gross_cents": amount, "tax_cents": tax_amount, "channel_fee_cents": fee_amount,
               "line_cents": sum(line_amounts), "residual": True},
    ))
    return entries


def conservation_check(entries: list[dict[str, Any]], signed_gross: int) -> None:
    """断言成分分录之和等于有符号票款；不等即视为引擎或口径被破坏。"""

    total = sum(int(e["amount_cents"]) for e in entries)
    if total != signed_gross:
        raise ValueError(f"金额不守恒：成分合计 {total} != 票款 {signed_gross}")
