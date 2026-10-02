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
            self.device["credential"] if device_id == "sensor-01"
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

    def listener(self, topic_filter, client_id="sub"):
        session = self.connect(device_id="sensor-02", client_id=client_id)
        self.subscribe(session, topic_filter)
        return session

    def force_expire(self, session) -> None:
        record = self.service._sessions[session["session_id"]]
        record["expires_at"] = _utc_now() - timedelta(seconds=1)

    # ------------------------------------------------------------------
    # QoS 0：默认与显式语义不变
    # ------------------------------------------------------------------

    def test_default_publish_is_qos0_and_consumed_on_poll(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        result = self.publish(publisher, "t", 1)
        self.assertEqual(result["matched_count"], 1)
        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 1)
        # QoS 0 消息保持原有字段，不带 qos/delivery_id/dup。
        self.assertEqual(
            set(messages[0]),
            {"message_id", "topic", "payload", "publisher_device_id",
             "published_at"},
        )
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_explicit_qos0_behaves_like_default(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        self.publish(publisher, "t", 1, qos=0)
        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 1)
        self.assertNotIn("delivery_id", messages[0])
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_invalid_qos_rejected_without_delivery(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        for qos in (2, -1, "1", 1.0, True, False, None, [1], {"q": 1}):
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
    # QoS 1：首次投递与重投
    # ------------------------------------------------------------------

    def test_qos1_first_delivery_then_redelivery_with_dup(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        result = self.publish(publisher, "t", {"v": 1}, qos=1)
        self.assertEqual(result["matched_count"], 1)
        self.assertTrue(result["message_id"])

        first = self.poll(listener)["messages"]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["message_id"], result["message_id"])
        self.assertEqual(first[0]["topic"], "t")
        self.assertEqual(first[0]["payload"], {"v": 1})
        self.assertEqual(first[0]["publisher_device_id"], "sensor-01")
        self.assertEqual(first[0]["qos"], 1)
        self.assertTrue(first[0]["delivery_id"])
        self.assertIs(first[0]["dup"], False)

        # 确认前再次拉取：同一 message_id 与 delivery_id 重投，dup 为 true。
        second = self.poll(listener)["messages"]
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["message_id"], first[0]["message_id"])
        self.assertEqual(second[0]["delivery_id"], first[0]["delivery_id"])
        self.assertIs(second[0]["dup"], True)

    def test_qos1_delivery_ids_unique_and_unpredictable_per_session(self) -> None:
        publisher = self.connect(client_id="pub")
        one = self.listener("t", client_id="s1")
        two = self.listener("t", client_id="s2")
        result = self.publish(publisher, "t", 1, qos=1)
        self.assertEqual(result["matched_count"], 2)
        id_one = self.poll(one)["messages"][0]["delivery_id"]
        id_two = self.poll(two)["messages"][0]["delivery_id"]
        self.assertNotEqual(id_one, id_two)

    def test_multiple_filters_still_single_delivery(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("a/#")
        self.subscribe(listener, "a/b")
        self.subscribe(listener, "#")
        result = self.publish(publisher, "a/b", 1, qos=1)
        self.assertEqual(result["matched_count"], 1)
        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["qos"], 1)

    def test_unacked_redelivered_in_publish_order_before_new(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        ids = [self.publish(publisher, "t", i, qos=1)["message_id"]
               for i in range(3)]
        first = self.poll(listener, max_messages=2)["messages"]
        self.assertEqual([m["message_id"] for m in first], ids[:2])
        # 未确认消息优先于尚未首次交付的第三条消息。
        second = self.poll(listener, max_messages=2)["messages"]
        self.assertEqual([m["message_id"] for m in second], ids[:2])
        self.assertTrue(all(m["dup"] for m in second))
        # 确认最早一条后，下一条未确认消息仍优先，剩余额度交付新消息。
        self.assertEqual(self.ack(listener, [first[0]["delivery_id"]]),
                         {"acked_count": 1})
        third = self.poll(listener, max_messages=2)["messages"]
        self.assertEqual([m["message_id"] for m in third], ids[1:])
        self.assertIs(third[0]["dup"], True)
        self.assertIs(third[1]["dup"], False)

    def test_ack_removes_unacked_and_repeated_ack_is_idempotent(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        self.publish(publisher, "t", 1, qos=1)
        delivery_id = self.poll(listener)["messages"][0]["delivery_id"]

        self.assertEqual(self.ack(listener, [delivery_id]), {"acked_count": 1})
        # 确认后不再重投。
        self.assertEqual(self.poll(listener)["messages"], [])
        # 重复确认幂等成功且不增加计数。
        self.assertEqual(self.ack(listener, [delivery_id]), {"acked_count": 0})

    def test_ack_unknown_delivery_aborts_entire_request(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        self.publish(publisher, "t", 1, qos=1)
        delivery_id = self.poll(listener)["messages"][0]["delivery_id"]

        with self.assertRaises(ServiceError) as ctx:
            self.ack(listener, [delivery_id, "never-existed"])
        self.assertEqual(ctx.exception.code, "delivery_not_found")
        self.assertEqual(ctx.exception.status, 404)
        # 整体不确认：原消息仍按未确认重投。
        redelivered = self.poll(listener)["messages"]
        self.assertEqual(len(redelivered), 1)
        self.assertIs(redelivered[0]["dup"], True)

    def test_ack_state_is_per_session(self) -> None:
        publisher = self.connect(client_id="pub")
        one = self.listener("t", client_id="s1")
        two = self.listener("t", client_id="s2")
        self.publish(publisher, "t", 1, qos=1)
        id_one = self.poll(one)["messages"][0]["delivery_id"]
        id_two = self.poll(two)["messages"][0]["delivery_id"]

        self.assertEqual(self.ack(one, [id_one]), {"acked_count": 1})
        # 另一会话的未确认状态互不影响。
        redelivered = self.poll(two)["messages"]
        self.assertEqual(len(redelivered), 1)
        self.assertEqual(redelivered[0]["delivery_id"], id_two)
        self.assertIs(redelivered[0]["dup"], True)
        # 跨会话确认他人标识按从未属于处理。
        with self.assertRaises(ServiceError) as ctx:
            self.ack(one, [id_two])
        self.assertEqual(ctx.exception.code, "delivery_not_found")

    def test_ack_invalid_payloads(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        self.publish(publisher, "t", 1, qos=1)
        delivery_id = self.poll(listener)["messages"][0]["delivery_id"]
        token = listener["session_token"]
        bad_payloads = [
            "not-an-object",
            {},
            {"session_token": token},
            {"delivery_ids": [delivery_id]},
            {"session_token": token, "delivery_ids": [delivery_id], "extra": 1},
            {"session_token": 1, "delivery_ids": [delivery_id]},
            {"session_token": token, "delivery_ids": "x"},
            {"session_token": token, "delivery_ids": []},
            {"session_token": token, "delivery_ids": [str(i) for i in range(101)]},
            {"session_token": token, "delivery_ids": [1]},
            {"session_token": token, "delivery_ids": [None]},
            {"session_token": token,
             "delivery_ids": [delivery_id, delivery_id]},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.ack_messages(listener["session_id"], payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
        # 非法请求不确认任何消息。
        self.assertEqual(len(self.poll(listener)["messages"]), 1)

    def test_ack_session_error_semantics(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        self.publish(publisher, "t", 1, qos=1)
        delivery_id = self.poll(listener)["messages"][0]["delivery_id"]

        # 未知会话 404。
        with self.assertRaises(ServiceError) as ctx:
            self.service.ack_messages(
                "ghost", {"session_token": "x", "delivery_ids": [delivery_id]}
            )
        self.assertEqual(ctx.exception.code, "session_not_found")
        # 令牌错误 401。
        with self.assertRaises(ServiceError) as ctx:
            self.service.ack_messages(
                listener["session_id"],
                {"session_token": "wrong", "delivery_ids": [delivery_id]},
            )
        self.assertEqual(ctx.exception.code, "invalid_session_token")
        # 非在线会话 409。
        self.force_expire(listener)
        with self.assertRaises(ServiceError) as ctx:
            self.ack(listener, [delivery_id])
        self.assertEqual(ctx.exception.code, "session_not_online")

    # ------------------------------------------------------------------
    # 会话生命周期清理
    # ------------------------------------------------------------------

    def test_expired_session_drops_unacked_and_ack_history(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        self.publish(publisher, "t", 1, qos=1)
        delivery_id = self.poll(listener)["messages"][0]["delivery_id"]
        self.ack(listener, [delivery_id])
        self.publish(publisher, "t", 2, qos=1)
        pending = self.poll(listener)["messages"][0]["delivery_id"]
        self.force_expire(listener)
        # 超时判定在下一次会话操作时触发，届时路由状态一并清除。
        with self.assertRaises(ServiceError):
            self.poll(listener)

        record = self.service._sessions[listener["session_id"]]
        self.assertEqual(record["unacked"], {})
        self.assertEqual(record["acked"], set())
        self.assertEqual(len(record["queue"]), 0)

        # 新会话不继承确认历史与未确认消息。
        reconnect = self.connect(device_id="sensor-02", client_id="sub")
        with self.assertRaises(ServiceError) as ctx:
            self.ack(reconnect, [delivery_id])
        self.assertEqual(ctx.exception.code, "delivery_not_found")
        with self.assertRaises(ServiceError) as ctx:
            self.ack(reconnect, [pending])
        self.assertEqual(ctx.exception.code, "delivery_not_found")
        self.assertEqual(self.poll(reconnect)["messages"], [])

    def test_replaced_session_drops_delivery_state(self) -> None:
        old = self.connect(client_id="same")
        self.subscribe(old, "t")
        self.publish(old, "t", 1, qos=1)
        delivery_id = self.poll(old)["messages"][0]["delivery_id"]
        new = self.connect(client_id="same")
        record = self.service._sessions[new["session_id"]]
        self.assertEqual(record["unacked"], {})
        self.assertEqual(record["acked"], set())
        with self.assertRaises(ServiceError) as ctx:
            self.ack(new, [delivery_id])
        self.assertEqual(ctx.exception.code, "delivery_not_found")

    def test_revoked_device_drops_delivery_state(self) -> None:
        publisher = self.connect(client_id="pub")
        victim = self.listener("t")
        self.publish(publisher, "t", 1, qos=1)
        delivery_id = self.poll(victim)["messages"][0]["delivery_id"]
        self.service.revoke_device("sensor-02")
        record = self.service._sessions[victim["session_id"]]
        self.assertEqual(record["unacked"], {})
        self.assertEqual(record["acked"], set())
        with self.assertRaises(ServiceError) as ctx:
            self.ack(victim, [delivery_id])
        self.assertEqual(ctx.exception.code, "session_not_online")

    def test_mixed_qos_queue_preserves_publish_order(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        self.publish(publisher, "t", "a", qos=1)
        self.publish(publisher, "t", "b")
        self.publish(publisher, "t", "c", qos=1)
        messages = self.poll(listener)["messages"]
        self.assertEqual([m["payload"] for m in messages], ["a", "b", "c"])
        self.assertEqual(messages[0]["qos"], 1)
        self.assertNotIn("qos", messages[1])
        self.assertEqual(messages[2]["qos"], 1)
        # QoS 0 已消费；仅两条 QoS 1 重投。
        redelivered = self.poll(listener)["messages"]
        self.assertEqual([m["payload"] for m in redelivered], ["a", "c"])
        self.assertTrue(all(m["dup"] for m in redelivered))


if __name__ == "__main__":
    unittest.main()
