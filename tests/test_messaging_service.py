import re
import unittest
from datetime import timedelta

from devicefabric.service import Service, ServiceError, _utc_now

RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")


class MessagingServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.device_a = self.service.register_device(
            {"device_id": "sensor-a", "display_name": "甲传感器"}
        )
        self.device_b = self.service.register_device(
            {"device_id": "sensor-b", "display_name": "乙传感器"}
        )

    def create_session(self, device="a", client_id="cli-1", keepalive=30):
        device_id = {"a": "sensor-a", "b": "sensor-b"}[device]
        credential = {"a": self.device_a, "b": self.device_b}[device]["credential"]
        return self.service.create_session({
            "device_id": device_id,
            "credential": credential,
            "client_id": client_id,
            "keepalive_seconds": keepalive,
        })

    def subscribe(self, session, topic_filter):
        return self.service.add_subscription(
            session["session_id"],
            {"session_token": session["session_token"], "topic_filter": topic_filter},
        )

    def publish(self, session, topic, payload=None):
        return self.service.publish_message(
            session["session_id"],
            {"session_token": session["session_token"], "topic": topic,
             "payload": payload},
        )

    def poll(self, session, max_messages=100):
        return self.service.poll_messages(
            session["session_id"],
            {"session_token": session["session_token"], "max_messages": max_messages},
        )

    def force_expire(self, session_id):
        session = self.service._sessions[session_id]
        session["expires_at"] = _utc_now() - timedelta(seconds=1)

    # --------------------------------------------------------------
    # 订阅
    # --------------------------------------------------------------

    def test_subscribe_returns_current_filters(self) -> None:
        session = self.create_session()
        result = self.subscribe(session, "factory/+/temp")
        self.assertEqual(result["session_id"], session["session_id"])
        self.assertEqual(result["topic_filter"], "factory/+/temp")
        self.assertEqual(result["subscriptions"], ["factory/+/temp"])

    def test_duplicate_subscription_is_idempotent(self) -> None:
        session = self.create_session()
        self.subscribe(session, "a/b")
        result = self.subscribe(session, "a/b")
        self.assertEqual(result["subscriptions"], ["a/b"])
        self.assertEqual(len(self.service._sessions[session["session_id"]]["subscriptions"]), 1)

    def test_subscribe_accepts_unicode_and_wildcards(self) -> None:
        session = self.create_session()
        for topic_filter in ("#", "a/#", "+", "+/+", "传感器/#", "a/+/c"):
            with self.subTest(topic_filter=topic_filter):
                result = self.subscribe(session, topic_filter)
                self.assertIn(topic_filter, result["subscriptions"])

    def test_subscribe_invalid_filters_are_rejected_without_state(self) -> None:
        session = self.create_session()
        bad_filters = [
            "", "a//b", "/a", "a/", "a\x00b", "x" * 257,
            "a#", "a/#/b", "#/a", "#/#", "a+", "a/+b", "+a",
        ]
        for topic_filter in bad_filters:
            with self.subTest(topic_filter=topic_filter):
                with self.assertRaises(ServiceError) as ctx:
                    self.subscribe(session, topic_filter)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(
            self.service._sessions[session["session_id"]]["subscriptions"], set()
        )

    def test_subscribe_invalid_payloads(self) -> None:
        session = self.create_session()
        token = session["session_token"]
        bad_payloads = [
            "not-an-object",
            {},
            {"session_token": token},
            {"topic_filter": "a/b"},
            {"session_token": token, "topic_filter": "a/b", "extra": 1},
            {"session_token": 123, "topic_filter": "a/b"},
            {"session_token": token, "topic_filter": 5},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.add_subscription(session["session_id"], payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(
            self.service._sessions[session["session_id"]]["subscriptions"], set()
        )

    # --------------------------------------------------------------
    # 发布与匹配
    # --------------------------------------------------------------

    def test_publish_routes_to_matching_sessions_once(self) -> None:
        publisher = self.create_session(client_id="pub")
        subscriber = self.create_session(device="b", client_id="sub")
        # 两个过滤器同时命中同一会话，仍只入队一份。
        self.subscribe(subscriber, "factory/#")
        self.subscribe(subscriber, "factory/+/temp")

        result = self.publish(publisher, "factory/line1/temp", {"value": 21})
        self.assertEqual(result["matched_count"], 1)
        self.assertTrue(result["message_id"])

        messages = self.poll(subscriber)["messages"]
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message["message_id"], result["message_id"])
        self.assertEqual(message["topic"], "factory/line1/temp")
        self.assertEqual(message["payload"], {"value": 21})
        self.assertEqual(message["publisher_device_id"], "sensor-a")
        self.assertTrue(RFC3339_RE.match(message["published_at"]))

    def test_publish_to_self_is_allowed(self) -> None:
        session = self.create_session()
        self.subscribe(session, "self/topic")
        result = self.publish(session, "self/topic", "hello")
        self.assertEqual(result["matched_count"], 1)
        messages = self.poll(session)["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"], "hello")
        self.assertEqual(messages[0]["publisher_device_id"], "sensor-a")

    def test_publish_without_matching_subscriptions_matches_nothing(self) -> None:
        publisher = self.create_session(client_id="pub")
        subscriber = self.create_session(device="b", client_id="sub")
        self.subscribe(subscriber, "other/topic")
        result = self.publish(publisher, "factory/temp", 1)
        self.assertEqual(result["matched_count"], 0)
        self.assertEqual(self.poll(subscriber)["messages"], [])

    def test_wildcard_matching_semantics(self) -> None:
        cases = [
            # (过滤器, 主题, 是否匹配)
            ("a/b", "a/b", True),
            ("a/b", "a/b/c", False),
            ("a/b", "a/c", False),
            ("a/+", "a/b", True),
            ("a/+", "a/b/c", False),
            ("a/+", "a", False),
            ("+", "a", True),
            ("+", "a/b", False),
            ("a/#", "a", True),
            ("a/#", "a/b", True),
            ("a/#", "a/b/c", True),
            ("a/#", "ab", False),
            ("#", "a", True),
            ("#", "a/b/c", True),
            ("+/+", "a/b", True),
            ("+/+", "a", False),
            ("传感器/+", "传感器/温度", True),
        ]
        for topic_filter, topic, expected in cases:
            with self.subTest(topic_filter=topic_filter, topic=topic):
                # 每个用例使用全新服务，避免既有订阅干扰计数。
                service = Service()
                device = service.register_device(
                    {"device_id": "dev-1", "display_name": "设备"}
                )

                def connect(client_id):
                    return service.create_session({
                        "device_id": "dev-1",
                        "credential": device["credential"],
                        "client_id": client_id,
                        "keepalive_seconds": 30,
                    })

                publisher = connect("pub")
                subscriber = connect("sub")
                service.add_subscription(
                    subscriber["session_id"],
                    {"session_token": subscriber["session_token"],
                     "topic_filter": topic_filter},
                )
                result = service.publish_message(
                    publisher["session_id"],
                    {"session_token": publisher["session_token"],
                     "topic": topic, "payload": None},
                )
                self.assertEqual(result["matched_count"], 1 if expected else 0)

    def test_publish_accepts_any_json_payload(self) -> None:
        session = self.create_session()
        self.subscribe(session, "p/#")
        for index, payload in enumerate((None, True, 0, 1.5, "文本", [1, 2], {"k": "v"})):
            with self.subTest(payload=payload):
                result = self.publish(session, f"p/{index}", payload)
                self.assertEqual(result["matched_count"], 1)
        messages = self.poll(session)["messages"]
        self.assertEqual(
            [m["payload"] for m in messages],
            [None, True, 0, 1.5, "文本", [1, 2], {"k": "v"}],
        )

    def test_publish_message_ids_are_unique(self) -> None:
        session = self.create_session()
        ids = {self.publish(session, "t", i)["message_id"] for i in range(50)}
        self.assertEqual(len(ids), 50)

    def test_publish_invalid_topics_are_rejected_without_delivery(self) -> None:
        publisher = self.create_session(client_id="pub")
        subscriber = self.create_session(device="b", client_id="sub")
        self.subscribe(subscriber, "#")
        bad_topics = [
            "", "a//b", "/a", "a/", "a\x00b", "x" * 257,
            "a/+", "a/#", "+", "#", "a+/b",
        ]
        for topic in bad_topics:
            with self.subTest(topic=topic):
                with self.assertRaises(ServiceError) as ctx:
                    self.publish(publisher, topic)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.poll(subscriber)["messages"], [])

    def test_publish_invalid_payloads(self) -> None:
        session = self.create_session()
        token = session["session_token"]
        bad_payloads = [
            "not-an-object",
            {},
            {"session_token": token, "topic": "a"},
            {"session_token": token, "payload": None},
            {"session_token": token, "topic": "a", "payload": None, "extra": 1},
            {"session_token": token, "topic": 5, "payload": None},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.publish_message(session["session_id"], payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)

    # --------------------------------------------------------------
    # 拉取
    # --------------------------------------------------------------

    def test_poll_returns_messages_in_publish_order_and_drains(self) -> None:
        publisher = self.create_session(client_id="pub")
        subscriber = self.create_session(device="b", client_id="sub")
        self.subscribe(subscriber, "q/#")
        for index in range(5):
            self.publish(publisher, "q/topic", index)

        first = self.poll(subscriber, max_messages=2)["messages"]
        self.assertEqual([m["payload"] for m in first], [0, 1])
        rest = self.poll(subscriber)["messages"]
        self.assertEqual([m["payload"] for m in rest], [2, 3, 4])
        self.assertEqual(self.poll(subscriber)["messages"], [])

    def test_poll_empty_queue_returns_empty_list(self) -> None:
        session = self.create_session()
        self.assertEqual(self.poll(session)["messages"], [])

    def test_poll_max_messages_boundaries(self) -> None:
        publisher = self.create_session(client_id="pub")
        subscriber = self.create_session(device="b", client_id="sub")
        self.subscribe(subscriber, "#")
        for index in range(3):
            self.publish(publisher, "t", index)
        self.assertEqual(len(self.poll(subscriber, max_messages=1)["messages"]), 1)
        self.assertEqual(len(self.poll(subscriber, max_messages=100)["messages"]), 2)

    def test_poll_invalid_max_messages_keeps_queue(self) -> None:
        publisher = self.create_session(client_id="pub")
        subscriber = self.create_session(device="b", client_id="sub")
        self.subscribe(subscriber, "#")
        self.publish(publisher, "t", "kept")
        token = subscriber["session_token"]
        for max_messages in (0, 101, -1, 1.5, "5", True, None):
            with self.subTest(max_messages=max_messages):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.poll_messages(
                        subscriber["session_id"],
                        {"session_token": token, "max_messages": max_messages},
                    )
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        # 失败请求不改变消费位置。
        messages = self.poll(subscriber)["messages"]
        self.assertEqual([m["payload"] for m in messages], ["kept"])

    def test_poll_invalid_payloads(self) -> None:
        session = self.create_session()
        token = session["session_token"]
        bad_payloads = [
            "not-an-object",
            {},
            {"session_token": token},
            {"session_token": token, "max_messages": 10, "extra": 1},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.poll_messages(session["session_id"], payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)

    # --------------------------------------------------------------
    # 会话错误语义
    # --------------------------------------------------------------

    def test_unknown_session_is_not_found(self) -> None:
        for call in (
            lambda: self.service.add_subscription(
                "ghost", {"session_token": "x", "topic_filter": "a"}
            ),
            lambda: self.service.publish_message(
                "ghost", {"session_token": "x", "topic": "a", "payload": None}
            ),
            lambda: self.service.poll_messages(
                "ghost", {"session_token": "x", "max_messages": 1}
            ),
        ):
            with self.assertRaises(ServiceError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "session_not_found")
            self.assertEqual(ctx.exception.status, 404)

    def test_wrong_token_is_unauthorized(self) -> None:
        session = self.create_session()
        sid = session["session_id"]
        for call in (
            lambda: self.service.add_subscription(
                sid, {"session_token": "wrong", "topic_filter": "a"}
            ),
            lambda: self.service.publish_message(
                sid, {"session_token": "wrong", "topic": "a", "payload": None}
            ),
            lambda: self.service.poll_messages(
                sid, {"session_token": "wrong", "max_messages": 1}
            ),
        ):
            with self.assertRaises(ServiceError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "invalid_session_token")
            self.assertEqual(ctx.exception.status, 401)

    def test_offline_session_conflicts(self) -> None:
        expired = self.create_session(client_id="exp", keepalive=5)
        self.force_expire(expired["session_id"])
        replaced = self.create_session(client_id="rep")
        self.create_session(client_id="rep")  # 取代前一个会话
        for session in (expired, replaced):
            sid = session["session_id"]
            token = session["session_token"]
            for call in (
                lambda: self.service.add_subscription(
                    sid, {"session_token": token, "topic_filter": "a"}
                ),
                lambda: self.service.publish_message(
                    sid, {"session_token": token, "topic": "a", "payload": None}
                ),
                lambda: self.service.poll_messages(
                    sid, {"session_token": token, "max_messages": 1}
                ),
            ):
                with self.subTest(session=sid):
                    with self.assertRaises(ServiceError) as ctx:
                        call()
                    self.assertEqual(ctx.exception.code, "session_not_online")
                    self.assertEqual(ctx.exception.status, 409)

    # --------------------------------------------------------------
    # 生命周期：超时、取代、吊销
    # --------------------------------------------------------------

    def test_timed_out_subscriber_is_expired_and_not_delivered(self) -> None:
        publisher = self.create_session(client_id="pub")
        subscriber = self.create_session(device="b", client_id="sub", keepalive=5)
        self.subscribe(subscriber, "#")
        self.force_expire(subscriber["session_id"])

        result = self.publish(publisher, "t", "lost")
        self.assertEqual(result["matched_count"], 0)
        snapshot = self.service.get_session(subscriber["session_id"])
        self.assertEqual(snapshot["state"], "expired")
        self.assertEqual(snapshot["reason"], "keepalive_timeout")

    def test_replaced_session_loses_subscriptions_and_new_session_starts_clean(self) -> None:
        publisher = self.create_session(client_id="pub")
        first = self.create_session(device="b", client_id="sub")
        self.subscribe(first, "#")
        self.publish(publisher, "t", "old")

        second = self.create_session(device="b", client_id="sub")  # 取代 first
        # 旧会话的订阅与未取消息失效，新会话不继承。
        result = self.publish(publisher, "t", "new")
        self.assertEqual(result["matched_count"], 0)
        self.assertEqual(self.poll(second)["messages"], [])
        self.assertEqual(
            self.service._sessions[first["session_id"]]["subscriptions"], set()
        )
        self.assertEqual(self.service._sessions[first["session_id"]]["inbox"], [])

    def test_revoke_discards_subscriptions_and_pending_messages(self) -> None:
        publisher = self.create_session(client_id="pub")
        subscriber = self.create_session(device="b", client_id="sub")
        self.subscribe(subscriber, "#")
        self.publish(publisher, "t", "pending")

        self.service.revoke_device("sensor-b")
        self.assertEqual(
            self.service._sessions[subscriber["session_id"]]["subscriptions"], set()
        )
        self.assertEqual(self.service._sessions[subscriber["session_id"]]["inbox"], [])

        result = self.publish(publisher, "t", "after")
        self.assertEqual(result["matched_count"], 0)


if __name__ == "__main__":
    unittest.main()
