import unittest
from datetime import timedelta

from devicefabric.service import Service, ServiceError, _utc_now


class RulesServiceTest(unittest.TestCase):
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

    def rule(self, rule_id="r1", *, topic_filter="sensor/+/data", enabled=True,
             path=("temp",), operator="gt", value=30, action_topic="alerts/high",
             action_payload=None, action_qos=0):
        if action_payload is None:
            action_payload = {"alert": True}
        return self.service.create_rule({
            "rule_id": rule_id,
            "topic_filter": topic_filter,
            "enabled": enabled,
            "condition": {"path": list(path), "operator": operator, "value": value},
            "action": {"topic": action_topic, "payload": action_payload,
                       "qos": action_qos},
        })

    def listener(self, topic_filter, device_id="sensor-02", client_id="sub"):
        session = self.connect(device_id=device_id, client_id=client_id)
        self.subscribe(session, topic_filter)
        return session

    def force_expire(self, session) -> None:
        record = self.service._sessions[session["session_id"]]
        record["expires_at"] = _utc_now() - timedelta(seconds=1)

    # ------------------------------------------------------------------
    # 规则 CRUD
    # ------------------------------------------------------------------

    def test_create_rule_returns_full_rule(self) -> None:
        result = self.rule("temp-high")
        self.assertEqual(result, {
            "rule_id": "temp-high",
            "topic_filter": "sensor/+/data",
            "enabled": True,
            "condition": {"path": ["temp"], "operator": "gt", "value": 30},
            "action": {"topic": "alerts/high", "payload": {"alert": True},
                       "qos": 0},
        })

    def test_create_rule_disabled(self) -> None:
        result = self.rule("r-off", enabled=False)
        self.assertFalse(result["enabled"])

    def test_duplicate_rule_id_conflicts(self) -> None:
        self.rule("dup")
        with self.assertRaises(ServiceError) as ctx:
            self.rule("dup")
        self.assertEqual(ctx.exception.code, "rule_already_exists")
        self.assertEqual(ctx.exception.status, 409)
        # 冲突不覆盖原规则。
        rules = self.service.list_rules()["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["rule_id"], "dup")

    def test_list_rules_in_creation_order(self) -> None:
        self.rule("r3")
        self.rule("r1")
        self.rule("r2")
        self.assertEqual(
            [r["rule_id"] for r in self.service.list_rules()["rules"]],
            ["r3", "r1", "r2"],
        )

    def test_empty_list(self) -> None:
        self.assertEqual(self.service.list_rules(), {"rules": []})

    def test_set_enabled(self) -> None:
        created = self.rule("r1", enabled=True)
        self.assertTrue(created["enabled"])
        updated = self.service.set_rule_enabled("r1", {"enabled": False})
        self.assertFalse(updated["enabled"])
        # 返回完整规则。
        self.assertEqual(set(updated),
                         {"rule_id", "topic_filter", "enabled", "condition",
                          "action"})
        again = self.service.set_rule_enabled("r1", {"enabled": True})
        self.assertTrue(again["enabled"])
        # 列表反映最新状态。
        self.assertTrue(self.service.list_rules()["rules"][0]["enabled"])

    def test_delete_rule(self) -> None:
        self.rule("r1")
        self.rule("r2")
        result = self.service.delete_rule("r1")
        self.assertEqual(result, {"deleted": True})
        self.assertEqual(
            [r["rule_id"] for r in self.service.list_rules()["rules"]], ["r2"]
        )
        # 已删除规则的启停与删除均 404。
        with self.assertRaises(ServiceError) as ctx:
            self.service.set_rule_enabled("r1", {"enabled": True})
        self.assertEqual(ctx.exception.code, "rule_not_found")
        with self.assertRaises(ServiceError) as ctx:
            self.service.delete_rule("r1")
        self.assertEqual(ctx.exception.code, "rule_not_found")

    def test_set_enabled_unknown_rule_404(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.set_rule_enabled("ghost", {"enabled": True})
        self.assertEqual(ctx.exception.code, "rule_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_delete_unknown_rule_404(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.delete_rule("ghost")
        self.assertEqual(ctx.exception.code, "rule_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_invalid_rule_id(self) -> None:
        bad_ids = ["", "a/b", "bad id", "x" * 65, 1, True, None]
        for rule_id in bad_ids:
            with self.subTest(rule_id=rule_id):
                with self.assertRaises(ServiceError) as ctx:
                    self.rule(rule_id)
                self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(self.service.list_rules()["rules"], [])

    def test_invalid_create_payloads(self) -> None:
        base = {
            "rule_id": "r1",
            "topic_filter": "sensor/+/data",
            "enabled": True,
            "condition": {"path": ["temp"], "operator": "gt", "value": 30},
            "action": {"topic": "alerts/high", "payload": {"alert": True},
                       "qos": 0},
        }

        def without(field):
            body = dict(base)
            del body[field]
            return body

        bad_payloads = [
            "not-an-object",
            None,
            [],
            without("rule_id"),
            without("topic_filter"),
            without("enabled"),
            without("condition"),
            without("action"),
            {**base, "extra": 1},
            {**base, "enabled": "yes"},
            {**base, "enabled": 1},
            {**base, "topic_filter": "a/"},
            {**base, "topic_filter": "#/a"},
            {**base, "topic_filter": "a/+b"},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.create_rule(payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(self.service.list_rules()["rules"], [])

    def test_invalid_condition(self) -> None:
        base = {
            "rule_id": "r1",
            "topic_filter": "sensor/+/data",
            "enabled": True,
            "action": {"topic": "alerts/high", "payload": 1, "qos": 0},
        }
        good = {"path": ["temp"], "operator": "gt", "value": 30}
        conditions = [
            None, "x", [],
            {},
            {"path": ["temp"], "operator": "gt"},
            {"operator": "gt", "value": 30},
            {"path": ["temp"], "value": 30},
            {"path": ["temp"], "operator": "gt", "value": 30, "extra": 1},
            {"path": [], "operator": "gt", "value": 30},
            {"path": [""] , "operator": "gt", "value": 30},
            {"path": ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k",
                      "l", "m", "n", "o", "p", "q"],
             "operator": "gt", "value": 30},
            {"path": [1], "operator": "gt", "value": 30},
            {"path": ["temp"], "operator": "Gt", "value": 30},
            {"path": ["temp"], "operator": "xx", "value": 30},
            {"path": ["temp"], "operator": 1, "value": 30},
            # 大小比较的 value 必须是非布尔数字。
            {"path": ["temp"], "operator": "gt", "value": True},
            {"path": ["temp"], "operator": "gte", "value": "30"},
            {"path": ["temp"], "operator": "lt", "value": None},
            {"path": ["temp"], "operator": "lte", "value": [1]},
        ]
        for condition in conditions:
            with self.subTest(condition=condition):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.create_rule({**base, "condition": condition})
                self.assertEqual(ctx.exception.code, "invalid_request")
        # eq/ne 的 value 允许任意 JSON 值（含布尔与 null）。
        for operator, cond_value in (("eq", True), ("ne", None),
                                     ("eq", [1, {"a": 2}])):
            body = {**base,
                    "condition": {"path": ["temp"], "operator": operator,
                                  "value": cond_value}}
            self.service.create_rule(body)
            self.service.delete_rule("r1")

    def test_invalid_action(self) -> None:
        base = {
            "rule_id": "r1",
            "topic_filter": "sensor/+/data",
            "enabled": True,
            "condition": {"path": ["temp"], "operator": "gt", "value": 30},
        }
        actions = [
            None, "x", [],
            {},
            {"topic": "a", "payload": 1},
            {"payload": 1, "qos": 0},
            {"topic": "a", "qos": 0},
            {"topic": "a", "payload": 1, "qos": 0, "extra": 1},
            # action topic 是固定 topic，不得含通配符或空层。
            {"topic": "a/+", "payload": 1, "qos": 0},
            {"topic": "a/#", "payload": 1, "qos": 0},
            {"topic": "a/", "payload": 1, "qos": 0},
            {"topic": "", "payload": 1, "qos": 0},
            # payload 字段必须存在（即使为 null 也合法）。
            {"topic": "a", "qos": 0},
            {"topic": "a", "payload": 1, "qos": 2},
            {"topic": "a", "payload": 1, "qos": "0"},
            {"topic": "a", "payload": 1, "qos": True},
        ]
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.create_rule({**base, "action": action})
                self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(self.service.list_rules()["rules"], [])

    def test_action_payload_accepts_any_json(self) -> None:
        for payload in (None, True, 1, 1.5, "str", [1, 2], {"k": [0]}):
            self.service.create_rule({
                "rule_id": "rp",
                "topic_filter": "t",
                "enabled": False,
                "condition": {"path": ["v"], "operator": "eq", "value": 1},
                "action": {"topic": "out", "payload": payload, "qos": 1},
            })
            self.service.delete_rule("rp")

    def test_set_enabled_invalid_payloads_change_nothing(self) -> None:
        self.rule("r1", enabled=True)
        for payload in ("x", None, [], {}, {"enabled": True, "x": 1},
                        {"enabled": "true"}, {"enabled": 1}):
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.set_rule_enabled("r1", payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertTrue(self.service.list_rules()["rules"][0]["enabled"])

    # ------------------------------------------------------------------
    # 规则评估：过滤器与条件
    # ------------------------------------------------------------------

    def test_action_fires_on_filter_and_condition_match(self) -> None:
        self.rule("r1")
        publisher = self.connect(client_id="pub")
        listener = self.listener("alerts/#")
        result = self.publish(publisher, "sensor/room1/data", {"temp": 31})
        # 原消息没有订阅者；动作投递不计入 matched_count。
        self.assertEqual(result["matched_count"], 0)
        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message["topic"], "alerts/high")
        self.assertEqual(message["payload"], {"alert": True})
        self.assertEqual(message["publisher_device_id"], "sensor-01")
        self.assertNotIn("retained", message)
        self.assertTrue(message["message_id"])
        self.assertNotEqual(message["message_id"], result["message_id"])
        self.assertRegex(
            message["published_at"],
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z",
        )

    def test_no_action_when_filter_does_not_match(self) -> None:
        self.rule("r1")
        publisher = self.connect(client_id="pub")
        listener = self.listener("alerts/#")
        self.publish(publisher, "other/topic", {"temp": 99})
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_no_action_when_condition_fails(self) -> None:
        self.rule("r1")
        publisher = self.connect(client_id="pub")
        listener = self.listener("alerts/#")
        self.publish(publisher, "sensor/room1/data", {"temp": 30})
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_disabled_rule_does_not_fire(self) -> None:
        self.rule("r1", enabled=False)
        publisher = self.connect(client_id="pub")
        listener = self.listener("alerts/#")
        self.publish(publisher, "sensor/room1/data", {"temp": 99})
        self.assertEqual(self.poll(listener)["messages"], [])
        # 启用后立即生效。
        self.service.set_rule_enabled("r1", {"enabled": True})
        self.publish(publisher, "sensor/room1/data", {"temp": 99})
        self.assertEqual(len(self.poll(listener)["messages"]), 1)

    def test_deleted_rule_does_not_fire(self) -> None:
        self.rule("r1")
        self.service.delete_rule("r1")
        publisher = self.connect(client_id="pub")
        listener = self.listener("alerts/#")
        self.publish(publisher, "sensor/room1/data", {"temp": 99})
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_operators(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("out/#")
        for operator, cond_value, actual, expect_hit in (
            ("eq", 1, 1, True),
            ("eq", 1, 2, False),
            ("ne", 1, 2, True),
            ("ne", 1, 1, False),
            ("gt", 5, 6, True),
            ("gt", 5, 5, False),
            ("gte", 5, 5, True),
            ("lt", 5, 4, True),
            ("lt", 5, 5, False),
            ("lte", 5, 5, True),
        ):
            with self.subTest(operator=operator, actual=actual):
                self.service.create_rule({
                    "rule_id": "rop",
                    "topic_filter": "t",
                    "enabled": True,
                    "condition": {"path": ["v"], "operator": operator,
                                  "value": cond_value},
                    "action": {"topic": "out/x", "payload": None, "qos": 0},
                })
                self.publish(publisher, "t", {"v": actual})
                messages = self.poll(listener)["messages"]
                self.assertEqual(len(messages) == 1, expect_hit)
                self.service.delete_rule("rop")

    def test_eq_ne_deep_comparison(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("out/#")
        self.service.create_rule({
            "rule_id": "req",
            "topic_filter": "t",
            "enabled": True,
            "condition": {"path": ["v"], "operator": "eq",
                          "value": {"a": 1, "b": [1, 2, {"c": 3}]}},
            "action": {"topic": "out/x", "payload": 1, "qos": 0},
        })
        self.publish(publisher, "t", {"v": {"b": [1, 2, {"c": 3}], "a": 1}})
        self.assertEqual(len(self.poll(listener)["messages"]), 1)
        self.publish(publisher, "t", {"v": {"a": 1, "b": [1, 2, {"c": 4}]}})
        self.assertEqual(len(self.poll(listener)["messages"]), 0)
        # 键集合不同也不相等。
        self.publish(publisher, "t", {"v": {"a": 1}})
        self.assertEqual(len(self.poll(listener)["messages"]), 0)

    def test_eq_bool_not_equal_to_number(self) -> None:
        publisher = self.connect(client_id="pub")
        listener = self.listener("out/#")
        self.service.create_rule({
            "rule_id": "rb",
            "topic_filter": "t",
            "enabled": True,
            "condition": {"path": ["v"], "operator": "eq", "value": 1},
            "action": {"topic": "out/x", "payload": 1, "qos": 0},
        })
        self.publish(publisher, "t", {"v": True})
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_ordering_evaluates_rules_in_creation_order_and_original_first(
        self,
    ) -> None:
        publisher = self.connect(client_id="pub")
        # 同一会话订阅原主题与动作主题；每条发布都应先收到原消息。
        self.subscribe(publisher, "sensor/#")
        self.subscribe(publisher, "alerts/#")
        self.service.create_rule({
            "rule_id": "r-first",
            "topic_filter": "sensor/x",
            "enabled": True,
            "condition": {"path": ["temp"], "operator": "gt", "value": 0},
            "action": {"topic": "alerts/a", "payload": 1, "qos": 0},
        })
        self.service.create_rule({
            "rule_id": "r-second",
            "topic_filter": "sensor/x",
            "enabled": True,
            "condition": {"path": ["temp"], "operator": "gt", "value": 0},
            "action": {"topic": "alerts/b", "payload": 2, "qos": 0},
        })
        result = self.publish(publisher, "sensor/x", {"temp": 1})
        self.assertEqual(result["matched_count"], 1)
        messages = self.poll(publisher)["messages"]
        self.assertEqual([m["topic"] for m in messages],
                         ["sensor/x", "alerts/a", "alerts/b"])
        self.assertEqual([m["payload"] for m in messages], [{"temp": 1}, 1, 2])

    def test_each_matching_rule_generates_own_message(self) -> None:
        listener = self.listener("alerts/#")
        self.rule("r1", action_topic="alerts/one")
        self.rule("r2", action_topic="alerts/two")
        publisher = self.connect(client_id="pub")
        self.publish(publisher, "sensor/room1/data", {"temp": 31})
        messages = self.poll(listener)["messages"]
        self.assertEqual({m["topic"] for m in messages},
                         {"alerts/one", "alerts/two"})
        self.assertEqual(len({m["message_id"] for m in messages}), 2)
        self.assertEqual(
            {m["publisher_device_id"] for m in messages}, {"sensor-01"}
        )

    def test_action_messages_do_not_trigger_rules(self) -> None:
        # 规则动作主题也命中自身过滤器：若递归触发将无限生成消息。
        self.service.create_rule({
            "rule_id": "loop",
            "topic_filter": "chain/#",
            "enabled": True,
            "condition": {"path": ["v"], "operator": "gte", "value": 0},
            "action": {"topic": "chain/step", "payload": {"v": 1}, "qos": 0},
        })
        publisher = self.connect(client_id="pub")
        listener = self.listener("chain/#")
        self.publish(publisher, "chain/start", {"v": 1})
        messages = self.poll(listener)["messages"]
        # 原消息加一条动作消息；动作消息不再级联触发规则。
        self.assertEqual([m["topic"] for m in messages],
                         ["chain/start", "chain/step"])

    def test_action_delivery_uses_existing_qos_semantics(self) -> None:
        self.rule("r1", action_qos=1, action_payload={"alert": True})
        publisher = self.connect(client_id="pub")
        listener = self.listener("alerts/#")
        self.publish(publisher, "sensor/room1/data", {"temp": 31})
        first = self.poll(listener)["messages"]
        self.assertEqual(len(first), 1)
        message = first[0]
        self.assertEqual(message["qos"], 1)
        self.assertFalse(message["dup"])
        self.assertIn("delivery_id", message)
        delivery_id = message["delivery_id"]
        # 未确认前重投带 dup=true，message_id 不变。
        redelivered = self.poll(listener)["messages"]
        self.assertEqual(len(redelivered), 1)
        self.assertTrue(redelivered[0]["dup"])
        self.assertEqual(redelivered[0]["message_id"], message["message_id"])
        self.assertEqual(redelivered[0]["delivery_id"], delivery_id)
        self.assertEqual(self.ack(listener, [delivery_id]), {"acked_count": 1})
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_action_uses_action_qos_not_original_qos(self) -> None:
        self.rule("r1", action_qos=1)
        publisher = self.connect(client_id="pub")
        listener = self.listener("alerts/#")
        # 原发布 QoS 0，动作按规则配置的 QoS 1 投递。
        self.publish(publisher, "sensor/room1/data", {"temp": 31}, qos=0)
        message = self.poll(listener)["messages"][0]
        self.assertEqual(message["qos"], 1)

    def test_backpressure_original_and_actions_share_queue(self) -> None:
        self.rule("r1", action_topic="alerts/one")
        self.rule("r2", action_topic="alerts/two")
        publisher = self.connect(client_id="pub")
        listener_session = self.connect(device_id="sensor-02", client_id="sub")
        self.subscribe(listener_session, "sensor/#")
        self.subscribe(listener_session, "alerts/#")
        self.publish(publisher, "sensor/room1/data", {"temp": 31})
        first = self.poll(listener_session, max_messages=1)["messages"]
        self.assertEqual([m["topic"] for m in first], ["sensor/room1/data"])
        rest = self.poll(listener_session)["messages"]
        self.assertEqual([m["topic"] for m in rest],
                         ["alerts/one", "alerts/two"])

    def test_matched_count_unaffected_by_actions(self) -> None:
        self.rule("r1")
        publisher = self.connect(client_id="pub")
        # 原主题的订阅者计入 matched_count；动作订阅者不计。
        original_listener = self.listener("sensor/#", client_id="sub-orig")
        action_listener = self.listener("alerts/#", client_id="sub-act")
        result = self.publish(publisher, "sensor/room1/data", {"temp": 31})
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(len(self.poll(original_listener)["messages"]), 1)
        self.assertEqual(len(self.poll(action_listener)["messages"]), 1)

    # ------------------------------------------------------------------
    # path 语义
    # ------------------------------------------------------------------

    def test_nested_path(self) -> None:
        self.rule("r1", path=("a", "b", "c"), operator="eq", value=7,
                  action_topic="out/x")
        publisher = self.connect(client_id="pub")
        listener = self.listener("out/#")
        self.publish(publisher, "sensor/room1/data",
                     {"a": {"b": {"c": 7}}})
        self.assertEqual(len(self.poll(listener)["messages"]), 1)
        self.publish(publisher, "sensor/room1/data", {"a": {"b": {"c": 8}}})
        self.assertEqual(len(self.poll(listener)["messages"]), 0)

    def test_path_missing_does_not_match(self) -> None:
        self.rule("r1", path=("a", "b"))
        publisher = self.connect(client_id="pub")
        listener = self.listener("alerts/#")
        for payload in ({}, {"a": {}}, {"a": None}, {"x": 1}):
            with self.subTest(payload=payload):
                self.publish(publisher, "sensor/room1/data", payload)
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_path_through_non_object_does_not_match(self) -> None:
        self.rule("r1", path=("a", "b"))
        publisher = self.connect(client_id="pub")
        listener = self.listener("alerts/#")
        # 数组是 JSON 对象下钻的终止：遇到非对象不命中。
        self.publish(publisher, "sensor/room1/data", {"a": [{"b": 1}]})
        self.publish(publisher, "sensor/room1/data", {"a": 5})
        self.publish(publisher, "sensor/room1/data", {"a": "str"})
        self.publish(publisher, "sensor/room1/data", {"a": True})
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_leaf_may_be_any_type_but_numeric_ops_require_number(self) -> None:
        self.rule("r1", path=("v",), operator="gt", value=0,
                  action_topic="out/x")
        publisher = self.connect(client_id="pub")
        listener = self.listener("out/#")
        # 终点存在但不是非布尔数字：大小比较不命中。
        for payload in ({"v": "1"}, {"v": None}, {"v": [1]}, {"v": {"x": 1}},
                        {"v": True}):
            with self.subTest(payload=payload):
                self.publish(publisher, "sensor/room1/data", payload)
        self.assertEqual(self.poll(listener)["messages"], [])
        # int 与 float 均可。
        self.publish(publisher, "sensor/room1/data", {"v": 1})
        self.publish(publisher, "sensor/room1/data", {"v": 1.5})
        self.assertEqual(len(self.poll(listener)["messages"]), 2)

    def test_payload_not_object_never_matches(self) -> None:
        self.rule("r1", path=("v",), operator="eq", value=1,
                  action_topic="out/x")
        publisher = self.connect(client_id="pub")
        listener = self.listener("out/#")
        for payload in (None, 1, "str", [1], True):
            with self.subTest(payload=payload):
                self.publish(publisher, "sensor/room1/data", payload)
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_path_keys_are_sensitive(self) -> None:
        self.rule("r1", path=("Temp",), action_topic="out/x")
        publisher = self.connect(client_id="pub")
        listener = self.listener("out/#")
        self.publish(publisher, "sensor/room1/data", {"Temp": 99})
        self.assertEqual(len(self.poll(listener)["messages"]), 1)
        self.publish(publisher, "sensor/room1/data", {"temp": 99})
        self.assertEqual(len(self.poll(listener)["messages"]), 0)

    # ------------------------------------------------------------------
    # 保留消息、鉴权与生命周期边界
    # ------------------------------------------------------------------

    def test_retained_replay_does_not_trigger_rules(self) -> None:
        self.rule("r1")
        publisher = self.connect(client_id="pub")
        # 先发布保留消息（此刻尚无订阅者，规则即便评估也没有动作接收者；
        # 随后新建订阅触发回放）。
        self.publish(publisher, "sensor/room1/data", {"temp": 99}, retain=True)
        late = self.connect(device_id="sensor-02", client_id="late")
        self.subscribe(late, "sensor/+/data")
        self.subscribe(late, "alerts/#")
        messages = self.poll(late)["messages"]
        # 只有保留回放本身，没有动作消息。
        self.assertEqual([m["topic"] for m in messages], ["sensor/room1/data"])
        self.assertTrue(messages[0]["retained"])

    def test_live_retained_publish_still_evaluates_rules(self) -> None:
        self.rule("r1")
        publisher = self.connect(client_id="pub")
        listener = self.listener("alerts/#")
        # 带 retain=true 的实时发布仍然评估规则（动作是普通非保留消息）。
        self.publish(publisher, "sensor/room1/data", {"temp": 99}, retain=True)
        messages = self.poll(listener)["messages"]
        self.assertEqual(len(messages), 1)
        self.assertNotIn("retained", messages[0])

    def test_rules_evaluated_after_successful_auth_only(self) -> None:
        self.rule("r1")
        listener = self.listener("alerts/#")
        # 鉴权失败：不评估规则、不产生动作。
        with self.assertRaises(ServiceError) as ctx:
            self.service.publish_message(
                "not-a-session",
                {"session_token": "x", "topic": "sensor/room1/data",
                 "payload": {"temp": 99}},
            )
        self.assertEqual(ctx.exception.code, "session_not_found")
        self.assertEqual(self.poll(listener)["messages"], [])

        session = self.connect()
        with self.assertRaises(ServiceError) as ctx:
            self.service.publish_message(
                session["session_id"],
                {"session_token": "wrong", "topic": "sensor/room1/data",
                 "payload": {"temp": 99}},
            )
        self.assertEqual(ctx.exception.code, "invalid_session_token")
        self.assertEqual(self.poll(listener)["messages"], [])

        # 字段校验失败同样不评估规则。
        with self.assertRaises(ServiceError):
            self.service.publish_message(
                session["session_id"],
                {"session_token": session["session_token"], "topic": "sensor/+",
                 "payload": {"temp": 99}},
            )
        self.assertEqual(self.poll(listener)["messages"], [])

    def test_rules_survive_revoke_and_session_offline(self) -> None:
        self.rule("r1")
        publisher = self.connect(client_id="pub")
        listener = self.listener("alerts/#")
        self.publish(publisher, "sensor/room1/data", {"temp": 99})
        self.assertEqual(len(self.poll(listener)["messages"]), 1)
        # 吊销动作接收设备不删除规则；发布者重连后规则仍生效。
        self.service.revoke_device("sensor-02")
        publisher2 = self.connect(client_id="pub2")
        fresh = self.connect(device_id="sensor-01", client_id="other-sub")
        self.subscribe(fresh, "alerts/#")
        self.publish(publisher2, "sensor/room1/data", {"temp": 99})
        self.assertEqual(len(self.poll(fresh)["messages"]), 1)
        # 规则列表完好。
        self.assertEqual(len(self.service.list_rules()["rules"]), 1)

    def test_action_payload_is_deep_copied_on_fire(self) -> None:
        self.service.create_rule({
            "rule_id": "rc",
            "topic_filter": "t",
            "enabled": True,
            "condition": {"path": ["v"], "operator": "eq", "value": 1},
            "action": {"topic": "out/x", "payload": {"nested": {"k": 1}},
                       "qos": 0},
        })
        publisher = self.connect(client_id="pub")
        listener = self.listener("out/#")
        self.publish(publisher, "t", {"v": 1})
        message = self.poll(listener)["messages"][0]
        message["payload"]["nested"]["k"] = 99
        # 再次命中时动作 payload 仍是规则定义的原值。
        self.publish(publisher, "t", {"v": 1})
        second = self.poll(listener)["messages"][0]
        self.assertEqual(second["payload"], {"nested": {"k": 1}})

    def test_create_rule_does_not_accept_topic_filter_as_action_topic(self) -> None:
        # 双保险：action.topic 必须是普通固定主题。
        with self.assertRaises(ServiceError) as ctx:
            self.service.create_rule({
                "rule_id": "rx",
                "topic_filter": "t",
                "enabled": True,
                "condition": {"path": ["v"], "operator": "eq", "value": 1},
                "action": {"topic": "a/+/b", "payload": 1, "qos": 0},
            })
        self.assertEqual(ctx.exception.code, "invalid_request")


if __name__ == "__main__":
    unittest.main()
