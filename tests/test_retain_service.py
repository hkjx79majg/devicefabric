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
            {"session_token": session["session_token"],
             "topic_filter": topic_filter},
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
        )["messages"]

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
    # retain 字段校验
    # ------------------------------------------------------------------

    def test_retain_defaults_to_false_and_changes_nothing(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1)
        self.publish(publisher, "t", 2, retain=False)
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "t")
        self.assertEqual(self.poll(listener), [])

    def test_invalid_retain_rejected_without_side_effects(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        self.publish(publisher, "t", "keep", retain=True)
        # 订阅在发布之前，先排空这次实时投递。
        self.assertEqual(len(self.poll(listener)), 1)
        for retain in (0, 1, "true", None, 1.0, [True], {"r": 1}):
            with self.subTest(retain=retain):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.publish_message(
                        publisher["session_id"],
                        {"session_token": publisher["session_token"],
                         "topic": "t", "payload": 1, "retain": retain},
                    )
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        # 实时队列与保留状态均未改变。
        self.assertEqual(self.poll(listener), [])
        late = self.connect(device_id="sensor-02", client_id="late")
        self.subscribe(late, "t")
        messages = self.poll(late)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"], "keep")

    def test_extra_field_still_rejected(self) -> None:
        publisher = self.connect(client_id="pub")
        with self.assertRaises(ServiceError) as ctx:
            self.service.publish_message(
                publisher["session_id"],
                {"session_token": publisher["session_token"], "topic": "t",
                 "payload": 1, "retain": True, "extra": 1},
            )
        self.assertEqual(ctx.exception.code, "invalid_request")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "t")
        self.assertEqual(self.poll(listener), [])

    # ------------------------------------------------------------------
    # 保留状态的保存、覆盖与清除
    # ------------------------------------------------------------------

    def test_retain_true_saved_even_without_match(self) -> None:
        publisher = self.connect(client_id="pub")
        result = self.publish(publisher, "a/b", {"v": 1}, retain=True)
        self.assertEqual(result["matched_count"], 0)

        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "a/b")
        messages = self.poll(listener)
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message["message_id"], result["message_id"])
        self.assertEqual(message["topic"], "a/b")
        self.assertEqual(message["payload"], {"v": 1})
        self.assertEqual(message["publisher_device_id"], "sensor-01")
        self.assertIs(message["retained"], True)

    def test_retain_true_overwrites_previous_value(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", "old", retain=True)
        self.publish(publisher, "t", "new", retain=True)
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "t")
        messages = self.poll(listener)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"], "new")

    def test_retain_null_payload_delivers_live_and_clears(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", "kept", retain=True)
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "t")
        # 订阅触发保留回放。
        retained = self.poll(listener)
        self.assertEqual(len(retained), 1)
        self.assertIs(retained[0]["retained"], True)

        result = self.publish(publisher, "t", None, retain=True)
        self.assertEqual(result["matched_count"], 1)
        # 实时投递照常，不带 retained 字段。
        live = self.poll(listener)
        self.assertEqual(len(live), 1)
        self.assertIsNone(live[0]["payload"])
        self.assertNotIn("retained", live[0])
        # 保留值已删除，新订阅不再收到。
        late = self.connect(device_id="sensor-02", client_id="late")
        self.subscribe(late, "t")
        self.assertEqual(self.poll(late), [])

    def test_retain_null_payload_on_absent_topic_succeeds(self) -> None:
        publisher = self.connect(client_id="pub")
        result = self.publish(publisher, "never-retained", None, retain=True)
        self.assertEqual(result["matched_count"], 0)

    def test_retain_false_does_not_clear_existing_value(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", "kept", retain=True)
        self.publish(publisher, "t", "live-only")
        self.publish(publisher, "t", "also-live", retain=False)
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "t")
        messages = self.poll(listener)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"], "kept")

    def test_publisher_offline_or_revoked_keeps_retained(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", "kept", retain=True)
        self.service.revoke_device("sensor-01")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "t")
        messages = self.poll(listener)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"], "kept")

    # ------------------------------------------------------------------
    # 订阅回放
    # ------------------------------------------------------------------

    def test_replay_ordered_by_latest_retain_publish(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "a/1", "first", retain=True)
        self.publish(publisher, "a/2", "second", retain=True)
        # 覆盖 a/1 后，a/1 的最近一次保留发布晚于 a/2。
        self.publish(publisher, "a/1", "first-v2", retain=True)
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "a/+")
        messages = self.poll(listener)
        self.assertEqual([(m["topic"], m["payload"]) for m in messages],
                         [("a/2", "second"), ("a/1", "first-v2")])

    def test_repeated_subscribe_is_idempotent_without_replay(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, retain=True)
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.assertEqual(self.subscribe(listener, "t"), {"topic_filter": "t"})
        self.assertEqual(len(self.poll(listener)), 1)
        # 重复订阅保持幂等且不再次回放。
        self.assertEqual(self.subscribe(listener, "t"), {"topic_filter": "t"})
        self.assertEqual(self.poll(listener), [])

    def test_new_overlapping_filter_replays_independently(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "a/b", 1, retain=True)
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "a/b")
        self.assertEqual(len(self.poll(listener)), 1)
        # 新增不同过滤器即使重叠，仍回放当前快照。
        self.subscribe(listener, "a/+")
        messages = self.poll(listener)
        self.assertEqual(len(messages), 1)
        self.assertIs(messages[0]["retained"], True)

    def test_replay_uses_current_snapshot(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", "v1", retain=True)
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "t")
        self.assertEqual(self.poll(listener)[0]["payload"], "v1")
        # 订阅后保留值被覆盖，新过滤器回放的是新快照。
        self.publish(publisher, "t", "v2", retain=True)
        self.subscribe(listener, "#")
        payloads = [m["payload"] for m in self.poll(listener)]
        # 队列里先有 v2 的实时投递，再有 # 触发的回放。
        self.assertEqual(payloads, ["v2", "v2"])

    def test_failed_subscribe_does_not_replay(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, retain=True)
        listener = self.connect(device_id="sensor-02", client_id="sub")
        with self.assertRaises(ServiceError):
            self.service.subscribe_topic(
                listener["session_id"],
                {"session_token": "wrong", "topic_filter": "t"},
            )
        self.assertEqual(self.poll(listener), [])

    def test_no_replay_for_offline_or_reconnected_sessions(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, retain=True)
        listener = self.listener("t")
        self.assertEqual(len(self.poll(listener)), 1)
        self.force_expire(listener)
        with self.assertRaises(ServiceError) as ctx:
            self.subscribe(listener, "t")
        self.assertEqual(ctx.exception.code, "session_not_online")
        # 重连得到的新会话不继承旧订阅，重新订阅才回放当前快照。
        reconnect = self.connect(device_id="sensor-02", client_id="sub")
        self.assertEqual(self.poll(reconnect), [])
        self.subscribe(reconnect, "t")
        messages = self.poll(reconnect)
        self.assertEqual(len(messages), 1)
        self.assertIs(messages[0]["retained"], True)

    def test_live_delivery_never_carries_retained_flag(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("t")
        self.publish(publisher, "t", 1, retain=True)
        messages = self.poll(listener)
        self.assertEqual(len(messages), 1)
        self.assertNotIn("retained", messages[0])

    # ------------------------------------------------------------------
    # QoS 语义
    # ------------------------------------------------------------------

    def test_qos0_replay_consumed_on_poll_without_qos_fields(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, retain=True)
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "t")
        messages = self.poll(listener)
        self.assertEqual(len(messages), 1)
        self.assertEqual(
            set(messages[0]),
            {"message_id", "topic", "payload", "publisher_device_id",
             "published_at", "retained"},
        )
        self.assertEqual(self.poll(listener), [])

    def test_qos1_replay_redelivers_with_dup_then_acks(self) -> None:
        publisher = self.connect(client_id="pub")
        result = self.publish(publisher, "t", {"v": 1}, qos=1, retain=True)
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "t")

        first = self.poll(listener)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["message_id"], result["message_id"])
        self.assertEqual(first[0]["qos"], 1)
        self.assertIs(first[0]["retained"], True)
        self.assertIs(first[0]["dup"], False)
        self.assertTrue(first[0]["delivery_id"])

        # 确认前重投保持 message_id 与 delivery_id，dup 为 true。
        second = self.poll(listener)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["delivery_id"], first[0]["delivery_id"])
        self.assertIs(second[0]["dup"], True)
        self.assertIs(second[0]["retained"], True)

        self.assertEqual(self.ack(listener, [first[0]["delivery_id"]]),
                         {"acked_count": 1})
        self.assertEqual(self.poll(listener), [])

    def test_qos1_replay_delivery_ids_independent_per_session(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, qos=1, retain=True)
        one = self.listener("t", client_id="s1")
        two = self.listener("t", client_id="s2")
        id_one = self.poll(one)[0]["delivery_id"]
        id_two = self.poll(two)[0]["delivery_id"]
        self.assertNotEqual(id_one, id_two)
        # 确认状态互不影响。
        self.assertEqual(self.ack(one, [id_one]), {"acked_count": 1})
        self.assertEqual(self.poll(one), [])
        redelivered = self.poll(two)
        self.assertEqual(len(redelivered), 1)
        self.assertIs(redelivered[0]["dup"], True)

    def test_replay_backpressure_shares_max_messages_budget(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, qos=1, retain=True)
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "t")
        self.publish(publisher, "t", 2)
        # 首次拉取先交付回放的 QoS 1 消息。
        first = self.poll(listener, max_messages=1)
        self.assertEqual([m["payload"] for m in first], [1])
        # 未确认的回放消息优先于新消息重投，形成既有背压语义。
        second = self.poll(listener, max_messages=1)
        self.assertEqual([m["payload"] for m in second], [1])
        self.assertIs(second[0]["dup"], True)


if __name__ == "__main__":
    unittest.main()
