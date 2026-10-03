import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urllib_request
from urllib.error import HTTPError

from devicefabric.server import Handler
from devicefabric.service import Service


class DeviceGroupHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        handler_cls = type("TestHandler", (Handler,), {"service": Service()})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.credentials = {}
        for device_id in ("dev-a", "dev-b", "dev-c"):
            status, device = self.call(
                "POST", "/v1/devices",
                {"device_id": device_id, "display_name": device_id},
            )
            self.assertEqual(status, 201)
            self.credentials[device_id] = device["credential"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def call(self, method: str, path: str, body=None, raw=None):
        headers = {}
        if raw is not None:
            data = raw
            headers["Content-Type"] = "application/json"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            data = b"{}"
        req = urllib_request.Request(
            self.url(path), data=data, headers=headers, method=method
        )
        try:
            with urllib_request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            status = exc.code
            exc.close()
            return status, payload

    def create_group(self, group_id="grp-1", device_ids=("dev-a", "dev-b")):
        return self.call("POST", "/v1/device-groups", {
            "group_id": group_id, "device_ids": list(device_ids),
        })

    def replace(self, group_id, device_ids, expected=None):
        body = {"device_ids": device_ids}
        if expected is not None:
            body["expected_version"] = expected
        return self.call("PUT", f"/v1/device-groups/{group_id}/members", body)

    def dispatch(self, group_id="grp-1", request_id="req-1", body=None,
                 expected=None):
        if body is None:
            body = {"request_id": request_id, "command_name": "reboot",
                    "payload": {"delay": 1}, "ttl_seconds": 60}
        if expected is not None:
            body = {**body, "expected_group_version": expected}
        return self.call(
            "POST", f"/v1/device-groups/{group_id}/command-batches", body
        )

    def get_batch(self, group_id, batch_id):
        return self.call(
            "GET",
            f"/v1/device-groups/{group_id}/command-batches/{batch_id}",
        )

    def create_session(self, device_id):
        status, session = self.call("POST", "/v1/device-sessions", {
            "device_id": device_id,
            "credential": self.credentials[device_id],
            "client_id": f"cli-{device_id}",
            "keepalive_seconds": 30,
        })
        self.assertEqual(status, 201)
        return session

    def poll(self, session, max_commands=10):
        return self.call(
            "POST",
            f"/v1/device-sessions/{session['session_id']}/commands/poll",
            {"session_token": session["session_token"],
             "max_commands": max_commands},
        )

    def ack(self, session, command_id, status="succeeded"):
        return self.call(
            "POST",
            f"/v1/device-sessions/{session['session_id']}"
            f"/commands/{command_id}/ack",
            {"session_token": session["session_token"], "status": status,
             "result": {"exit_code": 0}},
        )

    # ------------------------------------------------------------------
    # 组管理
    # ------------------------------------------------------------------

    def test_group_lifecycle(self) -> None:
        status, group = self.create_group(device_ids=("dev-c", "dev-a"))
        self.assertEqual(status, 201)
        self.assertEqual(group, {
            "group_id": "grp-1",
            "version": 1,
            "device_ids": ["dev-c", "dev-a"],
        })

        status, fetched = self.call("GET", "/v1/device-groups/grp-1")
        self.assertEqual(status, 200)
        self.assertEqual(fetched, group)

        status, body = self.replace("grp-1", ["dev-b"], expected=1)
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["device_ids"], ["dev-b"])

        # 相同成员也递增。
        status, body = self.replace("grp-1", ["dev-b"])
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 3)

    def test_group_errors(self) -> None:
        self.create_group()
        status, body = self.create_group()
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "group_already_exists")

        status, body = self.call("GET", "/v1/device-groups/ghost")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "group_not_found")

        status, body = self.replace("ghost", [])
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "group_not_found")

        status, body = self.replace("grp-1", ["ghost"], expected=1)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")

        status, body = self.replace("grp-1", ["dev-a"], expected=99)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "group_version_conflict")

        # 失败后组不变。
        status, body = self.call("GET", "/v1/device-groups/grp-1")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["device_ids"], ["dev-a", "dev-b"])

    def test_group_invalid_bodies(self) -> None:
        # 非 JSON 对象的顶层数组。
        status, body = self.call(
            "POST", "/v1/device-groups", None, raw=b"[1, 2]"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        bad_bodies = [
            {},
            {"device_ids": ["dev-a"]},
            {"group_id": "grp-1"},
            {"group_id": "grp-1", "device_ids": ["dev-a"], "extra": 1},
            {"group_id": "bad id", "device_ids": []},
            {"group_id": 1, "device_ids": []},
            {"group_id": "grp-1", "device_ids": ["dev-a", "dev-a"]},
            {"group_id": "grp-1", "device_ids": ["bad id"]},
        ]
        for body in bad_bodies:
            with self.subTest(body=body):
                status, payload = self.call("POST", "/v1/device-groups", body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")

        # 成员设备不存在是 404 且不建组。
        status, body = self.call("POST", "/v1/device-groups", {
            "group_id": "grp-x", "device_ids": ["ghost"],
        })
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")
        self.assertNotIn(
            "grp-x", self.server.RequestHandlerClass.service._groups
        )

        # 替换的各类非法请求。
        self.create_group()
        for body in [
            {"device_ids": [], "extra": 1},
            {"device_ids": ["dev-a", "dev-a"]},
            {"device_ids": [1]},
            {"device_ids": [], "expected_version": -1},
            {"device_ids": [], "expected_version": "1"},
            {"device_ids": [], "expected_version": True},
        ]:
            with self.subTest(body=body):
                status, payload = self.call(
                    "PUT", "/v1/device-groups/grp-1/members", body
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")
        status, body = self.call("GET", "/v1/device-groups/grp-1")
        self.assertEqual(body["version"], 1)

    def test_illegal_identifier_in_path_is_invalid_request(self) -> None:
        for method, path, body in [
            ("GET", "/v1/device-groups/bad%20id", None),
            ("PUT", "/v1/device-groups/bad%20id/members",
             {"device_ids": []}),
            ("POST", "/v1/device-groups/bad%20id/command-batches",
             {"request_id": "r", "command_name": "reboot",
              "payload": None, "ttl_seconds": 10}),
            ("GET", "/v1/device-groups/grp-1/command-batches/bad%20id", None),
        ]:
            with self.subTest(method=method, path=path):
                status, payload = self.call(method, path, body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")

    # ------------------------------------------------------------------
    # 命令批次
    # ------------------------------------------------------------------

    def test_dispatch_and_query_batch(self) -> None:
        self.create_group(device_ids=("dev-c", "dev-a"))
        status, batch = self.dispatch()
        self.assertEqual(status, 202)
        self.assertTrue(batch["batch_id"])
        self.assertEqual(batch["group_version"], 1)
        self.assertEqual(
            [item["device_id"] for item in batch["commands"]],
            ["dev-c", "dev-a"],
        )
        self.assertTrue(all(item["command_id"] for item in batch["commands"]))

        status, fetched = self.get_batch("grp-1", batch["batch_id"])
        self.assertEqual(status, 200)
        self.assertEqual(len(fetched["commands"]), 2)
        self.assertEqual(
            [c["device_id"] for c in fetched["commands"]],
            ["dev-c", "dev-a"],
        )
        self.assertEqual(fetched["status_counts"], {
            "queued": 2, "delivered": 0, "succeeded": 0, "failed": 0,
            "expired": 0, "cancelled": 0,
        })

    def test_empty_group_batch(self) -> None:
        self.create_group(group_id="grp-empty", device_ids=())
        status, batch = self.dispatch("grp-empty")
        self.assertEqual(status, 202)
        self.assertEqual(batch["commands"], [])
        self.assertEqual(batch["group_version"], 1)
        status, fetched = self.get_batch("grp-empty", batch["batch_id"])
        self.assertEqual(status, 200)
        self.assertEqual(fetched["commands"], [])
        self.assertEqual(fetched["status_counts"], {
            "queued": 0, "delivered": 0, "succeeded": 0, "failed": 0,
            "expired": 0, "cancelled": 0,
        })

    def test_batch_conflicts_and_not_found(self) -> None:
        self.create_group()
        status, body = self.dispatch("ghost")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "group_not_found")

        self.dispatch()
        status, body = self.dispatch(expected=99, request_id="req-v")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "group_version_conflict")

        self.call("POST", "/v1/devices/dev-a/revoke")
        # 吊销不移除成员，但含吊销成员的批次被拒绝。
        status, group = self.call("GET", "/v1/device-groups/grp-1")
        self.assertEqual(group["device_ids"], ["dev-a", "dev-b"])
        status, body = self.dispatch(request_id="req-2")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"],
                         "group_contains_revoked_device")

        status, body = self.get_batch("grp-1", "nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "batch_not_found")

    def test_batch_idempotency_and_conflict(self) -> None:
        self.create_group(device_ids=())
        body = {"request_id": "dup", "command_name": "reboot",
                "payload": None, "ttl_seconds": 10}
        status, first = self.dispatch(body=body)
        self.assertEqual(status, 202)
        status, again = self.dispatch(body=dict(body))
        self.assertEqual(status, 202)
        self.assertEqual(again["batch_id"], first["batch_id"])

        status, conflict = self.dispatch(body={**body, "ttl_seconds": 20})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["error"]["code"], "batch_request_conflict")

        # 同 request_id 不同组互不影响。
        self.create_group("grp-2", device_ids=())
        status, other = self.dispatch("grp-2", body=dict(body))
        self.assertEqual(status, 202)
        self.assertNotEqual(other["batch_id"], first["batch_id"])

    def test_batch_invalid_bodies(self) -> None:
        self.create_group(device_ids=())
        base = {"request_id": "r", "command_name": "reboot",
                "payload": None, "ttl_seconds": 10}
        bad_bodies = [
            "__empty__",  # 空体 -> JSON 解析失败
            {},
            {**base, "extra": 1},
            {**{k: v for k, v in base.items() if k != "request_id"}},
            {**base, "request_id": ""},
            {**base, "request_id": "x" * 65},
            {**base, "request_id": 5},
            {**{k: v for k, v in base.items() if k != "command_name"}},
            {**base, "command_name": "bad name"},
            {**{k: v for k, v in base.items() if k != "ttl_seconds"}},
            {**base, "ttl_seconds": 4},
            {**base, "ttl_seconds": True},
            {**base, "expected_group_version": -1},
            {**base, "expected_group_version": "1"},
        ]
        for body in bad_bodies:
            with self.subTest(body=body):
                if body == "__empty__":
                    status, payload = self.call(
                        "POST",
                        "/v1/device-groups/grp-1/command-batches",
                        None, raw=b"",
                    )
                else:
                    status, payload = self.dispatch(body=body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")
        service = self.server.RequestHandlerClass.service
        self.assertEqual(service._commands, {})
        self.assertEqual(service._batches, {})

    def test_member_changes_do_not_affect_old_batch(self) -> None:
        self.create_group()
        status, first = self.dispatch(request_id="req-1")
        self.assertEqual(status, 202)
        self.assertEqual(
            len(first["commands"]), 2
        )
        status, _ = self.replace("grp-1", ["dev-c"], expected=1)
        self.assertEqual(status, 200)
        status, old = self.get_batch("grp-1", first["batch_id"])
        self.assertEqual(status, 200)
        self.assertEqual(
            [c["device_id"] for c in old["commands"]], ["dev-a", "dev-b"]
        )
        self.assertEqual(old["group_version"], 1)

    def test_subcommands_flow_through_existing_endpoints(self) -> None:
        self.create_group(device_ids=("dev-a", "dev-b"))
        status, batch = self.dispatch()
        self.assertEqual(status, 202)
        first_id = batch["commands"][0]["command_id"]
        second_id = batch["commands"][1]["command_id"]

        # 子命令可经单命令查询入口读取。
        status, snapshot = self.call(
            "GET", f"/v1/devices/dev-a/commands/{first_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["status"], "queued")
        self.assertEqual(snapshot["command_name"], "reboot")
        self.assertEqual(snapshot["payload"], {"delay": 1})

        # 轮询、重领与确认全部沿用既有入口。
        session = self.create_session("dev-a")
        status, polled = self.poll(session)
        self.assertEqual(status, 200)
        self.assertEqual(polled["commands"][0]["command_id"], first_id)
        self.assertFalse(polled["commands"][0]["dup"])
        status, redelivered = self.poll(session)
        self.assertTrue(redelivered["commands"][0]["dup"])
        status, completed = self.ack(session, first_id)
        self.assertEqual(status, 200)
        self.assertEqual(completed["status"], "succeeded")

        # 另一成员尚未确认；吊销 dev-b 取消其非终态子命令。
        self.call("POST", "/v1/devices/dev-b/revoke")
        status, fetched = self.get_batch("grp-1", batch["batch_id"])
        self.assertEqual(status, 200)
        by_id = {c["command_id"]: c for c in fetched["commands"]}
        self.assertEqual(by_id[first_id]["status"], "succeeded")
        self.assertEqual(by_id[second_id]["status"], "cancelled")
        self.assertEqual(fetched["status_counts"], {
            "queued": 0, "delivered": 0, "succeeded": 1, "failed": 0,
            "expired": 0, "cancelled": 1,
        })


if __name__ == "__main__":
    unittest.main()
