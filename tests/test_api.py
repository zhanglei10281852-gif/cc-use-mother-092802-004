"""HTTP API 冒烟测试：真实起服务、走完整核证流程。"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from steel_audit.api import make_server  # noqa: E402
from support import make_service  # noqa: E402


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service, cls.clock = make_service()
        cls.server = make_server(cls.service, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_verification_flow(self):
        status, line = self.call("POST", "/api/lines", {
            "line_id": "L-API", "plant_id": "P1", "name": "API 产线"})
        self.assertEqual(status, 201)

        status, _ = self.call("POST", "/api/lines/L-API/equipment-intervals", {
            "equipment_code": "BF-1", "commissioned_from": "2026-01-01T00:00:00Z"})
        self.assertEqual(status, 201)

        status, rule = self.call("POST", "/api/rule-versions", {
            "code": "R-API", "effective_from": "2026-01-01T00:00:00Z",
            "params": {"indicators": ["pm"], "method": "measured",
                       "min_coverage_ratio": "0"}})
        self.assertEqual(status, 201)

        status, sample1 = self.call("POST", "/api/lines/L-API/samples", {
            "agency_uid": "api-1", "indicator": "pm", "value": "100", "unit": "kg",
            "interval_start": "2026-06-10T00:00:00Z",
            "interval_end": "2026-06-10T12:00:00Z",
            "received_at": "2026-06-10T13:00:00Z"})
        self.assertEqual(status, 201)
        self.assertEqual(sample1["outcome"], "accepted")

        status, sample2 = self.call("POST", "/api/lines/L-API/samples", {
            "agency_uid": "api-2", "indicator": "pm", "value": "50", "unit": "kg",
            "interval_start": "2026-06-20T00:00:00Z",
            "interval_end": "2026-06-20T12:00:00Z",
            "received_at": "2026-06-20T13:00:00Z"})
        self.assertEqual(status, 201)

        # 同一编号不同内容 → 409 冲突
        status, conflict = self.call("POST", "/api/lines/L-API/samples", {
            "agency_uid": "api-1", "indicator": "pm", "value": "999", "unit": "kg",
            "interval_start": "2026-06-10T00:00:00Z",
            "interval_end": "2026-06-10T12:00:00Z"})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "conflict")

        # 计算并签发
        status, draft = self.call("POST", "/api/computations", {
            "line_id": "L-API", "period_type": "month",
            "period_start": "2026-06-01T00:00:00Z"})
        self.assertEqual(status, 201)
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["totals"]["total_emission"], "150.000000")

        status, issued = self.call(
            "POST", f"/api/conclusions/{draft['conclusion_id']}/issue", {})
        self.assertEqual(status, 200)
        self.assertEqual(issued["status"], "issued")
        self.assertEqual(issued["explanation"]["type"], "initial")

        # 监管查询：计算明细
        status, detail = self.call(
            "GET", f"/api/conclusions/{issued['conclusion_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(len(detail["detail"]["samples"]), 2)
        self.assertIn("segments", detail["detail"])

        # 撤回一个样本 → 重算 → 签发更正
        status, _ = self.call(
            "POST", f"/api/samples/{sample2['sample']['sample_id']}/withdraw",
            {"reason": "机构作废"})
        self.assertEqual(status, 200)

        status, draft2 = self.call("POST", "/api/computations", {
            "line_id": "L-API", "period_type": "month",
            "period_start": "2026-06-01T00:00:00Z"})
        self.assertEqual(draft2["totals"]["total_emission"], "100.000000")
        status, issued2 = self.call(
            "POST", f"/api/conclusions/{draft2['conclusion_id']}/issue", {})
        self.assertEqual(issued2["corrects_id"], issued["conclusion_id"])

        # 监管查询：变更脉络
        status, lineage = self.call(
            "GET", f"/api/conclusions/{issued['conclusion_id']}/lineage")
        self.assertEqual(status, 200)
        self.assertEqual(len(lineage["chain"]), 2)
        self.assertEqual(lineage["current_head"], issued2["conclusion_id"])
        self.assertEqual(lineage["chain"][1]["explanation"]["samples_removed"],
                         [sample2["sample"]["sample_id"]])

        # 审计轨迹
        status, audit = self.call("GET", "/api/audit?line_id=L-API")
        self.assertEqual(status, 200)
        actions = [e["action"] for e in audit["items"]]
        self.assertIn("sample_withdrawn", actions)
        self.assertIn("conclusion_issued", actions)

    def test_not_found_and_bad_request(self):
        status, body = self.call("GET", "/api/conclusions/con_missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

        status, _ = self.call("POST", "/api/computations", {
            "line_id": "L-API", "period_type": "month",
            "period_start": "2026-06-15T00:00:00Z"})  # 未对齐
        self.assertEqual(status, 400)

        status, _ = self.call("GET", "/api/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
