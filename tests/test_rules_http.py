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
        status, device = self.call(
            "POST", "/v1/devices",
            {"device_id": "dev-a", "display_name": "甲"},
        )
        self.assertEqual(status, 201)
        self.credential = device["credential"]

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
        req = urllib_request.Request(self.url(path), data=data, headers=headers,
                                     method=method)
        try:
            with urllib_request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            status = exc.code
            exc.close()
            return status, payload

    def valid_rule(self, rule_id="r1", **overrides):
        body = {
            "rule_id": rule_id,
            "topic_filter": "sensor/+/data",
            "enabled": True,
            "condition": {"path": ["temp"], "operator": "gt", "value": 30},
            "action": {"topic": "alerts/high", "payload": {"alert": True},
                       "qos": 0},
        }
        body.update(overrides)
        return body

    def connect(self, device_id="dev-a", client_id="cli-1"):
        status, session = self.call("POST", "/v1/device-sessions", {
            "device_id": device_id,
            "credential": self.credential,
            "client_id": client_id,
            "keepalive_seconds": 30,
        })
        self.assertEqual(status, 201)
        return session

    def subscribe(self, session, topic_filter):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/subscriptions",
            {"session_token": session["session_token"],
             "topic_filter": topic_filter},
        )

    def publish(self, session, topic, payload):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/publish",
            {"session_token": session["session_token"], "topic": topic,
             "payload": payload},
        )

    def poll(self, session):
        return self.call(
            "POST", f"/v1/device-sessions/{session['session_id']}/messages/poll",
            {"session_token": session["session_token"], "max_messages": 100},
        )

    # ------------------------------------------------------------------
    # CRUD over HTTP
    # ------------------------------------------------------------------

    def test_create_list_enable_delete_flow(self) -> None:
        status, body = self.call("POST", "/v1/rules", self.valid_rule())
        self.assertEqual(status, 201)
        self.assertEqual(body, self.valid_rule())

        status, body = self.call("GET", "/v1/rules")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"rules": [self.valid_rule()]})

        status, body = self.call(
            "PUT", "/v1/rules/r1/enabled", {"enabled": False}
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["enabled"])
        self.assertEqual(body["rule_id"], "r1")

        status, body = self.call("GET", "/v1/rules")
        self.assertFalse(body["rules"][0]["enabled"])

        status, body = self.call("DELETE", "/v1/rules/r1")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"deleted": True})

        status, body = self.call("GET", "/v1/rules")
        self.assertEqual(body, {"rules": []})

    def test_create_duplicate_conflict(self) -> None:
        status, _ = self.call("POST", "/v1/rules", self.valid_rule())
        self.assertEqual(status, 201)
        status, body = self.call("POST", "/v1/rules", self.valid_rule())
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "rule_already_exists")

    def test_enabled_and_delete_unknown_rule_404(self) -> None:
        status, body = self.call(
            "PUT", "/v1/rules/ghost/enabled", {"enabled": True}
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "rule_not_found")

        status, body = self.call("DELETE", "/v1/rules/ghost")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "rule_not_found")

    def test_invalid_requests(self) -> None:
        # 非 JSON。
        status, body = self.call("POST", "/v1/rules", raw_body=b"{bad")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        # 非对象。
        status, _ = self.call("POST", "/v1/rules", body=[1, 2])
        self.assertEqual(status, 400)

        # 缺字段 / 多字段。
        incomplete = self.valid_rule()
        del incomplete["condition"]
        status, _ = self.call("POST", "/v1/rules", incomplete)
        self.assertEqual(status, 400)
        status, _ = self.call("POST", "/v1/rules",
                              {**self.valid_rule("r2"), "nope": 1})
        self.assertEqual(status, 400)

        # 非法 rule_id。
        status, _ = self.call("POST", "/v1/rules",
                              self.valid_rule("bad/id"))
        self.assertEqual(status, 400)

        # 大小比较 value 为布尔。
        bad = self.valid_rule("r2")
        bad["condition"]["value"] = True
        status, _ = self.call("POST", "/v1/rules", bad)
        self.assertEqual(status, 400)

        # action.qos 为 2。
        bad = self.valid_rule("r2")
        bad["action"]["qos"] = 2
        status, _ = self.call("POST", "/v1/rules", bad)
        self.assertEqual(status, 400)

        # action.topic 含通配符。
        bad = self.valid_rule("r2")
        bad["action"]["topic"] = "a/+"
        status, _ = self.call("POST", "/v1/rules", bad)
        self.assertEqual(status, 400)

        # 启停只接受 {"enabled": bool}。
        self.call("POST", "/v1/rules", self.valid_rule())
        for body in ("x", None, [], {}, {"enabled": "yes"}, {"enabled": 1},
                     {"enabled": True, "extra": 1}):
            with self.subTest(body=body):
                status, payload = self.call(
                    "PUT", "/v1/rules/r1/enabled", body=body
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")

        # 全部失败后规则集合仍只有创建成功的一条且保持启用。
        status, body = self.call("GET", "/v1/rules")
        self.assertEqual(len(body["rules"]), 1)
        self.assertTrue(body["rules"][0]["enabled"])

    def test_unknown_rule_routes_404_not_400(self) -> None:
        # 路径段非法字符落到通用 not_found；合法但不存在才 rule_not_found。
        status, body = self.call(
            "PUT", "/v1/rules/ghost/enabled", {"enabled": True}
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "rule_not_found")

    # ------------------------------------------------------------------
    # 端到端：发布触发动作
    # ------------------------------------------------------------------

    def test_publish_triggers_action_end_to_end(self) -> None:
        rule = self.valid_rule()
        status, _ = self.call("POST", "/v1/rules", rule)
        self.assertEqual(status, 201)

        publisher = self.connect(client_id="pub")
        subscriber = self.connect(device_id="dev-a", client_id="sub")
        status, _ = self.subscribe(subscriber, "alerts/#")
        self.assertEqual(status, 200)

        status, body = self.publish(
            publisher, "sensor/room1/data", {"temp": 31}
        )
        self.assertEqual(status, 202)
        # 原主题没有订阅者：matched_count 为 0。
        self.assertEqual(body["matched_count"], 0)

        status, body = self.poll(subscriber)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["messages"]), 1)
        message = body["messages"][0]
        self.assertEqual(message["topic"], "alerts/high")
        self.assertEqual(message["payload"], {"alert": True})
        self.assertEqual(message["publisher_device_id"], "dev-a")
        self.assertNotIn("retained", message)

    def test_disabled_rule_fires_nothing(self) -> None:
        self.call("POST", "/v1/rules", self.valid_rule(enabled=False))
        publisher = self.connect(client_id="pub")
        subscriber = self.connect(device_id="dev-a", client_id="sub")
        self.subscribe(subscriber, "alerts/#")
        status, body = self.publish(
            publisher, "sensor/room1/data", {"temp": 99}
        )
        self.assertEqual(status, 202)
        status, body = self.poll(subscriber)
        self.assertEqual(body["messages"], [])


if __name__ == "__main__":
    unittest.main()
