import unittest
from datetime import timedelta

from devicefabric.service import Service, ServiceError, _utc_now


class RetainServiceTest(unittest.TestCase):
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

    def publish(self, session, topic, payload, qos=None, retain=None):
        body = {"session_token": session["session_token"], "topic": topic,
                "payload": payload}
        if qos is not None:
            body["qos"] = qos
        if retain is not None:
            body["retain"] = retain
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

    def listener(self, topic_filter=None, device_id="sensor-02", client_id="sub"):
        session = self.connect(device_id=device_id, client_id=client_id)
        if topic_filter is not None:
            self.subscribe(session, topic_filter)
        return session

    def force_expire(self, session) -> None:
        record = self.service._sessions[session["session_id"]]
        record["expires_at"] = _utc_now() - timedelta(seconds=1)

    # ------------------------------------------------------------------
    # 发布：retain 校验与保存
    # ------------------------------------------------------------------

    def test_retain_defaults_to_false_and_changes_nothing(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        self.publish(publisher, "t", 1)
        self.assertEqual(self.service._retained, {})
        # 实时投递不带 retained 字段。
        message = self.poll(listener)["messages"][0]
        self.assertNotIn("retained", message)

    def test_retain_true_saves_even_without_matches(self) -> None:
        publisher = self.connect(client_id="pub")
        result = self.publish(publisher, "lonely/x", {"v": 1}, retain=True)
        self.assertEqual(result["matched_count"], 0)
        self.assertIn("lonely/x", self.service._retained)
        record = self.service._retained["lonely/x"]
        self.assertEqual(record["message"]["payload"], {"v": 1})
        self.assertEqual(record["message"]["message_id"], result["message_id"])

    def test_retain_true_overwrites_old_value_by_exact_topic(self) -> None:
        publisher = self.connect(client_id="pub")
        first = self.publish(publisher, "a/b", 1, retain=True)
        second = self.publish(publisher, "a/b", 2, retain=True)
        self.assertEqual(len(self.service._retained), 1)
        record = self.service._retained["a/b"]
        self.assertEqual(record["message"]["payload"], 2)
        self.assertEqual(record["message"]["message_id"], second["message_id"])
        self.assertNotEqual(first["message_id"], second["message_id"])

    def test_topics_are_retained_independently(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "a", 1, retain=True)
        self.publish(publisher, "a/b", 2, retain=True)
        self.assertEqual(set(self.service._retained), {"a", "a/b"})

    def test_retain_true_null_deletes_and_still_delivers(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        self.publish(publisher, "t", 1, retain=True)
        # 第一次实时投递（payload 1）先取走。
        self.assertEqual(self.poll(listener)["messages"][0]["payload"], 1)
        result = self.publish(publisher, "t", None, retain=True)
        # 实时仍投递给订阅者，payload 为 null。
        self.assertEqual(result["matched_count"], 1)
        message = self.poll(listener)["messages"][0]
        self.assertIsNone(message["payload"])
        self.assertNotIn("retained", message)
        # 保留值被删除。
        self.assertNotIn("t", self.service._retained)

    def test_retain_true_clear_missing_topic_succeeds(self) -> None:
        publisher = self.connect(client_id="pub")
        result = self.publish(publisher, "never", None, retain=True)
        self.assertEqual(result["matched_count"], 0)
        self.assertEqual(self.service._retained, {})

    def test_retain_false_does_not_change_retained_state(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, retain=True)
        self.publish(publisher, "t", 2, retain=False)
        self.publish(publisher, "t", None, retain=False)
        self.assertEqual(self.service._retained["t"]["message"]["payload"], 1)

    def test_explicit_false_null_does_not_delete(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, retain=True)
        self.publish(publisher, "t", None, retain=False)
        self.assertIn("t", self.service._retained)

    def test_invalid_retain_rejected_without_effect(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        self.publish(publisher, "t", "kept", retain=True)
        # 合法发布的实时投递先取走。
        self.assertEqual(self.poll(listener)["messages"][0]["payload"], "kept")
        for value in (1, 0, "true", None, [True], {"retain": True}):
            with self.subTest(value=value):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.publish_message(
                        publisher["session_id"],
                        {"session_token": publisher["session_token"], "topic": "t",
                         "payload": "x", "retain": value},
                    )
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        # 保留状态与实时队列均不变。
        self.assertEqual(self.service._retained["t"]["message"]["payload"], "kept")
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_unknown_retain_field_is_invalid_request(self) -> None:
        publisher = self.connect(client_id="pub")
        with self.assertRaises(ServiceError) as ctx:
            self.service.publish_message(
                publisher["session_id"],
                {"session_token": publisher["session_token"], "topic": "t",
                 "payload": 1, "retained": True},
            )
        self.assertEqual(ctx.exception.code, "invalid_request")

    # ------------------------------------------------------------------
    # 订阅回放
    # ------------------------------------------------------------------

    def test_new_subscription_replays_matching_retained(self) -> None:
        publisher = self.connect(client_id="pub")
        result = self.publish(publisher, "house/r1/temp", 21, retain=True)
        listener = self.listener("house/+/temp")
        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message["message_id"], result["message_id"])
        self.assertEqual(message["topic"], "house/r1/temp")
        self.assertEqual(message["payload"], 21)
        self.assertEqual(message["publisher_device_id"], "sensor-01")
        self.assertIn("published_at", message)
        self.assertIs(message["retained"], True)
        # QoS 0 回放不带 qos，拉取后移除。
        self.assertNotIn("qos", message)
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_replay_orders_by_latest_retained_publish_per_topic(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "a", 1, retain=True)
        self.publish(publisher, "b", 2, retain=True)
        # 重新发布 a：a 的最近保留时间晚于 b。
        self.publish(publisher, "a", 3, retain=True)
        listener = self.listener("#")
        messages = self.poll(listener)["messages"]
        self.assertEqual([(m["topic"], m["payload"]) for m in messages],
                         [("b", 2), ("a", 3)])
        self.assertTrue(all(m["retained"] is True for m in messages))

    def test_replay_one_copy_per_exact_topic_even_with_wildcards(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "x/y", 1, retain=True)
        # 同一过滤器只匹配该 topic 一次。
        listener = self.listener("#")
        self.assertEqual(len(self.poll(listener)["messages"]), 1)

    def test_duplicate_subscription_does_not_replay_again(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, retain=True)
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "t")
        self.assertEqual(len(self.poll(listener)["messages"]), 1)
        # 重复订阅幂等，不再次回放。
        self.subscribe(listener, "t")
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_overlapping_new_filters_replay_independently(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "a/b", 1, retain=True)
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "a/#")
        self.subscribe(listener, "#")
        self.subscribe(listener, "a/b")
        # 每个新过滤器独立回放当前快照，共三份。
        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 3)
        self.assertTrue(all(m["retained"] is True for m in messages))
        # 订阅响应保持不变。
        self.assertEqual(self.subscribe(listener, "a/#"), {"topic_filter": "a/#"})

    def test_replay_snapshot_filters_by_topic(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "a/1", "match", retain=True)
        self.publish(publisher, "b/1", "other", retain=True)
        listener = self.listener("a/#")
        messages = self.poll(listener)["messages"]
        self.assertEqual([m["payload"] for m in messages], ["match"])

    def test_replay_sees_latest_snapshot_at_subscribe_time(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, retain=True)
        self.publish(publisher, "t", 2, retain=True)
        self.publish(publisher, "t", None, retain=True)
        # 订阅时无保留值可回放。
        listener = self.listener("t")
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_realtime_delivery_does_not_carry_retained(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        self.publish(publisher, "t", 1, retain=True)
        # 订阅者已在线：实时投递不带 retained；保留值也已保存，
        # 但不会二次注入该已存在的订阅。
        message = self.poll(listener)["messages"][0]
        self.assertNotIn("retained", message)
        self.assertEqual(self.service._retained["t"]["message"]["payload"], 1)

    # ------------------------------------------------------------------
    # QoS 1 回放
    # ------------------------------------------------------------------

    def test_qos1_replay_fields_and_redelivery(self) -> None:
        publisher = self.connect(client_id="pub")
        result = self.publish(publisher, "t", {"v": 1}, qos=1, retain=True)
        listener = self.listener("t")
        first = self.poll(listener)["messages"]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["message_id"], result["message_id"])
        self.assertEqual(first[0]["qos"], 1)
        self.assertTrue(first[0]["delivery_id"])
        self.assertIs(first[0]["dup"], False)
        self.assertIs(first[0]["retained"], True)

        # 确认前重投：dup=true，delivery_id 不变，retained 保留。
        second = self.poll(listener)["messages"]
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["delivery_id"], first[0]["delivery_id"])
        self.assertIs(second[0]["dup"], True)
        self.assertIs(second[0]["retained"], True)

        # 确认后不再重投。
        self.assertEqual(self.ack(listener, [first[0]["delivery_id"]]),
                         {"acked_count": 1})
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_qos1_replay_delivery_id_is_per_session(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, qos=1, retain=True)
        one = self.listener("t", client_id="s1")
        two = self.listener("t", client_id="s2")
        id_one = self.poll(one)["messages"][0]["delivery_id"]
        id_two = self.poll(two)["messages"][0]["delivery_id"]
        self.assertNotEqual(id_one, id_two)

    def test_overlapping_qos1_replays_get_distinct_deliveries(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "a/b", 1, qos=1, retain=True)
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "a/#")
        self.subscribe(listener, "#")
        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 2)
        self.assertNotEqual(messages[0]["delivery_id"], messages[1]["delivery_id"])
        # 两份都需独立确认。
        self.assertEqual(
            self.ack(listener, [m["delivery_id"] for m in messages]),
            {"acked_count": 2},
        )
        self.assertEqual(self.poll(listener)["messages"], [])

    # ------------------------------------------------------------------
    # 生命周期：保留消息不随发布者离线/吊销删除，回放只给在线会话
    # ------------------------------------------------------------------

    def test_publisher_replaced_does_not_delete_retained(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, retain=True)
        self.connect(client_id="pub")  # 发布会话被取代
        self.assertIn("t", self.service._retained)
        # 新订阅者仍可回放。
        listener = self.listener("t")
        self.assertEqual(len(self.poll(listener)["messages"]), 1)

    def test_publisher_revoked_does_not_delete_retained(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, retain=True)
        self.service.revoke_device("sensor-01")
        self.assertIn("t", self.service._retained)
        listener = self.listener("t")
        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["publisher_device_id"], "sensor-01")

    def test_subscriber_reconnect_does_not_inherit_replay(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, retain=True)
        old = self.listener("t", client_id="same")
        self.assertEqual(len(self.poll(old)["messages"]), 1)
        # 重连替换：旧回放不继承；新会话重新订阅时仍按当前快照回放。
        new = self.connect(device_id="sensor-02", client_id="same")
        self.assertEqual(self.poll(new)["messages"], [])
        self.subscribe(new, "t")
        self.assertEqual(len(self.poll(new)["messages"]), 1)

    def test_failed_subscribe_does_not_replay(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, retain=True)
        session = self.connect(device_id="sensor-02")
        with self.assertRaises(ServiceError):
            self.subscribe(session, "t/")
        self.assertEqual(self.poll(session)["messages"], [])

    def test_replay_backpressure_interleaves_with_unacked(self) -> None:
        publisher = self.connect(client_id="pub")
        # 先订阅 live：收到一条 QoS 1 实时消息但不确认。
        listener = self.listener("live")
        self.publish(publisher, "live", "live", qos=1)
        first = self.poll(listener, max_messages=1)["messages"]
        self.assertEqual(first[0]["payload"], "live")
        # 无订阅者时发布保留消息：只保存不入任何实时队列。
        kept = self.publish(publisher, "r", "kept", qos=1, retain=True)
        self.assertEqual(kept["matched_count"], 0)
        # 新过滤器触发回放，回放消息进入队列；未确认消息始终优先重投。
        self.subscribe(listener, "#")
        batch = self.poll(listener, max_messages=1)["messages"]
        self.assertEqual(batch[0]["payload"], "live")
        self.assertIs(batch[0]["dup"], True)
        # 确认实时消息后，回放消息按首次投递交付。
        self.ack(listener, [first[0]["delivery_id"]])
        batch = self.poll(listener, max_messages=1)["messages"]
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["message_id"], kept["message_id"])
        self.assertEqual(batch[0]["payload"], "kept")
        self.assertIs(batch[0]["dup"], False)
        self.assertIs(batch[0]["retained"], True)
        self.assertEqual(batch[0]["qos"], 1)


if __name__ == "__main__":
    unittest.main()
