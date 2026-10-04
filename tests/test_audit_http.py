import json
import threading
import unittest
from collections import deque
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError

from devicefabric.server import Handler
from devicefabric.service import Service


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
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib_request.Request(
            self.url(path), data=data, headers=headers, method=method
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

    def register(self, device_id: str):
        return self.call(
            "POST", "/v1/devices",
            {"device_id": device_id, "display_name": device_id},
        )

    # ------------------------------------------------------------------
    # 基本查询
    # ------------------------------------------------------------------

    def test_empty_log_returns_empty_page(self) -> None:
        status, payload = self.get("/v1/audit-events")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"events": [], "next_after": 0})

    def test_events_visible_after_committed_changes(self) -> None:
        status, _ = self.register("dev-1")
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/v1/devices/dev-1/credential/rotate")
        self.assertEqual(status, 200)
        status, _ = self.call("POST", "/v1/devices/dev-1/revoke")
        self.assertEqual(status, 200)

        status, payload = self.get("/v1/audit-events")
        self.assertEqual(status, 200)
        self.assertEqual(
            [event["action"] for event in payload["events"]],
            ["device.created", "device.credential_rotated", "device.revoked"],
        )
        self.assertEqual(
            [event["sequence"] for event in payload["events"]], [1, 2, 3]
        )
        self.assertEqual(payload["next_after"], 3)
        for event in payload["events"]:
            self.assertEqual(event["resource_type"], "device")
            self.assertEqual(event["resource_id"], "dev-1")
            self.assertNotIn("credential", event)
            self.assertNotIn("session_token", event)
            self.assertNotIn("payload", event)
            self.assertNotIn("result", event)

    def test_pagination_round_trip(self) -> None:
        for index in range(5):
            self.register(f"dev-{index}")
        status, page1 = self.get("/v1/audit-events?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(
            [event["sequence"] for event in page1["events"]], [1, 2]
        )
        status, page2 = self.get(f"/v1/audit-events?after={page1['next_after']}&limit=2")
        self.assertEqual(
            [event["sequence"] for event in page2["events"]], [3, 4]
        )
        status, page3 = self.get(f"/v1/audit-events?after={page2['next_after']}&limit=2")
        self.assertEqual([event["sequence"] for event in page3["events"]], [5])
        self.assertEqual(page3["next_after"], 5)
        status, page4 = self.get(f"/v1/audit-events?after={page3['next_after']}")
        self.assertEqual(page4, {"events": [], "next_after": 5})

    def test_filters(self) -> None:
        self.register("dev-1")
        self.register("dev-2")
        self.call("POST", "/v1/devices/dev-2/revoke")
        status, payload = self.get("/v1/audit-events?action=device.revoked")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["resource_id"], "dev-2")
        status, payload = self.get("/v1/audit-events?resource_id=dev-1")
        self.assertEqual(
            [event["action"] for event in payload["events"]], ["device.created"]
        )

    def test_query_does_not_append_audit_events(self) -> None:
        self.register("dev-1")
        self.get("/v1/audit-events")
        self.get("/v1/audit-events?limit=1")
        _, payload = self.get("/v1/audit-events")
        self.assertEqual(len(payload["events"]), 1)

    # ------------------------------------------------------------------
    # 参数校验
    # ------------------------------------------------------------------

    def assert_bad_request(self, path: str) -> None:
        status, payload = self.get(path)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_invalid_query_parameters(self) -> None:
        for path in (
            "/v1/audit-events?foo=1",
            "/v1/audit-events?after=1&after=2",
            "/v1/audit-events?limit=1&limit=2",
            "/v1/audit-events?after=-1",
            "/v1/audit-events?after=abc",
            "/v1/audit-events?after=",
            "/v1/audit-events?limit=0",
            "/v1/audit-events?limit=101",
            "/v1/audit-events?limit=1.5",
        ):
            self.assert_bad_request(path)

    # ------------------------------------------------------------------
    # 游标过期
    # ------------------------------------------------------------------

    def test_evicted_cursor_returns_410(self) -> None:
        self.service._audit_events = deque(maxlen=3)
        for index in range(5):
            self.register(f"dev-{index}")
        status, payload = self.get("/v1/audit-events?after=1")
        self.assertEqual(status, 410)
        self.assertEqual(payload["error"]["code"], "audit_cursor_expired")
        # 未传 after 时从现存最早事件读取。
        status, payload = self.get("/v1/audit-events")
        self.assertEqual(status, 200)
        self.assertEqual(
            [event["sequence"] for event in payload["events"]], [3, 4, 5]
        )


if __name__ == "__main__":
    unittest.main()
