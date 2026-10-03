import re
import threading
import unittest
from datetime import timedelta

from devicefabric.service import Service, ServiceError, _utc_now

RFC3339_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")


class ShadowServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Service()
        self.device = self.service.register_device(
            {"device_id": "sensor-01", "display_name": "一号传感器"}
        )

    def create_session(self, client_id="cli-1", keepalive=30, device_id="sensor-01",
                       credential=None):
        return self.service.create_session({
            "device_id": device_id,
            "credential": credential if credential is not None else self.device["credential"],
            "client_id": client_id,
            "keepalive_seconds": keepalive,
        })

    def force_expire(self, session_id):
        session = self.service._sessions[session_id]
        session["expires_at"] = _utc_now() - timedelta(seconds=1)

    # ------------------------------------------------------------------
    # 初始化与读取
    # ------------------------------------------------------------------

    def test_shadow_initialized_empty_on_registration(self) -> None:
        shadow = self.service.get_device_shadow("sensor-01")
        self.assertEqual(shadow["device_id"], "sensor-01")
        self.assertEqual(shadow["version"], 0)
        self.assertEqual(shadow["desired"], {})
        self.assertEqual(shadow["reported"], {})
        self.assertEqual(shadow["delta"], {})
        self.assertIsNone(shadow["updated_at"])

    def test_get_shadow_unknown_device_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.get_device_shadow("ghost")
        self.assertEqual(ctx.exception.code, "device_not_found")
        self.assertEqual(ctx.exception.status, 404)

    # ------------------------------------------------------------------
    # desired 写入
    # ------------------------------------------------------------------

    def test_update_desired_replaces_and_returns_full_snapshot(self) -> None:
        result = self.service.update_shadow_desired(
            "sensor-01", {"state": {"power": "on", "level": 3}}
        )
        self.assertEqual(result["device_id"], "sensor-01")
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["desired"], {"power": "on", "level": 3})
        self.assertEqual(result["reported"], {})
        # reported 尚为空，整个 desired 都构成差异。
        self.assertEqual(result["delta"], {"power": "on", "level": 3})
        self.assertIsNotNone(result["updated_at"])
        self.assertTrue(RFC3339_RE.match(result["updated_at"]))

        # 再次写入整体替换 desired（旧键消失），version 再加一。
        result = self.service.update_shadow_desired(
            "sensor-01", {"state": {"level": 4}}
        )
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["desired"], {"level": 4})

    def test_same_state_still_increments_version_and_timestamp(self) -> None:
        state = {"a": 1}
        first = self.service.update_shadow_desired("sensor-01", {"state": state})
        second = self.service.update_shadow_desired("sensor-01", {"state": dict(state)})
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertGreaterEqual(second["updated_at"], first["updated_at"])

    def test_update_desired_unknown_device_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.update_shadow_desired("ghost", {"state": {"a": 1}})
        self.assertEqual(ctx.exception.code, "device_not_found")
        self.assertEqual(ctx.exception.status, 404)

    # ------------------------------------------------------------------
    # reported 写入（会话鉴权）
    # ------------------------------------------------------------------

    def test_update_reported_replaces_owned_device_shadow(self) -> None:
        session = self.create_session()
        result = self.service.update_shadow_reported(
            session["session_id"],
            {"session_token": session["session_token"], "state": {"level": 3}},
        )
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["reported"], {"level": 3})
        self.assertEqual(result["desired"], {})
        # 无期望状态，差异为空。
        self.assertEqual(result["delta"], {})

        # reported 也是整体替换。
        result = self.service.update_shadow_reported(
            session["session_id"],
            {"session_token": session["session_token"], "state": {"temp": 20}},
        )
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["reported"], {"temp": 20})

    def test_reported_unknown_session_is_not_found(self) -> None:
        with self.assertRaises(ServiceError) as ctx:
            self.service.update_shadow_reported(
                "ghost", {"session_token": "x", "state": {"a": 1}}
            )
        self.assertEqual(ctx.exception.code, "session_not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_reported_wrong_token_is_unauthorized(self) -> None:
        session = self.create_session()
        with self.assertRaises(ServiceError) as ctx:
            self.service.update_shadow_reported(
                session["session_id"],
                {"session_token": "nope", "state": {"a": 1}},
            )
        self.assertEqual(ctx.exception.code, "invalid_session_token")
        self.assertEqual(ctx.exception.status, 401)

    def test_reported_closed_or_expired_session_is_not_online(self) -> None:
        first = self.create_session()
        self.create_session()  # 取代第一个会话
        with self.assertRaises(ServiceError) as ctx:
            self.service.update_shadow_reported(
                first["session_id"],
                {"session_token": first["session_token"], "state": {"a": 1}},
            )
        self.assertEqual(ctx.exception.code, "session_not_online")
        self.assertEqual(ctx.exception.status, 409)

        expired = self.create_session(client_id="cli-exp")
        self.force_expire(expired["session_id"])
        with self.assertRaises(ServiceError) as ctx:
            self.service.update_shadow_reported(
                expired["session_id"],
                {"session_token": expired["session_token"], "state": {"a": 1}},
            )
        self.assertEqual(ctx.exception.code, "session_not_online")
        self.assertEqual(ctx.exception.status, 409)
        # 失败的上报不改变影子。
        self.assertEqual(self.service.get_device_shadow("sensor-01")["version"], 0)

    # ------------------------------------------------------------------
    # delta 递归语义
    # ------------------------------------------------------------------

    def test_delta_is_recursive_and_ignores_reported_only_members(self) -> None:
        self.service.update_shadow_desired("sensor-01", {"state": {
            "a": 1,
            "b": {"c": 2, "d": 3},
            "e": [1, 2],
            "f": 5,
            "n": None,
        }})
        session = self.create_session()
        result = self.service.update_shadow_reported(
            session["session_id"],
            {"session_token": session["session_token"], "state": {
                "a": 1,                 # 一致，剔除
                "b": {"c": 2, "e": 9},  # c 一致，d 缺失；e 为 reported 独有
                "e": [1, 2],            # 数组整体一致，剔除
                "g": 7,                 # reported 独有，忽略
                "n": None,              # null 一致，剔除
            }},
        )
        self.assertEqual(result["delta"], {"b": {"d": 3}, "f": 5})

    def test_delta_compares_arrays_and_non_objects_whole(self) -> None:
        self.service.update_shadow_desired("sensor-01", {"state": {
            "arr": [1, 2, 3],
            "obj_as_scalar": {"x": 1},
            "scalar_as_obj": 5,
            "num": 10,
        }})
        session = self.create_session()
        result = self.service.update_shadow_reported(
            session["session_id"],
            {"session_token": session["session_token"], "state": {
                "arr": [1, 2],          # 数组不同：整体保留 desired
                "obj_as_scalar": 9,     # 类型不同：保留 desired 对象
                "scalar_as_obj": {"y": 2},
                "num": 10,              # 一致
            }},
        )
        self.assertEqual(result["delta"], {
            "arr": [1, 2, 3],
            "obj_as_scalar": {"x": 1},
            "scalar_as_obj": 5,
        })

    def test_delta_empty_when_fully_aligned(self) -> None:
        state = {"b": {"c": 2, "d": 3}, "list": [1, {"x": 2}]}
        self.service.update_shadow_desired("sensor-01", {"state": state})
        session = self.create_session()
        result = self.service.update_shadow_reported(
            session["session_id"],
            {"session_token": session["session_token"], "state": {
                "b": {"c": 2, "d": 3, "extra": "ignored"},
                "list": [1, {"x": 2}],
            }},
        )
        self.assertEqual(result["delta"], {})

    # ------------------------------------------------------------------
    # 乐观版本控制
    # ------------------------------------------------------------------

    def test_expected_version_match_succeeds(self) -> None:
        first = self.service.update_shadow_desired(
            "sensor-01", {"state": {"a": 1}}
        )
        self.assertEqual(first["version"], 1)
        second = self.service.update_shadow_desired(
            "sensor-01", {"state": {"a": 2}, "expected_version": 1}
        )
        self.assertEqual(second["version"], 2)

    def test_expected_version_mismatch_conflicts_and_leaves_shadow_unchanged(self) -> None:
        self.service.update_shadow_desired("sensor-01", {"state": {"a": 1}})
        before = self.service.get_device_shadow("sensor-01")
        with self.assertRaises(ServiceError) as ctx:
            self.service.update_shadow_desired(
                "sensor-01", {"state": {"a": 2}, "expected_version": 0}
            )
        self.assertEqual(ctx.exception.code, "shadow_version_conflict")
        self.assertEqual(ctx.exception.status, 409)
        after = self.service.get_device_shadow("sensor-01")
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["desired"], before["desired"])
        self.assertEqual(after["updated_at"], before["updated_at"])

    def test_concurrent_same_expected_version_at_most_one_succeeds(self) -> None:
        barrier = threading.Barrier(8)
        outcomes = []

        def writer() -> None:
            barrier.wait()
            try:
                self.service.update_shadow_desired(
                    "sensor-01", {"state": {"n": 1}, "expected_version": 0}
                )
                outcomes.append("ok")
            except ServiceError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=writer) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("shadow_version_conflict"), 7)
        self.assertEqual(self.service.get_device_shadow("sensor-01")["version"], 1)

    def test_invalid_expected_version_is_bad_request(self) -> None:
        for bad in (-1, 1.0, True, "0", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.update_shadow_desired(
                        "sensor-01", {"state": {"a": 1}, "expected_version": bad}
                    )
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.service.get_device_shadow("sensor-01")["version"], 0)

    # ------------------------------------------------------------------
    # 非法请求
    # ------------------------------------------------------------------

    def test_desired_invalid_payloads(self) -> None:
        for payload in (
            "not-an-object",
            {},
            {"state": {"a": 1}, "extra": 1},
            {"state": [1, 2]},
            {"state": "string"},
            {"state": None},
            {"state": 5},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.update_shadow_desired("sensor-01", payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        shadow = self.service.get_device_shadow("sensor-01")
        self.assertEqual(shadow["version"], 0)
        self.assertIsNone(shadow["updated_at"])

    def test_reported_invalid_payloads(self) -> None:
        session = self.create_session()
        sid = session["session_id"]
        token = session["session_token"]
        for payload in (
            "not-an-object",
            {},
            {"session_token": token},
            {"state": {"a": 1}},
            {"session_token": token, "state": {"a": 1}, "extra": 1},
            {"session_token": token, "state": [1]},
            {"session_token": 123, "state": {"a": 1}},
            {"session_token": token, "state": {"a": 1}, "expected_version": -2},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ServiceError) as ctx:
                    self.service.update_shadow_reported(sid, payload)
                self.assertEqual(ctx.exception.code, "invalid_request")
                self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.service.get_device_shadow("sensor-01")["version"], 0)

    # ------------------------------------------------------------------
    # 生命周期：吊销、轮换、会话离线均不删除影子
    # ------------------------------------------------------------------

    def test_shadow_survives_rotation_revocation_and_offline(self) -> None:
        self.service.update_shadow_desired("sensor-01", {"state": {"a": 1}})
        session = self.create_session()
        self.service.update_shadow_reported(
            session["session_id"],
            {"session_token": session["session_token"], "state": {"a": 1}},
        )
        snapshot = self.service.get_device_shadow("sensor-01")
        self.assertEqual(snapshot["version"], 2)

        # 凭据轮换不删除影子。
        rotated_credential = self.service.rotate_credential("sensor-01")["credential"]
        rotated = self.service.get_device_shadow("sensor-01")
        self.assertEqual(rotated["version"], 2)
        self.assertEqual(rotated["desired"], {"a": 1})
        self.assertEqual(rotated["reported"], {"a": 1})

        # 会话离线（重连取代）不删除影子。
        self.create_session(credential=rotated_credential)
        offline = self.service.get_device_shadow("sensor-01")
        self.assertEqual(offline["version"], 2)

        # 吊销不删除影子。
        self.service.revoke_device("sensor-01")
        revoked = self.service.get_device_shadow("sensor-01")
        self.assertEqual(revoked["version"], 2)
        self.assertEqual(revoked["desired"], {"a": 1})
        self.assertEqual(revoked["reported"], {"a": 1})

    def test_revoked_device_can_still_read_and_write_desired(self) -> None:
        self.service.revoke_device("sensor-01")
        readable = self.service.get_device_shadow("sensor-01")
        self.assertEqual(readable["version"], 0)
        written = self.service.update_shadow_desired(
            "sensor-01", {"state": {"a": 1}}
        )
        self.assertEqual(written["version"], 1)

    def test_revoked_device_session_closed_so_reported_blocked(self) -> None:
        session = self.create_session()
        self.service.revoke_device("sensor-01")
        with self.assertRaises(ServiceError) as ctx:
            self.service.update_shadow_reported(
                session["session_id"],
                {"session_token": session["session_token"], "state": {"a": 1}},
            )
        # 吊销已关闭会话：沿用会话鉴权，返回 session_not_online。
        self.assertEqual(ctx.exception.code, "session_not_online")
        self.assertEqual(ctx.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
