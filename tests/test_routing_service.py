import re
import unittest
from datetime import timedelta

from devicefabric.service import (
    Service,
    ServiceError,
    _topic_matches_filter,
    _utc_now,
)

RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")


class TopicMatchTest(unittest.TestCase):
    def match(self, topic: str, topic_filter: str) -> bool:
        return _topic_matches_filter(topic.split("/"), topic_filter.split("/"))

    def test_exact_topic(self) -> None:
        self.assertTrue(self.match("a/b/c", "a/b/c"))
        self.assertFalse(self.match("a/b/c", "a/b"))
        self.assertFalse(self.match("a/b", "a/b/c"))
        self.assertFalse(self.match("a/x/c", "a/b/c"))

    def test_plus_matches_single_layer(self) -> None:
        self.assertTrue(self.match("a/b/c", "a/+/c"))
        self.assertFalse(self.match("a/b/x/c", "a/+/c"))
        self.assertFalse(self.match("a/c", "a/+/c"))

    def test_hash_matches_zero_or_more_layers(self) -> None:
        self.assertTrue(self.match("a/b/c", "a/#"))
        self.assertTrue(self.match("a", "a/#"))
        self.assertTrue(self.match("a/b/c/d", "a/#"))
        self.assertFalse(self.match("x/b", "a/#"))

    def test_hash_only_matches_everything(self) -> None:
        self.assertTrue(self.match("a", "#"))
        self.assertTrue(self.match("a/b/c", "#"))


class RoutingServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.device = self.service.register_device(
            {"device_id": "sensor-01", "display_name": "一号传感器"}
        )
        self.other = self.service.register_device(
            {"device_id": "sensor-02", "display_name": "二号传感器"}
        )

    def connect(self, device_id="sensor-01", credential=None, client_id="cli-1",
                keepalive=30):
        return self.service.create_session({
            "device_id": device_id,
            "credential": credential if credential is not None
            else (self.device["credential"] if device_id == "sensor-01"
                  else self.other["credential"]),
            "client_id": client_id,
            "keepalive_seconds": keepalive,
        })

    def subscribe(self, session, topic_filter):
        return self.service.subscribe_topic(
            session["session_id"],
            {"session_token": session["session_token"], "topic_filter": topic_filter},
        )

    def publish(self, session, topic, payload):
        return self.service.publish_message(
            session["session_id"],
            {"session_token": session["session_token"], "topic": topic,
             "payload": payload},
        )

    def poll(self, session, max_messages=100):
        return self.service.poll_messages(
            session["session_id"],
            {"session_token": session["session_token"],
             "max_messages": max_messages},
        )

    def force_expire(self, session) -> None:
        record = self.service._sessions[session["session_id"]]
        record["expires_at"] = _utc_now() - timedelta(seconds=1)

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------

    def test_subscribe_returns_filter_and_is_idempotent(self) -> None:
        session = self.connect()
        result = self.subscribe(session, "a/+/c")
        self.assertEqual(result, {"topic_filter": "a/+/c"})
        self.subscribe(session, "a/+/c")
        record = self.service._sessions[session["session_id"]]
        self.assertEqual(record["subscriptions"], {"a/+/c"})

    def test_subscribe_unknown_session_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.subscribe_topic(
                "ghost", {"session_token": "x", "topic_filter": "a"}
            )
        self.assertEqual(ctx.exception.code, "session_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_subscribe_wrong_token_unauthorized(self) -> None:
        session = self.connect()
        with self.assertRaises(ServiceError) as ctx:
            self.service.subscribe_topic(
                session["session_id"],
                {"session_token": "wrong", "topic_filter": "a"},
            )
        self.assertEqual(ctx.exception.code, "invalid_session_token")
        self.assertEqual(ctx.exception.status, 401)

    def test_subscribe_closed_session_conflicts(self) -> None:
        first = self.connect()
        self.connect()  # 取代
        with self.assertRaises(ServiceError) as ctx:
            self.subscribe(first, "a")
        self.assertEqual(ctx.exception.code, "session_not_online")
        self.assertEqual(ctx.exception.status, 409)

    def test_subscribe_invalid_payloads(self) -> None:
        session = self.connect()
        bad_payloads = [
            "not-an-object",
            {},
            {"topic_filter": "a"},
            {"session_token": session["session_token"]},
            {"session_token": session["session_token"], "topic_filter": "a",
             "extra": 1},
            {"session_token": 123, "topic_filter": "a"},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.subscribe_topic(session["session_id"], payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(
            self.service._sessions[session["session_id"]]["subscriptions"], set()
        )

    def test_invalid_topics_and_filters(self) -> None:
        session = self.connect()
        bad_topics = [
            "", "a/", "/a", "a//b", "a" * 257, "a\x00b", "a/+/b", "a/#", "+",
            "a/+b", "a/b#",
        ]
        for topic in bad_topics:
            with self.subTest(topic=topic):
                with self.assertRaises(ServiceError) as ctx:
                    self.publish(session, topic, 1)
                self.assertEqual(ctx.exception.code, "invalid_request")
        bad_filters = ["", "a/", "/a", "a//b", "f" * 257, "a\x00", "a/+b",
                       "#/a", "a/#/b", "a/b#", "a/#/#"]
        for topic_filter in bad_filters:
            with self.subTest(topic_filter=topic_filter):
                with self.assertRaises(ServiceError) as ctx:
                    self.subscribe(session, topic_filter)
                self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(
            self.service._sessions[session["session_id"]]["subscriptions"], set()
        )

    def test_unicode_topic_length_counts_codepoints(self) -> None:
        session = self.connect()
        # 每个中文字符占多字节但只算一个码点。
        self.subscribe(session, "中" * 256)
        result = self.publish(session, "中" * 256, None)
        self.assertEqual(result["matched_count"], 1)

    # ------------------------------------------------------------------
    # 发布与投递
    # ------------------------------------------------------------------

    def test_publish_delivers_to_matching_online_session(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "house/+/temp")

        result = self.publish(publisher, "house/room1/temp", {"v": 21.5})
        self.assertEqual(result["matched_count"], 1)
        self.assertTrue(result["message_id"])

        polled = self.poll(listener)
        self.assertEqual(len(polled["messages"]), 1)
        message = polled["messages"][0]
        self.assertEqual(message["message_id"], result["message_id"])
        self.assertEqual(message["topic"], "house/room1/temp")
        self.assertEqual(message["payload"], {"v": 21.5})
        self.assertEqual(message["publisher_device_id"], "sensor-01")
        self.assertTrue(RFC3339_RE.match(message["published_at"]))

        # 拉取后消息移出队列。
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_publish_to_self_allowed(self) -> None:
        session = self.connect()
        self.subscribe(session, "self")
        result = self.publish(session, "self", "hello")
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(len(self.poll(session)["messages"]), 1)

    def test_multiple_filters_hit_session_once(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "a/#")
        self.subscribe(listener, "a/+/c")
        self.subscribe(listener, "#")
        result = self.publish(publisher, "a/b/c", None)
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(len(self.poll(listener)["messages"]), 1)

    def test_matched_count_counts_sessions_and_no_offline_delivery(self) -> None:
        publisher = self.connect(client_id="pub")
        one = self.subscribe_session("s1", "a/#", device_id="sensor-02")
        two = self.subscribe_session("s2", "a/b", device_id="sensor-02")
        result = self.publish(publisher, "a/b", None)
        self.assertEqual(result["matched_count"], 2)

        # 其中一个会话过期后不再被投递。
        self.force_expire(one)
        result = self.publish(publisher, "a/b", None)
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(len(self.poll(two)["messages"]), 2)

    def subscribe_session(self, client_id, topic_filter, device_id="sensor-02"):
        session = self.connect(device_id=device_id, client_id=client_id)
        self.subscribe(session, topic_filter)
        return session

    def test_payload_accepts_all_json_value_kinds(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("s9", "p", device_id="sensor-02")
        for payload in (None, True, 1, "str", [1, 2], {"k": [0]}):
            with self.subTest(payload=payload):
                self.publish(publisher, "p", payload)
        messages = self.poll(listener)["messages"]
        self.assertEqual([m["payload"] for m in messages],
                         [None, True, 1, "str", [1, 2], {"k": [0]}])

    def test_preserves_publication_order(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("s3", "t", device_id="sensor-02")
        ids = [self.publish(publisher, "t", i)["message_id"] for i in range(5)]
        messages = self.poll(listener, max_messages=3)["messages"]
        self.assertEqual([m["message_id"] for m in messages], ids[:3])
        messages = self.poll(listener, max_messages=100)["messages"]
        self.assertEqual([m["message_id"] for m in messages], ids[3:])

    def test_message_ids_are_unique(self) -> None:
        publisher = self.connect(client_id="pub")
        ids = {self.publish(publisher, "unused", i)["message_id"]
               for i in range(50)}
        self.assertEqual(len(ids), 50)

    def test_publish_invalid_payloads_change_nothing(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("s4", "ok", device_id="sensor-02")
        bad_payloads = [
            "not-an-object",
            {},
            {"session_token": publisher["session_token"], "topic": "ok"},
            {"session_token": publisher["session_token"], "payload": 1},
            {"topic": "ok", "payload": 1},
            {"session_token": publisher["session_token"], "topic": "ok",
             "payload": 1, "extra": 1},
            {"session_token": 1, "topic": "ok", "payload": 1},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.publish_message(publisher["session_id"], payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(self.poll(listener)["messages"], [])

    # ------------------------------------------------------------------
    # 拉取
    # ------------------------------------------------------------------

    def test_poll_empty_queue_returns_empty_list(self) -> None:
        session = self.connect()
        self.assertEqual(self.poll(session)["messages"], [])

    def test_poll_max_messages_validation(self) -> None:
        session = self.connect()
        for value in (0, 101, -1, "10", 5.0, True, None):
            with self.subTest(value=value):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.poll_messages(
                        session["session_id"],
                        {"session_token": session["session_token"],
                         "max_messages": value},
                    )
                self.assertEqual(ctx.exception.code, "invalid_request")

    def test_poll_unknown_session_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.poll_messages(
                "ghost", {"session_token": "x", "max_messages": 10}
            )
        self.assertEqual(ctx.exception.code, "session_not_found")

    def test_poll_wrong_token_does_not_consume(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("s5", "q", device_id="sensor-02")
        self.publish(publisher, "q", 1)
        with self.assertRaises(ServiceError) as ctx:
            self.service.poll_messages(
                listener["session_id"],
                {"session_token": "wrong", "max_messages": 10},
            )
        self.assertEqual(ctx.exception.code, "invalid_session_token")
        # 消费位置不变，消息仍可取回。
        self.assertEqual(len(self.poll(listener)["messages"]), 1)

    def test_poll_expired_session_conflicts(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("s6", "q", device_id="sensor-02")
        self.publish(publisher, "q", 1)
        self.force_expire(listener)
        with self.assertRaises(ServiceError) as ctx:
            self.poll(listener)
        self.assertEqual(ctx.exception.code, "session_not_online")

    # ------------------------------------------------------------------
    # 离线即失效
    # ------------------------------------------------------------------

    def test_timeout_discards_subscriptions_and_queued_messages(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("s7", "q", device_id="sensor-02")
        self.publish(publisher, "q", 1)
        self.force_expire(listener)

        # 对过期会话的任何操作都返回 409。
        with self.assertRaises(ServiceError) as ctx:
            self.subscribe(listener, "q")
        self.assertEqual(ctx.exception.code, "session_not_online")

        # 同 client_id 重连得到新会话，不继承订阅，也收不到旧消息。
        reconnect = self.connect(device_id="sensor-02", client_id="s7")
        self.assertEqual(
            self.service._sessions[reconnect["session_id"]]["subscriptions"], set()
        )
        self.assertEqual(self.poll(reconnect)["messages"], [])
        self.publish(publisher, "q", 2)
        self.assertEqual(self.poll(reconnect)["messages"], [])

    def test_replaced_session_discards_routes(self) -> None:
        old = self.connect(client_id="same")
        self.subscribe(old, "q")
        self.publish(old, "q", 1)
        new = self.connect(client_id="same")
        record = self.service._sessions[new["session_id"]]
        self.assertEqual(record["subscriptions"], set())
        self.assertEqual(self.poll(new)["messages"], [])

    def test_revoked_device_sessions_drop_routes(self) -> None:
        publisher = self.connect(client_id="pub")
        victim = self.subscribe_session("s8", "q", device_id="sensor-02")
        self.publish(publisher, "q", 1)
        self.service.revoke_device("sensor-02")
        with self.assertRaises(ServiceError) as ctx:
            self.poll(victim)
        self.assertEqual(ctx.exception.code, "session_not_online")
        # 发布时吊销设备的会话既不在线也不计入 matched_count。
        result = self.publish(publisher, "q", 2)
        self.assertEqual(result["matched_count"], 0)


if __name__ == "__main__":
    unittest.main()
