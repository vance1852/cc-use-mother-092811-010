"""委托运输收入清算服务。

职责边界：
1. 登记委托协议与生效日期版本化的运价（线路方、运营方、渠道、税口径、赔付分担）；
2. 接收只追加的运输事实（售票/运单、改签、退运、停运赔付、跨月回执、追加更正）；
3. 生成参与方可复算的**结算草案**：由引擎从规则+事实逐事件重算，每行带规则指纹；
4. 按职责确认与关账：分录一旦物化即不可变，版本只是事件集合快照；晚到事件只进入
   新版本，绝不改写已确认/已关账分录；
5. 争议只冻结相关分录（open/upheld 计入冻结），无争议收入照常支付；关账后更正
   以 adjustment 追加分录进入新版本；
6. 参与方经 API 只能读取自己的明细与共享依据，auditor 可还原任一期间全貌。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from ..audit import append_event, canonical_json, digest
from ..clock import Clock, SystemClock
from ..errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from ..models import Actor
from ..storage import Database
from . import engine
from .engine import (
    Agreement,
    FareVersion,
    Line,
    SegmentRule,
    SettlementError,
    TransportEvent,
)


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PERIOD = re.compile(r"^\d{4}-\d{2}$")

CONFIRM_ROLES = ("admin", "operator")
EVENT_TYPES = ("sale", "change", "refund", "cancel_payout", "receipt", "adjustment")
# 争议未决与裁决维持都计入冻结；裁决放行才解除冻结。
FROZEN_DISPUTE_STATES = ("open", "upheld")


class ClearingService:
    """协调清算规则、事实、分录、版本、争议、关账与审计。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: Any, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _date(self, value: Any, field: str) -> str:
        value = str(value).strip()
        if not DATE.fullmatch(value):
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD")
        return value

    def _cents(self, value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数（辅币单位）")
        return value

    def _nonneg(self, value: Any, field: str) -> int:
        value = self._cents(value, field)
        if value < 0:
            raise ValidationError(f"{field} 不能为负")
        return value

    def _bp(self, value: Any, field: str) -> int:
        value = self._cents(value, field)
        if value < 0 or value > 10000:
            raise ValidationError(f"{field} 必须是 0..10000 基点")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {**json.loads(row["response_json"]), "request_id": request_id,
                    "resource_type": row["resource_type"], "resource_id": row["resource_id"],
                    "replayed": True}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {**response, "request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False}

    # -------------------------------------------------------------- 规则装载

    def _load_agreement(self, connection, agreement_id: str) -> Agreement:
        row = connection.execute(
            "SELECT * FROM clearing_agreements WHERE agreement_id=?", (agreement_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("委托协议不存在")
        return Agreement(row["agreement_id"], frozenset(json.loads(row["party_orgs_json"])),
                         row["currency"])

    def _agreement_map(self, connection) -> dict[str, Agreement]:
        return {
            row["agreement_id"]: Agreement(
                row["agreement_id"], frozenset(json.loads(row["party_orgs_json"])), row["currency"])
            for row in connection.execute("SELECT * FROM clearing_agreements")
        }

    def _org_in_any_agreement(self, connection, org: str) -> bool:
        return any(
            org in json.loads(row["party_orgs_json"])
            for row in connection.execute("SELECT party_orgs_json FROM clearing_agreements")
        )

    def _load_versions(self, connection, agreement_id: str) -> list[FareVersion]:
        rows = connection.execute(
            "SELECT * FROM clearing_fare_versions WHERE agreement_id=? "
            "ORDER BY effective_date, version_id", (agreement_id,)
        ).fetchall()
        versions: list[FareVersion] = []
        for row in rows:
            segments = tuple(
                SegmentRule(seg["segment_id"], seg["line_owner_org"], seg["carrier_org"],
                            int(seg["mileage"]), int(seg["per_km_cents"]),
                            int(seg["line_bp"]), int(seg["carrier_bp"]))
                for seg in json.loads(row["segments_json"])
            )
            versions.append(FareVersion(
                row["version_id"], row["agreement_id"], row["effective_date"], segments,
                row["channel_org"], row["channel_bp"], row["tax_bp"], row["tax_mode"],
                row["comp_line_bp"], row["comp_carrier_bp"],
            ))
        return versions

    def _pick_version(self, versions: list[FareVersion], service_date: str) -> FareVersion:
        candidates = [v for v in versions if v.effective_date <= service_date]
        if not candidates:
            raise SettlementError(f"日期 {service_date} 没有可用运价版本")
        return max(candidates, key=lambda v: (v.effective_date, v.version_id))

    def _version_fingerprints(self, connection, agreements: dict[str, Agreement]) -> dict[str, str]:
        """返回 agreement_id -> 该协议登记的最新运价版本指纹（共享依据）。"""

        result: dict[str, str] = {}
        for agreement_id, agreement in agreements.items():
            versions = self._load_versions(connection, agreement_id)
            if versions:
                result[agreement_id] = engine.rules_fingerprint(agreement, versions[-1])
        return result

    # -------------------------------------------------------------- 规则登记

    def register_agreement(self, *, request_id: str, actor_id: str, agreement_id: str,
                           party_orgs: list[str], currency: str = "CNY") -> dict[str, Any]:
        """登记委托协议：绑定线路方、运营方与渠道方（组织须已建档）。"""

        payload = {"actor_id": actor_id, "agreement_id": agreement_id,
                   "party_orgs": party_orgs, "currency": currency}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            agreement_id = self._id(agreement_id, "agreement_id")
            if not isinstance(party_orgs, list) or not party_orgs:
                raise ValidationError("party_orgs 必须是非空数组")
            cleaned = sorted({self._id(org, "party_orgs[]") for org in party_orgs})
            for org in cleaned:
                if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                      (org,)).fetchone() is None:
                    raise ValidationError(f"协议参与方组织 {org} 尚未建档")
            currency = str(currency).strip().upper() or "CNY"

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO clearing_agreements(agreement_id,currency,party_orgs_json,"
                        "created_by,created_at) VALUES(?,?,?,?,?)",
                        (agreement_id, currency, canonical_json(cleaned), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("协议编号已经存在") from exc
                append_event(connection, actor_id=actor_id,
                             action="clearing.agreement.registered",
                             resource_type="clearing_agreement", resource_id=agreement_id,
                             detail={"party_orgs": cleaned, "currency": currency},
                             occurred_at=self._now())
                return "clearing_agreement", agreement_id, {"agreement_id": agreement_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="clearing_register_agreement", payload=payload,
                                    create=create)

    def register_fare_version(self, *, request_id: str, actor_id: str, version_id: str,
                              agreement_id: str, effective_date: str, channel_org: str,
                              channel_bp: int, tax_bp: int, tax_mode: str,
                              comp_line_bp: int, comp_carrier_bp: int,
                              segments: list[dict[str, Any]]) -> dict[str, Any]:
        """登记带生效日期的运价版本（区段里程单价、分成、渠道费、税、赔付分担）。"""

        payload = {"actor_id": actor_id, "version_id": version_id,
                   "agreement_id": agreement_id, "effective_date": effective_date,
                   "channel_org": channel_org, "channel_bp": channel_bp, "tax_bp": tax_bp,
                   "tax_mode": tax_mode, "comp_line_bp": comp_line_bp,
                   "comp_carrier_bp": comp_carrier_bp, "segments": segments}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            agreement_id = self._id(agreement_id, "agreement_id")
            agreement = self._load_agreement(connection, agreement_id)
            version_id = self._id(version_id, "version_id")
            effective_date = self._date(effective_date, "effective_date")
            channel_org = self._id(channel_org, "channel_org")
            if channel_org not in agreement.party_orgs:
                raise ValidationError("渠道方必须是协议参与方")
            channel_bp_v = self._bp(channel_bp, "channel_bp")
            tax_bp_v = self._bp(tax_bp, "tax_bp")
            if tax_mode not in ("inclusive", "exclusive"):
                raise ValidationError("tax_mode 只能是 inclusive 或 exclusive")
            comp_line = self._bp(comp_line_bp, "comp_line_bp")
            comp_carrier = self._bp(comp_carrier_bp, "comp_carrier_bp")
            if comp_line + comp_carrier != 10000:
                raise ValidationError("赔付承担基点之和必须为 10000")
            if not isinstance(segments, list) or not segments:
                raise ValidationError("segments 必须是非空数组")

            clean_segments: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in segments:
                seg_id = self._id(item.get("segment_id", ""), "segment_id")
                if seg_id in seen:
                    raise ValidationError(f"区段 {seg_id} 重复")
                seen.add(seg_id)
                line_org = self._id(item.get("line_owner_org", ""), "line_owner_org")
                carrier_org = self._id(item.get("carrier_org", ""), "carrier_org")
                if line_org not in agreement.party_orgs or carrier_org not in agreement.party_orgs:
                    raise ValidationError(f"区段 {seg_id} 的线路方/运营方必须是协议参与方")
                mileage = self._cents(item.get("mileage"), "mileage")
                per_km = self._cents(item.get("per_km_cents"), "per_km_cents")
                if mileage <= 0 or per_km < 0:
                    raise ValidationError(f"区段 {seg_id} 里程必须为正、单价不能为负")
                line_bp_v = self._bp(item.get("line_bp", 0), "line_bp")
                carrier_bp_v = self._bp(item.get("carrier_bp", 0), "carrier_bp")
                if line_bp_v + carrier_bp_v != 10000:
                    raise ValidationError(f"区段 {seg_id} 线路/运营基点之和必须为 10000")
                clean_segments.append({
                    "segment_id": seg_id, "line_owner_org": line_org, "carrier_org": carrier_org,
                    "mileage": mileage, "per_km_cents": per_km,
                    "line_bp": line_bp_v, "carrier_bp": carrier_bp_v,
                })

            version = FareVersion(
                version_id, agreement_id, effective_date,
                tuple(SegmentRule(**seg) for seg in clean_segments),
                channel_org, channel_bp_v, tax_bp_v, tax_mode, comp_line, comp_carrier,
            )
            fingerprint = engine.rules_fingerprint(agreement, version)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO clearing_fare_versions(version_id,agreement_id,effective_date,"
                        "channel_org,channel_bp,tax_bp,tax_mode,comp_line_bp,comp_carrier_bp,"
                        "segments_json,fingerprint,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (version_id, agreement_id, effective_date, channel_org, channel_bp_v,
                         tax_bp_v, tax_mode, comp_line, comp_carrier, canonical_json(clean_segments),
                         fingerprint, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("运价版本编号已经存在") from exc
                append_event(connection, actor_id=actor_id,
                             action="clearing.fare_version.registered",
                             resource_type="clearing_fare_version", resource_id=version_id,
                             detail={"agreement_id": agreement_id, "effective_date": effective_date,
                                     "fingerprint": fingerprint, "segments": sorted(seen)},
                             occurred_at=self._now())
                return "clearing_fare_version", version_id, {
                    "version_id": version_id, "fingerprint": fingerprint}

            return self._idempotent(connection, request_id=request_id,
                                    action="clearing_register_fare_version", payload=payload,
                                    create=create)

    # -------------------------------------------------------------- 事实接收

    def record_event(self, *, request_id: str, actor_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """接收一个运输事实。同一 request_id / event_id 重放绝不重复计费。

        事件入册前由引擎做可清算性校验（守恒、口径、引用完整、版本可用）；分录在草案
        构建时统一物化，确保晚到事件能进入正确期间的新版本。
        """

        payload = {"actor_id": actor_id, "event": event}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CONFIRM_ROLES)
            parsed = self._parse_event(connection, event)
            self._settle_in_memory(connection, parsed)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO clearing_events(event_id,agreement_id,type,service_date,"
                        "occurred_at,ref_event_id,payload_json,payload_hash,drafted_version,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,NULL,?,?)",
                        (parsed.event_id, parsed.agreement_id, parsed.type, parsed.service_date,
                         parsed.occurred_at, parsed.ref_event_id, canonical_json(event),
                         digest(event), actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("运输事件编号已经存在") from exc
                append_event(connection, actor_id=actor_id,
                             action=f"clearing.event.{parsed.type}.recorded",
                             resource_type="clearing_event", resource_id=parsed.event_id,
                             detail={"agreement_id": parsed.agreement_id, "type": parsed.type,
                                     "service_date": parsed.service_date,
                                     "period": engine.period_of(parsed.service_date),
                                     "ref_event_id": parsed.ref_event_id,
                                     "occurred_at": parsed.occurred_at},
                             occurred_at=self._now())
                return "clearing_event", parsed.event_id, {"event_id": parsed.event_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="clearing_record_event", payload=payload, create=create)

    def _parse_event(self, connection, raw: dict[str, Any]) -> TransportEvent:
        if not isinstance(raw, dict):
            raise ValidationError("event 必须是对象")
        event_id = self._id(raw.get("event_id", ""), "event_id")
        agreement_id = self._id(raw.get("agreement_id", ""), "agreement_id")
        etype = str(raw.get("type", "")).strip()
        if etype not in EVENT_TYPES:
            raise ValidationError(f"事件类型必须是 {EVENT_TYPES} 之一")
        service_date = self._date(raw.get("service_date", ""), "service_date")
        occurred_at = str(raw.get("occurred_at", "")).strip() or (service_date + "T00:00:00Z")
        segments_raw = raw.get("segment_ids", [])
        if not isinstance(segments_raw, list):
            raise ValidationError("segment_ids 必须是数组")
        segment_ids = tuple(self._id(s, "segment_ids[]") for s in segments_raw)
        ref = str(raw.get("ref_event_id", "") or "")
        if ref:
            ref = self._id(ref, "ref_event_id")
        raw_lines = raw.get("adjustment_lines", [])
        if etype == "adjustment":
            if not isinstance(raw_lines, list) or not raw_lines:
                raise ValidationError("追加分录必须提供非空 adjustment_lines")
        adjustment_lines = tuple(dict(item) for item in raw_lines) if isinstance(raw_lines, list) else ()

        event = TransportEvent(
            event_id=event_id, agreement_id=agreement_id, type=etype,
            service_date=service_date, occurred_at=occurred_at,
            segment_ids=segment_ids,
            fare_cents=self._nonneg(raw.get("fare_cents", 0), "fare_cents"),
            service_fee_cents=self._nonneg(raw.get("service_fee_cents", 0), "service_fee_cents"),
            collected_cents=self._nonneg(raw.get("collected_cents", 0), "collected_cents"),
            change_fee_cents=self._nonneg(raw.get("change_fee_cents", 0), "change_fee_cents"),
            ref_event_id=ref,
            refund_fare_cents=self._nonneg(raw.get("refund_fare_cents", 0), "refund_fare_cents"),
            refund_service_cents=self._nonneg(raw.get("refund_service_cents", 0),
                                              "refund_service_cents"),
            refund_fee_cents=self._nonneg(raw.get("refund_fee_cents", 0), "refund_fee_cents"),
            compensation_cents=self._nonneg(raw.get("compensation_cents", 0),
                                            "compensation_cents"),
            ratio_bp=self._bp(raw.get("ratio_bp", 0), "ratio_bp"),
            adjustment_lines=adjustment_lines,
            note=str(raw.get("note", "")),
        )
        self._load_agreement(connection, agreement_id)
        if etype in ("change", "refund") and not ref:
            raise ValidationError(f"{etype} 事件必须提供 ref_event_id")
        if etype in ("sale", "change", "cancel_payout") and not segment_ids:
            raise ValidationError(f"{etype} 事件必须关联运行区段")
        return event

    def _event_row(self, connection, event_id: str):
        row = connection.execute("SELECT * FROM clearing_events WHERE event_id=?",
                                 (event_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"运输事件 {event_id} 不存在")
        return row

    def _to_engine_event(self, row) -> TransportEvent:
        raw = json.loads(row["payload_json"])
        return TransportEvent(
            event_id=row["event_id"], agreement_id=row["agreement_id"], type=row["type"],
            service_date=row["service_date"], occurred_at=row["occurred_at"],
            segment_ids=tuple(raw.get("segment_ids", [])),
            fare_cents=int(raw.get("fare_cents", 0)),
            service_fee_cents=int(raw.get("service_fee_cents", 0)),
            collected_cents=int(raw.get("collected_cents", 0)),
            change_fee_cents=int(raw.get("change_fee_cents", 0)),
            refund_fare_cents=int(raw.get("refund_fare_cents", 0)),
            refund_service_cents=int(raw.get("refund_service_cents", 0)),
            refund_fee_cents=int(raw.get("refund_fee_cents", 0)),
            compensation_cents=int(raw.get("compensation_cents", 0)),
            ratio_bp=int(raw.get("ratio_bp", 0)),
            ref_event_id=row["ref_event_id"],
            adjustment_lines=tuple(raw.get("adjustment_lines", [])),
            note=str(raw.get("note", "")),
        )

    def _prior_lines(self, connection, agreements: dict[str, Agreement],
                     cache: dict[str, list[Line]], event: TransportEvent) -> dict[str, list[Line]]:
        """取改签/退运所引用原票的已物化分录；原票尚未入册则临时按相同规则清算。"""

        if not event.ref_event_id:
            return {}
        existing = self._lines_for_event(connection, event.ref_event_id)
        if existing:
            return {event.ref_event_id: existing}
        return {event.ref_event_id: self._materialize_one(connection, agreements, cache,
                                                          event.ref_event_id)}

    def _materialize_one(self, connection, agreements: dict[str, Agreement],
                         cache: dict[str, list[Line]], event_id: str) -> list[Line]:
        if event_id in cache:
            return cache[event_id]
        row = self._event_row(connection, event_id)
        event = self._to_engine_event(row)
        versions = self._load_versions(connection, event.agreement_id)
        prior = self._prior_lines(connection, agreements, cache, event)
        lines = engine.settle_event(event, agreements, versions, prior)
        cache[event_id] = lines
        return lines

    def _settle_in_memory(self, connection, event: TransportEvent) -> list[Line]:
        """预演单个事件清算（不落库），用于入册前拒绝非法事实。"""

        agreements = self._agreement_map(connection)
        versions = self._load_versions(connection, event.agreement_id)
        prior = self._prior_lines(connection, agreements, {}, event)
        return engine.settle_event(event, agreements, versions, prior)

    def _lines_for_event(self, connection, event_id: str) -> list[Line]:
        rows = connection.execute(
            "SELECT * FROM clearing_lines WHERE event_id=? ORDER BY rowid", (event_id,)
        ).fetchall()
        return [Line(row["line_id"], row["event_id"], row["period"], row["segment_id"],
                     row["account"], row["kind"], row["amount"], row["ref_line_id"])
                for row in rows]

    # -------------------------------------------------------------- 草案版本

    def _latest_draft(self, connection, period: str):
        return connection.execute(
            "SELECT * FROM clearing_drafts WHERE period=? ORDER BY version_no DESC LIMIT 1",
            (period,),
        ).fetchone()

    def build_draft(self, *, request_id: str, actor_id: str, period: str) -> dict[str, Any]:
        """生成结算草案。任何参与方拿到相同规则与事实都能复算出一致结果。

        - 该期间无版本：物化全部事件分录，形成 v1（open）；
        - 最新版本 open：在同一编号上吸纳晚到事件（仅追加新事件分录，旧行不动）；
        - 最新版本已 confirmed/closed：开新版本号，版本是事件集合快照，
          历史分录原样保留、绝不推翻。
        """

        payload = {"actor_id": actor_id, "period": period}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CONFIRM_ROLES)
            period = str(period).strip()
            if not PERIOD.fullmatch(period):
                raise ValidationError("period 必须是 YYYY-MM")

            def create() -> tuple[str, str, dict[str, Any]]:
                latest = self._latest_draft(connection, period)
                replacing_open = latest is not None and latest["status"] == "open"
                version_no = latest["version_no"] if replacing_open else (
                    1 if latest is None else latest["version_no"] + 1)

                event_rows = connection.execute(
                    "SELECT * FROM clearing_events WHERE substr(service_date,1,7)=? "
                    "ORDER BY occurred_at, event_id", (period,)).fetchall()
                agreements = self._agreement_map(connection)
                versions_cache: dict[str, list[FareVersion]] = {}
                line_cache: dict[str, list[Line]] = {}
                event_ids: list[str] = []
                new_lines: list[tuple[Line, str]] = []
                new_event_ids: list[str] = []
                chosen_map: dict[str, FareVersion] = {}

                for erow in event_rows:
                    event = self._to_engine_event(erow)
                    event_ids.append(event.event_id)
                    versions = versions_cache.setdefault(
                        event.agreement_id,
                        self._load_versions(connection, event.agreement_id))
                    if erow["drafted_version"] is None:
                        prior = self._prior_lines(connection, agreements, line_cache, event)
                        lines = engine.settle_event(event, agreements, versions, prior)
                        line_cache[event.event_id] = lines
                        chosen = self._pick_version(versions, event.service_date) \
                            if versions else None
                        fp = engine.rules_fingerprint(agreements[event.agreement_id], chosen) \
                            if chosen is not None else ""
                        for line in lines:
                            new_lines.append((line, fp))
                        new_event_ids.append(event.event_id)
                        if event.agreement_id not in chosen_map and chosen is not None:
                            chosen_map[event.agreement_id] = chosen
                    elif event.agreement_id not in chosen_map and versions:
                        chosen_map[event.agreement_id] = self._pick_version(
                            versions, event.service_date)

                if replacing_open:
                    # open 快照删除后用同一编号重建；已物化分录（不可变）保留不动。
                    connection.execute(
                        "DELETE FROM clearing_drafts WHERE period=? AND version_no=?",
                        (period, version_no))

                for line, fp in new_lines:
                    connection.execute(
                        "INSERT INTO clearing_lines(line_id,event_id,period,segment_id,account,"
                        "kind,amount,ref_line_id,rules_fp,settled_version) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (line.line_id, line.event_id, line.period, line.segment_id,
                         line.account, line.kind, line.amount, line.ref_line_id, fp, version_no),
                    )
                for eid in new_event_ids:
                    connection.execute(
                        "UPDATE clearing_events SET drafted_version=? WHERE event_id=?",
                        (version_no, eid))

                basis = {
                    aid: engine.rules_fingerprint(agreements[aid], ver)
                    for aid, ver in chosen_map.items()
                }
                # 版本整体零和：由该版本事件集合快照对应的全部分录校验（每事件自身已零和）
                version_lines = self._version_lines(connection, event_ids)
                version_zero_sum = sum(line.amount for line in version_lines) == 0
                connection.execute(
                    "INSERT INTO clearing_drafts(period,version_no,status,event_ids_json,basis_json,"
                    "totals_json,zero_sum,opened_by,opened_at,confirmed_by_json,closed_by,closed_at) "
                    "VALUES(?,?,'open',?,?,?,1,?,?, '[]', NULL, NULL)",
                    (period, version_no, canonical_json(event_ids), canonical_json(basis),
                     canonical_json({}), actor_id, self._now()),
                )
                totals = self._totals_for(connection, event_ids)
                connection.execute(
                    "UPDATE clearing_drafts SET totals_json=? WHERE period=? AND version_no=?",
                    (canonical_json(totals), period, version_no))
                append_event(connection, actor_id=actor_id,
                             action="clearing.draft.rebuilt" if replacing_open
                             else "clearing.draft.built",
                             resource_type="clearing_draft",
                             resource_id=f"{period}:v{version_no}",
                             detail={"period": period, "version_no": version_no,
                                     "events": len(event_ids), "new_events": len(new_event_ids),
                                     "new_lines": len(new_lines),
                                     "zero_sum": version_zero_sum, "basis": basis,
                                     "replaced_open": replacing_open},
                             occurred_at=self._now())
                return "clearing_draft", f"{period}:v{version_no}", {
                    "period": period, "version_no": version_no, "status": "open",
                    "events": len(event_ids), "new_events": len(new_event_ids),
                    "zero_sum": version_zero_sum, "totals": totals, "basis": basis}

            return self._idempotent(connection, request_id=request_id,
                                    action="clearing_build_draft", payload=payload, create=create)

    def _version_lines(self, connection, event_ids: list[str],
                       account: str | None = None) -> list[Line]:
        """取一个版本事件集合快照对应的全部分录（分录不可变，版本只圈定事件集合）。"""

        if not event_ids:
            return []
        placeholders = ",".join("?" for _ in event_ids)
        query = (f"SELECT * FROM clearing_lines WHERE event_id IN ({placeholders})")
        params: list[Any] = list(event_ids)
        if account is not None:
            query += " AND account=?"
            params.append(account)
        query += " ORDER BY rowid"
        rows = connection.execute(query, params).fetchall()
        return [Line(row["line_id"], row["event_id"], row["period"], row["segment_id"],
                     row["account"], row["kind"], row["amount"], row["ref_line_id"])
                for row in rows]

    def _frozen_line_ids(self, connection, period: str) -> set[str]:
        rows = connection.execute(
            "SELECT line_id FROM clearing_disputes WHERE period=? AND status IN (?,?)",
            (period, *FROZEN_DISPUTE_STATES),
        ).fetchall()
        return {row["line_id"] for row in rows}

    def _totals_for(self, connection, event_ids: list[str],
                    period: str | None = None) -> dict[str, dict[str, int]]:
        """按账户汇总总额/冻结/可付；争议冻结实时反映，无争议收入不被冻结。"""

        lines = self._version_lines(connection, event_ids)
        frozen = self._frozen_line_ids(connection, period or "") if period else set()
        totals: dict[str, dict[str, int]] = {}
        for line in lines:
            bucket = totals.setdefault(line.account,
                                       {"total_amount": 0, "frozen_amount": 0, "payable_amount": 0})
            bucket["total_amount"] += line.amount
            if period and line.line_id in frozen:
                bucket["frozen_amount"] += line.amount
        for bucket in totals.values():
            bucket["payable_amount"] = bucket["total_amount"] - bucket["frozen_amount"]
        return dict(sorted(totals.items()))

    def _draft(self, connection, period: str, version_no: int | None = None):
        if version_no is None:
            row = self._latest_draft(connection, period)
        else:
            row = connection.execute(
                "SELECT * FROM clearing_drafts WHERE period=? AND version_no=?",
                (period, version_no)).fetchone()
        if row is None:
            raise NotFoundError("结算草案不存在")
        return row

    # -------------------------------------------------------------- 确认关账

    def confirm_draft(self, *, request_id: str, actor_id: str, period: str,
                      version_no: int | None = None, party_org: str = "") -> dict[str, Any]:
        """职责确认：参与方组织对指定草案版本确认（无争议部分照付，争议另挂）。"""

        payload = {"actor_id": actor_id, "period": period, "version_no": version_no,
                   "party_org": party_org}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CONFIRM_ROLES)
            draft = self._draft(connection, period, version_no)
            party_org = party_org or actor.organization_id
            if party_org != actor.organization_id and actor.role != "admin":
                raise PermissionDenied("只能代表本组织确认")
            if not self._org_in_any_agreement(connection, party_org):
                raise ValidationError("确认方不是协议参与方")

            def create() -> tuple[str, str, dict[str, Any]]:
                if draft["status"] == "closed":
                    raise ConflictError("该版本已经关账，确认须在新版本进行")
                confirmed = json.loads(draft["confirmed_by_json"])
                if party_org in {item["org"] for item in confirmed}:
                    raise ConflictError("该组织已经确认此版本")
                confirmed.append({"org": party_org, "by": actor_id, "at": self._now()})
                connection.execute(
                    "UPDATE clearing_drafts SET confirmed_by_json=? WHERE period=? AND version_no=?",
                    (canonical_json(confirmed), period, draft["version_no"]))
                append_event(connection, actor_id=actor_id, action="clearing.draft.confirmed",
                             resource_type="clearing_draft",
                             resource_id=f"{period}:v{draft['version_no']}",
                             detail={"period": period, "version_no": draft["version_no"],
                                     "party_org": party_org}, occurred_at=self._now())
                return "clearing_confirmation", f"{period}:v{draft['version_no']}:{party_org}", {
                    "period": period, "version_no": draft["version_no"],
                    "party_org": party_org, "confirmed": len(confirmed)}

            return self._idempotent(connection, request_id=request_id,
                                    action="clearing_confirm_draft", payload=payload, create=create)

    def close_period(self, *, request_id: str, actor_id: str, period: str,
                     version_no: int | None = None) -> dict[str, Any]:
        """关账：固化指定版本事件集合对应的分录与余额；更正只能以追加分录进新版本。"""

        payload = {"actor_id": actor_id, "period": period, "version_no": version_no}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CONFIRM_ROLES)
            draft = self._draft(connection, period, version_no)
            version_no = draft["version_no"]
            event_ids = json.loads(draft["event_ids_json"])
            version_lines = self._version_lines(connection, event_ids)
            if sum(line.amount for line in version_lines) != 0:
                raise ConflictError("分录金额不守恒，不能关账")

            def create() -> tuple[str, str, dict[str, Any]]:
                if draft["status"] == "closed":
                    raise ConflictError("该版本已经关账")
                totals = self._totals_for(connection, event_ids, period)
                now = self._now()
                connection.execute(
                    "UPDATE clearing_drafts SET status='closed', totals_json=?, closed_by=?, "
                    "closed_at=? WHERE period=? AND version_no=?",
                    (canonical_json(totals), actor_id, now, period, version_no))
                connection.execute(
                    "DELETE FROM clearing_balances WHERE period=? AND version_no=?",
                    (period, version_no))
                for account, bucket in totals.items():
                    connection.execute(
                        "INSERT INTO clearing_balances(period,version_no,account,total_amount,"
                        "frozen_amount,payable_amount,closed_at) VALUES(?,?,?,?,?,?,?)",
                        (period, version_no, account, bucket["total_amount"],
                         bucket["frozen_amount"], bucket["payable_amount"], now))
                append_event(connection, actor_id=actor_id, action="clearing.period.closed",
                             resource_type="clearing_draft", resource_id=f"{period}:v{version_no}",
                             detail={"period": period, "version_no": version_no,
                                     "accounts": len(totals), "totals": totals}, occurred_at=now)
                return "clearing_close", f"{period}:v{version_no}", {
                    "period": period, "version_no": version_no, "status": "closed",
                    "balances": totals}

            return self._idempotent(connection, request_id=request_id,
                                    action="clearing_close_period", payload=payload, create=create)

    # ------------------------------------------------------------------ 争议

    def open_dispute(self, *, request_id: str, actor_id: str, period: str, line_id: str,
                     event_id: str, reason: str) -> dict[str, Any]:
        """对单条分录提争议：只冻结该项目，不影响同账户其他无争议收入的支付。"""

        payload = {"actor_id": actor_id, "period": period, "line_id": line_id,
                   "event_id": event_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CONFIRM_ROLES)
            line_row = connection.execute(
                "SELECT * FROM clearing_lines WHERE line_id=? AND event_id=?",
                (line_id, event_id)).fetchone()
            if line_row is None:
                raise NotFoundError("被争议分录不存在")
            if line_row["period"] != period:
                raise ValidationError("争议期间与分录期间不一致")
            if line_row["account"] != actor.organization_id and actor.role != "admin":
                raise PermissionDenied("只能对计入本组织的分录提争议")
            reason = str(reason).strip()
            if not reason:
                raise ValidationError("争议理由不能为空")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute(
                        "SELECT dispute_id FROM clearing_disputes WHERE line_id=? AND status IN (?,?)",
                        (line_id, *FROZEN_DISPUTE_STATES)).fetchone():
                    raise ConflictError("该分录已有未决或维持的争议")
                dispute_id = "dp_" + uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO clearing_disputes(dispute_id,period,line_id,event_id,account,"
                    "status,reason,opened_by,opened_at) VALUES(?,?,?,?,?, 'open', ?,?,?)",
                    (dispute_id, period, line_id, event_id, line_row["account"], reason,
                     actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="clearing.dispute.opened",
                             resource_type="clearing_dispute", resource_id=dispute_id,
                             detail={"period": period, "line_id": line_id,
                                     "event_id": event_id, "amount": line_row["amount"]},
                             occurred_at=self._now())
                return "clearing_dispute", dispute_id, {"dispute_id": dispute_id,
                                                        "line_id": line_id, "frozen": True}

            return self._idempotent(connection, request_id=request_id,
                                    action="clearing_open_dispute", payload=payload, create=create)

    def resolve_dispute(self, *, request_id: str, actor_id: str, dispute_id: str,
                        resolution: str, release: bool) -> dict[str, Any]:
        """裁决争议：release=True 放行解冻计入支付；False 维持冻结，差额以后续追加分录处理。"""

        payload = {"actor_id": actor_id, "dispute_id": dispute_id,
                   "resolution": resolution, "release": release}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *CONFIRM_ROLES)
            row = connection.execute("SELECT * FROM clearing_disputes WHERE dispute_id=?",
                                     (dispute_id,)).fetchone()
            if row is None:
                raise NotFoundError("争议不存在")
            resolution = str(resolution).strip()
            if not resolution:
                raise ValidationError("裁决结论不能为空")

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] != "open":
                    raise ConflictError("争议已经裁决")
                status = "released" if release else "upheld"
                connection.execute(
                    "UPDATE clearing_disputes SET status=?, resolved_by=?, resolved_at=?, "
                    "resolution=? WHERE dispute_id=?",
                    (status, actor_id, self._now(), resolution, dispute_id))
                append_event(connection, actor_id=actor_id, action="clearing.dispute.resolved",
                             resource_type="clearing_dispute", resource_id=dispute_id,
                             detail={"period": row["period"], "line_id": row["line_id"],
                                     "outcome": status}, occurred_at=self._now())
                return "clearing_dispute", dispute_id, {"dispute_id": dispute_id,
                                                        "outcome": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="clearing_resolve_dispute", payload=payload,
                                    create=create)

    # -------------------------------------------------------------- 只读视图

    def _visibility(self, actor: Actor) -> str | None:
        """auditor/admin 全量（返回 None）；其他角色仅本组织账户。"""

        if actor.role in ("auditor", "admin"):
            return None
        return actor.organization_id

    def _line_dict(self, row) -> dict[str, Any]:
        return {"line_id": row["line_id"], "event_id": row["event_id"], "period": row["period"],
                "segment_id": row["segment_id"], "account": row["account"], "kind": row["kind"],
                "amount": row["amount"], "ref_line_id": row["ref_line_id"],
                "rules_fp": row["rules_fp"]}

    def get_lines(self, actor_id: str, *, period: str | None = None,
                  event_id: str | None = None) -> dict[str, Any]:
        """参与方只读自己的分录明细；auditor/admin 读全量。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        account = self._visibility(actor)
        query = "SELECT * FROM clearing_lines WHERE 1=1"
        params: list[Any] = []
        if period:
            query += " AND period=?"
            params.append(period)
        if event_id:
            query += " AND event_id=?"
            params.append(event_id)
        if account is not None:
            query += " AND account=?"
            params.append(account)
        query += " ORDER BY period, rowid"
        rows = connection.execute(query, params).fetchall()
        return {"items": [self._line_dict(row) for row in rows]}

    def get_shared_basis(self, actor_id: str, *, agreement_id: str) -> dict[str, Any]:
        """共享依据：协议参与方与 auditor/admin 读取规则版本与指纹，供各自独立复算。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        agreement = self._load_agreement(connection, agreement_id)
        if actor.role not in ("auditor", "admin") and actor.organization_id not in agreement.party_orgs:
            raise PermissionDenied("不是该协议参与方")
        versions = []
        for row in connection.execute(
                "SELECT * FROM clearing_fare_versions WHERE agreement_id=? ORDER BY effective_date",
                (agreement_id,)):
            versions.append({"version_id": row["version_id"], "effective_date": row["effective_date"],
                             "channel_org": row["channel_org"], "channel_bp": row["channel_bp"],
                             "tax_bp": row["tax_bp"], "tax_mode": row["tax_mode"],
                             "comp_line_bp": row["comp_line_bp"],
                             "comp_carrier_bp": row["comp_carrier_bp"],
                             "segments": json.loads(row["segments_json"]),
                             "fingerprint": row["fingerprint"]})
        return {"agreement_id": agreement_id, "currency": agreement.currency,
                "party_orgs": sorted(agreement.party_orgs), "versions": versions}

    def get_draft(self, actor_id: str, *, period: str, version_no: int | None = None) -> dict[str, Any]:
        """读取草案/关账版本；参与方只见本账户分录，auditor/admin 见全貌与争议。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        draft = self._draft(connection, period, version_no)
        event_ids = json.loads(draft["event_ids_json"])
        account = self._visibility(actor)
        lines = [self._line_dict(row) for row in connection.execute(
            "SELECT * FROM clearing_lines WHERE event_id IN (%s) "
            % ",".join("?" for _ in event_ids) + (
                " AND account=?" if account is not None else "") + " ORDER BY rowid",
            [*event_ids, *([account] if account is not None else [])]).fetchall()] \
            if event_ids else []
        # 余额按当前争议状态实时计算，保证“争议只冻结相关项目”
        all_totals = self._totals_for(connection, event_ids, period)
        totals = all_totals if account is None else {
            key: value for key, value in all_totals.items() if key == account}
        disputes: list[dict[str, Any]] = []
        if account is None:
            disputes = [{"dispute_id": r["dispute_id"], "line_id": r["line_id"],
                         "event_id": r["event_id"], "account": r["account"],
                         "status": r["status"], "reason": r["reason"],
                         "resolution": r["resolution"]}
                        for r in connection.execute(
                            "SELECT * FROM clearing_disputes WHERE period=? ORDER BY opened_at",
                            (period,)).fetchall()]
        return {"period": period, "version_no": draft["version_no"], "status": draft["status"],
                "basis": json.loads(draft["basis_json"]),
                "confirmed_by": json.loads(draft["confirmed_by_json"]),
                "opened_at": draft["opened_at"], "closed_at": draft["closed_at"],
                "lines": lines, "disputes": disputes, "totals": totals}

    def get_balance(self, actor_id: str, *, period: str, version_no: int | None = None) -> dict[str, Any]:
        """读取关账后最终余额；参与方只读自己，auditor/admin 可还原任一期间任一版本。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        if version_no is None:
            row = connection.execute(
                "SELECT version_no FROM clearing_drafts WHERE period=? AND status='closed' "
                "ORDER BY version_no DESC LIMIT 1", (period,)).fetchone()
            if row is None:
                raise NotFoundError("该期间尚无关账版本")
            version_no = row["version_no"]
        account = self._visibility(actor)
        query = "SELECT * FROM clearing_balances WHERE period=? AND version_no=?"
        params: list[Any] = [period, version_no]
        if account is not None:
            query += " AND account=?"
            params.append(account)
        items = [{"account": r["account"], "total_amount": r["total_amount"],
                  "frozen_amount": r["frozen_amount"], "payable_amount": r["payable_amount"]}
                 for r in connection.execute(query, params).fetchall()]
        return {"period": period, "version_no": version_no, "items": items}

    def audit_reconstruction(self, actor_id: str, *, period: str) -> dict[str, Any]:
        """监管审计：还原任一期间的规则依据、事件集合、分录、争议、确认与最终余额。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._require(actor, "auditor", "admin")
        versions_out = []
        for draft in connection.execute(
                "SELECT * FROM clearing_drafts WHERE period=? ORDER BY version_no", (period,)):
            event_ids = json.loads(draft["event_ids_json"])
            lines = [self._line_dict(row) for row in (
                connection.execute(
                    "SELECT * FROM clearing_lines WHERE event_id IN (%s) ORDER BY rowid"
                    % ",".join("?" for _ in event_ids), event_ids).fetchall()
                if event_ids else [])]
            disputes = [dict(r) for r in connection.execute(
                "SELECT * FROM clearing_disputes WHERE period=? ORDER BY opened_at",
                (period,)).fetchall()]
            balances = [{"account": r["account"], "total_amount": r["total_amount"],
                         "frozen_amount": r["frozen_amount"], "payable_amount": r["payable_amount"]}
                        for r in connection.execute(
                            "SELECT account,total_amount,frozen_amount,payable_amount "
                            "FROM clearing_balances WHERE period=? AND version_no=? ORDER BY account",
                            (period, draft["version_no"])).fetchall()]
            versions_out.append({
                "version_no": draft["version_no"], "status": draft["status"],
                "opened_at": draft["opened_at"], "closed_at": draft["closed_at"],
                "confirmed_by": json.loads(draft["confirmed_by_json"]),
                "basis": json.loads(draft["basis_json"]), "events": event_ids,
                "zero_sum": bool(draft["zero_sum"]),
                "lines": lines, "disputes": disputes, "balances": balances,
            })
        return {"period": period, "versions": versions_out}
