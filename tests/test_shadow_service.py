import threading
import unittest

from devicefabric.service import Service, ServiceError


class ShadowServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.device = self.service.register_device(
            {"device_id": "sensor-01", "display_name": "一号传感器"}
        )

    def create_session(self, device_id="sensor-01", credential=None,
                       client_id="cli-1", keepalive=30):
        return self.service.create_session({
            "device_id": device_id,
            "credential": credential if credential is not None else self.device["credential"],
            "client_id": client_id,
            "keepalive_seconds": keepalive,
        })

    # ------------------------------------------------------------------
    # 初始快照
    # ------------------------------------------------------------------

    def test_shadow_initialized_empty_at_registration(self) -> None:
        shadow = self.service.get_shadow("sensor-01")
        self.assertEqual(shadow, {
            "device_id": "sensor-01",
            "version": 0,
            "desired": {},
            "reported": {},
            "delta": {},
            "updated_at": None,
        })

    def test_each_device_gets_independent_shadow(self) -> None:
        self.service.register_device({"device_id": "sensor-02", "display_name": "二号"})
        self.service.set_desired_shadow("sensor-01", {"state": {"a": 1}})
        other = self.service.get_shadow("sensor-02")
        self.assertEqual(other["version"], 0)
        self.assertEqual(other["desired"], {})

    def test_get_unknown_device_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_shadow("ghost")
        self.assertEqual(ctx.exception.code, "device_not_found")
        self.assertEqual(ctx.exception.status, 404)

    # ------------------------------------------------------------------
    # desired 写入
    # ------------------------------------------------------------------

    def test_set_desired_replaces_state_and_returns_full_snapshot(self) -> None:
        result = self.service.set_desired_shadow(
            "sensor-01", {"state": {"power": "on", "level": 3}}
        )
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["desired"], {"power": "on", "level": 3})
        self.assertEqual(result["reported"], {})
        self.assertEqual(result["delta"], {"power": "on", "level": 3})
        self.assertIsNotNone(result["updated_at"])
        self.assertEqual(result["device_id"], "sensor-01")

        # 再次写入整体替换，而非合并。
        result = self.service.set_desired_shadow("sensor-01", {"state": {"level": 4}})
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["desired"], {"level": 4})
        self.assertEqual(result["delta"], {"level": 4})

    def test_set_desired_same_state_still_increments_version(self) -> None:
        payload = {"state": {"a": 1}}
        first = self.service.set_desired_shadow("sensor-01", payload)
        second = self.service.set_desired_shadow("sensor-01", payload)
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertGreaterEqual(second["updated_at"], first["updated_at"])

    def test_set_desired_empty_object_also_increments(self) -> None:
        self.service.set_desired_shadow("sensor-01", {"state": {"a": 1}})
        result = self.service.set_desired_shadow("sensor-01", {"state": {}})
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["desired"], {})
        self.assertEqual(result["delta"], {})

    def test_set_desired_unknown_device_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.set_desired_shadow("ghost", {"state": {}})
        self.assertEqual(ctx.exception.code, "device_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_revoked_device_can_still_read_and_write_desired(self) -> None:
        self.service.set_desired_shadow("sensor-01", {"state": {"a": 1}})
        self.service.revoke_device("sensor-01")
        shadow = self.service.get_shadow("sensor-01")
        self.assertEqual(shadow["desired"], {"a": 1})
        result = self.service.set_desired_shadow("sensor-01", {"state": {"b": 2}})
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["desired"], {"b": 2})

    def test_credential_rotation_and_session_offline_keep_shadow(self) -> None:
        session = self.create_session()
        self.service.set_desired_shadow("sensor-01", {"state": {"a": 1}})
        self.service.report_shadow(
            session["session_id"],
            {"session_token": session["session_token"], "state": {"a": 1}},
        )
        rotated = self.service.rotate_credential("sensor-01")
        # 同 client_id 用新凭据重连，旧会话离线；影子保留。
        new_session = self.create_session(credential=rotated["credential"])
        self.assertNotEqual(new_session["session_id"], session["session_id"])
        shadow = self.service.get_shadow("sensor-01")
        self.assertEqual(shadow["version"], 2)
        self.assertEqual(shadow["desired"], {"a": 1})
        self.assertEqual(shadow["reported"], {"a": 1})
        self.assertEqual(shadow["delta"], {})

        # 吊销关闭会话同样不删除影子。
        self.service.revoke_device("sensor-01")
        shadow = self.service.get_shadow("sensor-01")
        self.assertEqual(shadow["version"], 2)
        self.assertEqual(shadow["desired"], {"a": 1})
        self.assertEqual(shadow["reported"], {"a": 1})

    # ------------------------------------------------------------------
    # reported 写入
    # ------------------------------------------------------------------

    def test_report_reported_requires_online_owning_session(self) -> None:
        session = self.create_session()
        result = self.service.report_shadow(
            session["session_id"],
            {"session_token": session["session_token"], "state": {"power": "on"}},
        )
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["reported"], {"power": "on"})
        self.assertEqual(result["desired"], {})
        self.assertEqual(result["delta"], {})
        self.assertIsNotNone(result["updated_at"])

    def test_report_by_session_updates_its_own_device_only(self) -> None:
        second = self.service.register_device(
            {"device_id": "sensor-02", "display_name": "二号"}
        )
        self.service.set_desired_shadow("sensor-01", {"state": {"k": "d1"}})
        session_two = self.create_session(
            device_id="sensor-02", credential=second["credential"], client_id="cli-2"
        )
        result = self.service.report_shadow(session_two["session_id"], {
            "session_token": session_two["session_token"],
            "state": {"k": "d2"},
        })
        self.assertEqual(result["device_id"], "sensor-02")
        self.assertEqual(result["reported"], {"k": "d2"})
        # sensor-01 的影子不受影响。
        shadow_one = self.service.get_shadow("sensor-01")
        self.assertEqual(shadow_one["reported"], {})
        self.assertEqual(shadow_one["version"], 1)

    def test_report_unknown_session_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.report_shadow("ghost", {"session_token": "x", "state": {}})
        self.assertEqual(ctx.exception.code, "session_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_report_wrong_token_is_invalid_session_token(self) -> None:
        session = self.create_session()
        with self.assertRaises(ServiceError) as ctx:
            self.service.report_shadow(
                session["session_id"], {"session_token": "wrong", "state": {}}
            )
        self.assertEqual(ctx.exception.code, "invalid_session_token")
        self.assertEqual(ctx.exception.status, 401)
        # 失败不改变影子。
        self.assertEqual(self.service.get_shadow("sensor-01")["version"], 0)

    def test_report_closed_or_expired_session_is_not_online(self) -> None:
        first = self.create_session()
        self.create_session()  # 取代第一个会话
        with self.assertRaises(ServiceError) as ctx:
            self.service.report_shadow(
                first["session_id"],
                {"session_token": first["session_token"], "state": {"a": 1}},
            )
        self.assertEqual(ctx.exception.code, "session_not_online")
        self.assertEqual(ctx.exception.status, 409)

        # 设备已吊销，在线会话被关闭，reported 不能再更新。
        session = self.create_session()
        self.service.revoke_device("sensor-01")
        with self.assertRaises(ServiceError) as ctx:
            self.service.report_shadow(
                session["session_id"],
                {"session_token": session["session_token"], "state": {"a": 1}},
            )
        self.assertEqual(ctx.exception.code, "session_not_online")
        self.assertEqual(self.service.get_shadow("sensor-01")["reported"], {})

    # ------------------------------------------------------------------
    # delta 计算
    # ------------------------------------------------------------------

    def test_delta_recursive_and_report_owned_only(self) -> None:
        self.service.set_desired_shadow("sensor-01", {"state": {
            "nested": {"a": 1, "b": 2, "c": {"x": 1}},
            "tags": [1, 2],
            "mode": "auto",
            "only_desired": 9,
        }})
        session = self.create_session()
        result = self.service.report_shadow(session["session_id"], {
            "session_token": session["session_token"],
            "state": {
                "nested": {"a": 1, "b": 3, "c": {"x": 1}, "extra": 7},
                "tags": [1, 2],
                "mode": "manual",
                "reported_only": "ignored",
            },
        })
        # nested.a 相同；nested.b 值不同（delta 取 desired 值）；
        # nested.c 完全一致不下钻出现；数组整体相等；mode 不同（取 desired
        # 值）；only_desired 缺失；reported 独有成员忽略。
        self.assertEqual(result["delta"], {
            "nested": {"b": 2},
            "mode": "auto",
            "only_desired": 9,
        })

    def test_delta_arrays_compared_whole(self) -> None:
        self.service.set_desired_shadow("sensor-01", {"state": {"arr": [1, {"a": 2}]}})
        session = self.create_session()
        # 数组即便内部结构相似也整体比较，不等则保留整个 desired 数组。
        result = self.service.report_shadow(session["session_id"], {
            "session_token": session["session_token"],
            "state": {"arr": [1, {"a": 3}]},
        })
        self.assertEqual(result["delta"], {"arr": [1, {"a": 2}]})

        result = self.service.report_shadow(session["session_id"], {
            "session_token": session["session_token"],
            "state": {"arr": [1, {"a": 2}]},
        })
        self.assertEqual(result["delta"], {})

    def test_delta_type_mismatch_takes_desired_value(self) -> None:
        self.service.set_desired_shadow("sensor-01", {"state": {"k": {"x": 1}}})
        session = self.create_session()
        result = self.service.report_shadow(session["session_id"], {
            "session_token": session["session_token"],
            "state": {"k": 5},
        })
        self.assertEqual(result["delta"], {"k": {"x": 1}})

    def test_delta_empty_when_cleared(self) -> None:
        self.service.set_desired_shadow("sensor-01", {"state": {"a": 1}})
        session = self.create_session()
        self.service.report_shadow(session["session_id"], {
            "session_token": session["session_token"], "state": {"a": 1},
        })
        result = self.service.set_desired_shadow("sensor-01", {"state": {}})
        self.assertEqual(result["delta"], {})

    # ------------------------------------------------------------------
    # expected_version 乐观并发
    # ------------------------------------------------------------------

    def test_expected_version_must_match_before_write(self) -> None:
        self.service.set_desired_shadow("sensor-01", {"state": {"a": 1}})
        # 当前 version 为 1；用过期版本写入冲突。
        with self.assertRaises(ServiceError) as ctx:
            self.service.set_desired_shadow(
                "sensor-01", {"state": {"b": 2}, "expected_version": 0}
            )
        self.assertEqual(ctx.exception.code, "shadow_version_conflict")
        self.assertEqual(ctx.exception.status, 409)
        shadow = self.service.get_shadow("sensor-01")
        self.assertEqual(shadow["version"], 1)
        self.assertEqual(shadow["desired"], {"a": 1})

        # 匹配则成功。
        result = self.service.set_desired_shadow(
            "sensor-01", {"state": {"b": 2}, "expected_version": 1}
        )
        self.assertEqual(result["version"], 2)

    def test_expected_version_zero_matches_initial_shadow(self) -> None:
        result = self.service.set_desired_shadow(
            "sensor-01", {"state": {"a": 1}, "expected_version": 0}
        )
        self.assertEqual(result["version"], 1)

    def test_report_expected_version_conflict_leaves_shadow_unchanged(self) -> None:
        session = self.create_session()
        self.service.set_desired_shadow("sensor-01", {"state": {"a": 1}})
        with self.assertRaises(ServiceError) as ctx:
            self.service.report_shadow(session["session_id"], {
                "session_token": session["session_token"],
                "state": {"a": 2},
                "expected_version": 0,
            })
        self.assertEqual(ctx.exception.code, "shadow_version_conflict")
        self.assertEqual(ctx.exception.status, 409)
        shadow = self.service.get_shadow("sensor-01")
        self.assertEqual(shadow["version"], 1)
        self.assertEqual(shadow["reported"], {})

    def test_concurrent_writes_with_same_expected_version_at_most_one_succeeds(
        self,
    ) -> None:
        results: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                self.service.set_desired_shadow(
                    "sensor-01",
                    {"state": {"v": threading.get_ident()}, "expected_version": 0},
                )
                outcome = "ok"
            except ServiceError as exc:
                outcome = exc.code
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("shadow_version_conflict"), 15)
        self.assertEqual(self.service.get_shadow("sensor-01")["version"], 1)

    # ------------------------------------------------------------------
    # 请求校验
    # ------------------------------------------------------------------

    def test_set_desired_invalid_payloads(self) -> None:
        bad_payloads = [
            "not-an-object",
            {},
            {"state": {}, "extra": 1},
            {"state": []},
            {"state": "x"},
            {"state": None},
            {"state": 1},
            {"state": {}, "expected_version": -1},
            {"state": {}, "expected_version": "0"},
            {"state": {}, "expected_version": 1.0},
            {"state": {}, "expected_version": True},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.set_desired_shadow("sensor-01", payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.service.get_shadow("sensor-01")["version"], 0)

    def test_report_invalid_payloads(self) -> None:
        session = self.create_session()
        token = session["session_token"]
        bad_payloads = [
            "not-an-object",
            {},
            {"session_token": token},
            {"state": {}},
            {"session_token": token, "state": {}, "extra": 1},
            {"session_token": 123, "state": {}},
            {"session_token": token, "state": []},
            {"session_token": token, "state": {}, "expected_version": -1},
            {"session_token": token, "state": {}, "expected_version": False},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.report_shadow(session["session_id"], payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.service.get_shadow("sensor-01")["version"], 0)

    def test_state_is_decopied_from_payload_and_snapshot(self) -> None:
        state = {"nested": {"a": 1}}
        result = self.service.set_desired_shadow("sensor-01", {"state": state})
        state["nested"]["a"] = 99
        self.assertEqual(self.service.get_shadow("sensor-01")["desired"],
                         {"nested": {"a": 1}})
        result["desired"]["nested"]["a"] = 99
        self.assertEqual(self.service.get_shadow("sensor-01")["desired"],
                         {"nested": {"a": 1}})


if __name__ == "__main__":
    unittest.main()
