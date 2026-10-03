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
        # 每个用例使用全新的 Service，保证进程内状态互不影响。
        handler_cls = type("TestHandler", (Handler,), {"service": Service()})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        for device_id in ("sensor-1", "sensor-2"):
            status, _ = self.call(
                "POST", "/v1/devices",
                {"device_id": device_id, "display_name": "传感器"},
            )
            self.assertEqual(status, 201)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def call(self, method: str, path: str, body=None, raw_body: bytes | None = None):
        data = raw_body
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib_request.Request(self.url(path), data=data, headers=headers, method=method)
        try:
            with urllib_request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            status = exc.code
            exc.close()
            return status, payload

    def create_group(self, group_id="group-a", device_ids=("sensor-1", "sensor-2")):
        return self.call("POST", "/v1/device-groups", {
            "group_id": group_id,
            "device_ids": list(device_ids),
        })

    def create_batch(self, group_id="group-a", request_id="req-1", **overrides):
        body = {
            "command_name": "reboot",
            "payload": {"delay": 3},
            "ttl_seconds": 60,
            "request_id": request_id,
        }
        body.update(overrides)
        return self.call(
            "POST", f"/v1/device-groups/{group_id}/command-batches", body
        )

    def test_group_create_get_replace_flow(self) -> None:
        status, group = self.create_group()
        self.assertEqual(status, 201)
        self.assertEqual(group, {
            "group_id": "group-a",
            "device_ids": ["sensor-1", "sensor-2"],
            "version": 1,
        })

        status, fetched = self.call("GET", "/v1/device-groups/group-a")
        self.assertEqual(status, 200)
        self.assertEqual(fetched, group)

        status, replaced = self.call("PUT", "/v1/device-groups/group-a", {
            "device_ids": ["sensor-2"],
            "expected_version": 1,
        })
        self.assertEqual(status, 200)
        self.assertEqual(replaced, {
            "group_id": "group-a",
            "device_ids": ["sensor-2"],
            "version": 2,
        })

    def test_group_errors(self) -> None:
        self.create_group()
        status, body = self.create_group()
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "group_already_exists")

        status, body = self.call("GET", "/v1/device-groups/ghost")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "group_not_found")

        status, body = self.call("PUT", "/v1/device-groups/ghost", {"device_ids": []})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "group_not_found")

        status, body = self.call("PUT", "/v1/device-groups/group-a", {
            "device_ids": ["ghost"],
        })
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "device_not_found")

        status, body = self.call("PUT", "/v1/device-groups/group-a", {
            "device_ids": [],
            "expected_version": 9,
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "group_version_conflict")

        status, body = self.call("POST", "/v1/device-groups", {
            "group_id": "bad", "device_ids": ["sensor-1", "sensor-1"],
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        status, body = self.call(
            "POST", "/v1/device-groups", raw_body=b"not json"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

    def test_command_batch_flow(self) -> None:
        self.create_group()
        status, batch = self.create_batch()
        self.assertEqual(status, 202)
        self.assertTrue(batch["batch_id"])
        self.assertEqual(batch["group_version"], 1)
        self.assertEqual(
            [item["device_id"] for item in batch["commands"]],
            ["sensor-1", "sensor-2"],
        )

        status, fetched = self.call(
            "GET",
            f"/v1/device-groups/group-a/command-batches/{batch['batch_id']}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(fetched["batch_id"], batch["batch_id"])
        self.assertEqual(
            [c["device_id"] for c in fetched["commands"]],
            ["sensor-1", "sensor-2"],
        )
        self.assertEqual(fetched["counts"]["queued"], 2)

        # 幂等重放返回原批次。
        status, replay = self.create_batch()
        self.assertEqual(status, 202)
        self.assertEqual(replay["batch_id"], batch["batch_id"])

        # 子命令可通过既有单命令查询入口读取。
        command = batch["commands"][0]
        status, snapshot = self.call(
            "GET",
            f"/v1/devices/{command['device_id']}/commands/{command['command_id']}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["status"], "queued")

    def test_command_batch_errors(self) -> None:
        self.create_group()
        status, body = self.create_batch(group_id="ghost")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "group_not_found")

        status, body = self.create_batch(expected_group_version=5)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "group_version_conflict")

        status, body = self.create_batch(request_id="")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")

        status, batch = self.create_batch()
        self.assertEqual(status, 202)
        status, body = self.create_batch(command_name="shutdown")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "batch_request_conflict")

        status, body = self.call(
            "GET", "/v1/device-groups/group-a/command-batches/nope"
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "batch_not_found")

        # 吊销成员后整批拒绝，不创建部分命令。
        self.call("POST", "/v1/devices/sensor-2/revoke")
        status, body = self.create_batch(request_id="req-2")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "group_contains_revoked_device")

        # 已创建批次中该成员的子命令按既有规则取消。
        status, fetched = self.call(
            "GET",
            f"/v1/device-groups/group-a/command-batches/{batch['batch_id']}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(fetched["counts"]["cancelled"], 1)
        self.assertEqual(fetched["counts"]["queued"], 1)


if __name__ == "__main__":
    unittest.main()
