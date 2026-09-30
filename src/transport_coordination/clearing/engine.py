"""委托运输收入清算引擎。

引擎是纯函数：输入确定的规则版本与运输事实，输出一组**金额守恒**的结算分录
（所有分录金额之和恒为 0）。它不接触数据库、不产生随机量，任何参与方拿到相同
依据都能独立复算出完全一致的结果。

金额单位统一为不可再分的整数辅币单位（如分），比例单位为基点 bp（1bp=万分之一），
杜绝浮点误差。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..audit import canonical_json, digest

CUSTOMER_ACCOUNT = "@customer"
TAX_ACCOUNT = "@tax"

FARE_KINDS = frozenset({"line", "carrier", "channel", "tax"})
SERVICE_KINDS = frozenset({"service"})


class SettlementError(ValueError):
    """规则或运输事实无法形成合法清算。"""


@dataclass(frozen=True)
class SegmentRule:
    """运行区段在某一运价版本下的计费口径。"""

    segment_id: str
    line_owner_org: str          # 线路管理方
    carrier_org: str             # 客货运营方（担当方）
    mileage: int                 # 计价里程（整数公里）
    per_km_cents: int            # 该区段里程单价（分/公里）
    line_bp: int                 # 净运费中线路方分配基点
    carrier_bp: int               # 净运费中运营方分配基点


@dataclass(frozen=True)
class FareVersion:
    """带生效日期的运价版本。"""

    version_id: str
    agreement_id: str
    effective_date: str           # YYYY-MM-DD，按运输日期取 <= 当日的最新版本
    segments: tuple[SegmentRule, ...]
    channel_org: str              # 代售渠道方
    channel_bp: int               # 渠道扣费率（对运费+服务费合计收取）
    tax_bp: int                   # 税率基点
    tax_mode: str                 # "inclusive" 价内税 / "exclusive" 价外税
    comp_line_bp: int             # 停运赔付线路方承担基点
    comp_carrier_bp: int          # 停运赔付运营方承担基点

    def segment(self, segment_id: str) -> SegmentRule:
        for segment in self.segments:
            if segment.segment_id == segment_id:
                return segment
        raise SettlementError(f"运价版本 {self.version_id} 未覆盖区段 {segment_id}")


@dataclass(frozen=True)
class Agreement:
    """委托运输协议：绑定线路方、运营方与渠道方。"""

    agreement_id: str
    party_orgs: frozenset[str]
    currency: str = "CNY"


@dataclass(frozen=True)
class TransportEvent:
    """只追加的运输事实。

    type 取值：sale 售票/运单、change 改签、refund 部分退运、
    cancel_payout 停运赔付、receipt 跨月回执（自身无金额）、
    adjustment 关账后追加分录。
    """

    event_id: str
    agreement_id: str
    type: str
    service_date: str             # 运输服务日期 YYYY-MM-DD，决定归属期间
    occurred_at: str              # 业务事件实际发生时间（回执可能跨月）
    segment_ids: tuple[str, ...] = ()
    fare_cents: int = 0
    service_fee_cents: int = 0
    collected_cents: int = 0       # 实际从客户/渠道收讫总额，用于守恒校验
    change_fee_cents: int = 0
    refund_fare_cents: int = 0
    refund_service_cents: int = 0
    refund_fee_cents: int = 0
    compensation_cents: int = 0
    ratio_bp: int = 0              # 部分退运比例（refund 未显式给金额时使用）
    ref_event_id: str = ""         # 改签/退运/回执关联的原事件
    adjustment_lines: tuple[dict[str, Any], ...] = ()  # adjustment 显式分录
    note: str = ""


@dataclass(frozen=True)
class Line:
    """一条结算分录。amount 为带符号整数：对参与方为正表示应付，@customer 为负。"""

    line_id: str
    event_id: str
    period: str
    segment_id: str
    account: str
    kind: str
    amount: int
    ref_line_id: str = ""


@dataclass
class _Build:
    """单次事件的分录构建器，集中做零和记账。"""

    event: TransportEvent
    period: str
    rows: list[tuple[str, str, str, int, str]] = field(default_factory=list)
    # 元组：(segment_id, account, kind, amount, ref_line_id)

    def add(self, segment_id: str, account: str, kind: str, amount: int,
            ref_line_id: str = "") -> None:
        if amount == 0:
            return
        self.rows.append((segment_id, account, kind, amount, ref_line_id))

    def finish(self) -> list[Line]:
        total = sum(row[3] for row in self.rows)
        if total != 0:
            raise SettlementError(
                f"事件 {self.event.event_id} 分录不守恒：{total} != 0"
            )
        lines: list[Line] = []
        for index, (segment_id, account, kind, amount, ref_line_id) in enumerate(self.rows):
            material = [self.event.event_id, index, segment_id, account, kind, amount, ref_line_id]
            line_id = digest(material)[:24]
            lines.append(Line(line_id, self.event.event_id, self.period, segment_id,
                              account, kind, amount, ref_line_id))
        return lines


def allocate(total: int, weights: dict[str, int]) -> dict[str, int]:
    """按权重用最大余数法把整数 total 精确分配，结果之和恒等于 total。"""

    if total < 0:
        raise SettlementError("待分配金额不能为负")
    keys = list(weights)
    weight_sum = sum(weights.values())
    if weight_sum <= 0:
        raise SettlementError("分配权重之和必须为正")
    if not keys:
        if total != 0:
            raise SettlementError("没有分配对象却存在待分配金额")
        return {}
    floors = {key: total * weights[key] // weight_sum for key in keys}
    remainder = total - sum(floors.values())
    if remainder:
        # 余数按 (-余数, 键) 确定性顺序逐个补 1，保证各参与方复算一致
        order = sorted(
            keys,
            key=lambda key: (-(total * weights[key] % weight_sum), key),
        )
        for key in order[:remainder]:
            floors[key] += 1
    return floors


def period_of(service_date: str) -> str:
    """运输日期 -> 归属期间 YYYY-MM。"""

    return service_date[:7]


def _pick_version(versions: list[FareVersion], agreement_id: str, service_date: str) -> FareVersion:
    candidates = [
        version for version in versions
        if version.agreement_id == agreement_id and version.effective_date <= service_date
    ]
    if not candidates:
        raise SettlementError(f"日期 {service_date} 没有可用运价版本（协议 {agreement_id}）")
    return max(candidates, key=lambda version: (version.effective_date, version.version_id))


def _segment_weights(version: FareVersion, segment_ids: tuple[str, ...]) -> dict[str, int]:
    weights: dict[str, int] = {}
    for segment_id in segment_ids:
        segment = version.segment(segment_id)
        weight = segment.mileage * max(segment.per_km_cents, 0)
        if weight <= 0:
            raise SettlementError(f"区段 {segment_id} 里程或单价无效")
        weights[segment_id] = weight
    return weights


def _inclusive_tax(fare: int, tax_bp: int) -> int:
    """价内税：从含税票面中剥出税额（floor，余量保留在运营收入中）。"""

    return fare - (fare * 10000) // (10000 + tax_bp)


def _settle_sale(event: TransportEvent, version: FareVersion, build: _Build,
                 sign: int = 1) -> None:
    """售票/运单的正向结算。

    sign=-1 的整票冲回不重新舍入，而由 :func:`_reverse_exact` 对原分录逐行取负，
    保证改签金额严格守恒。
    """

    if event.fare_cents < 0 or event.service_fee_cents < 0:
        raise SettlementError("票面金额不能为负")
    weights = _segment_weights(version, event.segment_ids)
    seg_fare = allocate(event.fare_cents, weights)
    seg_service = allocate(event.service_fee_cents, weights)
    base_total = event.fare_cents + event.service_fee_cents
    seg_channel = allocate(base_total * version.channel_bp // 10000, weights)
    if version.tax_mode == "inclusive":
        tax_total = _inclusive_tax(event.fare_cents, version.tax_bp)
        expected_collected = base_total
    elif version.tax_mode == "exclusive":
        tax_total = event.fare_cents * version.tax_bp // 10000
        expected_collected = base_total + tax_total
    else:
        raise SettlementError(f"未知税口径 {version.tax_mode}")
    if event.collected_cents != expected_collected:
        raise SettlementError(
            f"事件 {event.event_id} 收讫金额 {event.collected_cents} 与口径合计 "
            f"{expected_collected} 不一致"
        )
    seg_tax = allocate(tax_total, {key: value for key, value in seg_fare.items() if value} or weights)

    for segment_id in event.segment_ids:
        segment = version.segment(segment_id)
        fare_part = seg_fare[segment_id]
        channel_part = seg_channel[segment_id]
        tax_part = seg_tax.get(segment_id, 0)
        # 价内税已含在票面中，须从可分净运费剥出；价外税在票面之外另收，不占净运费。
        pool = fare_part - channel_part - (tax_part if version.tax_mode == "inclusive" else 0)
        if pool < 0:
            raise SettlementError(f"区段 {segment_id} 扣费税额超过运费")
        if segment.line_bp + segment.carrier_bp != 10000:
            raise SettlementError(f"区段 {segment_id} 线路/运营基点之和必须为 10000")
        line_part = pool * segment.line_bp // 10000
        carrier_part = pool - line_part
        build.add(segment_id, segment.line_owner_org, "line", sign * line_part)
        build.add(segment_id, segment.carrier_org, "carrier", sign * carrier_part)
        build.add(segment_id, segment.carrier_org, "service", sign * seg_service[segment_id])
        build.add(segment_id, version.channel_org, "channel", sign * channel_part)
        build.add(segment_id, TAX_ACCOUNT, "tax", sign * tax_part)
    build.add("", CUSTOMER_ACCOUNT, "customer", -sign * expected_collected)


def _reverse_exact(originals: list[Line], build: _Build) -> None:
    """改签场景：原票全部分录逐行精确取负，关联原行。"""

    for line in originals:
        build.add(line.segment_id, line.account, line.kind, -line.amount, line.line_id)


def _fee_to_carriers(event: TransportEvent, version: FareVersion, fee: int,
                     build: _Build, kind: str) -> None:
    """手续费/退改费按行程权重分配给各段运营方，客户为负。"""

    if fee < 0:
        raise SettlementError("手续费不能为负")
    if fee:
        weights = _segment_weights(version, event.segment_ids)
        shares = allocate(fee, weights)
        for segment_id, amount in shares.items():
            build.add(segment_id, version.segment(segment_id).carrier_org, kind, amount)
    build.add("", CUSTOMER_ACCOUNT, kind, -fee)


def _reverse_partial(originals: list[Line], event: TransportEvent, version: FareVersion,
                     build: _Build) -> None:
    """部分退运：对原票**当前有效**的正向收入行按金额权重精确比例冲回，另扣退票费。

    被引用事件若是改签，其分录含原票冲回负行；这些负行不是当前有效收入，
    不参与退运基数，只对新票的正向行冲回。
    """

    fare_lines = [line for line in originals if line.kind in FARE_KINDS and line.amount > 0]
    service_lines = [line for line in originals if line.kind in SERVICE_KINDS and line.amount > 0]
    fare_sum = sum(line.amount for line in fare_lines)
    service_sum = sum(line.amount for line in service_lines)
    if event.refund_fare_cents > fare_sum or event.refund_service_cents > service_sum:
        raise SettlementError(f"事件 {event.event_id} 退运金额超过当前有效收入")
    refunds: dict[str, int] = {}
    for target, group in ((event.refund_fare_cents, fare_lines),
                          (event.refund_service_cents, service_lines)):
        if not target:
            continue
        weights = {line.line_id: line.amount for line in group if line.amount > 0}
        shares = allocate(target, weights)
        for line in group:
            part = shares.get(line.line_id, 0)
            if part:
                build.add(line.segment_id, line.account, line.kind, -part, line.line_id)
                refunds[line.line_id] = part
    # 客户收回退款（正向），退票费从客户侧扣减（负向）
    build.add("", CUSTOMER_ACCOUNT, "refund",
              event.refund_fare_cents + event.refund_service_cents)
    if event.refund_fee_cents:
        _fee_to_carriers(event, version, event.refund_fee_cents, build, "refund_fee")


def _settle_cancel_payout(event: TransportEvent, version: FareVersion, build: _Build) -> None:
    """停运赔付：按规则基点在线路方与运营方之间分担，赔付给客户。"""

    total_bp = version.comp_line_bp + version.comp_carrier_bp
    if total_bp != 10000:
        raise SettlementError("停运赔付承担基点之和必须为 10000")
    compensation = event.compensation_cents
    if compensation <= 0:
        raise SettlementError("停运赔付金额必须为正")
    weights = _segment_weights(version, event.segment_ids)
    line_share = compensation * version.comp_line_bp // 10000
    carrier_share = compensation - line_share
    line_by_segment = allocate(line_share, weights)
    carrier_by_segment = allocate(carrier_share, weights)
    for segment_id, amount in line_by_segment.items():
        build.add(segment_id, version.segment(segment_id).line_owner_org, "comp_line", -amount)
    for segment_id, amount in carrier_by_segment.items():
        build.add(segment_id, version.segment(segment_id).carrier_org, "comp_carrier", -amount)
    build.add("", CUSTOMER_ACCOUNT, "compensation", compensation)


def _settle_adjustment(event: TransportEvent, agreement: Agreement, build: _Build) -> None:
    """关账后更正：显式追加分录，只允许进入协议参与方或特殊账户，强制零和。"""

    if not event.adjustment_lines:
        raise SettlementError("追加分录不能为空")
    allowed = set(agreement.party_orgs) | {CUSTOMER_ACCOUNT, TAX_ACCOUNT}
    for index, item in enumerate(event.adjustment_lines):
        account = str(item.get("account", ""))
        amount = int(item.get("amount", 0))
        if account not in allowed:
            raise SettlementError(f"追加分录账户 {account} 不属于协议参与方")
        build.add(str(item.get("segment_id", "")), account,
                  str(item.get("kind", "adjust")), amount)


def settle_event(
    event: TransportEvent,
    agreements: dict[str, Agreement],
    versions: list[FareVersion],
    prior_lines: dict[str, list[Line]] | None = None,
) -> list[Line]:
    """把单个运输事实清算为一组零和分录。

    同一 event_id 的重放由上层存储幂等保证；引擎本身对相同输入恒定输出，
    且每行 line_id 由事件内容与行序号派生，天然可复算、可比对。
    """

    prior_lines = prior_lines or {}
    period = period_of(event.service_date)
    build = _Build(event=event, period=period)

    if event.type == "sale":
        agreement = agreements.get(event.agreement_id)
        if agreement is None:
            raise SettlementError(f"协议 {event.agreement_id} 不存在")
        version = _pick_version(versions, event.agreement_id, event.service_date)
        _settle_sale(event, version, build)

    elif event.type == "change":
        originals = prior_lines.get(event.ref_event_id)
        if not originals:
            raise SettlementError(f"改签事件 {event.event_id} 缺少原票 {event.ref_event_id}")
        _reverse_exact(originals, build)
        version = _pick_version(versions, event.agreement_id, event.service_date)
        _settle_sale(event, version, build)
        if event.change_fee_cents:
            _fee_to_carriers(event, version, event.change_fee_cents, build, "change_fee")

    elif event.type == "refund":
        originals = prior_lines.get(event.ref_event_id)
        if not originals:
            raise SettlementError(f"退运事件 {event.event_id} 缺少原票 {event.ref_event_id}")
        version = _pick_version(versions, event.agreement_id, event.service_date)
        if event.refund_fare_cents == 0 and event.refund_service_cents == 0 and event.ratio_bp:
            fare_sum = sum(line.amount for line in originals
                           if line.kind in FARE_KINDS and line.amount > 0)
            service_sum = sum(line.amount for line in originals
                              if line.kind in SERVICE_KINDS and line.amount > 0)
            event = replace(
                event,
                refund_fare_cents=fare_sum * event.ratio_bp // 10000,
                refund_service_cents=service_sum * event.ratio_bp // 10000,
            )
        _reverse_partial(originals, event, version, build)

    elif event.type == "cancel_payout":
        version = _pick_version(versions, event.agreement_id, event.service_date)
        _settle_cancel_payout(event, version, build)

    elif event.type == "receipt":
        # 跨月回执只是凭证事实：不产生金额，重放天然不重复计费。
        return []

    elif event.type == "adjustment":
        agreement = agreements.get(event.agreement_id)
        if agreement is None:
            raise SettlementError(f"协议 {event.agreement_id} 不存在")
        _settle_adjustment(event, agreement, build)

    else:
        raise SettlementError(f"未知运输事件类型 {event.type}")

    return build.finish()


def rules_fingerprint(agreement: Agreement, version: FareVersion) -> str:
    """生成参与方共享的规则依据指纹。"""

    material = {
        "agreement": {
            "agreement_id": agreement.agreement_id,
            "party_orgs": sorted(agreement.party_orgs),
            "currency": agreement.currency,
        },
        "version": {
            "version_id": version.version_id,
            "agreement_id": version.agreement_id,
            "effective_date": version.effective_date,
            "channel_org": version.channel_org,
            "channel_bp": version.channel_bp,
            "tax_bp": version.tax_bp,
            "tax_mode": version.tax_mode,
            "comp_line_bp": version.comp_line_bp,
            "comp_carrier_bp": version.comp_carrier_bp,
            "segments": [
                {"segment_id": s.segment_id, "line_owner_org": s.line_owner_org,
                 "carrier_org": s.carrier_org, "mileage": s.mileage,
                 "per_km_cents": s.per_km_cents, "line_bp": s.line_bp, "carrier_bp": s.carrier_bp}
                for s in sorted(version.segments, key=lambda s: s.segment_id)
            ],
        },
    }
    return digest(canonical_json(material))
