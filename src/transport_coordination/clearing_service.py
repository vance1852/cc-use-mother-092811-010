"""委托运输收入清算域服务。

在基础服务的权限、幂等、SQLite 事务与哈希审计边界之上，组装版本化规则、
只追加运输事实、确定性清算引擎与期间版本工作流。所有写操作在单个
``BEGIN IMMEDIATE`` 事务内完成并追加审计事件；任一运输事实在整个期间生命
周期内只能进入一个版本，因此晚到事件与重放都不会重复计费。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clearing import build_entries, conservation_check
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .storage import Database


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PARTY_KINDS = {"line_manager", "operator", "channel"}
FACT_TYPES = {"sale", "change", "partial_refund", "suspension_compensation"}
NEGATIVE_FACTS = {"partial_refund", "suspension_compensation"}


class ClearingService:
    """实现委托运输收入清算的登记、记账、确认、关账与审计查询。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础工具
    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _date(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not DATE.fullmatch(value):
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD")
        return value

    def _cents(self, value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是以分为单位的整数")
        if value < 0:
            raise ValidationError(f"{field} 不能为负")
        return value

    def _actor(self, connection, actor_id: str):
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        if not row["active"]:
            raise PermissionDenied("操作者已停用")
        return row

    def _require(self, actor, *roles: str) -> None:
        if actor["role"] not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _replay_receipt(self, connection, *, request_id: str, action: str,
                        payload: dict[str, Any]) -> dict[str, Any] | None:
        """若 request_id 已成功处理则返回重放回执，否则返回 None。"""

        request_id = self._identifier(request_id, "request_id")
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?",
                                 (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return {"request_id": request_id, "resource_type": row["resource_type"],
                "resource_id": row["resource_id"], "replayed": True}

    def _idempotent(self, connection, *, request_id: str, action: str, payload: dict[str, Any],                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True}
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False}

    def _audit(self, connection, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(connection, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _agreement(self, connection, agreement_id: str):
        row = connection.execute("SELECT * FROM clearing_agreements WHERE agreement_id=?",
                                 (agreement_id,)).fetchone()
        if row is None:
            raise NotFoundError("委托协议不存在")
        return row

    def _party(self, connection, party_id: str):
        row = connection.execute("SELECT * FROM clearing_parties WHERE party_id=?", (party_id,)).fetchone()
        if row is None:
            raise NotFoundError("清算参与方不存在")
        return row

    def _version(self, connection, version_id: str):
        row = connection.execute("SELECT * FROM settlement_versions WHERE version_id=?",
                                 (version_id,)).fetchone()
        if row is None:
            raise NotFoundError("结算版本不存在")
        return row

    def _tariff_for(self, connection, agreement_id: str, day: str):
        rows = connection.execute(
            "SELECT * FROM clearing_tariff_versions WHERE agreement_id=? AND status='active' "
            "AND effective_from<=? AND effective_through>=? ORDER BY version_no DESC",
            (agreement_id, day, day),
        ).fetchall()
        if not rows:
            raise ValidationError(f"{day} 没有生效的运价版本")
        return rows[0]

    # ------------------------------------------------------------------ 规则登记
    def create_agreement(self, *, request_id: str, actor_id: str, agreement_id: str,
                         code: str, title: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "code": code, "title": title}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            agreement_id = self._identifier(agreement_id, "agreement_id")
            code = self._identifier(code, "code")
            title = self._text(title, "title")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO clearing_agreements(agreement_id,code,title,status,created_by,created_at) "
                        "VALUES(?,?,?,'draft',?,?)",
                        (agreement_id, code, title, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("协议编号或代码已经存在") from exc
                self._audit(connection, actor_id, "agreement.created", "agreement", agreement_id,
                            {"code": code, "title": title})
                return "agreement", agreement_id, {"agreement_id": agreement_id}

            return self._idempotent(connection, request_id=request_id, action="create_agreement",
                                    payload=payload, create=create)

    def register_party(self, *, request_id: str, actor_id: str, agreement_id: str, party_id: str,
                       organization_id: str, kind: str, name: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "party_id": party_id,
                   "organization_id": organization_id, "kind": kind, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            self._agreement(connection, agreement_id)
            if kind not in PARTY_KINDS:
                raise ValidationError("参与方职责无效")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")
            party_id = self._identifier(party_id, "party_id")
            name = self._text(name, "name")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO clearing_parties(party_id,agreement_id,organization_id,kind,name,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (party_id, agreement_id, organization_id, kind, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("参与方已存在或组织已在协议中登记") from exc
                self._audit(connection, actor_id, "party.registered", "party", party_id,
                            {"agreement_id": agreement_id, "organization_id": organization_id,
                             "kind": kind, "name": name})
                return "party", party_id, {"party_id": party_id}

            return self._idempotent(connection, request_id=request_id, action="register_party",
                                    payload=payload, create=create)

    def activate_agreement(self, *, request_id: str, actor_id: str, agreement_id: str,
                           operating_party_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id,
                   "operating_party_id": operating_party_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            agreement = self._agreement(connection, agreement_id)
            operator = self._party(connection, operating_party_id)
            if operator["agreement_id"] != agreement_id or operator["kind"] != "operator":
                raise ValidationError("运营方必须是本协议内 kind=operator 的参与方")
            if agreement["status"] == "active":
                raise ConflictError("协议已激活")
            if not connection.execute(
                "SELECT 1 FROM clearing_tariff_versions WHERE agreement_id=? AND status='active'",
                (agreement_id,),
            ).fetchone():
                raise ValidationError("激活前必须存在生效运价版本")

            def create():
                connection.execute(
                    "UPDATE clearing_agreements SET status='active',operating_party_id=?,activated_at=? "
                    "WHERE agreement_id=?",
                    (operating_party_id, self._now(), agreement_id),
                )
                self._audit(connection, actor_id, "agreement.activated", "agreement", agreement_id,
                            {"operating_party_id": operating_party_id})
                return "agreement", agreement_id, {"agreement_id": agreement_id, "status": "active"}

            return self._idempotent(connection, request_id=request_id, action="activate_agreement",
                                    payload=payload, create=create)

    def define_segment(self, *, request_id: str, actor_id: str, agreement_id: str, segment_id: str,
                       code: str, from_station: str, to_station: str, length_km: int,
                       line_manager_party_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "segment_id": segment_id,
                   "code": code, "from_station": from_station, "to_station": to_station,
                   "length_km": length_km, "line_manager_party_id": line_manager_party_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            self._agreement(connection, agreement_id)
            manager = self._party(connection, line_manager_party_id)
            if manager["agreement_id"] != agreement_id or manager["kind"] != "line_manager":
                raise ValidationError("区段必须归属本协议内的线路管理方")
            segment_id = self._identifier(segment_id, "segment_id")
            code = self._identifier(code, "segment code")
            from_station = self._text(from_station, "from_station", 40)
            to_station = self._text(to_station, "to_station", 40)
            if not isinstance(length_km, int) or length_km <= 0:
                raise ValidationError("里程必须为正整数")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO clearing_segments(segment_id,agreement_id,code,from_station,to_station,"
                        "length_km,line_manager_party_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (segment_id, agreement_id, code, from_station, to_station, length_km,
                         line_manager_party_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("区段编号或代码已经存在") from exc
                self._audit(connection, actor_id, "segment.defined", "segment", segment_id,
                            {"agreement_id": agreement_id, "code": code, "length_km": length_km,
                             "line_manager_party_id": line_manager_party_id})
                return "segment", segment_id, {"segment_id": segment_id}

            return self._idempotent(connection, request_id=request_id, action="define_segment",
                                    payload=payload, create=create)

    def define_service(self, *, request_id: str, actor_id: str, agreement_id: str, service_id: str,
                       code: str, name: str, kind: str, items: list[dict[str, Any]],
                       route: list[dict[str, Any]]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "service_id": service_id,
                   "code": code, "name": name, "kind": kind, "items": items, "route": route}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            self._agreement(connection, agreement_id)
            if kind not in {"passenger", "freight"}:
                raise ValidationError("服务类型必须是 passenger 或 freight")
            if not isinstance(items, list) or not items:
                raise ValidationError("席位/货运服务项不能为空")
            if not isinstance(route, list) or not route:
                raise ValidationError("跨线径路不能为空")
            normalised_items: list[tuple[str, str]] = []
            for item in items:
                item_code = self._identifier(item["item_code"], "item_code")
                item_name = self._text(item["name"], "item name", 80)
                normalised_items.append((item_code, item_name))
            if len({code_ for code_, _ in normalised_items}) != len(normalised_items):
                raise ValidationError("服务项代码不能重复")
            ordinals = set()
            normalised_route: list[tuple[int, str]] = []
            for step in route:
                ordinal = step["ordinal"]
                segment_id = step["segment_id"]
                if not isinstance(ordinal, int) or ordinal < 1:
                    raise ValidationError("径路序号必须为正整数")
                if ordinal in ordinals:
                    raise ValidationError("径路序号不能重复")
                ordinals.add(ordinal)
                segment = connection.execute(
                    "SELECT * FROM clearing_segments WHERE segment_id=? AND agreement_id=?",
                    (segment_id, agreement_id),
                ).fetchone()
                if segment is None:
                    raise NotFoundError(f"径路区段 {segment_id} 不存在")
                normalised_route.append((ordinal, segment_id))
            if ordinals != set(range(1, len(ordinals) + 1)):
                raise ValidationError("径路序号必须从 1 连续编号")
            normalised_route.sort()
            service_id = self._identifier(service_id, "service_id")
            code = self._identifier(code, "service code")
            name = self._text(name, "name")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO clearing_services(service_id,agreement_id,code,name,kind,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (service_id, agreement_id, code, name, kind, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("服务编号或代码已经存在") from exc
                for item_code, item_name in normalised_items:
                    connection.execute(
                        "INSERT INTO clearing_service_items(service_id,item_code,name) VALUES(?,?,?)",
                        (service_id, item_code, item_name),
                    )
                for ordinal, segment_id in normalised_route:
                    connection.execute(
                        "INSERT INTO clearing_service_route(service_id,ordinal,segment_id) VALUES(?,?,?)",
                        (service_id, ordinal, segment_id),
                    )
                self._audit(connection, actor_id, "service.defined", "service", service_id,
                            {"agreement_id": agreement_id, "code": code, "kind": kind,
                             "items": [c for c, _ in normalised_items],
                             "segments": [s for _, s in normalised_route]})
                return "service", service_id, {"service_id": service_id}

            return self._idempotent(connection, request_id=request_id, action="define_service",
                                    payload=payload, create=create)

    def create_tariff_version(self, *, request_id: str, actor_id: str, agreement_id: str,
                              version_no: int, effective_from: str, effective_through: str,
                              rules: dict[str, Any]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "version_no": version_no,
                   "effective_from": effective_from, "effective_through": effective_through,
                   "rules": rules}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            self._agreement(connection, agreement_id)
            if not isinstance(version_no, int) or version_no < 1:
                raise ValidationError("运价版本号必须为正整数")
            effective_from = self._date(effective_from, "effective_from")
            effective_through = self._date(effective_through, "effective_through")
            if effective_from > effective_through:
                raise ValidationError("生效起点不能晚于终点")
            rules = self._validate_rules(rules)

            overlap = connection.execute(
                "SELECT version_no FROM clearing_tariff_versions WHERE agreement_id=? AND status='active' "
                "AND effective_from<=? AND effective_through>=?",
                (agreement_id, effective_through, effective_from),
            ).fetchone()
            if overlap:
                raise ConflictError(f"生效区间与运价版本 {overlap['version_no']} 重叠")
            tariff_version_id = uuid.uuid4().hex
            rules_json = canonical_json(rules)
            rules_hash = digest(rules)

            def create():
                try:
                    connection.execute(
                        "INSERT INTO clearing_tariff_versions(tariff_version_id,agreement_id,version_no,"
                        "effective_from,effective_through,status,rules_json,rules_hash,created_by,created_at) "
                        "VALUES(?,?,?,?,?,'active',?,?,?,?)",
                        (tariff_version_id, agreement_id, version_no, effective_from, effective_through,
                         rules_json, rules_hash, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("运价版本号已经存在") from exc
                self._audit(connection, actor_id, "tariff.version_created", "tariff_version",
                            tariff_version_id,
                            {"agreement_id": agreement_id, "version_no": version_no,
                             "effective_from": effective_from, "effective_through": effective_through,
                             "rules_hash": rules_hash})
                return "tariff_version", tariff_version_id, {"tariff_version_id": tariff_version_id}

            return self._idempotent(connection, request_id=request_id, action="create_tariff_version",
                                    payload=payload, create=create)

    def _validate_rules(self, rules: Any) -> dict[str, Any]:
        if not isinstance(rules, dict) or not rules:
            raise ValidationError("运价口径必须是非空对象")
        prices = rules.get("segment_prices")
        if not isinstance(prices, dict) or not prices:
            raise ValidationError("运价必须包含 segment_prices 区段里程单价")
        normalised_prices: dict[str, int] = {}
        for code, price in prices.items():
            if not IDENTIFIER.fullmatch(str(code)):
                raise ValidationError(f"区段代码 {code} 格式无效")
            if isinstance(price, bool) or not isinstance(price, int) or price <= 0:
                raise ValidationError(f"区段 {code} 的单价必须是正整数（分/公里）")
            normalised_prices[str(code)] = price
        cleaned = dict(rules)
        cleaned["segment_prices"] = normalised_prices
        for key, by_item in (("tax_rate_bp", "tax_rate_by_item"),
                             ("channel_fee_rate_bp", "channel_fee_rate_by_item")):
            self._validate_bp(cleaned, key)
            overrides = cleaned.get(by_item)
            if overrides is not None:
                if not isinstance(overrides, dict):
                    raise ValidationError(f"{by_item} 必须是服务项到费率的映射")
                for item_code, bp in overrides.items():
                    if not IDENTIFIER.fullmatch(str(item_code)):
                        raise ValidationError(f"服务项代码 {item_code} 格式无效")
                    self._validate_bp_value(bp, f"{by_item}.{item_code}")
                cleaned[by_item] = {str(k): int(v) for k, v in overrides.items()}
        return cleaned

    def _validate_bp(self, rules: dict[str, Any], key: str) -> None:
        if key in rules:
            self._validate_bp_value(rules[key], key)
            rules[key] = int(rules[key])

    @staticmethod
    def _validate_bp_value(value: Any, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000:
            raise ValidationError(f"{label} 必须是 0..10000 之间的基点整数")

    # ------------------------------------------------------------------ 运输事实
    def record_fact(self, *, request_id: str, actor_id: str, agreement_id: str,
                    external_event_id: str, service_id: str, item_code: str, fact_type: str,
                    gross_cents: int, occurred_at: str, acknowledged_at: str,
                    channel_party_id: str | None = None, related_external_id: str | None = None,
                    payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        message = {"actor_id": actor_id, "agreement_id": agreement_id,
                   "external_event_id": external_event_id, "service_id": service_id,
                   "item_code": item_code, "fact_type": fact_type, "gross_cents": gross_cents,
                   "occurred_at": occurred_at, "acknowledged_at": acknowledged_at,
                   "channel_party_id": channel_party_id, "related_external_id": related_external_id,
                   "payload": payload}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            agreement = self._agreement(connection, agreement_id)
            if agreement["status"] != "active":
                raise ValidationError("协议尚未激活，不能登记运输事实")
            external_event_id = self._identifier(external_event_id, "external_event_id")
            if fact_type not in FACT_TYPES:
                raise ValidationError("运输事件类型无效")
            gross_cents = self._cents(gross_cents, "gross_cents")
            if fact_type == "sale" and gross_cents == 0:
                raise ValidationError("销售票款必须为正")
            occurred_at = self._text(occurred_at, "occurred_at", 40)
            acknowledged_at = self._text(acknowledged_at, "acknowledged_at", 40)
            service = connection.execute(
                "SELECT * FROM clearing_services WHERE service_id=? AND agreement_id=?",
                (service_id, agreement_id),
            ).fetchone()
            if service is None:
                raise NotFoundError("服务不存在")
            if connection.execute("SELECT 1 FROM clearing_service_items WHERE service_id=? AND item_code=?",
                                  (service_id, item_code)).fetchone() is None:
                raise NotFoundError("席位/货运服务项不存在")
            if channel_party_id is not None:
                channel = self._party(connection, channel_party_id)
                if channel["agreement_id"] != agreement_id or channel["kind"] != "channel":
                    raise ValidationError("渠道必须是本协议内 kind=channel 的参与方")
            if fact_type in ("change", "partial_refund"):
                if not related_external_id:
                    raise ValidationError(f"{fact_type} 必须提供 related_external_id")
                related = connection.execute(
                    "SELECT 1 FROM transport_facts WHERE agreement_id=? AND external_event_id=?",
                    (agreement_id, related_external_id),
                ).fetchone()
                if related is None:
                    raise NotFoundError("关联的原始运输事件不存在")
            tariff = self._tariff_for(connection, agreement_id, occurred_at[:10])
            payload_hash = digest(message)

            def create():
                existing = connection.execute(
                    "SELECT * FROM transport_facts WHERE agreement_id=? AND external_event_id=?",
                    (agreement_id, external_event_id),
                ).fetchone()
                if existing:
                    if existing["payload_hash"] != payload_hash:
                        raise ConflictError("同一外部事件号已登记不同内容")
                    return "transport_fact", existing["fact_id"], {"fact_id": existing["fact_id"]}
                fact_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO transport_facts(fact_id,agreement_id,external_event_id,tariff_version_id,"
                    "service_id,item_code,channel_party_id,fact_type,gross_cents,related_external_id,"
                    "payload_json,payload_hash,occurred_at,acknowledged_at,recorded_by,recorded_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (fact_id, agreement_id, external_event_id, tariff["tariff_version_id"], service_id,
                     item_code, channel_party_id, fact_type, gross_cents, related_external_id,
                     canonical_json(payload), payload_hash, occurred_at, acknowledged_at,
                     actor_id, self._now()),
                )
                self._audit(connection, actor_id, "fact.recorded", "transport_fact", fact_id,
                            {"agreement_id": agreement_id, "external_event_id": external_event_id,
                             "fact_type": fact_type, "gross_cents": gross_cents,
                             "occurred_at": occurred_at, "acknowledged_at": acknowledged_at,
                             "tariff_version_id": tariff["tariff_version_id"]})
                return "transport_fact", fact_id, {"fact_id": fact_id}

            return self._idempotent(connection, request_id=request_id, action="record_fact",
                                    payload=message, create=create)

    # ------------------------------------------------------------------ 期间与版本
    def open_period(self, *, request_id: str, actor_id: str, agreement_id: str, period_key: str,
                    start_date: str, end_date: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "agreement_id": agreement_id, "period_key": period_key,
                   "start_date": start_date, "end_date": end_date}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._agreement(connection, agreement_id)
            period_key = self._identifier(period_key, "period_key")
            start_date = self._date(start_date, "start_date")
            end_date = self._date(end_date, "end_date")
            if start_date > end_date:
                raise ValidationError("期间起点不能晚于终点")
            period_id = uuid.uuid4().hex

            def create():
                try:
                    connection.execute(
                        "INSERT INTO settlement_periods(period_id,agreement_id,period_key,start_date,end_date,"
                        "status,created_by,created_at) VALUES(?,?,?,?,?,'open',?,?)",
                        (period_id, agreement_id, period_key, start_date, end_date, actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("期间键已存在") from exc
                self._audit(connection, actor_id, "period.opened", "period", period_id,
                            {"agreement_id": agreement_id, "period_key": period_key,
                             "start_date": start_date, "end_date": end_date})
                return "period", period_id, {"period_id": period_id}

            return self._idempotent(connection, request_id=request_id, action="open_period",
                                    payload=payload, create=create)

    def _period(self, connection, period_id: str):
        row = connection.execute("SELECT * FROM settlement_periods WHERE period_id=?",
                                 (period_id,)).fetchone()
        if row is None:
            raise NotFoundError("结算期间不存在")
        return row

    def _load_snapshot(self, connection, agreement_id: str, period, tariff_ids: set[str]) -> dict[str, Any]:
        parties = [dict(party_id=r["party_id"], organization_id=r["organization_id"], kind=r["kind"],
                        name=r["name"])
                   for r in connection.execute(
                       "SELECT * FROM clearing_parties WHERE agreement_id=? ORDER BY party_id",
                       (agreement_id,))]
        segments = [dict(segment_id=r["segment_id"], code=r["code"], from_station=r["from_station"],
                         to_station=r["to_station"], length_km=r["length_km"],
                         line_manager_party_id=r["line_manager_party_id"])
                    for r in connection.execute(
                        "SELECT * FROM clearing_segments WHERE agreement_id=? ORDER BY segment_id",
                        (agreement_id,))]
        services = []
        for service in connection.execute(
                "SELECT * FROM clearing_services WHERE agreement_id=? ORDER BY service_id", (agreement_id,)):
            items = [dict(item_code=r["item_code"], name=r["name"]) for r in connection.execute(
                "SELECT * FROM clearing_service_items WHERE service_id=? ORDER BY item_code",
                (service["service_id"],))]
            route = [dict(ordinal=r["ordinal"], segment_id=r["segment_id"]) for r in connection.execute(
                "SELECT * FROM clearing_service_route WHERE service_id=? ORDER BY ordinal",
                (service["service_id"],))]
            services.append(dict(service_id=service["service_id"], code=service["code"],
                                 name=service["name"], kind=service["kind"], items=items, route=route))
        tariffs = {}
        for tariff_id in tariff_ids:
            row = connection.execute("SELECT * FROM clearing_tariff_versions WHERE tariff_version_id=?",
                                     (tariff_id,)).fetchone()
            tariffs[tariff_id] = dict(version_no=row["version_no"], effective_from=row["effective_from"],
                                      effective_through=row["effective_through"],
                                      rules_hash=row["rules_hash"], rules=json.loads(row["rules_json"]))
        return {"agreement_id": agreement_id,
                "period": {"period_key": period["period_key"], "start_date": period["start_date"],
                           "end_date": period["end_date"]},
                "parties": parties, "segments": segments, "services": services, "tariffs": tariffs}

    def generate_settlement(self, *, request_id: str, actor_id: str, period_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "period_id": period_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            period = self._period(connection, period_id)
            agreement_id = period["agreement_id"]
            # 生成请求在草案已存在时重放，应原样返回回执而不是被草案守卫拒绝。
            early = self._replay_receipt(connection, request_id=request_id, action="generate_settlement",
                                         payload=payload)
            if early is not None:
                version = self._version(connection, early["resource_id"])
                early.update(version_no=version["version_no"], kind=version["kind"])
                return early
            agreement = self._agreement(connection, agreement_id)
            operator_party_id = agreement["operating_party_id"]
            if not operator_party_id:
                raise ValidationError("协议未指定运营方")
            draft = connection.execute(
                "SELECT version_id FROM settlement_versions WHERE period_id=? AND status='draft'",
                (period_id,),
            ).fetchone()
            if draft:
                raise ConflictError("当前期间已有未关账草案，请先确认并关账")

            facts = connection.execute(
                "SELECT f.* FROM transport_facts f LEFT JOIN fact_inclusions i ON f.fact_id=i.fact_id "
                "WHERE f.agreement_id=? AND i.fact_id IS NULL AND substr(f.occurred_at,1,10) BETWEEN ? AND ? "
                "ORDER BY f.occurred_at, f.fact_id",
                (agreement_id, period["start_date"], period["end_date"]),
            ).fetchall()
            # 早期版本关账后仍未决的争议项，结转到新版本重新确认/争议，原分录标记 carried 被替代。
            carryovers = connection.execute(
                "SELECT e.* FROM settlement_entries e JOIN settlement_versions v ON e.version_id=v.version_id "
                "WHERE v.period_id=? AND e.status='disputed' ORDER BY e.entry_key",
                (period_id,),
            ).fetchall()
            if not facts and not carryovers:
                raise ConflictError("没有可计入的新运输事实或待结转争议（晚到事件登记后再生成新版本）")

            version_no = connection.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 AS next FROM settlement_versions WHERE period_id=?",
                (period_id,),
            ).fetchone()["next"]
            kind = "correction" if period["status"] == "closed" else (
                "regular" if version_no == 1 else "adjustment")
            tariff_ids = {fact["tariff_version_id"] for fact in facts}
            for carried in carryovers:
                if carried["fact_id"]:
                    tariff_row = connection.execute(
                        "SELECT tariff_version_id FROM transport_facts WHERE fact_id=?",
                        (carried["fact_id"],)).fetchone()
                    tariff_ids.add(tariff_row["tariff_version_id"])
            snapshot = self._load_snapshot(connection, agreement_id, period, tariff_ids)
            snapshot_hash = digest(snapshot)
            version_id = uuid.uuid4().hex
            segments_by_id = {s["segment_id"]: s for s in snapshot["segments"]}
            services_by_id = {s["service_id"]: s for s in snapshot["services"]}

            produced_entries: list[dict[str, Any]] = []
            total_signed = 0
            for fact in facts:
                tariff = snapshot["tariffs"][fact["tariff_version_id"]]
                rules = tariff["rules"]
                service = services_by_id[fact["service_id"]]
                route = []
                for step in service["route"]:
                    segment = segments_by_id[step["segment_id"]]
                    price = rules["segment_prices"].get(segment["code"])
                    if price is None:
                        raise ValidationError(f"运价版本 {tariff['version_no']} 缺少区段 {segment['code']} 的单价")
                    route.append({**segment, "segment_code": segment["code"],
                                  "unit_price_cents_per_km": price})
                signed_gross = -fact["gross_cents"] if fact["fact_type"] in NEGATIVE_FACTS else fact["gross_cents"]
                total_signed += signed_gross
                components = build_entries(
                    fact_type=fact["fact_type"], signed_gross=signed_gross, item_code=fact["item_code"],
                    operator_party_id=operator_party_id, channel_party_id=fact["channel_party_id"],
                    route=route, rules=rules)
                conservation_check(components, signed_gross)
                for component in components:
                    component = dict(component)
                    component["fact_id"] = fact["fact_id"]
                    component["correlation"] = fact["external_event_id"]
                    produced_entries.append(component)

            # 同一事实内成分按 1 起顺序编号，entry_key 稳定可复算、可逐行对账。
            produced_entries = self._renumber_entries(produced_entries)

            carried_total = sum(int(r["amount_cents"]) for r in carryovers)

            def create():
                connection.execute(
                    "INSERT INTO settlement_versions(version_id,period_id,agreement_id,version_no,kind,status,"
                    "rule_snapshot_json,snapshot_hash,created_by,created_at) "
                    "VALUES(?,?,?,?,?,'draft',?,?,?,?)",
                    (version_id, period_id, agreement_id, version_no, kind, canonical_json(snapshot),
                     snapshot_hash, actor_id, self._now()),
                )
                included: set[str] = set()
                for component in produced_entries:
                    status = "memo" if component["account"] == "memo" else "projected"
                    entry_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO settlement_entries(entry_id,version_id,entry_key,fact_id,line_no,account,"
                        "beneficiary_party_id,responsible_party_id,amount_cents,basis_json,correlation,status,"
                        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (entry_id, version_id, component["entry_key"], component["fact_id"],
                         component["line_no"], component["account"], component["beneficiary_party_id"],
                         component["responsible_party_id"], component["amount_cents"],
                         canonical_json(component["basis"]), component["correlation"], status, self._now()),
                    )
                    if component["fact_id"] not in included:
                        connection.execute(
                            "INSERT INTO fact_inclusions(fact_id,version_id,included_at) VALUES(?,?,?)",
                            (component["fact_id"], version_id, self._now()),
                        )
                        included.add(component["fact_id"])
                # 结转争议项：复制金额与依据到新版本（重新进入确认流程），原分录置 carried 被替代。
                carried_pairs: list[tuple[str, str]] = []
                for old in carryovers:
                    new_entry_id = uuid.uuid4().hex
                    new_key = f"{old['correlation'] or 'carried'}:carry:{old['entry_key']}"
                    basis = {**json.loads(old["basis_json"]), "carried_from_entry_id": old["entry_id"],
                             "carried_from_version_no": version_no - 1}
                    connection.execute(
                        "INSERT INTO settlement_entries(entry_id,version_id,entry_key,fact_id,line_no,account,"
                        "beneficiary_party_id,responsible_party_id,amount_cents,basis_json,correlation,status,"
                        "carried_from_entry_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (new_entry_id, version_id, new_key, old["fact_id"], old["line_no"], old["account"],
                         old["beneficiary_party_id"], old["responsible_party_id"], old["amount_cents"],
                         canonical_json(basis), old["correlation"], "projected", old["entry_id"], self._now()),
                    )
                    connection.execute(
                        "UPDATE settlement_entries SET status='carried' WHERE entry_id=?", (old["entry_id"],))
                    connection.execute(
                        "UPDATE settlement_disputes SET status='carried' WHERE entry_id=? AND status='open'",
                        (old["entry_id"],))
                    carried_pairs.append((old["entry_id"], new_entry_id))
                entries_hash = digest(self._entries_digest_material(connection, version_id))
                connection.execute("UPDATE settlement_versions SET entries_hash=? WHERE version_id=?",
                                   (entries_hash, version_id))
                self._audit(connection, actor_id, "settlement.generated", "settlement_version", version_id,
                            {"period_id": period_id, "version_no": version_no, "kind": kind,
                             "fact_count": len(facts), "carried_count": len(carryovers),
                             "signed_gross_cents": total_signed, "carried_cents": carried_total,
                             "snapshot_hash": snapshot_hash, "entries_hash": entries_hash})
                return "settlement_version", version_id, {"version_id": version_id, "version_no": version_no}

            result = self._idempotent(connection, request_id=request_id, action="generate_settlement",
                                      payload=payload, create=create)
            result["version_no"] = version_no
            result["kind"] = kind
            result["fact_count"] = len(facts)
            result["carried_count"] = len(carryovers)
            result["signed_gross_cents"] = total_signed
            return result

    @staticmethod
    def _renumber_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counters: dict[str, int] = {}
        for entry in entries:
            fact = entry["fact_id"]
            counters[fact] = counters.get(fact, 0) + 1
            entry["line_no"] = counters[fact]
            entry["entry_key"] = f"{entry['correlation']}:{entry['line_no']}"
        return entries

    def _entries_digest_material(self, connection, version_id: str) -> list[dict[str, Any]]:
        # 只固化不可变的记账内容（键、科目、金额、依据、关联），不含生命周期状态，
        # 这样确认/支付/争议/结转都不会让已生成版本的分录摘要漂移。
        return [dict(entry_key=r["entry_key"], fact_id=r["fact_id"], line_no=r["line_no"],
                     account=r["account"], beneficiary_party_id=r["beneficiary_party_id"],
                     responsible_party_id=r["responsible_party_id"], amount_cents=r["amount_cents"],
                     basis=json.loads(r["basis_json"]), correlation=r["correlation"])
                for r in connection.execute(
                    "SELECT * FROM settlement_entries WHERE version_id=? ORDER BY entry_key", (version_id,))]

    def _entry(self, connection, version_id: str, entry_id: str):
        row = connection.execute("SELECT * FROM settlement_entries WHERE entry_id=? AND version_id=?",
                                 (entry_id, version_id)).fetchone()
        if row is None:
            raise NotFoundError("明细分录不存在")
        return row

    def _party_for_actor(self, connection, actor) -> str | None:
        row = connection.execute(
            "SELECT party_id FROM clearing_parties WHERE organization_id=?",
            (actor["organization_id"],),
        ).fetchone()
        return row["party_id"] if row else None

    def confirm_entry(self, *, request_id: str, actor_id: str, version_id: str,
                      entry_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "version_id": version_id, "entry_id": entry_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            version = self._version(connection, version_id)
            entry = self._entry(connection, version_id, entry_id)
            if version["status"] != "draft":
                raise ConflictError("版本已关账，确认请通过新版本更正")
            if entry["status"] != "projected":
                raise ConflictError("只有待确认分录可以确认")
            party_id = self._party_for_actor(connection, actor)
            if actor["role"] not in ("admin", "reviewer") and party_id != entry["responsible_party_id"]:
                raise PermissionDenied("只能由责任方确认本分录")

            def create():
                connection.execute(
                    "UPDATE settlement_entries SET status='payable',confirmed_at=? WHERE entry_id=?",
                    (self._now(), entry_id),
                )
                self._audit(connection, actor_id, "entry.confirmed", "settlement_entry", entry_id,
                            {"version_id": version_id, "entry_key": entry["entry_key"],
                             "amount_cents": entry["amount_cents"]})
                return "settlement_entry", entry_id, {"entry_id": entry_id, "status": "payable"}

            return self._idempotent(connection, request_id=request_id, action="confirm_entry",
                                    payload=payload, create=create)

    def raise_dispute(self, *, request_id: str, actor_id: str, version_id: str,
                      entry_id: str, reason: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "version_id": version_id, "entry_id": entry_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            version = self._version(connection, version_id)
            entry = self._entry(connection, version_id, entry_id)
            if version["status"] != "draft":
                raise ConflictError("版本已关账，请在新版本中对争议更正")
            if entry["status"] not in ("projected", "payable"):
                raise ConflictError("当前分录状态不能提出争议")
            party_id = self._party_for_actor(connection, actor)
            touched = {entry["beneficiary_party_id"], entry["responsible_party_id"]}
            if actor["role"] != "admin" and party_id not in touched:
                raise PermissionDenied("只有与分录相关的参与方可以提出争议")
            reason = self._text(reason, "reason", 500)
            dispute_id = uuid.uuid4().hex

            def create():
                connection.execute(
                    "UPDATE settlement_entries SET status='disputed' WHERE entry_id=?", (entry_id,))
                connection.execute(
                    "INSERT INTO settlement_disputes(dispute_id,version_id,entry_id,raised_by_party_id,reason,"
                    "status,prior_status,created_at) VALUES(?,?,?,?,?,'open',?,?)",
                    (dispute_id, version_id, entry_id, party_id, reason, entry["status"], self._now()),
                )
                self._audit(connection, actor_id, "dispute.raised", "settlement_dispute", dispute_id,
                            {"version_id": version_id, "entry_id": entry_id,
                             "raised_by_party_id": party_id, "prior_status": entry["status"]})
                return "settlement_dispute", dispute_id, {"dispute_id": dispute_id, "status": "open"}

            return self._idempotent(connection, request_id=request_id, action="raise_dispute",
                                    payload=payload, create=create)

    def withdraw_dispute(self, *, request_id: str, actor_id: str, dispute_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "dispute_id": dispute_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            dispute = connection.execute("SELECT * FROM settlement_disputes WHERE dispute_id=?",
                                         (dispute_id,)).fetchone()
            if dispute is None:
                raise NotFoundError("争议不存在")
            if dispute["status"] != "open":
                raise ConflictError("争议已撤回或已结转")
            version = self._version(connection, dispute["version_id"])
            if version["status"] != "draft":
                raise ConflictError("版本已关账，争议结转至新版本处理，不能回改已关账分录")
            party_id = self._party_for_actor(connection, actor)
            if actor["role"] != "admin" and party_id != dispute["raised_by_party_id"]:
                raise PermissionDenied("只能由提出方撤回争议")

            def create():
                connection.execute(
                    "UPDATE settlement_entries SET status=?,confirmed_at=COALESCE(confirmed_at,?) WHERE entry_id=?",
                    (dispute["prior_status"], self._now(), dispute["entry_id"]),
                )
                connection.execute(
                    "UPDATE settlement_disputes SET status='withdrawn',withdrawn_at=? WHERE dispute_id=?",
                    (self._now(), dispute_id),
                )
                self._audit(connection, actor_id, "dispute.withdrawn", "settlement_dispute", dispute_id,
                            {"entry_id": dispute["entry_id"], "restored_status": dispute["prior_status"]})
                return "settlement_dispute", dispute_id, {"dispute_id": dispute_id, "status": "withdrawn"}

            return self._idempotent(connection, request_id=request_id, action="withdraw_dispute",
                                    payload=payload, create=create)

    def pay_entry(self, *, request_id: str, actor_id: str, version_id: str, entry_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "version_id": version_id, "entry_id": entry_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            entry = self._entry(connection, version_id, entry_id)
            if entry["status"] != "payable":
                raise ConflictError("只有已确认且无争议的应付分录可以支付")

            def create():
                connection.execute("UPDATE settlement_entries SET status='paid',paid_at=? WHERE entry_id=?",
                                   (self._now(), entry_id))
                self._audit(connection, actor_id, "entry.paid", "settlement_entry", entry_id,
                            {"version_id": version_id, "entry_key": entry["entry_key"],
                             "amount_cents": entry["amount_cents"]})
                return "settlement_entry", entry_id, {"entry_id": entry_id, "status": "paid"}

            return self._idempotent(connection, request_id=request_id, action="pay_entry",
                                    payload=payload, create=create)

    def pay_confirmed(self, *, request_id: str, actor_id: str, version_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "version_id": version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            self._version(connection, version_id)
            payable = connection.execute(
                "SELECT * FROM settlement_entries WHERE version_id=? AND status='payable' ORDER BY entry_key",
                (version_id,),
            ).fetchall()
            if not payable:
                raise ConflictError("当前版本没有可支付的无争议分录")
            paid_ids = [row["entry_id"] for row in payable]

            def create():
                connection.execute(
                    "UPDATE settlement_entries SET status='paid',paid_at=? WHERE version_id=? AND status='payable'",
                    (self._now(), version_id),
                )
                self._audit(connection, actor_id, "entries.paid", "settlement_version", version_id,
                            {"count": len(paid_ids), "entry_ids": paid_ids})
                return "settlement_version", version_id, {"version_id": version_id, "paid_count": len(paid_ids)}

            result = self._idempotent(connection, request_id=request_id, action="pay_confirmed",
                                      payload=payload, create=create)
            result["paid_count"] = len(paid_ids)
            return result

    def close_version(self, *, request_id: str, actor_id: str, version_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "version_id": version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            version = self._version(connection, version_id)
            if version["status"] == "closed":
                raise ConflictError("版本已经关账")
            pending = connection.execute(
                "SELECT COUNT(*) AS count FROM settlement_entries WHERE version_id=? AND status='projected'",
                (version_id,),
            ).fetchone()["count"]
            if pending:
                raise ConflictError(f"仍有 {pending} 条分录未确认或未提出争议，不能关账")

            def create():
                entries_hash = digest(self._entries_digest_material(connection, version_id))
                connection.execute(
                    "UPDATE settlement_versions SET status='closed',closed_at=?,entries_hash=? WHERE version_id=?",
                    (self._now(), entries_hash, version_id),
                )
                self._audit(connection, actor_id, "version.closed", "settlement_version", version_id,
                            {"period_id": version["period_id"], "version_no": version["version_no"],
                             "entries_hash": entries_hash})
                return "settlement_version", version_id, {"version_id": version_id, "status": "closed"}

            return self._idempotent(connection, request_id=request_id, action="close_version",
                                    payload=payload, create=create)

    def close_period(self, *, request_id: str, actor_id: str, period_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "period_id": period_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            period = self._period(connection, period_id)
            if period["status"] == "closed":
                raise ConflictError("期间已经关账")
            drafts = connection.execute(
                "SELECT COUNT(*) AS count FROM settlement_versions WHERE period_id=? AND status='draft'",
                (period_id,),
            ).fetchone()["count"]
            if drafts:
                raise ConflictError("仍有未关账的结算版本")

            def create():
                connection.execute("UPDATE settlement_periods SET status='closed',closed_at=? WHERE period_id=?",
                                   (self._now(), period_id))
                self._audit(connection, actor_id, "period.closed", "period", period_id,
                            {"agreement_id": period["agreement_id"], "period_key": period["period_key"]})
                return "period", period_id, {"period_id": period_id, "status": "closed"}

            return self._idempotent(connection, request_id=request_id, action="close_period",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------ 查询与可见性
    def get_version(self, connection, version_id: str) -> dict[str, Any]:
        version = self._version(connection, version_id)
        return {"version_id": version_id, "period_id": version["period_id"],
                "agreement_id": version["agreement_id"], "version_no": version["version_no"],
                "kind": version["kind"], "status": version["status"],
                "snapshot_hash": version["snapshot_hash"], "entries_hash": version["entries_hash"],
                "created_at": version["created_at"], "closed_at": version["closed_at"]}

    def settlement_report(self, *, actor_id: str, version_id: str) -> dict[str, Any]:
        """参与方只读己方明细与共享依据；监管/管理员可读全量。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        version = self._version(connection, version_id)
        snapshot = json.loads(version["rule_snapshot_json"])
        unrestricted = actor["role"] in ("admin", "auditor")
        party_id = None if unrestricted else self._party_for_actor(connection, actor)
        if not unrestricted and party_id is None:
            raise PermissionDenied("当前组织不是该协议参与方")

        entries = []
        for row in connection.execute(
                "SELECT * FROM settlement_entries WHERE version_id=? ORDER BY entry_key", (version_id,)):
            visible = unrestricted or party_id in (row["beneficiary_party_id"], row["responsible_party_id"])
            if not visible:
                continue
            entries.append({"entry_id": row["entry_id"], "entry_key": row["entry_key"],
                            "fact_id": row["fact_id"], "line_no": row["line_no"], "account": row["account"],
                            "beneficiary_party_id": row["beneficiary_party_id"],
                            "responsible_party_id": row["responsible_party_id"],
                            "amount_cents": row["amount_cents"], "basis": json.loads(row["basis_json"]),
                            "correlation": row["correlation"], "status": row["status"]})
        disputes = []
        for row in connection.execute(
                "SELECT * FROM settlement_disputes WHERE version_id=? ORDER BY created_at", (version_id,)):
            visible = unrestricted or party_id == row["raised_by_party_id"]
            item = {"dispute_id": row["dispute_id"], "entry_id": row["entry_id"],
                    "raised_by_party_id": row["raised_by_party_id"], "reason": row["reason"],
                    "status": row["status"], "prior_status": row["prior_status"],
                    "created_at": row["created_at"]}
            disputes.append(item if visible else {k: v for k, v in item.items()
                                                  if k in ("dispute_id", "entry_id", "status")})

        shared = self._shared_basis(connection, version, snapshot, unrestricted, party_id)
        return {"version": self.get_version(connection, version_id),
                "shared_basis": shared, "entries": entries, "disputes": disputes}

    def _shared_basis(self, connection, version, snapshot: dict[str, Any], unrestricted: bool,
                      party_id: str | None) -> dict[str, Any]:
        """所有参与方复算所需的共享依据：规则口径、区段权重、事件票款与守恒汇总。"""

        # 只统计本版本新计入的事实（fact_inclusions）；结转争议副本来自旧版本事实，
        # 其金额已在旧版本体现，不能再次计入本版本票款守恒，仅以 carried 标记单独对账。
        included_fact_ids = [r["fact_id"] for r in connection.execute(
            "SELECT fact_id FROM fact_inclusions WHERE version_id=? ORDER BY fact_id",
            (version["version_id"],))]
        facts = []
        signed_total = 0
        for fact_id in included_fact_ids:
            fact = connection.execute("SELECT * FROM transport_facts WHERE fact_id=?", (fact_id,)).fetchone()
            signed = -fact["gross_cents"] if fact["fact_type"] in NEGATIVE_FACTS else fact["gross_cents"]
            signed_total += signed
            facts.append({"external_event_id": fact["external_event_id"], "fact_type": fact["fact_type"],
                          "service_id": fact["service_id"],
                          "item_code": fact["item_code"], "gross_cents": fact["gross_cents"],
                          "signed_gross_cents": signed, "occurred_at": fact["occurred_at"],
                          "acknowledged_at": fact["acknowledged_at"],
                          "channel_party_id": fact["channel_party_id"],
                          "tariff_version_id": fact["tariff_version_id"]})
        # 从早期版本结转来的争议副本：旧版本对应分录已置 carried 并从余额排除，
        # 金额在本版本重新进入确认流程，因此本版本守恒为 票款 + 转入 = 分录合计。
        carried_in = connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total, COUNT(*) AS count "
            "FROM settlement_entries WHERE version_id=? AND carried_from_entry_id IS NOT NULL",
            (version["version_id"],)).fetchone()
        entry_total = connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM settlement_entries "
            "WHERE version_id=? AND account!='memo'", (version["version_id"],)).fetchone()["total"]
        return {"period": snapshot["period"], "parties": snapshot["parties"],
                "segments": snapshot["segments"],
                "services": [{"service_id": s["service_id"], "code": s["code"], "kind": s["kind"],
                              "items": s["items"], "route": s["route"]} for s in snapshot["services"]],
                "tariffs": snapshot["tariffs"], "facts": facts,
                "conservation": {"signed_gross_cents": signed_total,
                                 "carried_in_cents": carried_in["total"],
                                 "carried_in_count": carried_in["count"],
                                 "entries_total_cents": entry_total,
                                 "balanced": entry_total == signed_total + carried_in["total"]},
                "snapshot_hash": version["snapshot_hash"], "entries_hash": version["entries_hash"]}

    def period_balances(self, *, actor_id: str, period_id: str) -> dict[str, Any]:
        """汇总任一期间内各版本的最终余额（按参与方与状态），供监管核对。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        period = self._period(connection, period_id)
        versions = connection.execute(
            "SELECT * FROM settlement_versions WHERE period_id=? ORDER BY version_no", (period_id,))
        version_rows = versions.fetchall()
        balances: dict[str, dict[str, int]] = {}
        status_totals: dict[str, int] = {}
        grand_total = 0

        def ensure(party: str | None) -> dict[str, int]:
            if party is None:
                party = "__external__"
            return balances.setdefault(party, {"projected": 0, "payable": 0, "paid": 0,
                                               "disputed": 0, "memo": 0, "net": 0})

        for version in version_rows:
            for entry in connection.execute(
                    "SELECT * FROM settlement_entries WHERE version_id=? ORDER BY entry_key",
                    (version["version_id"],)):
                amount = entry["amount_cents"]
                status_totals[entry["status"]] = status_totals.get(entry["status"], 0)
                # carried 是被新版本副本替代的旧争议项，不计入余额，避免重复。
                if entry["status"] == "carried":
                    continue
                beneficiary = entry["beneficiary_party_id"]
                responsible = entry["responsible_party_id"]
                target = beneficiary
                if target is None and responsible is not None and amount < 0:
                    target = responsible
                if target is not None:
                    bucket = ensure(target)
                    bucket[entry["status"]] = bucket.get(entry["status"], 0) + amount
                    if entry["status"] != "memo":
                        bucket["net"] += amount
                if entry["status"] != "memo":
                    grand_total += amount
        if actor["role"] not in ("admin", "auditor"):
            party_id = self._party_for_actor(connection, actor)
            balances = {k: v for k, v in balances.items() if k == party_id}
        return {"period_id": period_id, "agreement_id": period["agreement_id"],
                "period_key": period["period_key"], "status": period["status"],
                "versions": [{"version_id": v["version_id"], "version_no": v["version_no"],
                              "kind": v["kind"], "status": v["status"],
                              "snapshot_hash": v["snapshot_hash"], "entries_hash": v["entries_hash"]}
                             for v in version_rows],
                "party_balances_cents": balances, "grand_total_cents": grand_total,
                "status_totals_cents": status_totals}

    def audit_period(self, *, actor_id: str, period_id: str) -> dict[str, Any]:
        """监管审计：还原任一期间的规则快照、全量分录、争议与最终余额。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        if actor["role"] != "auditor":
            raise PermissionDenied("仅监管审计角色可还原期间全量数据")
        period = self._period(connection, period_id)
        versions_out = []
        for version in connection.execute(
                "SELECT * FROM settlement_versions WHERE period_id=? ORDER BY version_no", (period_id,)):
            snapshot = json.loads(version["rule_snapshot_json"])
            entries = [dict(entry_id=r["entry_id"], entry_key=r["entry_key"], fact_id=r["fact_id"],
                            line_no=r["line_no"], account=r["account"],
                            beneficiary_party_id=r["beneficiary_party_id"],
                            responsible_party_id=r["responsible_party_id"], amount_cents=r["amount_cents"],
                            basis=json.loads(r["basis_json"]), correlation=r["correlation"],
                            status=r["status"], confirmed_at=r["confirmed_at"], paid_at=r["paid_at"])
                       for r in connection.execute(
                           "SELECT * FROM settlement_entries WHERE version_id=? ORDER BY entry_key",
                           (version["version_id"],))]
            disputes = [dict(dispute_id=r["dispute_id"], entry_id=r["entry_id"],
                             raised_by_party_id=r["raised_by_party_id"], reason=r["reason"],
                             status=r["status"], prior_status=r["prior_status"],
                             created_at=r["created_at"], withdrawn_at=r["withdrawn_at"])
                        for r in connection.execute(
                            "SELECT * FROM settlement_disputes WHERE version_id=? ORDER BY created_at",
                            (version["version_id"],))]
            entry_sum = sum(e["amount_cents"] for e in entries)
            versions_out.append({"version_id": version["version_id"], "version_no": version["version_no"],
                                 "kind": version["kind"], "status": version["status"],
                                 "snapshot_hash": version["snapshot_hash"],
                                 "entries_hash": version["entries_hash"],
                                 "rule_snapshot": snapshot, "entries": entries, "disputes": disputes,
                                 "entry_sum_cents": entry_sum, "created_at": version["created_at"],
                                 "closed_at": version["closed_at"]})
        balances = self.period_balances(actor_id=actor_id, period_id=period_id)
        return {"period": {"period_id": period["period_id"], "agreement_id": period["agreement_id"],
                           "period_key": period["period_key"], "start_date": period["start_date"],
                           "end_date": period["end_date"], "status": period["status"]},
                "versions": versions_out, "balances": balances}
