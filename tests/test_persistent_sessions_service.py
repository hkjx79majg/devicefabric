import unittest
from datetime import timedelta

from devicefabric.service import Service, ServiceError, _utc_now


_UNSET = object()


class PersistentSessionServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.device = self.service.register_device(
            {"device_id": "sensor-01", "display_name": "一号传感器"}
        )
        self.other = self.service.register_device(
            {"device_id": "sensor-02", "display_name": "二号传感器"}
        )

    def connect(self, device_id="sensor-01", client_id="cli-1", keepalive=30,
                clean_start=_UNSET, credential=None):
        cred = credential
        if cred is None:
            cred = (
                self.device["credential"] if device_id == "sensor-01"
                else self.other["credential"]
            )
        body = {
            "device_id": device_id,
            "credential": cred,
            "client_id": client_id,
            "keepalive_seconds": keepalive,
        }
        if clean_start is not _UNSET:
            body["clean_start"] = clean_start
        return self.service.create_session(body)

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

    def force_expire(self, session) -> None:
        record = self.service._sessions[session["session_id"]]
        record["expires_at"] = _utc_now() - timedelta(seconds=1)

    def persistent_state(self, device_id="sensor-01", client_id="cli-1"):
        return self.service._persistent_sessions[(device_id, client_id)]

    # ------------------------------------------------------------------
    # 建连与重连
    # ------------------------------------------------------------------

    def test_first_persistent_connect_starts_empty(self) -> None:
        session = self.connect(clean_start=False)
        self.assertTrue(session["online"])
        state = self.persistent_state()
        self.assertEqual(state["subscriptions"], set())
        self.assertEqual(len(state["queue"]), 0)
        self.assertEqual(state["unacked"], {})
        self.assertEqual(state["acked"], set())
        self.assertEqual(self.poll(session)["messages"], [])

    def test_subscriptions_survive_reconnect_without_retained_replay(self) -> None:
        first = self.connect(clean_start=False)
        self.subscribe(first, "t")
        sid = first["session_id"]

        self.force_expire(first)
        second = self.connect(clean_start=False)
        self.assertNotEqual(first["session_id"], second["session_id"])
        # 旧会话已离线，session_id/session_token 失效。
        self.assertFalse(self.service.get_session(sid)["online"])
        with self.assertRaises(ServiceError) as ctx:
            self.poll(first)
        self.assertEqual(ctx.exception.code, "session_not_online")
        # 原订阅立即生效：直接发布即可收到。
        publisher = self.connect(client_id="pub", clean_start=True)
        self.publish(publisher, "t", 1, qos=1)
        messages = self.poll(second)["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.persistent_state()["subscriptions"], {"t"})

    def test_reconnect_false_replaces_online_session_and_keeps_state(self) -> None:
        first = self.connect(clean_start=False)
        self.subscribe(first, "t")
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, qos=1)
        first_delivery = self.poll(first)["messages"][0]["delivery_id"]

        second = self.connect(clean_start=False)
        old = self.service.get_session(first["session_id"])
        self.assertEqual(old["state"], "closed")
        self.assertEqual(old["reason"], "replaced")
        self.assertTrue(self.service.get_session(second["session_id"])["online"])
        # 旧 token 已失效。
        with self.assertRaises(ServiceError) as ctx:
            self.poll(first)
        self.assertEqual(ctx.exception.code, "session_not_online")
        # 未确认消息以相同 delivery_id、dup=true 继续投递。
        messages = self.poll(second)["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["delivery_id"], first_delivery)
        self.assertIs(messages[0]["dup"], True)

    def test_clean_start_true_wipes_persistent_state(self) -> None:
        first = self.connect(clean_start=False)
        self.subscribe(first, "t")
        publisher = self.connect(client_id="pub")
        self.force_expire(first)
        self.publish(publisher, "t", 1, qos=1)
        self.assertIn(("sensor-01", "cli-1"), self.service._persistent_sessions)

        fresh = self.connect(clean_start=True)
        record = self.service._sessions[fresh["session_id"]]
        self.assertEqual(record["subscriptions"], set())
        self.assertEqual(self.poll(fresh)["messages"], [])
        self.assertNotIn(("sensor-01", "cli-1"), self.service._persistent_sessions)

        # 此后离线不再保存任何消息。
        self.force_expire(fresh)
        self.publish(publisher, "t", 2, qos=1)
        self.assertNotIn(("sensor-01", "cli-1"), self.service._persistent_sessions)

    def test_default_and_missing_clean_start_behave_as_temporary(self) -> None:
        first = self.connect()  # 未携带 clean_start
        self.subscribe(first, "t")
        self.force_expire(first)
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, qos=1)
        second = self.connect()
        self.assertEqual(self.poll(second)["messages"], [])
        self.assertEqual(
            self.service._sessions[second["session_id"]]["subscriptions"], set()
        )

    def test_invalid_clean_start_is_400_without_side_effects(self) -> None:
        first = self.connect(clean_start=False)
        self.subscribe(first, "t")
        publisher = self.connect(client_id="pub")
        self.force_expire(first)
        self.publish(publisher, "t", 1, qos=1)
        queued_before = len(self.persistent_state()["queue"])
        self.assertEqual(queued_before, 1)

        # 重新让同组合上线，后续非法请求不得关闭它。
        online = self.connect(clean_start=False)
        for bad in ("false", "true", 1, 0, None, [], {}, 1.0):
            with self.subTest(bad=bad):
                with self.assertRaises(ServiceError) as ctx:
                    self.connect(clean_start=bad)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        # 旧在线会话仍在线且可用。
        self.assertTrue(self.service.get_session(online["session_id"])["online"])
        heartbeat = self.service.heartbeat_session(
            online["session_id"], {"session_token": online["session_token"]}
        )
        self.assertTrue(heartbeat["online"])
        # 持久状态未被改变：订阅仍在、离线队列仍是那一条。
        self.assertEqual(self.persistent_state()["subscriptions"], {"t"})
        self.assertEqual(len(self.persistent_state()["queue"]), queued_before)
        self.assertEqual(len(self.service._sessions), 3)

    # ------------------------------------------------------------------
    # 离线队列
    # ------------------------------------------------------------------

    def test_offline_qos1_queued_and_delivered_after_reconnect(self) -> None:
        subscriber = self.connect(device_id="sensor-02", client_id="sub",
                                  clean_start=False)
        self.subscribe(subscriber, "t")
        self.force_expire(subscriber)

        publisher = self.connect(client_id="pub")
        result = self.publish(publisher, "t", {"v": 1}, qos=1)
        # 离线保存不计入 matched_count。
        self.assertEqual(result["matched_count"], 0)

        reconnected = self.connect(device_id="sensor-02", client_id="sub",
                                   clean_start=False)
        messages = self.poll(reconnected)["messages"]
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message["message_id"], result["message_id"])
        self.assertEqual(message["topic"], "t")
        self.assertEqual(message["payload"], {"v": 1})
        self.assertEqual(message["publisher_device_id"], "sensor-01")
        self.assertTrue(message["published_at"])
        self.assertEqual(message["qos"], 1)
        self.assertIs(message["dup"], False)
        self.assertTrue(message["delivery_id"])
        self.assertNotIn("retained", message)
        # 从未拉取的消息首次 dup 为 false；再次拉取以相同 delivery_id、
        # dup 为 true 优先重投。
        redelivered = self.poll(reconnected)["messages"]
        self.assertEqual(len(redelivered), 1)
        self.assertEqual(redelivered[0]["delivery_id"], message["delivery_id"])
        self.assertIs(redelivered[0]["dup"], True)

    def test_offline_qos0_not_saved(self) -> None:
        subscriber = self.connect(device_id="sensor-02", client_id="sub",
                                  clean_start=False)
        self.subscribe(subscriber, "t")
        self.force_expire(subscriber)

        publisher = self.connect(client_id="pub")
        result = self.publish(publisher, "t", 1)
        self.assertEqual(result["matched_count"], 0)
        state = self.persistent_state(device_id="sensor-02", client_id="sub")
        self.assertEqual(len(state["queue"]), 0)

        reconnected = self.connect(device_id="sensor-02", client_id="sub",
                                   clean_start=False)
        self.assertEqual(self.poll(reconnected)["messages"], [])

    def test_offline_queue_deduplicates_across_filters(self) -> None:
        subscriber = self.connect(device_id="sensor-02", client_id="sub",
                                  clean_start=False)
        self.subscribe(subscriber, "a/#")
        self.subscribe(subscriber, "a/b")
        self.subscribe(subscriber, "#")
        self.force_expire(subscriber)

        publisher = self.connect(client_id="pub")
        result = self.publish(publisher, "a/b", 1, qos=1)
        self.assertEqual(result["matched_count"], 0)

        reconnected = self.connect(device_id="sensor-02", client_id="sub",
                                   clean_start=False)
        messages = self.poll(reconnected)["messages"]
        self.assertEqual(len(messages), 1)

    def test_offline_queue_preserves_publish_order(self) -> None:
        subscriber = self.connect(device_id="sensor-02", client_id="sub",
                                  clean_start=False)
        self.subscribe(subscriber, "t")
        self.force_expire(subscriber)

        publisher = self.connect(client_id="pub")
        ids = [self.publish(publisher, "t", i, qos=1)["message_id"]
               for i in range(3)]
        reconnected = self.connect(device_id="sensor-02", client_id="sub",
                                   clean_start=False)
        messages = self.poll(reconnected)["messages"]
        self.assertEqual([m["message_id"] for m in messages], ids)
        self.assertTrue(all(m["dup"] is False for m in messages))
        delivery_ids = [m["delivery_id"] for m in messages]
        self.assertEqual(len(set(delivery_ids)), 3)

    def test_unacked_offline_gets_same_delivery_id_dup_true_first(self) -> None:
        subscriber = self.connect(device_id="sensor-02", client_id="sub",
                                  clean_start=False)
        self.subscribe(subscriber, "t")
        publisher = self.connect(client_id="pub")
        first_id = self.publish(publisher, "t", "old", qos=1)["message_id"]
        delivered = self.poll(subscriber)["messages"]
        self.assertEqual(len(delivered), 1)
        old_delivery = delivered[0]["delivery_id"]

        # 已拉取未确认即离线，离线期间又到达一条新消息。
        self.force_expire(subscriber)
        second_id = self.publish(publisher, "t", "new", qos=1)["message_id"]

        reconnected = self.connect(device_id="sensor-02", client_id="sub",
                                   clean_start=False)
        messages = self.poll(reconnected, max_messages=2)["messages"]
        self.assertEqual([m["message_id"] for m in messages],
                         [first_id, second_id])
        self.assertEqual(messages[0]["delivery_id"], old_delivery)
        self.assertIs(messages[0]["dup"], True)
        self.assertIs(messages[1]["dup"], False)

    def test_ack_history_survives_reconnect(self) -> None:
        subscriber = self.connect(device_id="sensor-02", client_id="sub",
                                  clean_start=False)
        self.subscribe(subscriber, "t")
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, qos=1)
        delivery_id = self.poll(subscriber)["messages"][0]["delivery_id"]
        self.assertEqual(self.ack(subscriber, [delivery_id]), {"acked_count": 1})

        self.force_expire(subscriber)
        reconnected = self.connect(device_id="sensor-02", client_id="sub",
                                   clean_start=False)
        # 重复确认历史标识幂等成功且不增加计数；跨会话未知标识仍 404。
        self.assertEqual(self.ack(reconnected, [delivery_id]), {"acked_count": 0})
        with self.assertRaises(ServiceError) as ctx:
            self.ack(reconnected, ["never-existed"])
        self.assertEqual(ctx.exception.code, "delivery_not_found")

    def test_persistent_states_are_independent_per_combo(self) -> None:
        a = self.connect(device_id="sensor-02", client_id="ca",
                         clean_start=False)
        b = self.connect(device_id="sensor-02", client_id="cb",
                         clean_start=False)
        c = self.connect(device_id="sensor-01", client_id="ca",
                         clean_start=False)
        self.subscribe(a, "t")
        self.force_expire(a)
        self.force_expire(b)
        self.force_expire(c)

        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, qos=1)

        state_a = self.persistent_state("sensor-02", "ca")
        state_b = self.persistent_state("sensor-02", "cb")
        state_c = self.persistent_state("sensor-01", "ca")
        self.assertEqual(len(state_a["queue"]), 1)
        self.assertEqual(len(state_b["queue"]), 0)
        self.assertEqual(len(state_c["queue"]), 0)

    def test_rules_enqueue_qos1_offline_but_not_qos0(self) -> None:
        subscriber = self.connect(device_id="sensor-02", client_id="sub",
                                  clean_start=False)
        self.subscribe(subscriber, "alerts/high")
        self.force_expire(subscriber)

        self.service.create_rule({
            "rule_id": "r1",
            "topic_filter": "src",
            "enabled": True,
            "condition": {"path": ["v"], "operator": "eq", "value": 1},
            "action": {"topic": "alerts/high", "payload": "hit", "qos": 1},
        })
        self.service.create_rule({
            "rule_id": "r2",
            "topic_filter": "src",
            "enabled": True,
            "condition": {"path": ["v"], "operator": "eq", "value": 2},
            "action": {"topic": "alerts/high", "payload": "hit0", "qos": 0},
        })
        publisher = self.connect(client_id="pub")
        # 动作消息不计入 matched_count；离线持久会话只收到 QoS 1 动作。
        self.assertEqual(self.publish(publisher, "src", {"v": 1})["matched_count"], 0)
        self.assertEqual(self.publish(publisher, "src", {"v": 2})["matched_count"], 0)

        reconnected = self.connect(device_id="sensor-02", client_id="sub",
                                   clean_start=False)
        messages = self.poll(reconnected)["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"], "hit")
        self.assertEqual(messages[0]["qos"], 1)

    # ------------------------------------------------------------------
    # 保留消息：重连不回放，在线新订阅仍回放
    # ------------------------------------------------------------------

    def test_reconnect_does_not_replay_retained_snapshot(self) -> None:
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "r", "kept", qos=1, retain=True)

        subscriber = self.connect(device_id="sensor-02", client_id="sub",
                                  clean_start=False)
        self.subscribe(subscriber, "r")
        messages = self.poll(subscriber)["messages"]
        self.assertEqual(len(messages), 1)
        self.ack(subscriber, [messages[0]["delivery_id"]])

        self.force_expire(subscriber)
        reconnected = self.connect(device_id="sensor-02", client_id="sub",
                                   clean_start=False)
        # 原订阅立即生效但不触发保留消息回放。
        self.assertEqual(self.poll(reconnected)["messages"], [])
        # 在线期间首次新增过滤器仍回放当时快照。
        self.subscribe(reconnected, "#")
        replay = self.poll(reconnected)["messages"]
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0]["topic"], "r")
        self.assertIs(replay[0]["retained"], True)
        self.assertIs(replay[0]["dup"], False)

    # ------------------------------------------------------------------
    # 吊销与轮换
    # ------------------------------------------------------------------

    def test_revoke_clears_all_persistent_sessions_for_device(self) -> None:
        one = self.connect(device_id="sensor-02", client_id="c1",
                           clean_start=False)
        two = self.connect(device_id="sensor-02", client_id="c2",
                           clean_start=False)
        keep = self.connect(device_id="sensor-01", client_id="c1",
                            clean_start=False)
        self.subscribe(one, "t")
        self.subscribe(two, "t")
        self.subscribe(keep, "t")
        self.force_expire(one)
        self.force_expire(two)
        self.force_expire(keep)
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "t", 1, qos=1)

        self.service.revoke_device("sensor-02")
        self.assertNotIn(("sensor-02", "c1"), self.service._persistent_sessions)
        self.assertNotIn(("sensor-02", "c2"), self.service._persistent_sessions)
        # 其他设备的持久状态不受影响。
        self.assertIn(("sensor-01", "c1"), self.service._persistent_sessions)

    def test_credential_rotation_keeps_persistent_state(self) -> None:
        publisher = self.connect(device_id="sensor-02", client_id="pub")
        first = self.connect(clean_start=False)
        self.subscribe(first, "t")
        rotated = self.service.rotate_credential("sensor-01")
        self.force_expire(first)
        self.publish(publisher, "t", 1, qos=1)

        reconnected = self.connect(credential=rotated["credential"],
                                   clean_start=False)
        self.assertTrue(reconnected["online"])
        messages = self.poll(reconnected)["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"], 1)


if __name__ == "__main__":
    unittest.main()
