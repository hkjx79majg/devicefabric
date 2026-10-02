import unittest
from datetime import timedelta

from devicefabric.service import Service, ServiceError, _utc_now


class QosServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.device = self.service.register_device(
            {"device_id": "sensor-01", "display_name": "一号传感器"}
        )
        self.other = self.service.register_device(
            {"device_id": "sensor-02", "display_name": "二号传感器"}
        )

    def connect(self, device_id="sensor-01", client_id="cli-1", keepalive=30):
        credential = (
            self.device["credential"]
            if device_id == "sensor-01"
            else self.other["credential"]
        )
        return self.service.create_session({
            "device_id": device_id,
            "credential": credential,
            "client_id": client_id,
            "keepalive_seconds": keepalive,
        })

    def subscribe(self, session, topic_filter):
        return self.service.subscribe_topic(
            session["session_id"],
            {"session_token": session["session_token"], "topic_filter": topic_filter},
        )

    def subscribe_session(self, client_id, topic_filter, device_id="sensor-02"):
        session = self.connect(device_id=device_id, client_id=client_id)
        self.subscribe(session, topic_filter)
        return session

    def publish(self, session, topic, payload, qos=None):
        body = {"session_token": session["session_token"], "topic": topic,
                "payload": payload}
        if qos is not None:
            body["qos"] = qos
        return self.service.publish_message(session["session_id"], body)

    def poll(self, session, max_messages=100):
        return self.service.poll_messages(
            session["session_id"],
            {"session_token": session["session_token"],
             "max_messages": max_messages},
        )

    def ack(self, session, delivery_ids):
        return self.service.ack_messages(
            session["session_id"],
            {"session_token": session["session_token"],
             "delivery_ids": delivery_ids},
        )

    def force_expire(self, session) -> None:
        record = self.service._sessions[session["session_id"]]
        record["expires_at"] = _utc_now() - timedelta(seconds=1)

    # ------------------------------------------------------------------
    # qos 字段校验
    # ------------------------------------------------------------------

    def test_publish_without_qos_behaves_as_qos0(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("sub", "t")
        result = self.publish(publisher, "t", 1)
        self.assertEqual(result["matched_count"], 1)
        message = self.poll(listener)["messages"][0]
        self.assertNotIn("qos", message)
        self.assertNotIn("delivery_id", message)
        self.assertNotIn("dup", message)
        # 取出即删除。
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_publish_explicit_qos0_matches_default(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("sub", "t")
        result = self.publish(publisher, "t", 1, qos=0)
        self.assertEqual(result["matched_count"], 1)
        message = self.poll(listener)["messages"][0]
        self.assertEqual(
            set(message),
            {"message_id", "topic", "payload", "publisher_device_id",
             "published_at"},
        )
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_publish_invalid_qos_rejected_without_delivery(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("sub", "t")
        for qos in (2, -1, "0", "1", 1.0, 0.0, True, False, None, [1], {"v": 1}):
            with self.subTest(qos=qos):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.publish_message(
                        publisher["session_id"],
                        {"session_token": publisher["session_token"],
                         "topic": "t", "payload": 1, "qos": qos},
                    )
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.poll(listener)["messages"], [])

    # ------------------------------------------------------------------
    # QoS 1 投递与重投
    # ------------------------------------------------------------------

    def test_qos1_first_delivery_carries_delivery_fields(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("sub", "t")
        result = self.publish(publisher, "t", {"v": 1}, qos=1)
        self.assertEqual(result["matched_count"], 1)
        self.assertTrue(result["message_id"])

        message = self.poll(listener)["messages"][0]
        self.assertEqual(message["message_id"], result["message_id"])
        self.assertEqual(message["topic"], "t")
        self.assertEqual(message["payload"], {"v": 1})
        self.assertEqual(message["publisher_device_id"], "sensor-01")
        self.assertTrue(message["published_at"])
        self.assertEqual(message["qos"], 1)
        self.assertTrue(message["delivery_id"])
        self.assertIs(message["dup"], False)

    def test_qos1_redelivered_with_dup_until_acked(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("sub", "t")
        result = self.publish(publisher, "t", 1, qos=1)
        first = self.poll(listener)["messages"][0]

        again = self.poll(listener)["messages"][0]
        self.assertEqual(again["delivery_id"], first["delivery_id"])
        self.assertEqual(again["message_id"], result["message_id"])
        self.assertIs(again["dup"], True)

        acked = self.ack(listener, [first["delivery_id"]])
        self.assertEqual(acked, {"acked_count": 1})
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_qos1_redelivery_preserves_publish_order(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("sub", "t")
        ids = [self.publish(publisher, "t", i, qos=1)["message_id"]
               for i in range(3)]
        first = self.poll(listener)["messages"]
        self.assertEqual([m["message_id"] for m in first], ids)
        self.assertTrue(all(m["dup"] is False for m in first))
        second = self.poll(listener)["messages"]
        self.assertEqual([m["message_id"] for m in second], ids)
        self.assertTrue(all(m["dup"] is True for m in second))

    def test_unacked_has_priority_and_bounds_backpressure(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("sub", "t")
        old_id = self.publish(publisher, "t", "old", qos=1)["message_id"]
        # 首次交付 old 但不确认。
        self.assertEqual(len(self.poll(listener, max_messages=1)["messages"]), 1)
        new_id = self.publish(publisher, "t", "new", qos=1)["message_id"]

        # 未确认消息优先于尚未首次交付的消息，并共同受 max_messages 限制。
        messages = self.poll(listener, max_messages=1)["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["message_id"], old_id)
        self.assertIs(messages[0]["dup"], True)

        self.ack(listener, [messages[0]["delivery_id"]])
        messages = self.poll(listener, max_messages=1)["messages"]
        self.assertEqual(messages[0]["message_id"], new_id)
        self.assertIs(messages[0]["dup"], False)

    def test_mixed_qos_pending_keep_publish_order(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("sub", "t")
        self.publish(publisher, "t", "a", qos=0)
        self.publish(publisher, "t", "b", qos=1)
        self.publish(publisher, "t", "c")
        messages = self.poll(listener)["messages"]
        self.assertEqual([m["payload"] for m in messages], ["a", "b", "c"])
        self.assertNotIn("qos", messages[0])
        self.assertEqual(messages[1]["qos"], 1)
        self.assertNotIn("qos", messages[2])

    def test_multiple_filters_still_single_qos1_delivery(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("sub", "a/#")
        self.subscribe(listener, "a/+/c")
        self.subscribe(listener, "#")
        result = self.publish(publisher, "a/b/c", None, qos=1)
        self.assertEqual(result["matched_count"], 1)
        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 1)

    def test_delivery_ids_independent_across_sessions(self) -> None:
        publisher = self.connect(client_id="pub")
        one = self.subscribe_session("s1", "t")
        two = self.subscribe_session("s2", "t")
        result = self.publish(publisher, "t", 1, qos=1)
        self.assertEqual(result["matched_count"], 2)

        first = self.poll(one)["messages"][0]
        second = self.poll(two)["messages"][0]
        self.assertNotEqual(first["delivery_id"], second["delivery_id"])
        self.assertEqual(first["message_id"], second["message_id"])

        # 确认状态互不影响。
        self.ack(one, [first["delivery_id"]])
        self.assertEqual(self.poll(one)["messages"], [])
        redelivered = self.poll(two)["messages"][0]
        self.assertEqual(redelivered["delivery_id"], second["delivery_id"])
        self.assertIs(redelivered["dup"], True)

    def test_delivery_ids_are_unique(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("sub", "t")
        for i in range(50):
            self.publish(publisher, "t", i, qos=1)
        delivery_ids = {m["delivery_id"] for m in self.poll(listener)["messages"]}
        self.assertEqual(len(delivery_ids), 50)

    # ------------------------------------------------------------------
    # 确认
    # ------------------------------------------------------------------

    def test_ack_multiple_and_idempotent_reack(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("sub", "t")
        for i in range(3):
            self.publish(publisher, "t", i, qos=1)
        ids = [m["delivery_id"] for m in self.poll(listener)["messages"]]

        self.assertEqual(self.ack(listener, ids[:2]), {"acked_count": 2})
        # 重复确认幂等成功且不增加计数。
        self.assertEqual(self.ack(listener, ids[:2]), {"acked_count": 0})
        self.assertEqual(self.ack(listener, [ids[2]]), {"acked_count": 1})
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_ack_unknown_delivery_fails_atomically(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("sub", "t")
        self.publish(publisher, "t", 1, qos=1)
        delivery_id = self.poll(listener)["messages"][0]["delivery_id"]

        with self.assertRaises(ServiceError) as ctx:
            self.ack(listener, [delivery_id, "never-issued"])
        self.assertEqual(ctx.exception.code, "delivery_not_found")
        self.assertEqual(ctx.exception.status, 404)
        # 整个请求不确认任何消息：原投递仍会重投。
        messages = self.poll(listener)["messages"]
        self.assertEqual(messages[0]["delivery_id"], delivery_id)
        self.assertIs(messages[0]["dup"], True)

    def test_ack_delivery_of_other_session_not_found(self) -> None:
        publisher = self.connect(client_id="pub")
        one = self.subscribe_session("s1", "t")
        two = self.subscribe_session("s2", "t")
        self.publish(publisher, "t", 1, qos=1)
        foreign = self.poll(one)["messages"][0]["delivery_id"]
        self.poll(two)
        with self.assertRaises(ServiceError) as ctx:
            self.ack(two, [foreign])
        self.assertEqual(ctx.exception.code, "delivery_not_found")

    def test_ack_invalid_payloads(self) -> None:
        session = self.connect()
        token = session["session_token"]
        bad_payloads = [
            "not-an-object",
            {},
            {"session_token": token},
            {"delivery_ids": ["a"]},
            {"session_token": token, "delivery_ids": ["a"], "extra": 1},
            {"session_token": 1, "delivery_ids": ["a"]},
            {"session_token": token, "delivery_ids": "a"},
            {"session_token": token, "delivery_ids": []},
            {"session_token": token, "delivery_ids": ["a"] * 101},
            {"session_token": token, "delivery_ids": ["a", "a"]},
            {"session_token": token, "delivery_ids": ["a", 1]},
            {"session_token": token, "delivery_ids": [None]},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.ack_messages(session["session_id"], payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)

    def test_ack_session_error_semantics(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.ack_messages(
                "ghost", {"session_token": "x", "delivery_ids": ["a"]}
            )
        self.assertEqual(ctx.exception.code, "session_not_found")
        self.assertEqual(ctx.exception.status, 404)

        session = self.connect()
        with self.assertRaises(ServiceError) as ctx:
            self.service.ack_messages(
                session["session_id"],
                {"session_token": "wrong", "delivery_ids": ["a"]},
            )
        self.assertEqual(ctx.exception.code, "invalid_session_token")
        self.assertEqual(ctx.exception.status, 401)

        self.connect()  # 取代旧会话
        with self.assertRaises(ServiceError) as ctx:
            self.ack(session, ["a"])
        self.assertEqual(ctx.exception.code, "session_not_online")
        self.assertEqual(ctx.exception.status, 409)

    # ------------------------------------------------------------------
    # 离线即失效
    # ------------------------------------------------------------------

    def test_timeout_discards_unacked_and_ack_history(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.subscribe_session("sub", "t")
        self.publish(publisher, "t", 1, qos=1)
        delivery_id = self.poll(listener)["messages"][0]["delivery_id"]
        self.ack(listener, [delivery_id])
        self.publish(publisher, "t", 2, qos=1)
        self.poll(listener)  # 留下一条未确认
        self.force_expire(listener)

        with self.assertRaises(ServiceError) as ctx:
            self.poll(listener)
        self.assertEqual(ctx.exception.code, "session_not_online")

        # 新会话不继承未确认与确认历史。
        reconnect = self.connect(device_id="sensor-02", client_id="sub")
        self.assertEqual(self.poll(reconnect)["messages"], [])
        with self.assertRaises(ServiceError) as ctx:
            self.ack(reconnect, [delivery_id])
        self.assertEqual(ctx.exception.code, "delivery_not_found")

    def test_replaced_session_discards_qos1_state(self) -> None:
        old = self.connect(client_id="same")
        self.subscribe(old, "t")
        self.publish(old, "t", 1, qos=1)
        delivery_id = self.poll(old)["messages"][0]["delivery_id"]
        new = self.connect(client_id="same")
        self.assertEqual(self.poll(new)["messages"], [])
        with self.assertRaises(ServiceError) as ctx:
            self.ack(new, [delivery_id])
        self.assertEqual(ctx.exception.code, "delivery_not_found")

    def test_revoked_device_drops_qos1_state(self) -> None:
        publisher = self.connect(client_id="pub")
        victim = self.subscribe_session("sub", "t")
        self.publish(publisher, "t", 1, qos=1)
        self.poll(victim)
        self.service.revoke_device("sensor-02")
        with self.assertRaises(ServiceError) as ctx:
            self.poll(victim)
        self.assertEqual(ctx.exception.code, "session_not_online")
        result = self.publish(publisher, "t", 2, qos=1)
        self.assertEqual(result["matched_count"], 0)


if __name__ == "__main__":
    unittest.main()
