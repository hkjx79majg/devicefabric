import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError

from devicefabric.server import Handler
from devicefabric.service import Service

SHA_A = "a" * 64

VALID_RULE = {
    "rule_id": "rule-1",
    "topic_filter": "sensors/+/temp",
    "enabled": True,
    "condition": {"path": ["temp"], "operator": "gt", "value": 30},
    "action": {"topic": "alerts/high-temp", "payload": {"x": 1}, "qos": 1},
}


class AuditHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        handler_cls = type("TestHandler", (Handler,), {"service": self.service})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def call(self, method: str, path: str, body=None):
        if body is None:
            data = b"{}"
        else:
            data = json.dumps(body).encode("utf-8")
        req = urllib_request.Request(
            self.url(path),
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib_request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            status = exc.code
            exc.close()
            return status, payload

    def get(self, path: str):
        req = urllib_request.Request(self.url(path), method="GET")
        try:
            with urllib_request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            status = exc.code
            exc.close()
            return status, payload

    def seed_events(self) -> None:
        self.call("POST", "/v1/devices",
                  {"device_id": "dev-a", "display_name": "甲"})
        self.call("POST", "/v1/rules", VALID_RULE)
        self.call("POST", "/v1/devices",
                  {"device_id": "dev-b", "display_name": "乙"})

    def test_empty_log_returns_empty_page(self) -> None:
        status, body = self.get("/v1/audit-events")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"events": [], "next_after": 0})

    def test_events_recorded_and_paginated(self) -> None:
        self.seed_events()
        status, page1 = self.get("/v1/audit-events?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(
            [event["action"] for event in page1["events"]],
            ["device.created", "rule.created"],
        )
        self.assertEqual(page1["next_after"], 2)

        status, page2 = self.get("/v1/audit-events?after=2&limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(
            [event["action"] for event in page2["events"]],
            ["device.created"],
        )
        self.assertEqual(page2["next_after"], 3)

        status, empty = self.get("/v1/audit-events?after=3")
        self.assertEqual(status, 200)
        self.assertEqual(empty["events"], [])
        self.assertEqual(empty["next_after"], 3)

        for event in page1["events"] + page2["events"]:
            self.assertEqual(set(event), {
                "sequence", "occurred_at", "action",
                "resource_type", "resource_id",
            })
        self.assertEqual(page1["events"][0]["resource_type"], "device")
        self.assertEqual(page1["events"][0]["resource_id"], "dev-a")

    def test_filters(self) -> None:
        self.seed_events()
        status, body = self.get("/v1/audit-events?action=device.created")
        self.assertEqual(status, 200)
        self.assertEqual(
            [event["resource_id"] for event in body["events"]],
            ["dev-a", "dev-b"],
        )
        self.assertEqual(body["next_after"], 3)

        status, body = self.get("/v1/audit-events?resource_id=rule-1")
        self.assertEqual(status, 200)
        self.assertEqual(
            [event["action"] for event in body["events"]], ["rule.created"]
        )

    def test_invalid_query_returns_400(self) -> None:
        self.seed_events()
        for query in (
            "after=-1", "after=x", "limit=0", "limit=101",
            "limit=2.0", "after=1&after=2", "unknown=1", "after=",
        ):
            with self.subTest(query=query):
                status, body = self.get(f"/v1/audit-events?{query}")
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "invalid_request")

    def test_cursor_expired_returns_410(self) -> None:
        # after=0 在最老事件仍为序号 1 时合法；人为淘汰后再验证 410。
        self.seed_events()
        log = self.service._audit
        # 直接清空存储但保留 sequence 增长，模拟淘汰后游标失效。
        log._events.clear()
        self.call("POST", "/v1/devices",
                  {"device_id": "dev-c", "display_name": "丙"})
        status, body = self.get("/v1/audit-events?after=0")
        self.assertEqual(status, 410)
        self.assertEqual(body["error"]["code"], "audit_cursor_expired")

        # 未传 after 不受影响，从现存最早事件读取。
        status, body = self.get("/v1/audit-events")
        self.assertEqual(status, 200)
        self.assertEqual(
            [event["action"] for event in body["events"]], ["device.created"]
        )
        self.assertEqual(body["events"][0]["resource_id"], "dev-c")

    def test_revocation_idempotency_appends_once(self) -> None:
        self.seed_events()
        status, _ = self.call("POST", "/v1/devices/dev-a/revoke")
        self.assertEqual(status, 200)
        status, _ = self.call("POST", "/v1/devices/dev-a/revoke")
        self.assertEqual(status, 200)
        status, body = self.get("/v1/audit-events?action=device.revoked")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["events"]), 1)

    def test_failed_requests_not_audited(self) -> None:
        self.seed_events()
        status, _ = self.call("POST", "/v1/devices",
                              {"device_id": "dev-a", "display_name": "重名"})
        self.assertEqual(status, 409)
        status, _ = self.call("POST", "/v1/devices/ghost/revoke")
        self.assertEqual(status, 404)
        status, body = self.get("/v1/audit-events")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["events"]), 3)

    def test_unknown_audit_route_shape_is_404(self) -> None:
        status, body = self.get("/v1/audit-events/extra")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
