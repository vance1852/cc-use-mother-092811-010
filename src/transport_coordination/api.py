"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .clearing_service import ClearingService
from .service import DomainService
from .storage import Database


def _clearing(service: DomainService) -> ClearingService:
    return ClearingService(service.database, service.clock)


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}

        status, payload = _clearing_route(_clearing(service), method, parsed.path, query, body, actor_id)
        if status is not None:
            return status, payload
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _replayed(result: dict[str, Any]) -> int:
    return 200 if result.get("replayed") else 201


def _clearing_route(clearing: ClearingService, method: str, path: str,
                    query: dict[str, list[str]], body: dict[str, Any], actor_id: str
                    ) -> tuple[int | None, dict[str, Any]]:
    """分派委托运输收入清算域的路由。"""

    def arg(name: str, default: Any = None) -> Any:
        return query.get(name, [default])[0]

    if method == "POST":
        routes_post = {
            "/agreements": clearing.create_agreement,
            "/agreements/activate": clearing.activate_agreement,
            "/parties": clearing.register_party,
            "/segments": clearing.define_segment,
            "/services": clearing.define_service,
            "/tariff-versions": clearing.create_tariff_version,
            "/transport-facts": clearing.record_fact,
            "/periods": clearing.open_period,
            "/periods/close": clearing.close_period,
            "/settlements/generate": clearing.generate_settlement,
            "/settlements/pay-confirmed": clearing.pay_confirmed,
            "/settlements/close": clearing.close_version,
            "/entries/confirm": clearing.confirm_entry,
            "/entries/pay": clearing.pay_entry,
            "/disputes": clearing.raise_dispute,
            "/disputes/withdraw": clearing.withdraw_dispute,
        }
        handler = routes_post.get(path)
        if handler is not None:
            result = handler(actor_id=actor_id, **body)
            return _replayed(result), result
    if method == "GET":
        if path == "/settlements/report":
            version_id = arg("version_id", "")
            if not version_id:
                raise ValidationError("version_id 不能为空")
            return 200, clearing.settlement_report(actor_id=actor_id, version_id=version_id)
        if path == "/periods/balances":
            period_id = arg("period_id", "")
            if not period_id:
                raise ValidationError("period_id 不能为空")
            return 200, clearing.period_balances(actor_id=actor_id, period_id=period_id)
        if path == "/periods/audit":
            period_id = arg("period_id", "")
            if not period_id:
                raise ValidationError("period_id 不能为空")
            return 200, clearing.audit_period(actor_id=actor_id, period_id=period_id)
    return None, {}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
