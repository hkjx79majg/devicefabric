import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError

from devicefabric.server import Handler
from devicefabric.service import Service


class RulesHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        handler_cls = type("TestHandler", (Handler,), {"service": Service()})
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

    def call(self, method: str, path: str, body=None, raw_body: bytes | None = None):
        data = raw_body
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib_request.Request(self.url(path), data=data, headers=headers, method=method)
        try:
            with urllib_request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            status = exc.code
            exc.close()
            return status, payload

    def rule_body(self, **overrides):
        body = {
            "rule_id": "rule-01",
            "topic_filter": "house/+/temp",
            "enabled": True,
            "condition": {"path": ["temp"], "operator": "gt", "value": 30},
            "action": {"topic": "alerts/hot", "payload": {"a": 1}, "qos": 1},
        }
        body.update(overrides)
        return body

    def test_create_list_enable_delete_flow(self) -> None:
        status, created = self.call("POST", "/v1/rules", self.rule_body())
        self.assertEqual(status, 201)
        self.assertEqual(created["rule_id"], "rule-01")
        self.assertTrue(created["enabled"])

        status, listed = self.call("GET", "/v1/rules")
        self.assertEqual(status, 200)
        self.assertEqual(len(listed["rules"]), 1)
        self.assertEqual(listed["rules"][0], created)

        status, disabled = self.call(
            "PUT", "/v1/rules/rule-01/enabled", {"enabled": False}
        )
        self.assertEqual(status, 200)
        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["topic_filter"], "house/+/temp")

        status, enabled = self.call(
            "PUT", "/v1/rules/rule-01/enabled", {"enabled": True}
        )
        self.assertEqual(status, 200)
        self.assertTrue(enabled["enabled"])

        status, deleted = self.call("DELETE", "/v1/rules/rule-01")
        self.assertEqual(status, 200)
        self.assertEqual(deleted, {"deleted": True})

        status, listed = self.call("GET", "/v1/rules")
        self.assertEqual(listed, {"rules": []})

    def test_list_is_empty_initially(self) -> None:
        status, listed = self.call("GET", "/v1/rules")
        self.assertEqual(status, 200)
        self.assertEqual(listed, {"rules": []})

    def test_listing_order_is_creation_order(self) -> None:
        self.call("POST", "/v1/rules", self.rule_body(rule_id="r-b"))
        self.call("POST", "/v1/rules",
                  self.rule_body(rule_id="r-a", topic_filter="a"))
        self.call("POST", "/v1/rules",
                  self.rule_body(rule_id="r-c", topic_filter="c"))
        status, listed = self.call("GET", "/v1/rules")
        self.assertEqual(status, 200)
        self.assertEqual([r["rule_id"] for r in listed["rules"]],
                         ["r-b", "r-a", "r-c"])

    def test_duplicate_rule_id_conflict(self) -> None:
        self.call("POST", "/v1/rules", self.rule_body())
        status, payload = self.call(
            "POST", "/v1/rules", self.rule_body(topic_filter="other/#")
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "rule_already_exists")
        status, listed = self.call("GET", "/v1/rules")
        self.assertEqual(listed["rules"][0]["topic_filter"], "house/+/temp")

    def test_missing_rule_targets_are_not_found(self) -> None:
        status, payload = self.call(
            "PUT", "/v1/rules/ghost/enabled", {"enabled": True}
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "rule_not_found")

        status, payload = self.call("DELETE", "/v1/rules/ghost")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "rule_not_found")

    def test_invalid_create_requests(self) -> None:
        # 非 JSON 请求体。
        status, payload = self.call(
            "POST", "/v1/rules", raw_body=b"not json{"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

        invalid_bodies = [
            {},
            {"rule_id": "r1"},
            {"rule_id": "r1", "topic_filter": "a", "enabled": True,
             "condition": {"path": ["x"], "operator": "eq", "value": 1},
             "action": {"topic": "a", "payload": 1, "qos": 0}, "nope": 1},
            {"rule_id": "bad/id", "topic_filter": "a", "enabled": True,
             "condition": {"path": ["x"], "operator": "eq", "value": 1},
             "action": {"topic": "a", "payload": 1, "qos": 0}},
            {"rule_id": "r1", "topic_filter": "a", "enabled": "yes",
             "condition": {"path": ["x"], "operator": "eq", "value": 1},
             "action": {"topic": "a", "payload": 1, "qos": 0}},
            {"rule_id": "r1", "topic_filter": "a", "enabled": True,
             "condition": {"path": ["x"], "operator": "gt", "value": True},
             "action": {"topic": "a", "payload": 1, "qos": 0}},
            {"rule_id": "r1", "topic_filter": "a", "enabled": True,
             "condition": {"path": ["x"], "operator": "eq", "value": 1},
             "action": {"topic": "a/+", "payload": 1, "qos": 0}},
            {"rule_id": "r1", "topic_filter": "a", "enabled": True,
             "condition": {"path": ["x"], "operator": "eq", "value": 1},
             "action": {"topic": "a", "payload": 1, "qos": 5}},
        ]
        for body in invalid_bodies:
            with self.subTest(body=body):
                status, payload = self.call("POST", "/v1/rules", body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")
        status, listed = self.call("GET", "/v1/rules")
        self.assertEqual(listed, {"rules": []})

    def test_invalid_enabled_bodies(self) -> None:
        self.call("POST", "/v1/rules", self.rule_body())
        for body in (
            {}, {"enabled": 1}, {"enabled": 0}, {"enabled": "true"},
            {"enabled": None}, {"enabled": False, "extra": 1},
        ):
            with self.subTest(body=body):
                status, payload = self.call(
                    "PUT", "/v1/rules/rule-01/enabled", body
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")
        # 非 JSON。
        status, payload = self.call(
            "PUT", "/v1/rules/rule-01/enabled", raw_body=b"{"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        status, listed = self.call("GET", "/v1/rules")
        self.assertTrue(listed["rules"][0]["enabled"])

    def test_rule_fires_end_to_end_over_http(self) -> None:
        # 注册两台设备并建立在线会话。
        _, pub_device = self.call(
            "POST", "/v1/devices",
            {"device_id": "sensor-1", "display_name": "发布者"},
        )
        _, sub_device = self.call(
            "POST", "/v1/devices",
            {"device_id": "sensor-2", "display_name": "订阅者"},
        )

        def connect(device_id, credential, client_id):
            status, session = self.call("POST", "/v1/device-sessions", {
                "device_id": device_id,
                "credential": credential,
                "client_id": client_id,
                "keepalive_seconds": 30,
            })
            self.assertEqual(status, 201)
            return session

        publisher = connect("sensor-1", pub_device["credential"], "pub")
        listener = connect("sensor-2", sub_device["credential"], "sub")

        status, _ = self.call(
            "POST",
            f"/v1/device-sessions/{listener['session_id']}/subscriptions",
            {"session_token": listener["session_token"],
             "topic_filter": "alerts/#"},
        )
        self.assertEqual(status, 200)

        self.call("POST", "/v1/rules", {
            "rule_id": "hot",
            "topic_filter": "house/+/temp",
            "enabled": True,
            "condition": {"path": ["temp"], "operator": "gt", "value": 30},
            "action": {"topic": "alerts/hot",
                       "payload": {"level": "critical"}, "qos": 1},
        })

        status, publish_result = self.call(
            "POST",
            f"/v1/device-sessions/{publisher['session_id']}/publish",
            {"session_token": publisher["session_token"],
             "topic": "house/room1/temp", "payload": {"temp": 31}},
        )
        self.assertEqual(status, 202)
        # 监听器只订阅 alerts/#，原消息未命中；动作投递不计入。
        self.assertEqual(publish_result["matched_count"], 0)

        status, polled = self.call(
            "POST",
            f"/v1/device-sessions/{listener['session_id']}/messages/poll",
            {"session_token": listener["session_token"], "max_messages": 10},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(polled["messages"]), 1)
        message = polled["messages"][0]
        self.assertEqual(message["topic"], "alerts/hot")
        self.assertEqual(message["payload"], {"level": "critical"})
        self.assertEqual(message["publisher_device_id"], "sensor-1")
        self.assertEqual(message["qos"], 1)
        self.assertFalse(message["dup"])
        self.assertNotIn("retained", message)
        self.assertNotEqual(message["message_id"], publish_result["message_id"])

        # 确认该 QoS 1 动作投递，避免未确认消息在后续拉取时自然重投。
        status, acked = self.call(
            "POST",
            f"/v1/device-sessions/{listener['session_id']}/messages/ack",
            {"session_token": listener["session_token"],
             "delivery_ids": [message["delivery_id"]]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(acked["acked_count"], 1)

        # 停用后不再产生动作。
        self.call("PUT", "/v1/rules/hot/enabled", {"enabled": False})
        self.call(
            "POST",
            f"/v1/device-sessions/{publisher['session_id']}/publish",
            {"session_token": publisher["session_token"],
             "topic": "house/room1/temp", "payload": {"temp": 31}},
        )
        status, polled = self.call(
            "POST",
            f"/v1/device-sessions/{listener['session_id']}/messages/poll",
            {"session_token": listener["session_token"], "max_messages": 10},
        )
        self.assertEqual(polled["messages"], [])

    def test_unknown_rule_routes_404(self) -> None:
        status, payload = self.call("DELETE", "/v1/rules/")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
