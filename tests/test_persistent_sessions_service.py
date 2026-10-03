"""clean_start=false 进程内持久会话的服务层测试。"""

import unittest
from datetime import timedelta

from devicefabric.service import Service, ServiceError, _utc_now


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
                credential=None, clean_start=None):
        cred = credential if credential is not None else (
            self.device["credential"] if device_id == "sensor-01"
            else self.other["credential"]
        )
        body = {
            "device_id": device_id,
            "credential": cred,
            "client_id": client_id,
            "keepalive_seconds": keepalive,
        }
        if clean_start is not None:
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

    def persistent_key(self, device_id="sensor-01", client_id="sub"):
        return device_id, client_id

    # ------------------------------------------------------------------
    # 建立与默认行为兼容
    # ------------------------------------------------------------------

    def test_first_persistent_connect_starts_empty(self) -> None:
        session = self.connect(clean_start=False)
        self.assertTrue(session["online"])
        self.assertEqual(self.poll(session)["messages"], [])
        self.assertIn(("sensor-01", "cli-1"), self.service._persistent_sessions)

    def test_missing_or_true_clean_start_leaves_no_persistent_state(self) -> None:
        default = self.connect(client_id="a")
        explicit = self.connect(client_id="b", clean_start=True)
        self.assertTrue(default["online"])
        self.assertTrue(explicit["online"])
        self.assertEqual(self.service._persistent_sessions, {})

    # ------------------------------------------------------------------
    # 离线队列：仅 QoS 1、不计 matched_count、每组合一份
    # ------------------------------------------------------------------

    def test_offline_persistent_session_receives_qos1_after_reconnect(self) -> None:
        subscriber = self.connect(client_id="sub", clean_start=False)
        self.subscribe(subscriber, "t")
        self.force_expire(subscriber)

        publisher = self.connect(device_id="sensor-02", client_id="pub")
        result = self.publish(publisher, "t", {"v": 1}, qos=1)
        # 离线保存不计入 matched_count。
        self.assertEqual(result["matched_count"], 0)

        reopened = self.connect(client_id="sub", clean_start=False)
        messages = self.poll(reopened)["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["message_id"], result["message_id"])
        self.assertEqual(messages[0]["topic"], "t")
        self.assertEqual(messages[0]["payload"], {"v": 1})
        self.assertEqual(messages[0]["publisher_device_id"], "sensor-02")
        self.assertTrue(messages[0]["published_at"])
        self.assertEqual(messages[0]["qos"], 1)
        self.assertIs(messages[0]["dup"], False)
        self.assertNotIn("retained", messages[0])

    def test_offline_qos0_is_not_saved(self) -> None:
        subscriber = self.connect(client_id="sub", clean_start=False)
        self.subscribe(subscriber, "t")
        self.force_expire(subscriber)

        publisher = self.connect(device_id="sensor-02", client_id="pub")
        result = self.publish(publisher, "t", "dropped")
        self.assertEqual(result["matched_count"], 0)
        reopened = self.connect(client_id="sub", clean_start=False)
        self.assertEqual(self.poll(reopened)["messages"], [])

    def test_online_persistent_session_still_counts_as_matched(self) -> None:
        subscriber = self.connect(client_id="sub", clean_start=False)
        self.subscribe(subscriber, "t")
        publisher = self.connect(device_id="sensor-02", client_id="pub")
        result = self.publish(publisher, "t", 1, qos=1)
        self.assertEqual(result["matched_count"], 1)
        messages = self.poll(subscriber)["messages"]
        self.assertEqual(len(messages), 1)

    def test_multiple_filters_save_single_offline_copy(self) -> None:
        subscriber = self.connect(client_id="sub", clean_start=False)
        self.subscribe(subscriber, "a/b")
        self.subscribe(subscriber, "a/#")
        self.subscribe(subscriber, "#")
        self.force_expire(subscriber)

        publisher = self.connect(device_id="sensor-02", client_id="pub")
        self.publish(publisher, "a/b", 1, qos=1)
        reopened = self.connect(client_id="sub", clean_start=False)
        messages = self.poll(reopened)["messages"]
        self.assertEqual(len(messages), 1)

    def test_offline_delivery_id_is_independent_from_online_target(self) -> None:
        offline = self.connect(device_id="sensor-01", client_id="off",
                               clean_start=False)
        self.subscribe(offline, "t")
        self.force_expire(offline)

        online = self.connect(device_id="sensor-02", client_id="on")
        self.subscribe(online, "t")
        publisher = self.connect(device_id="sensor-02", client_id="pub")
        result = self.publish(publisher, "t", 1, qos=1)
        self.assertEqual(result["matched_count"], 1)
        online_id = self.poll(online)["messages"][0]["delivery_id"]

        reopened = self.connect(device_id="sensor-01", client_id="off",
                               clean_start=False)
        offline_id = self.poll(reopened)["messages"][0]["delivery_id"]
        self.assertNotEqual(online_id, offline_id)

    def test_unconsumed_online_qos0_is_dropped_when_going_offline(self) -> None:
        subscriber = self.connect(client_id="sub", clean_start=False)
        self.subscribe(subscriber, "t")
        publisher = self.connect(device_id="sensor-02", client_id="pub")
        # QoS 0 实时入队但尚未拉取；QoS 1 同时在队列中。
        self.publish(publisher, "t", "qos0")
        self.publish(publisher, "t", "qos1", qos=1)
        self.force_expire(subscriber)
        # 再发一条触发路由中的超时判定。
        self.publish(publisher, "t", "offline", qos=1)

        reopened = self.connect(client_id="sub", clean_start=False)
        messages = self.poll(reopened)["messages"]
        self.assertEqual([m["payload"] for m in messages], ["qos1", "offline"])
        self.assertTrue(all(m["qos"] == 1 for m in messages))

    # ------------------------------------------------------------------
    # 重连后的投递顺序、dup 与确认历史
    # ------------------------------------------------------------------

    def test_redelivery_keeps_delivery_id_dup_and_order_after_reconnect(self) -> None:
        subscriber = self.connect(client_id="sub", clean_start=False)
        self.subscribe(subscriber, "t")
        publisher = self.connect(device_id="sensor-02", client_id="pub")
        ids = [self.publish(publisher, "t", i, qos=1)["message_id"]
               for i in range(2)]
        first = self.poll(subscriber, max_messages=1)["messages"]
        self.assertEqual(first[0]["message_id"], ids[0])
        delivery_one = first[0]["delivery_id"]

        self.force_expire(subscriber)
        third = self.publish(publisher, "t", 2, qos=1)
        self.assertEqual(third["matched_count"], 0)

        reopened = self.connect(client_id="sub", clean_start=False)
        messages = self.poll(reopened, max_messages=3)["messages"]
        self.assertEqual([m["message_id"] for m in messages],
                         [ids[0], ids[1], third["message_id"]])
        # 曾拉取未确认：相同 delivery_id、dup 为 true、优先重投。
        self.assertEqual(messages[0]["delivery_id"], delivery_one)
        self.assertIs(messages[0]["dup"], True)
        # 从未拉取：首次返回 dup 为 false。
        self.assertIs(messages[1]["dup"], False)
        self.assertIs(messages[2]["dup"], False)
        self.assertEqual(len({m["delivery_id"] for m in messages}), 3)

    def test_ack_history_survives_reconnect(self) -> None:
        subscriber = self.connect(client_id="sub", clean_start=False)
        self.subscribe(subscriber, "t")
        publisher = self.connect(device_id="sensor-02", client_id="pub")
        self.publish(publisher, "t", 1, qos=1)
        delivery_id = self.poll(subscriber)["messages"][0]["delivery_id"]
        self.assertEqual(self.ack(subscriber, [delivery_id]), {"acked_count": 1})

        self.force_expire(subscriber)
        self.publish(publisher, "t", 2, qos=1)
        reopened = self.connect(client_id="sub", clean_start=False)
        # 历史确认的标识仍属本会话：重复确认幂等成功而非 404。
        self.assertEqual(self.ack(reopened, [delivery_id]), {"acked_count": 0})
        messages = self.poll(reopened)["messages"]
        self.assertEqual([m["payload"] for m in messages], [2])

    # ------------------------------------------------------------------
    # 保留消息：重连不回放，在线期间新订阅仍回放
    # ------------------------------------------------------------------

    def test_reconnect_does_not_replay_retained_snapshot(self) -> None:
        subscriber = self.connect(client_id="sub", clean_start=False)
        self.subscribe(subscriber, "t")
        publisher = self.connect(device_id="sensor-02", client_id="pub")
        self.publish(publisher, "t", "ret", qos=1, retain=True)
        # 在线时消费实时投递并确认，避免作为未确认消息跨重连重投。
        delivered = self.poll(subscriber)["messages"]
        self.assertEqual(len(delivered), 1)
        self.ack(subscriber, [delivered[0]["delivery_id"]])

        self.force_expire(subscriber)
        reopened = self.connect(client_id="sub", clean_start=False)
        # 订阅立即生效但不触发保留回放；离线实时消息也没有。
        self.assertEqual(self.poll(reopened)["messages"], [])

    def test_new_filter_while_online_still_replays_retained(self) -> None:
        subscriber = self.connect(client_id="sub", clean_start=False)
        publisher = self.connect(device_id="sensor-02", client_id="pub")
        self.publish(publisher, "u/x", "ret", qos=1, retain=True)
        # 重连后的在线会话首次新增过滤器，照常回放当时快照。
        reopened_session = self.connect(client_id="sub", clean_start=False)
        self.subscribe(reopened_session, "u/#")
        messages = self.poll(reopened_session)["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["payload"], "ret")
        self.assertIs(messages[0]["retained"], True)
        self.assertIs(messages[0]["dup"], False)

    # ------------------------------------------------------------------
    # clean_start=true 清状态；非布尔拒绝且无副作用
    # ------------------------------------------------------------------

    def test_clean_start_true_wipes_persistent_state(self) -> None:
        subscriber = self.connect(client_id="sub", clean_start=False)
        self.subscribe(subscriber, "t")
        self.force_expire(subscriber)
        publisher = self.connect(device_id="sensor-02", client_id="pub")
        self.publish(publisher, "t", 1, qos=1)
        self.assertIn(("sensor-01", "sub"), self.service._persistent_sessions)

        fresh = self.connect(client_id="sub", clean_start=True)
        self.assertNotIn(("sensor-01", "sub"), self.service._persistent_sessions)
        self.assertEqual(self.poll(fresh)["messages"], [])
        # 旧订阅已清除：后续发布不命中这个临时会话之外的任何持久目标。
        self.force_expire(fresh)
        self.assertEqual(self.publish(publisher, "t", 2, qos=1)["matched_count"], 0)
        # 再以 false 连接是空状态。
        reopened = self.connect(client_id="sub", clean_start=False)
        self.assertEqual(self.poll(reopened)["messages"], [])

    def test_non_boolean_clean_start_rejected_without_side_effects(self) -> None:
        existing = self.connect(client_id="sub", clean_start=False)
        self.subscribe(existing, "t")
        credential = self.device["credential"]
        for bad in ("false", "true", 0, 1, 0.0, [], {}):
            with self.subTest(bad=bad):
                with self.assertRaises(ServiceError) as ctx:
                    self.connect(client_id="sub", clean_start=bad)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        # None 需要显式进入请求体（helper 会跳过 None）。
        with self.assertRaises(ServiceError) as ctx:
            self.service.create_session({
                "device_id": "sensor-01",
                "credential": self.device["credential"],
                "client_id": "sub",
                "keepalive_seconds": 30,
                "clean_start": None,
            })
        self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(ctx.exception.status, 400)
        # 旧会话仍在线，持久状态未被触碰。
        self.assertTrue(self.service.get_session(existing["session_id"])["online"])
        routes = self.service._persistent_sessions[("sensor-01", "sub")]
        self.assertEqual(routes["subscriptions"], {"t"})

    # ------------------------------------------------------------------
    # 同组合在线替换、轮换、吊销与规则动作
    # ------------------------------------------------------------------

    def test_reconnect_replaces_online_persistent_session_and_shares_state(self) -> None:
        first = self.connect(client_id="sub", clean_start=False)
        self.subscribe(first, "t")
        publisher = self.connect(device_id="sensor-02", client_id="pub")
        self.publish(publisher, "t", 1, qos=1)
        first_id = self.poll(first)["messages"][0]["delivery_id"]

        second = self.connect(client_id="sub", clean_start=False)
        old = self.service.get_session(first["session_id"])
        self.assertFalse(old["online"])
        self.assertEqual(old["reason"], "replaced")
        # 未确认投递随共享容器立即归属新会话，dup 重投。
        messages = self.poll(second)["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["delivery_id"], first_id)
        self.assertIs(messages[0]["dup"], True)

    def test_credential_rotation_keeps_persistent_state(self) -> None:
        subscriber = self.connect(client_id="sub", clean_start=False)
        self.subscribe(subscriber, "t")
        self.force_expire(subscriber)

        rotated = self.service.rotate_credential("sensor-01")
        publisher = self.connect(device_id="sensor-02", client_id="pub")
        self.publish(publisher, "t", "after-rotate", qos=1)

        reopened = self.connect(client_id="sub",
                                credential=rotated["credential"],
                                clean_start=False)
        messages = self.poll(reopened)["messages"]
        self.assertEqual([m["payload"] for m in messages], ["after-rotate"])

    def test_revoke_clears_all_persistent_sessions_of_device(self) -> None:
        victim = self.connect(device_id="sensor-01", client_id="v",
                              clean_start=False)
        self.subscribe(victim, "t")
        survivor = self.connect(device_id="sensor-02", client_id="s",
                                clean_start=False)
        self.subscribe(survivor, "t")
        self.force_expire(victim)
        self.force_expire(survivor)

        self.service.revoke_device("sensor-01")
        self.assertNotIn(("sensor-01", "v"), self.service._persistent_sessions)
        self.assertIn(("sensor-02", "s"), self.service._persistent_sessions)

    def test_rule_action_qos1_is_saved_for_offline_persistent_session(self) -> None:
        subscriber = self.connect(client_id="sub", clean_start=False)
        self.subscribe(subscriber, "t")
        self.force_expire(subscriber)

        self.service.create_rule({
            "rule_id": "r1",
            "topic_filter": "cmd",
            "enabled": True,
            "condition": {"path": ["go"], "operator": "eq", "value": True},
            "action": {"topic": "t", "payload": "acted", "qos": 1},
        })
        publisher = self.connect(device_id="sensor-02", client_id="pub")
        result = self.publish(publisher, "cmd", {"go": True})
        self.assertEqual(result["matched_count"], 0)

        reopened = self.connect(client_id="sub", clean_start=False)
        messages = self.poll(reopened)["messages"]
        self.assertEqual([m["payload"] for m in messages], ["acted"])
        self.assertEqual(messages[0]["qos"], 1)
        self.assertIs(messages[0]["dup"], False)


if __name__ == "__main__":
    unittest.main()
