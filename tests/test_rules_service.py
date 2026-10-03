import unittest
from datetime import timedelta

from devicefabric.service import Service, ServiceError, _utc_now


class RulesCrudServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()

    def rule_payload(self, **overrides):
        payload = {
            "rule_id": "rule-01",
            "topic_filter": "house/+/temp",
            "enabled": True,
            "condition": {"path": ["temp"], "operator": "gt", "value": 30},
            "action": {"topic": "alerts/hot", "payload": {"alarm": True}, "qos": 1},
        }
        payload.update(overrides)
        return payload

    def test_create_returns_full_rule(self) -> None:
        result = self.service.create_rule(self.rule_payload())
        self.assertEqual(result, {
            "rule_id": "rule-01",
            "topic_filter": "house/+/temp",
            "enabled": True,
            "condition": {"path": ["temp"], "operator": "gt", "value": 30},
            "action": {"topic": "alerts/hot", "payload": {"alarm": True}, "qos": 1},
        })

    def test_create_accepts_false_enabled_and_null_payload(self) -> None:
        result = self.service.create_rule(
            self.rule_payload(
                rule_id="rule-02", enabled=False,
                action={"topic": "a", "payload": None, "qos": 0},
            )
        )
        self.assertFalse(result["enabled"])
        self.assertIsNone(result["action"]["payload"])

    def test_list_rules_in_creation_order(self) -> None:
        self.service.create_rule(self.rule_payload(rule_id="r3"))
        self.service.create_rule(
            self.rule_payload(rule_id="r1", topic_filter="a")
        )
        self.service.create_rule(
            self.rule_payload(rule_id="r2", topic_filter="b")
        )
        listed = self.service.list_rules()["rules"]
        self.assertEqual([r["rule_id"] for r in listed], ["r3", "r1", "r2"])

    def test_list_empty(self) -> None:
        self.assertEqual(self.service.list_rules(), {"rules": []})

    def test_duplicate_rule_id_conflicts(self) -> None:
        self.service.create_rule(self.rule_payload())
        with self.assertRaises(ServiceError) as ctx:
            self.service.create_rule(self.rule_payload(topic_filter="other/#"))
        self.assertEqual(ctx.exception.code, "rule_already_exists")
        self.assertEqual(ctx.exception.status, 409)
        # 原记录不变。
        self.assertEqual(
            self.service.list_rules()["rules"][0]["topic_filter"], "house/+/temp"
        )

    def test_enable_disable_returns_full_rule(self) -> None:
        created = self.service.create_rule(self.rule_payload())
        self.assertTrue(created["enabled"])
        disabled = self.service.set_rule_enabled("rule-01", {"enabled": False})
        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["rule_id"], "rule-01")
        enabled = self.service.set_rule_enabled("rule-01", {"enabled": True})
        self.assertTrue(enabled["enabled"])

    def test_set_enabled_unknown_rule_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.set_rule_enabled("ghost", {"enabled": True})
        self.assertEqual(ctx.exception.code, "rule_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_set_enabled_rejects_non_boolean(self) -> None:
        self.service.create_rule(self.rule_payload())
        for body in (
            "not-an-object", {}, {"enabled": 1}, {"enabled": 0},
            {"enabled": "true"}, {"enabled": None}, {"enabled": True, "extra": 1},
        ):
            with self.subTest(body=body):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.set_rule_enabled("rule-01", body)
                self.assertEqual(ctx.exception.code, "invalid_request")
        # 失败不改变启停状态。
        self.assertTrue(
            self.service.list_rules()["rules"][0]["enabled"]
        )

    def test_delete_rule(self) -> None:
        self.service.create_rule(self.rule_payload())
        result = self.service.delete_rule("rule-01")
        self.assertEqual(result, {"deleted": True})
        self.assertEqual(self.service.list_rules()["rules"], [])
        with self.assertRaises(ServiceError) as ctx:
            self.service.delete_rule("rule-01")
        self.assertEqual(ctx.exception.code, "rule_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_delete_unknown_rule_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.delete_rule("ghost")
        self.assertEqual(ctx.exception.code, "rule_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_invalid_create_payloads(self) -> None:
        bad_payloads = [
            "not-an-object",
            None,
            [],
            {},
            {"rule_id": "r1"},
            # 缺少各字段。
            {"rule_id": "r1", "topic_filter": "a", "enabled": True,
             "condition": {"path": ["x"], "operator": "eq", "value": 1}},
            {"rule_id": "r1", "topic_filter": "a", "enabled": True,
             "action": {"topic": "a", "payload": 1, "qos": 0}},
            {"rule_id": "r1", "enabled": True,
             "condition": {"path": ["x"], "operator": "eq", "value": 1},
             "action": {"topic": "a", "payload": 1, "qos": 0}},
            {"topic_filter": "a", "enabled": True,
             "condition": {"path": ["x"], "operator": "eq", "value": 1},
             "action": {"topic": "a", "payload": 1, "qos": 0}},
            # 多余字段。
            {"rule_id": "r1", "topic_filter": "a", "enabled": True,
             "condition": {"path": ["x"], "operator": "eq", "value": 1},
             "action": {"topic": "a", "payload": 1, "qos": 0}, "extra": 1},
            # rule_id 沿用设备标识规则。
            {"rule_id": "", "topic_filter": "a", "enabled": True,
             "condition": {"path": ["x"], "operator": "eq", "value": 1},
             "action": {"topic": "a", "payload": 1, "qos": 0}},
            {"rule_id": "a/b", "topic_filter": "a", "enabled": True,
             "condition": {"path": ["x"], "operator": "eq", "value": 1},
             "action": {"topic": "a", "payload": 1, "qos": 0}},
            # 非法过滤器。
            {"rule_id": "r1", "topic_filter": "a/+b", "enabled": True,
             "condition": {"path": ["x"], "operator": "eq", "value": 1},
             "action": {"topic": "a", "payload": 1, "qos": 0}},
            # enabled 非布尔。
            {"rule_id": "r1", "topic_filter": "a", "enabled": 1,
             "condition": {"path": ["x"], "operator": "eq", "value": 1},
             "action": {"topic": "a", "payload": 1, "qos": 0}},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.create_rule(payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(self.service.list_rules()["rules"], [])

    def test_invalid_condition_payloads(self) -> None:
        base = self.rule_payload()
        bad_conditions = [
            None,
            "x",
            [],
            {},
            {"path": ["x"], "operator": "eq"},
            {"path": ["x"], "value": 1},
            {"operator": "eq", "value": 1},
            {"path": ["x"], "operator": "eq", "value": 1, "extra": 1},
            {"path": [], "operator": "eq", "value": 1},
            {"path": ["x"] * 17, "operator": "eq", "value": 1},
            {"path": ["x", ""], "operator": "eq", "value": 1},
            {"path": ["x", 1], "operator": "eq", "value": 1},
            {"path": "x", "operator": "eq", "value": 1},
            {"path": ["x"], "operator": "=", "value": 1},
            {"path": ["x"], "operator": "EQ", "value": 1},
            {"path": ["x"], "operator": 1, "value": 1},
            # 大小比较的 value 必须是非布尔数字。
            {"path": ["x"], "operator": "gt", "value": True},
            {"path": ["x"], "operator": "gt", "value": "1"},
            {"path": ["x"], "operator": "lte", "value": None},
            {"path": ["x"], "operator": "gte", "value": [1]},
        ]
        for condition in bad_conditions:
            with self.subTest(condition=condition):
                payload = dict(base)
                payload["rule_id"] = "r-bad"
                payload["condition"] = condition
                with self.assertRaises(ServiceError) as ctx:
                    self.service.create_rule(payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(self.service.list_rules()["rules"], [])

    def test_eq_accepts_any_value_kind(self) -> None:
        for index, value in enumerate(
            (None, True, False, 1, 1.5, "s", [1], {"a": 1})
        ):
            with self.subTest(value=value):
                self.service.create_rule(self.rule_payload(
                    rule_id=f"r-eq-{index}",
                    condition={"path": ["x"], "operator": "eq", "value": value},
                ))

    def test_invalid_action_payloads(self) -> None:
        base = self.rule_payload()
        bad_actions = [
            None,
            "x",
            [],
            {},
            {"topic": "a", "payload": 1},
            {"payload": 1, "qos": 0},
            {"topic": "a", "qos": 0},
            {"topic": "a", "payload": 1, "qos": 0, "extra": 1},
            # 固定 topic 不得含通配符或非法层。
            {"topic": "a/+/b", "payload": 1, "qos": 0},
            {"topic": "a/#", "payload": 1, "qos": 0},
            {"topic": "a//b", "payload": 1, "qos": 0},
            {"topic": 1, "payload": 1, "qos": 0},
            # qos 只接受整数 0 或 1。
            {"topic": "a", "payload": 1, "qos": 2},
            {"topic": "a", "payload": 1, "qos": -1},
            {"topic": "a", "payload": 1, "qos": True},
            {"topic": "a", "payload": 1, "qos": "1"},
        ]
        for action in bad_actions:
            with self.subTest(action=action):
                payload = dict(base)
                payload["rule_id"] = "r-bad"
                payload["action"] = action
                with self.assertRaises(ServiceError) as ctx:
                    self.service.create_rule(payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(self.service.list_rules()["rules"], [])

    def test_path_supports_up_to_sixteen_segments(self) -> None:
        condition = {"path": [f"k{i}" for i in range(16)],
                     "operator": "eq", "value": 1}
        created = self.service.create_rule(self.rule_payload(condition=condition))
        self.assertEqual(len(created["condition"]["path"]), 16)


class RuleEngineServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.publisher_device = self.service.register_device(
            {"device_id": "sensor-01", "display_name": "一号"}
        )
        self.listener_device = self.service.register_device(
            {"device_id": "sensor-02", "display_name": "二号"}
        )

    def connect(self, device_id="sensor-01", client_id="cli", keepalive=30):
        credential = (self.publisher_device["credential"]
                      if device_id == "sensor-01"
                      else self.listener_device["credential"])
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

    def publish(self, session, topic, payload, qos=0, retain=False):
        body = {"session_token": session["session_token"], "topic": topic,
                "payload": payload, "qos": qos}
        if retain:
            body["retain"] = True
        return self.service.publish_message(session["session_id"], body)

    def poll(self, session, max_messages=100):
        return self.service.poll_messages(
            session["session_id"],
            {"session_token": session["session_token"],
             "max_messages": max_messages},
        )

    def make_rule(self, rule_id, topic_filter, condition, action, enabled=True):
        return self.service.create_rule({
            "rule_id": rule_id,
            "topic_filter": topic_filter,
            "enabled": enabled,
            "condition": condition,
            "action": action,
        })

    def force_expire(self, session) -> None:
        record = self.service._sessions[session["session_id"]]
        record["expires_at"] = _utc_now() - timedelta(seconds=1)

    def test_matching_rule_generates_action_message(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "alerts/#")
        self.make_rule(
            "r1", "house/+/temp",
            {"path": ["temp"], "operator": "gt", "value": 30},
            {"topic": "alerts/hot", "payload": {"alarm": True}, "qos": 0},
        )
        result = self.publish(publisher, "house/room1/temp", {"temp": 31})
        # 动作投递不计入 matched_count：监听器未订阅原 topic，故为 0。
        self.assertEqual(result["matched_count"], 0)

        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertNotEqual(message["message_id"], result["message_id"])
        self.assertEqual(message["topic"], "alerts/hot")
        self.assertEqual(message["payload"], {"alarm": True})
        # 沿用原发布设备身份。
        self.assertEqual(message["publisher_device_id"], "sensor-01")
        # 普通非保留消息：不携带 retained。
        self.assertNotIn("retained", message)
        self.assertNotIn("qos", message)

    def test_original_message_enqueued_before_action(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "house/#")
        self.subscribe(listener, "alerts/#")
        self.make_rule(
            "r1", "house/+/temp",
            {"path": ["temp"], "operator": "gte", "value": 10},
            {"topic": "alerts/hot", "payload": "go", "qos": 0},
        )
        result = self.publish(publisher, "house/r/temp", {"temp": 10})
        messages = self.poll(listener)["messages"]
        self.assertEqual([m["topic"] for m in messages],
                         ["house/r/temp", "alerts/hot"])
        self.assertEqual(messages[0]["message_id"], result["message_id"])

    def test_non_matching_filter_or_condition_no_action(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "#")
        self.make_rule(
            "r1", "house/+/temp",
            {"path": ["temp"], "operator": "gt", "value": 30},
            {"topic": "alerts/hot", "payload": "go", "qos": 0},
        )
        # 过滤器不命中。
        self.publish(publisher, "garden/room1/temp", {"temp": 31})
        # 条件不命中：温度未超过阈值。
        self.publish(publisher, "house/room1/temp", {"temp": 30})
        # 只有两条原消息。
        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 2)
        self.assertTrue(all(m["topic"] != "alerts/hot" for m in messages))

    def test_disabled_rule_skipped_and_toggle_works(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "#")
        self.make_rule(
            "r1", "t", {"path": ["v"], "operator": "eq", "value": 1},
            {"topic": "a", "payload": None, "qos": 0}, enabled=False,
        )
        self.publish(publisher, "t", {"v": 1})
        topics = [m["topic"] for m in self.poll(listener)["messages"]]
        self.assertEqual(topics, ["t"])

        self.service.set_rule_enabled("r1", {"enabled": True})
        self.publish(publisher, "t", {"v": 1})
        topics = [m["topic"] for m in self.poll(listener)["messages"]]
        self.assertEqual(topics, ["t", "a"])

        self.service.set_rule_enabled("r1", {"enabled": False})
        self.publish(publisher, "t", {"v": 1})
        topics = [m["topic"] for m in self.poll(listener)["messages"]]
        self.assertEqual(topics, ["t"])

    def test_multiple_rules_fire_in_creation_order_each_once(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "#")
        self.make_rule(
            "r-second", "t",
            {"path": ["v"], "operator": "eq", "value": 1},
            {"topic": "second", "payload": 2, "qos": 0},
        )
        self.make_rule(
            "r-first", "t",
            {"path": ["v"], "operator": "gt", "value": 0},
            {"topic": "first", "payload": 3, "qos": 0},
        )
        # 删除后创建顺序变化，验证按列表当前顺序评估。
        self.service.delete_rule("r-second")
        self.make_rule(
            "r-third", "t",
            {"path": ["v"], "operator": "ne", "value": 2},
            {"topic": "third", "payload": 4, "qos": 0},
        )
        self.publish(publisher, "t", {"v": 1})
        self.assertEqual(
            [m["topic"] for m in self.poll(listener)["messages"]],
            ["t", "first", "third"],
        )

    def test_actions_do_not_trigger_rules(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "#")
        self.make_rule(
            "r1", "src",
            {"path": ["v"], "operator": "eq", "value": 1},
            {"topic": "dst", "payload": {"v": 1}, "qos": 0},
        )
        # 若动作消息再次触发规则，本规则会造成无限级联。
        self.make_rule(
            "r2", "dst",
            {"path": ["v"], "operator": "eq", "value": 1},
            {"topic": "cascade", "payload": None, "qos": 0},
        )
        self.publish(publisher, "src", {"v": 1})
        self.assertEqual(
            [m["topic"] for m in self.poll(listener)["messages"]],
            ["src", "dst"],
        )

    def test_conditions_path_navigation_and_misses(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "hit")
        self.make_rule(
            "r1", "src",
            {"path": ["a", "b"], "operator": "eq", "value": 5},
            {"topic": "hit", "payload": None, "qos": 0},
        )
        cases = [
            ({"a": {"b": 5}}, True),       # 深度命中
            ({"a": {"b": 6}}, False),      # 值不等
            ({"a": {"x": 5}}, False),      # path 缺失
            ({"a": 5}, False),             # 中途遇到非对象
            ({}, False),                   # 首层缺失
            ({"a": {"b": [5]}}, False),    # 类型不同
            ({"a": {"b": {"c": 1}}}, False),
        ]
        for payload, should_hit in cases:
            with self.subTest(payload=payload):
                self.publish(publisher, "src", payload)
        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 1)

    def test_eq_ne_deep_comparison(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "#")
        self.make_rule(
            "eq-rule", "eqt",
            {"path": ["x"], "operator": "eq",
             "value": {"k": [1, 2, {"m": True}]}},
            {"topic": "eq-hit", "payload": None, "qos": 0},
        )
        self.make_rule(
            "ne-rule", "net",
            {"path": ["x"], "operator": "ne", "value": 1},
            {"topic": "ne-hit", "payload": None, "qos": 0},
        )
        self.publish(publisher, "eqt", {"x": {"k": [1, 2, {"m": True}]}})
        self.publish(publisher, "eqt", {"x": {"k": [1, 2, {"m": False}]}})
        self.publish(publisher, "net", {"x": 1})
        self.publish(publisher, "net", {"x": 2})
        topics = [m["topic"] for m in self.poll(listener)["messages"]]
        # 每次发布原消息都入队；仅 eq 命中一次、ne 命中一次。
        self.assertEqual(topics,
                         ["eqt", "eq-hit", "eqt", "net", "net", "ne-hit"])

    def test_ordering_requires_non_boolean_number_at_path(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "hit")
        self.make_rule(
            "r1", "src",
            {"path": ["v"], "operator": "gt", "value": 0},
            {"topic": "hit", "payload": None, "qos": 0},
        )
        for payload in ({"v": True}, {"v": "1"}, {"v": None}, {"v": [1]}, {}):
            with self.subTest(payload=payload):
                self.publish(publisher, "src", payload)
        # 全部不命中。
        self.assertEqual(self.poll(listener)["messages"], [])

        # 数值各边界。
        for value, should_hit in ((1, True), (0, False), (-1.5, False),
                                  (0.0, False), (1e9, True)):
            self.publish(publisher, "src", {"v": value})
        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 2)

    def test_eq_distinguishes_booleans_from_numbers(self) -> None:
        service = self.service
        for expected, read, hit in [
            (True, 1, False),
            (1, True, False),
            (False, 0, False),
            (0, False, False),
            (1, 1.0, True),
            (True, True, True),
            (None, False, False),
            ("1", 1, False),
        ]:
            rule = service.create_rule({
                "rule_id": f"r-type-{expected}-{read}",
                "topic_filter": "t",
                "enabled": True,
                "condition": {"path": ["v"], "operator": "eq", "value": expected},
                "action": {"topic": "hit", "payload": None, "qos": 0},
            })
            self.assertIs(
                service._rule_condition_matches_locked(rule["condition"],
                                                       {"v": read}),
                hit,
                msg=f"eq {expected!r} vs {read!r}",
            )

    def test_all_ordering_operators(self) -> None:
        service = self.service
        for operator, value, read, expected in [
            ("gt", 5, 6, True), ("gt", 5, 5, False),
            ("gte", 5, 5, True), ("lt", 5, 4, True), ("lt", 5, 5, False),
            ("lte", 5, 5, True), ("ne", 5, 6, True), ("ne", 5, 5, False),
        ]:
            rule = service.create_rule({
                "rule_id": f"r-{operator}-{value}-{read}",
                "topic_filter": "t",
                "enabled": True,
                "condition": {"path": ["v"], "operator": operator, "value": value},
                "action": {"topic": "hit", "payload": None, "qos": 0},
            })
            self.assertEqual(
                service._rule_condition_matches_locked(rule["condition"], {"v": read}),
                expected,
            )

    def test_action_qos1_uses_delivery_and_backpressure(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "#")
        self.make_rule(
            "r1", "src",
            {"path": ["v"], "operator": "eq", "value": 1},
            {"topic": "dst", "payload": None, "qos": 1},
        )
        self.publish(publisher, "src", {"v": 1})
        first = self.poll(listener, max_messages=2)["messages"]
        self.assertEqual([m["topic"] for m in first], ["src", "dst"])
        action = first[1]
        self.assertEqual(action["qos"], 1)
        self.assertFalse(action["dup"])
        delivery_id = action["delivery_id"]

        # 未确认动作消息优先重投，dup 为 true，message_id 保持不变。
        redelivered = self.poll(listener, max_messages=1)["messages"]
        self.assertEqual(len(redelivered), 1)
        self.assertEqual(redelivered[0]["topic"], "dst")
        self.assertTrue(redelivered[0]["dup"])
        self.assertEqual(redelivered[0]["delivery_id"], delivery_id)
        self.assertEqual(redelivered[0]["message_id"], action["message_id"])

        ack = self.service.ack_messages(
            listener["session_id"],
            {"session_token": listener["session_token"],
             "delivery_ids": [delivery_id]},
        )
        self.assertEqual(ack["acked_count"], 1)
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_action_messages_get_new_ids(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "#")
        self.make_rule(
            "r1", "src",
            {"path": ["v"], "operator": "eq", "value": 1},
            {"topic": "dst", "payload": None, "qos": 0},
        )
        ids = set()
        for _ in range(20):
            result = self.publish(publisher, "src", {"v": 1})
            ids.add(result["message_id"])
        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 40)
        action_ids = {m["message_id"] for m in messages if m["topic"] == "dst"}
        self.assertEqual(len(action_ids), 20)
        self.assertTrue(ids.isdisjoint(action_ids))

    def test_retained_replay_does_not_trigger_rules(self) -> None:
        publisher = self.connect(client_id="pub")
        self.make_rule(
            "r1", "src",
            {"path": ["v"], "operator": "eq", "value": 1},
            {"topic": "dst", "payload": None, "qos": 0},
        )
        # 保留发布：此刻无订阅者；回放不应评估规则。
        self.publish(publisher, "src", {"v": 1}, retain=True)

        late = self.connect(device_id="sensor-02", client_id="late")
        self.subscribe(late, "#")
        messages = self.poll(late)["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["topic"], "src")
        self.assertTrue(messages[0]["retained"])

    def test_retained_publish_still_fires_live_actions_but_not_retained(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "#")
        self.make_rule(
            "r1", "src",
            {"path": ["v"], "operator": "eq", "value": 1},
            {"topic": "dst", "payload": {"from": "action"}, "qos": 0},
        )
        self.publish(publisher, "src", {"v": 1}, retain=True)
        messages = self.poll(listener)["messages"]
        self.assertEqual([m["topic"] for m in messages], ["src", "dst"])
        # 动作消息不是保留消息。
        self.assertNotIn("retained", messages[1])

        # 之后订阅只回放原 topic 的保留消息，动作 topic 无保留值。
        late = self.connect(device_id="sensor-02", client_id="late")
        self.subscribe(late, "#")
        replayed = self.poll(late)["messages"]
        self.assertEqual([m["topic"] for m in replayed], ["src"])
        self.assertTrue(replayed[0]["retained"])

    def test_rules_survive_revoke_and_offline_sessions(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "dst")
        self.make_rule(
            "r1", "src",
            {"path": ["v"], "operator": "eq", "value": 1},
            {"topic": "dst", "payload": None, "qos": 0},
        )
        self.force_expire(listener)
        self.service.revoke_device("sensor-02")
        # 规则依然存在且仍为启用。
        rules = self.service.list_rules()["rules"]
        self.assertEqual(len(rules), 1)
        self.assertTrue(rules[0]["enabled"])

        # 发布者仍在线，发布照常评估；离线会话不接收。
        result = self.publish(publisher, "src", {"v": 1})
        self.assertEqual(result["matched_count"], 0)

    def test_failed_publish_validation_does_not_evaluate_rules(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "#")
        self.make_rule(
            "r1", "src",
            {"path": ["v"], "operator": "eq", "value": 1},
            {"topic": "dst", "payload": None, "qos": 0},
        )
        # 非法 qos：整个请求失败，不产生任何消息或动作。
        with self.assertRaises(ServiceError):
            self.publish(publisher, "src", {"v": 1}, qos=2)
        # 错误令牌：鉴权失败，不评估。
        with self.assertRaises(ServiceError):
            self.service.publish_message(
                publisher["session_id"],
                {"session_token": "wrong", "topic": "src", "payload": {"v": 1}},
            )
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_action_payload_is_deep_copied(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "dst")
        self.make_rule(
            "r1", "src",
            {"path": ["v"], "operator": "eq", "value": 1},
            {"topic": "dst", "payload": {"nested": [1, 2]}, "qos": 0},
        )
        self.publish(publisher, "src", {"v": 1})
        message = self.poll(listener)["messages"][0]
        message["payload"]["nested"].append(3)
        # 再次触发，规则内 payload 不被污染。
        self.publish(publisher, "src", {"v": 1})
        again = self.poll(listener)["messages"][0]
        self.assertEqual(again["payload"], {"nested": [1, 2]})

    def test_rule_with_hash_filter_matches_layers(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener, "dst")
        self.make_rule(
            "r1", "telemetry/#",
            {"path": ["v"], "operator": "gte", "value": 10},
            {"topic": "dst", "payload": None, "qos": 0},
        )
        self.publish(publisher, "telemetry/a/b/c", {"v": 10})
        self.publish(publisher, "telemetry", {"v": 10})
        self.publish(publisher, "other", {"v": 10})
        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 2)


if __name__ == "__main__":
    unittest.main()
