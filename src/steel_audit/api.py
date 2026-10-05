"""基于标准库的 JSON HTTP API（无第三方依赖）。

监管常用查询：
- GET /api/conclusions/{id}         结论计算明细（分段、分摊、样本清单、证据缺口）
- GET /api/conclusions/{id}/lineage 变更脉络（更正链 + 每版差异说明）
- GET /api/audit?line_id=...        产线全量操作审计
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import DomainError
from .service import SteelAuditService


def _p(pattern: str):
    return re.compile(pattern)


# (method, 路径正则, handler(service, path_params, query, body) -> (status, obj))
ROUTES = [
    ("POST", _p(r"/api/lines"),
     lambda s, p, q, b: (201, s.create_line(
         plant_id=b.get("plant_id", ""), name=b.get("name", ""),
         capacity_tonnes=b.get("capacity_tonnes", "0"),
         line_id=b.get("line_id"), actor=b.get("actor", "api")))),
    ("GET", _p(r"/api/lines/(?P<line_id>[^/]+)"),
     lambda s, p, q, b: (200, s.get_line(p["line_id"]))),
    ("POST", _p(r"/api/lines/(?P<line_id>[^/]+)/equipment-intervals"),
     lambda s, p, q, b: (201, s.add_equipment_interval(
         p["line_id"], equipment_code=b.get("equipment_code", ""),
         commissioned_from=b.get("commissioned_from"),
         decommissioned_to=b.get("decommissioned_to"),
         technology=b.get("technology", ""),
         capacity_tph=b.get("capacity_tph", "0"),
         actor=b.get("actor", "api")))),
    ("GET", _p(r"/api/lines/(?P<line_id>[^/]+)/equipment-intervals"),
     lambda s, p, q, b: (200, {"items": s.list_equipment(p["line_id"])})),
    ("POST", _p(r"/api/lines/(?P<line_id>[^/]+)/samples"),
     lambda s, p, q, b: (201, s.submit_sample(
         p["line_id"], agency_uid=b.get("agency_uid", ""),
         indicator=b.get("indicator", ""), value=b.get("value"),
         unit=b.get("unit", ""), interval_start=b.get("interval_start"),
         interval_end=b.get("interval_end"), received_at=b.get("received_at"),
         actor=b.get("actor", "api")))),
    ("GET", _p(r"/api/lines/(?P<line_id>[^/]+)/samples"),
     lambda s, p, q, b: (200, {"items": s.list_samples(p["line_id"])})),
    ("POST", _p(r"/api/samples/(?P<sample_id>[^/]+)/withdraw"),
     lambda s, p, q, b: (200, s.withdraw_sample(
         p["sample_id"], reason=b.get("reason", ""),
         actor=b.get("actor", "api")))),
    ("POST", _p(r"/api/lines/(?P<line_id>[^/]+)/shutdowns"),
     lambda s, p, q, b: (201, s.add_shutdown(
         p["line_id"], start=b.get("start"), end=b.get("end"),
         reason=b.get("reason"), evidence_ref=b.get("evidence_ref"),
         actor=b.get("actor", "api")))),
    ("POST", _p(r"/api/rule-versions"),
     lambda s, p, q, b: (201, s.create_rule_version(
         code=b.get("code", ""), effective_from=b.get("effective_from"),
         params=b.get("params") or {}, actor=b.get("actor", "api")))),
    ("GET", _p(r"/api/rule-versions"),
     lambda s, p, q, b: (200, {"items": s.list_rule_versions()})),
    ("POST", _p(r"/api/computations"),
     lambda s, p, q, b: (201, s.compute(
         line_id=b.get("line_id", ""), period_type=b.get("period_type", ""),
         period_start=b.get("period_start"),
         rule_version_id=b.get("rule_version_id"),
         data_cutoff=b.get("data_cutoff"), actor=b.get("actor", "api")))),
    ("POST", _p(r"/api/conclusions/(?P<conclusion_id>[^/]+)/issue"),
     lambda s, p, q, b: (200, s.issue(p["conclusion_id"],
                                      actor=b.get("actor", "api")))),
    ("GET", _p(r"/api/conclusions/(?P<conclusion_id>[^/]+)"),
     lambda s, p, q, b: (200, s.get_conclusion(p["conclusion_id"]))),
    ("GET", _p(r"/api/conclusions/(?P<conclusion_id>[^/]+)/lineage"),
     lambda s, p, q, b: (200, s.get_lineage(p["conclusion_id"]))),
    ("GET", _p(r"/api/lines/(?P<line_id>[^/]+)/conclusions"),
     lambda s, p, q, b: (200, {"items": s.list_conclusions(
         p["line_id"], period_type=q.get("period_type"),
         period_start=q.get("period_start"))})),
    ("GET", _p(r"/api/audit"),
     lambda s, p, q, b: (200, {"items": s.audit_trail(
         entity_type=q.get("entity_type"), entity_id=q.get("entity_id"),
         line_id=q.get("line_id"))})),
]


class _Handler(BaseHTTPRequestHandler):
    service: SteelAuditService = None  # 由 make_server 注入
    server_version = "SteelAudit/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        body = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except json.JSONDecodeError:
                    return self._send(400, {"error": {
                        "code": "bad_json", "message": "请求体不是合法 JSON"}})
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            match = pattern.fullmatch(parsed.path)
            if not match:
                continue
            try:
                status, obj = handler(self.service, match.groupdict(), query, body)
            except DomainError as exc:
                return self._send(exc.http_status, {"error": {
                    "code": exc.code, "message": str(exc)}})
            except (ValueError, TypeError) as exc:
                return self._send(400, {"error": {
                    "code": "bad_request", "message": str(exc)}})
            return self._send(status, obj)
        self._send(404, {"error": {"code": "not_found",
                                   "message": f"未知路径: {method} {parsed.path}"}})

    def _send(self, status: int, obj) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # 静默访问日志
        pass


def make_server(service: SteelAuditService, host: str = "127.0.0.1",
                port: int = 8080) -> ThreadingHTTPServer:
    """构造绑定指定服务的 HTTP 服务（port=0 时由系统分配端口，便于测试）。"""
    handler_cls = type("BoundSteelAuditHandler", (_Handler,), {"service": service})
    return ThreadingHTTPServer((host, port), handler_cls)
