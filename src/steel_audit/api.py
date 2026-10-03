"""JSON HTTP API（标准库实现，无第三方依赖）。

路由
----
登记类（全部幂等：相同内容重复提交返回 200，冲突返回 409）
- POST /lines /equipment /shutdowns /rules /samples
- GET  /lines /equipment /rules /samples
- POST /samples/{sample_id}/withdraw

核算
- POST /recalculate                     试算（不签发），返回计算明细与证据缺口

结论
- POST /conclusions                     签发阶段结论
- POST /conclusions/{id}/corrections    以更正关系引用旧版签发新版
- GET  /conclusions/{id}                结论 + 完整计算明细
- GET  /conclusions/{id}/lineage        变更脉络（当前 -> 旧版）
- GET  /conclusions/{id}/descendants    后续更正版本
- GET  /lines/{line_id}/conclusions     产线下全部结论
"""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import urlparse

from . import serializers
from .errors import AuditError, ImmutableError, NotFoundError, ValidationError
from .service import AuditService


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "SteelAudit/1.0"

    # ---- 公共收发工具 ----------------------------------------------------

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default,
                          sort_keys=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"请求体不是合法 JSON: {exc}")
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _handle_errors(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except NotFoundError as exc:
            self._send_json(404, {"error": exc.code, "message": str(exc)})
        except ImmutableError as exc:
            self._send_json(409, {"error": exc.code, "message": str(exc)})
        except (ValidationError, AuditError) as exc:
            status = 409 if exc.code == "conflict" else 400
            self._send_json(status, {"error": exc.code, "message": str(exc)})
        except (ValueError, KeyError) as exc:
            self._send_json(400, {"error": "bad_request", "message": str(exc)})

    service: AuditService = None  # 由 server 注入（类属性，每个 server 独立）

    # ---- 路由 ------------------------------------------------------------

    def do_GET(self) -> None:
        self._handle_errors(lambda: self._route("GET"))

    def do_POST(self) -> None:
        self._handle_errors(lambda: self._route("POST"))

    def log_message(self, fmt, *args) -> None:  # 静音默认访问日志
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _route(self, method: str) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        svc: AuditService = self.server.service  # type: ignore[attr-defined]
        routes = self._routes(method)
        for pattern, handler in routes:
            m = re.fullmatch(pattern, path)
            if m:
                handler(svc, m.groupdict())
                return
        self._send_json(404, {"error": "not_found",
                              "message": f"无此路由: {method} {path}"})

    def _routes(self, method: str):
        table = {
            "GET": [
                (r"/lines", self._list_lines),
                (r"/equipment", self._list_equipment),
                (r"/rules", self._list_rules),
                (r"/samples", self._list_samples),
                (r"/lines/(?P<line_id>[^/]+)/conclusions",
                 self._list_conclusions),
                (r"/conclusions/(?P<conclusion_id>[^/]+)",
                 self._get_conclusion),
                (r"/conclusions/(?P<conclusion_id>[^/]+)/lineage",
                 self._lineage),
                (r"/conclusions/(?P<conclusion_id>[^/]+)/descendants",
                 self._descendants),
            ],
            "POST": [
                (r"/lines", self._register_line),
                (r"/equipment", self._add_equipment),
                (r"/shutdowns", self._add_shutdown),
                (r"/rules", self._add_rule),
                (r"/samples", self._submit_sample),
                (r"/samples/(?P<sample_id>[^/]+)/withdraw",
                 self._withdraw_sample),
                (r"/recalculate", self._recalculate),
                (r"/conclusions", self._issue_conclusion),
                (r"/conclusions/(?P<conclusion_id>[^/]+)/corrections",
                 self._correct_conclusion),
            ],
        }
        return table[method]

    # ---- 登记类处理 ------------------------------------------------------

    def _register_line(self, svc, _p) -> None:
        line = svc.register_line(**self._read_json())
        self._send_json(200, serializers.line_to_dict(line))

    def _list_lines(self, svc, _p) -> None:
        self._send_json(200, [serializers.line_to_dict(x)
                              for x in svc.repo.list_lines()])

    def _add_equipment(self, svc, _p) -> None:
        item = svc.add_equipment(**self._read_json())
        self._send_json(200, serializers.equipment_to_dict(item))

    def _list_equipment(self, svc, _p) -> None:
        self._send_json(200, [serializers.equipment_to_dict(x)
                              for x in svc.repo.list_equipment()])

    def _add_shutdown(self, svc, _p) -> None:
        item = svc.add_shutdown(**self._read_json())
        self._send_json(200, serializers.shutdown_to_dict(item))

    def _add_rule(self, svc, _p) -> None:
        rule = svc.add_rule(**self._read_json())
        self._send_json(200, serializers.rule_to_dict(rule))

    def _list_rules(self, svc, _p) -> None:
        self._send_json(200, [serializers.rule_to_dict(r)
                              for r in svc.repo.list_rules()])

    def _submit_sample(self, svc, _p) -> None:
        sample = svc.submit_sample(**self._read_json())
        self._send_json(200, serializers.sample_to_dict(sample))

    def _list_samples(self, svc, _p) -> None:
        self._send_json(200, [serializers.sample_to_dict(s)
                              for s in svc.repo.list_samples()])

    def _withdraw_sample(self, svc, params) -> None:
        body = self._read_json()
        sample = svc.withdraw_sample(
            params["sample_id"],
            reason=body.get("reason", ""),
            at=body.get("at"),
        )
        self._send_json(200, serializers.sample_to_dict(sample))

    # ---- 核算与结论 ------------------------------------------------------

    def _recalculate(self, svc, _p) -> None:
        body = self._read_json()
        result = svc.recalculate(
            line_id=body["line_id"],
            period=body["period"],
            anchor=body["anchor"],
            rule_id=body["rule_id"],
            as_of=body.get("as_of"),
            rule_version=body.get("rule_version"),
        )
        self._send_json(200, serializers.result_to_dict(result))

    def _issue_conclusion(self, svc, _p) -> None:
        body = self._read_json()
        conclusion = svc.issue_conclusion(
            line_id=body["line_id"],
            period=body["period"],
            anchor=body["anchor"],
            rule_id=body["rule_id"],
            issued_by=body["issued_by"],
            as_of=body.get("as_of"),
            rule_version=body.get("rule_version"),
            conclusion_id=body.get("conclusion_id"),
            issued_at=body.get("issued_at"),
        )
        self._send_json(201, serializers.conclusion_to_dict(conclusion))

    def _correct_conclusion(self, svc, params) -> None:
        body = self._read_json()
        conclusion = svc.correct_conclusion(
            old_conclusion_id=params["conclusion_id"],
            reason=body["reason"],
            issued_by=body["issued_by"],
            rule_id=body.get("rule_id"),
            rule_version=body.get("rule_version"),
            as_of=body.get("as_of"),
            conclusion_id=body.get("conclusion_id"),
            issued_at=body.get("issued_at"),
        )
        self._send_json(201, serializers.conclusion_to_dict(conclusion))

    def _get_conclusion(self, svc, params) -> None:
        conclusion = svc.get_conclusion(params["conclusion_id"])
        self._send_json(200, serializers.conclusion_to_dict(conclusion))

    def _lineage(self, svc, params) -> None:
        chain = svc.lineage(params["conclusion_id"])
        self._send_json(200, [
            serializers.conclusion_to_dict(c, include_result=False) for c in chain
        ])

    def _descendants(self, svc, params) -> None:
        items = svc.descendants(params["conclusion_id"])
        self._send_json(200, [
            serializers.conclusion_to_dict(c, include_result=False) for c in items
        ])

    def _list_conclusions(self, svc, params) -> None:
        items = svc.conclusions.list_for_line(params["line_id"])
        self._send_json(200, [
            serializers.conclusion_to_dict(c, include_result=False) for c in items
        ])


def create_server(host: str = "127.0.0.1", port: int = 0,
                  service: Optional[AuditService] = None,
                  verbose: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.service = service or AuditService()  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def main(argv: Optional[list] = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="钢铁产能改造核证后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    server = create_server(args.host, args.port, verbose=True)
    print(f"钢铁核证 API 已启动: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
