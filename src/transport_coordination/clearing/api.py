"""委托运输收入清算的 HTTP/JSON 路由。

由主 API 边界委托：写入接口携带 ``X-Actor-Id`` 与 ``request_id``，参与方只读自己的
明细与共享依据，auditor/admin 可读取审计还原。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

from ..errors import DomainError
from .service import ClearingService


def route_clearing(service: ClearingService, method: str, path: str, body: dict[str, Any],
                   actor_id: str) -> tuple[int, dict[str, Any]] | None:
    """返回 (status, payload)；非清算路由返回 None 由主路由继续处理。"""

    parsed = urlparse(path)
    query = parse_qs(parsed.query)

    def one(name: str, default: str = "") -> str:
        return query.get(name, [default])[0]

    def as_int(name: str) -> int | None:
        value = one(name, "")
        return int(value) if value else None

    try:
        if method == "POST" and parsed.path == "/clearing/agreements":
            receipt = service.register_agreement(actor_id=actor_id, **body)
            return _receipt(receipt)
        if method == "POST" and parsed.path == "/clearing/fare-versions":
            receipt = service.register_fare_version(actor_id=actor_id, **body)
            return _receipt(receipt)
        if method == "POST" and parsed.path == "/clearing/events":
            receipt = service.record_event(actor_id=actor_id, **body)
            return _receipt(receipt)
        if method == "POST" and parsed.path == "/clearing/drafts/build":
            receipt = service.build_draft(actor_id=actor_id, **body)
            return 200 if receipt.get("replayed") else 201, receipt
        if method == "POST" and parsed.path == "/clearing/drafts/confirm":
            receipt = service.confirm_draft(actor_id=actor_id, **body)
            return _receipt(receipt)
        if method == "POST" and parsed.path == "/clearing/periods/close":
            receipt = service.close_period(actor_id=actor_id, **body)
            return 200 if receipt.get("replayed") else 200, receipt
        if method == "POST" and parsed.path == "/clearing/disputes":
            receipt = service.open_dispute(actor_id=actor_id, **body)
            return _receipt(receipt)
        if method == "POST" and parsed.path == "/clearing/disputes/resolve":
            receipt = service.resolve_dispute(actor_id=actor_id, **body)
            return _receipt(receipt)
        if method == "GET" and parsed.path == "/clearing/lines":
            return 200, service.get_lines(
                actor_id, period=one("period") or None, event_id=one("event_id") or None)
        if method == "GET" and parsed.path == "/clearing/basis":
            agreement_id = one("agreement_id")
            if not agreement_id:
                return 400, {"error": "validation_error", "message": "agreement_id 不能为空"}
            return 200, service.get_shared_basis(actor_id, agreement_id=agreement_id)
        if method == "GET" and parsed.path == "/clearing/drafts":
            period = one("period")
            if not period:
                return 400, {"error": "validation_error", "message": "period 不能为空"}
            return 200, service.get_draft(actor_id, period=period, version_no=as_int("version_no"))
        if method == "GET" and parsed.path == "/clearing/balances":
            period = one("period")
            if not period:
                return 400, {"error": "validation_error", "message": "period 不能为空"}
            return 200, service.get_balance(actor_id, period=period, version_no=as_int("version_no"))
        if method == "GET" and parsed.path == "/clearing/audit":
            period = one("period")
            if not period:
                return 400, {"error": "validation_error", "message": "period 不能为空"}
            return 200, service.audit_reconstruction(actor_id, period=period)
        return None
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _receipt(receipt: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return 200 if receipt.get("replayed") else 201, receipt
