"""Core service surface for DeviceFabric.

除健康检查外，本模块实现设备注册与身份凭据生命周期（注册、查询、
认证、轮换与吊销），以及进程内连接会话与心跳保活。所有数据仅保存
在当前进程内，进程退出即清空。
"""

from __future__ import annotations

import copy
import hmac
import re
import secrets
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

from . import __version__

DEVICE_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
DEVICE_ID_MIN = 1
DEVICE_ID_MAX = 64
DISPLAY_NAME_MIN = 1
DISPLAY_NAME_MAX = 128
KEEPALIVE_MIN = 5
KEEPALIVE_MAX = 3600

TOPIC_MIN = 1
TOPIC_MAX = 256
POLL_MIN = 1
POLL_MAX = 100

RULE_PATH_MIN = 1
RULE_PATH_MAX = 16
RULE_OPERATORS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})

SESSION_ONLINE = "online"
SESSION_CLOSED = "closed"
SESSION_EXPIRED = "expired"

COMMAND_TTL_MIN = 5
COMMAND_TTL_MAX = 86400
COMMAND_QUEUED = "queued"
COMMAND_DELIVERED = "delivered"
COMMAND_NON_TERMINAL = frozenset({COMMAND_QUEUED, COMMAND_DELIVERED})
COMMAND_ACK_STATUSES = frozenset({"succeeded", "failed"})


class ServiceError(Exception):
    """业务错误，携带稳定的 error.code、message 与 HTTP 状态码。"""

    code = "invalid_request"
    status = 400

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rfc3339_utc(dt: datetime) -> str:
    """以 RFC 3339 格式输出 UTC 时间，规范时区后缀写作 ``Z``。"""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_device_id(value: object) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("device_id must be a string")
    if not DEVICE_ID_MIN <= len(value) <= DEVICE_ID_MAX or not DEVICE_ID_RE.match(value):
        raise ServiceError(
            "device_id must be 1-64 ASCII letters, digits, dots, underscores or hyphens"
        )
    return value


def _validate_display_name(value: object) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("display_name must be a string")
    length = len(value)
    if not DISPLAY_NAME_MIN <= length <= DISPLAY_NAME_MAX:
        raise ServiceError("display_name must be 1-128 Unicode characters")
    return value


def _validate_client_id(value: object) -> str:
    # client_id 沿用 device_id 的校验规则。
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("client_id must be a string")
    if not DEVICE_ID_MIN <= len(value) <= DEVICE_ID_MAX or not DEVICE_ID_RE.match(value):
        raise ServiceError(
            "client_id must be 1-64 ASCII letters, digits, dots, underscores or hyphens"
        )
    return value


def _validate_keepalive(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ServiceError("keepalive_seconds must be an integer")
    if not KEEPALIVE_MIN <= value <= KEEPALIVE_MAX:
        raise ServiceError(
            f"keepalive_seconds must be between {KEEPALIVE_MIN} and {KEEPALIVE_MAX}"
        )
    return value


def _split_topic_layers(value: str) -> list[str]:
    """按斜杠分层；空层（含首尾或连续斜杠）非法。"""
    return value.split("/")


def _validate_topic(value: object) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("topic must be a string")
    if not TOPIC_MIN <= len(value) <= TOPIC_MAX:
        raise ServiceError("topic must be 1-256 Unicode characters")
    if "\x00" in value:
        raise ServiceError("topic must not contain NUL characters")
    layers = _split_topic_layers(value)
    if not layers or any(layer == "" for layer in layers):
        raise ServiceError("topic layers must be non-empty")
    for layer in layers:
        if "+" in layer or "#" in layer:
            raise ServiceError("topic must not contain wildcard characters")
    return value


def _validate_topic_filter(value: object) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("topic_filter must be a string")
    if not TOPIC_MIN <= len(value) <= TOPIC_MAX:
        raise ServiceError("topic_filter must be 1-256 Unicode characters")
    if "\x00" in value:
        raise ServiceError("topic_filter must not contain NUL characters")
    layers = _split_topic_layers(value)
    if not layers or any(layer == "" for layer in layers):
        raise ServiceError("topic_filter layers must be non-empty")
    for index, layer in enumerate(layers):
        if "#" in layer:
            # # 只能独占最后一层且最多出现一次。
            if layer != "#" or index != len(layers) - 1:
                raise ServiceError(
                    "'#' must occupy the last layer alone and appear at most once"
                )
        if "+" in layer and layer != "+":
            raise ServiceError("'+' must occupy a whole layer alone")
    return value


def _topic_matches_filter(topic_layers: list[str], filter_layers: list[str]) -> bool:
    """MQTT 风格匹配：+ 匹配一层，末尾 # 匹配零层或多层。"""
    ti = 0
    for fi, layer in enumerate(filter_layers):
        if layer == "#":
            # # 必为最后一层，剩余任意层数（含零层）均匹配。
            return True
        if ti >= len(topic_layers):
            return False
        if layer != "+" and layer != topic_layers[ti]:
            return False
        ti += 1
    return ti == len(topic_layers)


def _validate_max_messages(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ServiceError("max_messages must be an integer")
    if not POLL_MIN <= value <= POLL_MAX:
        raise ServiceError(f"max_messages must be between {POLL_MIN} and {POLL_MAX}")
    return value


def _validate_group_id(value: object) -> str:
    # group_id 沿用 device_id 的标识规则。
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("group_id must be a string")
    if not DEVICE_ID_MIN <= len(value) <= DEVICE_ID_MAX or not DEVICE_ID_RE.match(value):
        raise ServiceError(
            "group_id must be 1-64 ASCII letters, digits, dots, underscores or hyphens"
        )
    return value


def _validate_device_ids(value: object) -> list[str]:
    # device_ids 为无重复的已注册设备标识数组；注册检查在锁内完成。
    if not isinstance(value, list):
        raise ServiceError("device_ids must be an array")
    device_ids: list[str] = []
    seen: set[str] = set()
    for item in value:
        device_id = _validate_device_id(item)
        if device_id in seen:
            raise ServiceError("device_ids must not contain duplicates")
        seen.add(device_id)
        device_ids.append(device_id)
    return device_ids


def _validate_request_id(value: object) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("request_id must be a string")
    if not 1 <= len(value) <= 64:
        raise ServiceError("request_id must be 1-64 characters")
    return value


def _validate_command_name(value: object) -> str:
    # command_name 沿用 device_id 的标识规则。
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("command_name must be a string")
    if not DEVICE_ID_MIN <= len(value) <= DEVICE_ID_MAX or not DEVICE_ID_RE.match(value):
        raise ServiceError(
            "command_name must be 1-64 ASCII letters, digits, dots, underscores or hyphens"
        )
    return value


def _validate_ttl_seconds(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ServiceError("ttl_seconds must be an integer")
    if not COMMAND_TTL_MIN <= value <= COMMAND_TTL_MAX:
        raise ServiceError(
            f"ttl_seconds must be between {COMMAND_TTL_MIN} and {COMMAND_TTL_MAX}"
        )
    return value


def _validate_max_commands(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ServiceError("max_commands must be an integer")
    if not POLL_MIN <= value <= POLL_MAX:
        raise ServiceError(f"max_commands must be between {POLL_MIN} and {POLL_MAX}")
    return value


def _validate_rule_id(value: object) -> str:
    # rule_id 沿用 device_id 的标识规则。
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("rule_id must be a string")
    if not DEVICE_ID_MIN <= len(value) <= DEVICE_ID_MAX or not DEVICE_ID_RE.match(value):
        raise ServiceError(
            "rule_id must be 1-64 ASCII letters, digits, dots, underscores or hyphens"
        )
    return value


def _is_json_number(value: object) -> bool:
    """JSON 数字：int/float 但排除布尔（bool 是 int 的子类）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json_equal(left: object, right: object) -> bool:
    """按 JSON 值深度比较；不区分 int/float 的数值相等（1 与 1.0 相等）。

    布尔值不与数字相等（True != 1），因此先单独排除跨类型情形。
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if _is_json_number(left) and _is_json_number(right):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return False
        return all(_json_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _validate_rule_path(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ServiceError("condition.path must be an array of non-empty strings")
    if not RULE_PATH_MIN <= len(value) <= RULE_PATH_MAX:
        raise ServiceError("condition.path must contain 1-16 items")
    path: list[str] = []
    for segment in value:
        if not isinstance(segment, str) or isinstance(segment, bool) or segment == "":
            raise ServiceError(
                "condition.path must contain only non-empty strings"
            )
        path.append(segment)
    return path


def _validate_rule_condition(value: object) -> dict:
    if not isinstance(value, dict):
        raise ServiceError("condition must be a JSON object")
    unknown = set(value) - {"path", "operator", "value"}
    if unknown:
        raise ServiceError(f"unknown field(s): {', '.join(sorted(unknown))}")
    for field in ("path", "operator", "value"):
        if field not in value:
            raise ServiceError(f"missing required field: condition.{field}")
    path = _validate_rule_path(value["path"])
    operator = value["operator"]
    if not isinstance(operator, str) or isinstance(operator, bool):
        raise ServiceError("condition.operator must be a string")
    if operator not in RULE_OPERATORS:
        raise ServiceError(
            "condition.operator must be one of: eq, ne, gt, gte, lt, lte"
        )
    condition_value = value["value"]
    # 大小比较的 value 必须是非布尔数字；eq/ne 允许任意 JSON 值。
    if operator in ("gt", "gte", "lt", "lte") and not _is_json_number(condition_value):
        raise ServiceError(
            f"condition.value must be a non-boolean number for operator {operator!r}"
        )
    return {"path": path, "operator": operator, "value": condition_value}


def _validate_rule_action(value: object) -> dict:
    if not isinstance(value, dict):
        raise ServiceError("action must be a JSON object")
    unknown = set(value) - {"topic", "payload", "qos"}
    if unknown:
        raise ServiceError(f"unknown field(s): {', '.join(sorted(unknown))}")
    for field in ("topic", "payload", "qos"):
        if field not in value:
            raise ServiceError(f"missing required field: action.{field}")
    # 动作目标为固定 topic，沿用普通发布主题规则（不得含通配符）。
    topic = _validate_topic(value["topic"])
    # payload 可为任意 JSON 值（null、标量、数组或对象），原样保留。
    payload = value["payload"]
    qos = value["qos"]
    if not isinstance(qos, int) or isinstance(qos, bool) or qos not in (0, 1):
        raise ServiceError("action.qos must be the integer 0 or 1")
    return {"topic": topic, "payload": payload, "qos": qos}


class Service:
    """进程内设备注册与凭据生命周期服务。"""

    name = "devicefabric"
    version = __version__

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: dict[str, dict] = {}
        # 记录本进程签发过的全部凭据，保证凭据在进程内绝不重复。
        self._issued_credentials: set[str] = set()
        self._sessions: dict[str, dict] = {}
        # 仅索引在线会话：(device_id, client_id) -> session_id。
        self._online_session_keys: dict[tuple[str, str], str] = {}
        # 记录本进程签发过的全部会话令牌，保证令牌在进程内绝不重复。
        self._issued_session_tokens: set[str] = set()
        # 记录本进程签发过的全部消息 ID，保证消息在进程内绝不重复。
        self._issued_message_ids: set[str] = set()
        # 记录本进程签发过的全部投递 ID，保证投递在进程内绝不重复。
        self._issued_delivery_ids: set[str] = set()
        # 按精确 topic 保存的最近一条保留消息（仅当前进程内，重启即清空）。
        # 每项为 {"message": ..., "qos": ..., "seq": ...}；seq 单调递增，
        # 记录各 topic 最近一次保留发布的先后顺序，供订阅回放排序。
        self._retained: dict[str, dict] = {}
        self._retained_sequence = 0
        # 设备影子：device_id -> {"version", "desired", "reported",
        # "updated_at"}。注册时初始化，仅存在当前进程内；吊销、凭据轮换
        # 或会话离线都不删除。
        self._shadows: dict[str, dict] = {}
        # 进程内规则引擎：rule_id -> 规则记录。另以有序列表保存 rule_id，
        # 保证列表与评估均按创建顺序进行。重启即清空；设备吊销或会话离线
        # 均不删除规则。
        self._rules: dict[str, dict] = {}
        self._rule_order: list[str] = []
        # 进程内持久会话：(device_id, client_id) -> 持久路由状态容器。
        # 仅由 clean_start=false 的连接创建；会话离线（超时、重连替换、
        # 吊销）后容器仍保留，直至同组合 clean_start=true 重连或设备吊销。
        # 进程重启即清空。
        self._persistent_sessions: dict[tuple[str, str], dict] = {}
        # 进程内设备命令：command_id -> 命令记录；另按设备保存创建顺序的
        # command_id 列表，供按创建顺序领取。命令仅存在当前进程内，重启
        # 即清空；凭据轮换不影响命令，设备吊销时非终态命令变为 cancelled。
        self._commands: dict[str, dict] = {}
        self._device_command_ids: dict[str, list[str]] = {}
        # 记录本进程签发过的全部命令 ID，保证命令在进程内绝不重复。
        self._issued_command_ids: set[str] = set()
        # 进程内设备组：group_id -> 组记录（device_ids 为有序、无重复的
        # 已注册设备标识，version 自 1 起单调递增）。仅存在当前进程内，
        # 重启即清空；吊销设备不移除成员。
        self._groups: dict[str, dict] = {}
        # 进程内命令批次：batch_id -> 批次记录；另按组保存 request_id ->
        # batch_id 索引，保证同组同 request_id 幂等。批次成员为受理时的
        # 组快照，之后的成员替换不影响已创建批次。
        self._batches: dict[str, dict] = {}
        self._group_request_ids: dict[str, dict[str, str]] = {}
        # 记录本进程签发过的全部批次 ID，保证批次在进程内绝不重复。
        self._issued_batch_ids: set[str] = set()

    @staticmethod
    def _new_routes_locked() -> dict:
        """一份路由状态：订阅过滤器集合、待取队列、未确认投递与确认历史。"""
        return {
            "subscriptions": set(),
            "queue": deque(),
            "unacked": {},
            "acked": set(),
        }

    def health(self) -> dict[str, str]:
        return {"status": "ok", "service": self.name, "version": self.version}

    @staticmethod
    def _public_device(device: dict) -> dict:
        return {
            "device_id": device["device_id"],
            "display_name": device["display_name"],
            "active": device["active"],
            "created_at": device["created_at"],
            "credential_version": device["credential_version"],
        }

    def _mint_credential_locked(self) -> str:
        credential = secrets.token_urlsafe(32)
        while not credential or credential in self._issued_credentials:
            credential = secrets.token_urlsafe(32)
        self._issued_credentials.add(credential)
        return credential

    def register_device(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        unknown = set(payload) - {"device_id", "display_name"}
        if unknown:
            raise ServiceError(f"unknown field(s): {', '.join(sorted(unknown))}")
        if "device_id" not in payload:
            raise ServiceError("missing required field: device_id")
        if "display_name" not in payload:
            raise ServiceError("missing required field: display_name")
        device_id = _validate_device_id(payload["device_id"])
        display_name = _validate_display_name(payload["display_name"])

        with self._lock:
            if device_id in self._devices:
                raise ServiceError(
                    f"device {device_id!r} already exists",
                    code="device_already_exists",
                    status=409,
                )
            credential = self._mint_credential_locked()
            device = {
                "device_id": device_id,
                "display_name": display_name,
                "active": True,
                "created_at": _rfc3339_utc(_utc_now()),
                "credential_version": 1,
                "credential": credential,
            }
            self._devices[device_id] = device
            # 注册即初始化空影子：version 为 0、三个状态为空对象、
            # updated_at 为 None。
            self._shadows[device_id] = {
                "version": 0,
                "desired": {},
                "reported": {},
                "updated_at": None,
            }
            result = self._public_device(device)
            result["credential"] = credential
            return result

    def get_device(self, device_id: str) -> dict:
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                raise ServiceError(
                    f"device {device_id!r} not found",
                    code="device_not_found",
                    status=404,
                )
            return self._public_device(device)

    def authenticate(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        unknown = set(payload) - {"device_id", "credential"}
        if unknown:
            raise ServiceError(f"unknown field(s): {', '.join(sorted(unknown))}")
        if "device_id" not in payload:
            raise ServiceError("missing required field: device_id")
        if "credential" not in payload:
            raise ServiceError("missing required field: credential")
        device_id = payload["device_id"]
        credential = payload["credential"]
        if not isinstance(device_id, str) or not isinstance(credential, str):
            raise ServiceError("device_id and credential must be strings")

        with self._lock:
            device = self._devices.get(device_id)
            valid = (
                device is not None
                and device["active"]
                and bool(credential)
                and hmac.compare_digest(
                    device["credential"].encode("utf-8"), credential.encode("utf-8")
                )
            )
            if not valid:
                raise ServiceError(
                    "invalid credential", code="invalid_credential", status=401
                )
            return {"authenticated": True}

    def rotate_credential(self, device_id: str) -> dict:
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                raise ServiceError(
                    f"device {device_id!r} not found",
                    code="device_not_found",
                    status=404,
                )
            if not device["active"]:
                raise ServiceError(
                    f"device {device_id!r} is revoked",
                    code="device_revoked",
                    status=409,
                )
            credential = self._mint_credential_locked()
            device["credential"] = credential
            device["credential_version"] += 1
            result = self._public_device(device)
            result["credential"] = credential
            return result

    def revoke_device(self, device_id: str) -> dict:
        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                raise ServiceError(
                    f"device {device_id!r} not found",
                    code="device_not_found",
                    status=404,
                )
            # 吊销是幂等的：重复调用保持状态与版本不变。
            device["active"] = False
            # 吊销立即关闭该设备的全部在线会话。
            for key, session_id in list(self._online_session_keys.items()):
                if key[0] == device_id:
                    self._close_session_locked(self._sessions[session_id], "device_revoked")
            # 同时清除该设备的全部持久会话状态（订阅、离线队列、未确认
            # 投递与确认历史）；凭据轮换不触发本路径。
            for key in list(self._persistent_sessions):
                if key[0] == device_id:
                    del self._persistent_sessions[key]
            # 吊销使该设备全部非终态命令变为 cancelled；已终态（含已确认、
            # 已过期）的命令保持不变。凭据轮换不影响命令。
            now = _utc_now()
            for command_id in self._device_command_ids.get(device_id, []):
                command = self._commands[command_id]
                self._expire_command_locked(command, now)
                if command["status"] in COMMAND_NON_TERMINAL:
                    command["status"] = "cancelled"
            return self._public_device(device)

    # ------------------------------------------------------------------
    # 连接会话与心跳保活
    # ------------------------------------------------------------------

    @staticmethod
    def _public_session(session: dict) -> dict:
        """会话快照，绝不包含 session_token。"""
        return {
            "session_id": session["session_id"],
            "device_id": session["device_id"],
            "client_id": session["client_id"],
            "connected_at": _rfc3339_utc(session["connected_at"]),
            "last_seen_at": _rfc3339_utc(session["last_seen_at"]),
            "expires_at": _rfc3339_utc(session["expires_at"]),
            "online": session["state"] == SESSION_ONLINE,
            "state": session["state"],
            "reason": session["reason"],
        }

    def _mint_session_id_locked(self) -> str:
        session_id = secrets.token_urlsafe(18)
        while session_id in self._sessions:
            session_id = secrets.token_urlsafe(18)
        return session_id

    def _mint_session_token_locked(self) -> str:
        token = secrets.token_urlsafe(32)
        while not token or token in self._issued_session_tokens:
            token = secrets.token_urlsafe(32)
        self._issued_session_tokens.add(token)
        return token

    def _detach_routes_locked(self, session: dict) -> None:
        """会话离线时的路由状态收尾。

        临时会话：订阅、待取消息、未确认投递与确认历史立即失效，且不被
        新会话继承。持久会话：订阅与 QoS 1 待投递/未确认/确认历史随共享
        容器保留，但 QoS 0 不属于持久会话状态（含尚未拉取的 QoS 0 保留
        回放），离线时丢弃，等待同组合以 clean_start=false 重连。
        """
        if not session.get("persistent"):
            routes = session["routes"]
            routes["subscriptions"].clear()
            routes["queue"].clear()
            routes["unacked"].clear()
            routes["acked"].clear()
            return
        routes = session["routes"]
        routes["queue"] = deque(
            entry for entry in routes["queue"] if entry["qos"] == 1
        )
        session["queue"] = routes["queue"]

    def _expire_if_timed_out_locked(self, session: dict, now: datetime) -> bool:
        """在线会话超过 expires_at 即转为 expired，不可恢复。"""
        if session["state"] == SESSION_ONLINE and now > session["expires_at"]:
            session["state"] = SESSION_EXPIRED
            session["reason"] = "keepalive_timeout"
            self._online_session_keys.pop(
                (session["device_id"], session["client_id"]), None
            )
            self._detach_routes_locked(session)
            return True
        return False

    def _close_session_locked(self, session: dict, reason: str) -> None:
        session["state"] = SESSION_CLOSED
        session["reason"] = reason
        self._online_session_keys.pop(
            (session["device_id"], session["client_id"]), None
        )
        self._detach_routes_locked(session)

    def create_session(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        unknown = set(payload) - {
            "device_id", "credential", "client_id", "keepalive_seconds", "clean_start"
        }
        if unknown:
            raise ServiceError(f"unknown field(s): {', '.join(sorted(unknown))}")
        for field in ("device_id", "credential", "client_id", "keepalive_seconds"):
            if field not in payload:
                raise ServiceError(f"missing required field: {field}")
        device_id = _validate_device_id(payload["device_id"])
        client_id = _validate_client_id(payload["client_id"])
        credential = payload["credential"]
        if not isinstance(credential, str):
            raise ServiceError("credential must be a string")
        keepalive = _validate_keepalive(payload["keepalive_seconds"])
        # 缺省或显式 true：沿用既有临时会话；仅显式 JSON 布尔值可被接受，
        # 非布尔值在触碰任何会话状态之前即拒绝。
        clean_start = payload.get("clean_start", True)
        if not isinstance(clean_start, bool):
            raise ServiceError("clean_start must be a boolean")

        with self._lock:
            device = self._devices.get(device_id)
            valid = (
                device is not None
                and device["active"]
                and bool(credential)
                and hmac.compare_digest(
                    device["credential"].encode("utf-8"), credential.encode("utf-8")
                )
            )
            if not valid:
                raise ServiceError(
                    "invalid credential", code="invalid_credential", status=401
                )
            now = _utc_now()
            key = (device_id, client_id)
            previous_id = self._online_session_keys.get(key)
            if previous_id is not None:
                previous = self._sessions[previous_id]
                # 同设备同 client_id 重连：旧会话若已超时按超时过期，
                # 否则标记为被新会话取代。
                if not self._expire_if_timed_out_locked(previous, now):
                    self._close_session_locked(previous, "replaced")
            if clean_start:
                # 临时会话不继承任何状态；同组合此前的持久状态全部丢弃。
                self._persistent_sessions.pop(key, None)
                routes = self._new_routes_locked()
                persistent = False
            else:
                # 复用该组合的持久路由状态；首次连接时建立空状态。
                routes = self._persistent_sessions.get(key)
                if routes is None:
                    routes = self._new_routes_locked()
                    self._persistent_sessions[key] = routes
                persistent = True
            session = {
                "session_id": self._mint_session_id_locked(),
                "token": self._mint_session_token_locked(),
                "device_id": device_id,
                "client_id": client_id,
                "keepalive_seconds": keepalive,
                "connected_at": now,
                "last_seen_at": now,
                "expires_at": now + timedelta(seconds=keepalive),
                "state": SESSION_ONLINE,
                "reason": None,
                # 持久会话与离线容器共享同一份路由状态；临时会话持有
                # 仅本会话可见的独立状态。
                "persistent": persistent,
                "routes": routes,
                # 订阅过滤器集合（幂等、无副本）与待取消息队列。
                "subscriptions": routes["subscriptions"],
                "queue": routes["queue"],
                # QoS 1 已投递未确认的投递记录（按首次投递顺序）与已确认历史。
                "unacked": routes["unacked"],
                "acked": routes["acked"],
            }
            self._sessions[session["session_id"]] = session
            self._online_session_keys[key] = session["session_id"]
            result = self._public_session(session)
            # session_token 仅在创建响应中返回一次。
            result["session_token"] = session["token"]
            return result

    def get_session(self, session_id: str) -> dict:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ServiceError(
                    f"session {session_id!r} not found",
                    code="session_not_found",
                    status=404,
                )
            self._expire_if_timed_out_locked(session, _utc_now())
            return self._public_session(session)

    def heartbeat_session(self, session_id: str, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        unknown = set(payload) - {"session_token"}
        if unknown:
            raise ServiceError(f"unknown field(s): {', '.join(sorted(unknown))}")
        if "session_token" not in payload:
            raise ServiceError("missing required field: session_token")
        token = payload["session_token"]
        if not isinstance(token, str):
            raise ServiceError("session_token must be a string")

        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ServiceError(
                    f"session {session_id!r} not found",
                    code="session_not_found",
                    status=404,
                )
            now = _utc_now()
            # 心跳也会触发超时判定；已过期会话不能恢复。
            self._expire_if_timed_out_locked(session, now)
            if not hmac.compare_digest(
                session["token"].encode("utf-8"), token.encode("utf-8")
            ):
                raise ServiceError(
                    "invalid session token",
                    code="invalid_session_token",
                    status=401,
                )
            if session["state"] != SESSION_ONLINE:
                raise ServiceError(
                    f"session {session_id!r} is not online",
                    code="session_not_online",
                    status=409,
                )
            session["last_seen_at"] = now
            session["expires_at"] = now + timedelta(
                seconds=session["keepalive_seconds"]
            )
            return self._public_session(session)

    # ------------------------------------------------------------------
    # MQTT 风格主题路由：订阅、发布与拉取
    # ------------------------------------------------------------------

    def _mint_message_id_locked(self) -> str:
        message_id = secrets.token_urlsafe(18)
        while message_id in self._issued_message_ids:
            message_id = secrets.token_urlsafe(18)
        self._issued_message_ids.add(message_id)
        return message_id

    def _mint_delivery_id_locked(self) -> str:
        delivery_id = secrets.token_urlsafe(18)
        while delivery_id in self._issued_delivery_ids:
            delivery_id = secrets.token_urlsafe(18)
        self._issued_delivery_ids.add(delivery_id)
        return delivery_id

    def _authenticate_online_session_locked(
        self, session_id: str, token: str, now: datetime
    ) -> dict:
        """定位并校验会话：未知 404、令牌错误 401、已离线 409。

        校验令牌前先按既有保活规则处理超时。
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise ServiceError(
                f"session {session_id!r} not found",
                code="session_not_found",
                status=404,
            )
        self._expire_if_timed_out_locked(session, now)
        if not hmac.compare_digest(
            session["token"].encode("utf-8"), token.encode("utf-8")
        ):
            raise ServiceError(
                "invalid session token",
                code="invalid_session_token",
                status=401,
            )
        if session["state"] != SESSION_ONLINE:
            raise ServiceError(
                f"session {session_id!r} is not online",
                code="session_not_online",
                status=409,
            )
        return session

    @staticmethod
    def _require_fields(
        payload: object, fields: set[str], optional: set[str] | None = None
    ) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        unknown = set(payload) - fields - (optional or set())
        if unknown:
            raise ServiceError(f"unknown field(s): {', '.join(sorted(unknown))}")
        for field in fields:
            if field not in payload:
                raise ServiceError(f"missing required field: {field}")
        return payload

    def subscribe_topic(self, session_id: str, payload: object) -> dict:
        data = self._require_fields(payload, {"session_token", "topic_filter"})
        token = data["session_token"]
        if not isinstance(token, str) or isinstance(token, bool):
            raise ServiceError("session_token must be a string")
        topic_filter = _validate_topic_filter(data["topic_filter"])

        with self._lock:
            session = self._authenticate_online_session_locked(
                session_id, token, _utc_now()
            )
            # 集合保证重复订阅幂等；只有首次成功添加的过滤器才触发回放，
            # 相同过滤器重复订阅不再次回放。
            is_new_filter = topic_filter not in session["subscriptions"]
            session["subscriptions"].add(topic_filter)
            if is_new_filter:
                self._replay_retained_locked(session, topic_filter)
            return {"topic_filter": topic_filter}

    def _replay_retained_locked(self, session: dict, topic_filter: str) -> None:
        """把当前匹配该新过滤器的保留消息快照加入会话队列。

        即使与已有过滤器重叠，也独立回放当前快照；同次回放中每个精确
        topic 只入队一份（按 topic 存储天然唯一）。按各主题最近一次保留
        发布的先后顺序（seq）排列。回放只进入当前在线会话。
        """
        filter_layers = _split_topic_layers(topic_filter)
        matching = [
            record
            for topic, record in self._retained.items()
            if _topic_matches_filter(_split_topic_layers(topic), filter_layers)
        ]
        matching.sort(key=lambda record: record["seq"])
        for record in matching:
            qos = record["qos"]
            entry = {
                "qos": qos,
                "message": record["message"],
                "delivery_id": None,
                "retained": True,
            }
            if qos == 1:
                # QoS 1 回放为目标会话生成独立且不可预测的 delivery_id。
                entry["delivery_id"] = self._mint_delivery_id_locked()
            session["queue"].append(entry)

    def publish_message(self, session_id: str, payload: object) -> dict:
        data = self._require_fields(
            payload,
            {"session_token", "topic", "payload"},
            optional={"qos", "retain"},
        )
        token = data["session_token"]
        if not isinstance(token, str) or isinstance(token, bool):
            raise ServiceError("session_token must be a string")
        topic = _validate_topic(data["topic"])
        # payload 可为 null、标量、数组或对象，原样保留。
        message_payload = data["payload"]
        # 缺省按 QoS 0 处理；显式 qos 只接受整数 0 或 1。
        qos = data.get("qos", 0)
        if not isinstance(qos, int) or isinstance(qos, bool) or qos not in (0, 1):
            raise ServiceError("qos must be the integer 0 or 1")
        # 缺省按 false 处理；显式 retain 只接受 JSON 布尔值。
        retain = data.get("retain", False)
        if not isinstance(retain, bool):
            raise ServiceError("retain must be a boolean")

        with self._lock:
            now = _utc_now()
            publisher = self._authenticate_online_session_locked(
                session_id, token, now
            )
            message = {
                "message_id": self._mint_message_id_locked(),
                "topic": topic,
                "payload": message_payload,
                "publisher_device_id": publisher["device_id"],
                "published_at": _rfc3339_utc(now),
            }
            # 原消息先于一切动作消息入队。
            matched = self._route_message_locked(topic, qos, message, now)
            if retain:
                if message_payload is None:
                    # 保留清除：消息仍实时投递，同时删除该 topic 的保留值，
                    # 不存在也成功。
                    self._retained.pop(topic, None)
                else:
                    # 按精确 topic 保存并覆盖旧值；即使 matched_count 为零
                    # 也保存。保留记录沿用实时消息的 qos 与发布元数据。
                    self._retained_sequence += 1
                    self._retained[topic] = {
                        "message": message,
                        "qos": qos,
                        "seq": self._retained_sequence,
                    }
            # 原消息处理完毕后按创建顺序评估已启用规则；动作消息不再触发
            # 规则，其投递也不计入 matched_count。
            self._evaluate_rules_locked(topic, message_payload, publisher, now)
            return {"message_id": message["message_id"], "matched_count": matched}

    def _route_message_locked(
        self, topic: str, qos: int, message: dict, now: datetime
    ) -> int:
        """把一条普通非保留消息投递给当时匹配的在线会话。

        返回实际入队的在线会话数。沿用既有超时判定、订阅匹配、每会话一份、
        QoS 与 delivery_id 语义。无在线会话的持久组合若订阅匹配，QoS 1
        消息另存一份到其离线队列（QoS 0 不保存），但不计入返回计数。
        """
        topic_layers = _split_topic_layers(topic)
        matched = 0
        online_keys: set[tuple[str, str]] = set()
        for target in self._sessions.values():
            # 路由前按既有保活规则处理超时，只投递给在线会话。
            self._expire_if_timed_out_locked(target, now)
            if target["state"] != SESSION_ONLINE:
                continue
            online_keys.add((target["device_id"], target["client_id"]))
            if not any(
                _topic_matches_filter(topic_layers, _split_topic_layers(sub))
                for sub in target["subscriptions"]
            ):
                continue
            # 同一会话即使被多个过滤器命中也只入队一份。
            # 实时投递不携带 retained 标记。
            entry = {"qos": qos, "message": message, "delivery_id": None}
            if qos == 1:
                # 每个命中的在线会话获得独立且不可预测的 delivery_id。
                entry["delivery_id"] = self._mint_delivery_id_locked()
            target["queue"].append(entry)
            matched += 1
        # 离线持久会话只保存 QoS 1；每个持久组合即使多个过滤器命中也只
        # 一份，delivery_id 独立于在线投递，且不计入 matched_count。
        if qos == 1:
            for key, routes in self._persistent_sessions.items():
                if key in online_keys:
                    continue
                if not any(
                    _topic_matches_filter(topic_layers, _split_topic_layers(sub))
                    for sub in routes["subscriptions"]
                ):
                    continue
                routes["queue"].append(
                    {
                        "qos": 1,
                        "message": message,
                        "delivery_id": self._mint_delivery_id_locked(),
                    }
                )
        return matched

    def _evaluate_rules_locked(
        self, topic: str, payload: object, publisher: dict, now: datetime
    ) -> None:
        """在线发布校验与鉴权通过后评估规则。

        仅在显式发布路径调用（保留消息回放不会进入这里）。按规则创建顺序
        评估全部已启用规则：过滤器与条件均命中时，以 action 内容、原发布
        设备身份和新 message_id 生成普通非保留消息，按现有订阅、QoS、背压
        及确认语义投递给当时在线会话。每项命中各生成一条消息；动作消息
        不再触发规则，且不计入发布响应的 matched_count。
        """
        topic_layers = _split_topic_layers(topic)
        for rule_id in self._rule_order:
            rule = self._rules[rule_id]
            if not rule["enabled"]:
                continue
            if not _topic_matches_filter(topic_layers, rule["filter_layers"]):
                continue
            if not self._rule_condition_matches(rule["condition"], payload):
                continue
            action = rule["action"]
            action_message = {
                "message_id": self._mint_message_id_locked(),
                "topic": action["topic"],
                "payload": copy.deepcopy(action["payload"]),
                "publisher_device_id": publisher["device_id"],
                "published_at": _rfc3339_utc(now),
            }
            self._route_message_locked(
                action["topic"], action["qos"], action_message, now
            )

    @staticmethod
    def _rule_condition_matches(condition: dict, payload: object) -> bool:
        """按 path 读取发布 payload 并应用条件运算。

        path 任一段缺失，或下钻途中遇到非对象（数组、标量、null）即不命中；
        终点值允许任意 JSON 类型。eq/ne 按 JSON 值深度比较（布尔与数字
        不相等）；大小比较仅在读取值也是非布尔数字时判断，否则不命中。
        """
        current = payload
        for segment in condition["path"]:
            if not isinstance(current, dict):
                return False
            if segment not in current:
                return False
            current = current[segment]
        operator = condition["operator"]
        expected = condition["value"]
        if operator == "eq":
            return _json_equal(current, expected)
        if operator == "ne":
            return not _json_equal(current, expected)
        if not _is_json_number(current):
            return False
        if operator == "gt":
            return current > expected
        if operator == "gte":
            return current >= expected
        if operator == "lt":
            return current < expected
        if operator == "lte":
            return current <= expected
        return False

    @staticmethod
    def _render_delivery(entry: dict, dup: bool) -> dict:
        """QoS 0 仅含原有字段；QoS 1 额外携带 qos、delivery_id 与 dup。

        仅订阅回放的保留消息额外携带 retained；实时投递不携带该字段。
        """
        rendered = dict(entry["message"])
        if entry["qos"] == 1:
            rendered["qos"] = 1
            rendered["delivery_id"] = entry["delivery_id"]
            rendered["dup"] = dup
        if entry.get("retained"):
            rendered["retained"] = True
        return rendered

    def poll_messages(self, session_id: str, payload: object) -> dict:
        data = self._require_fields(payload, {"session_token", "max_messages"})
        token = data["session_token"]
        if not isinstance(token, str) or isinstance(token, bool):
            raise ServiceError("session_token must be a string")
        max_messages = _validate_max_messages(data["max_messages"])

        with self._lock:
            session = self._authenticate_online_session_locked(
                session_id, token, _utc_now()
            )
            unacked: dict = session["unacked"]
            queue: deque = session["queue"]
            messages: list[dict] = []
            remaining = max_messages
            # 未确认的 QoS 1 投递按原发布顺序优先重投，标记 dup；
            # 它们与首次交付共同受 max_messages 限制，形成自然背压。
            for entry in unacked.values():
                if remaining == 0:
                    break
                messages.append(self._render_delivery(entry, dup=True))
                remaining -= 1
            # 之后按发布顺序交付尚未首次投递的消息。
            while remaining > 0 and queue:
                entry = queue.popleft()
                if entry["qos"] == 1:
                    # QoS 1 首次投递后转入未确认集合，等待 ack。
                    unacked[entry["delivery_id"]] = entry
                messages.append(self._render_delivery(entry, dup=False))
                remaining -= 1
            return {"messages": messages}

    def ack_messages(self, session_id: str, payload: object) -> dict:
        data = self._require_fields(payload, {"session_token", "delivery_ids"})
        token = data["session_token"]
        if not isinstance(token, str) or isinstance(token, bool):
            raise ServiceError("session_token must be a string")
        delivery_ids = data["delivery_ids"]
        if not isinstance(delivery_ids, list):
            raise ServiceError("delivery_ids must be an array")
        if not 1 <= len(delivery_ids) <= 100:
            raise ServiceError("delivery_ids must contain 1-100 items")
        if any(not isinstance(d, str) for d in delivery_ids):
            raise ServiceError("delivery_ids must contain only strings")
        if len(set(delivery_ids)) != len(delivery_ids):
            raise ServiceError("delivery_ids must not contain duplicates")

        with self._lock:
            session = self._authenticate_online_session_locked(
                session_id, token, _utc_now()
            )
            unacked: dict = session["unacked"]
            acked: set = session["acked"]
            # 任一标识从未属于该会话：整体不确认任何消息。
            for delivery_id in delivery_ids:
                if delivery_id not in unacked and delivery_id not in acked:
                    raise ServiceError(
                        f"delivery {delivery_id!r} not found",
                        code="delivery_not_found",
                        status=404,
                    )
            # 原子地移除相应未确认消息；重复确认幂等且不增加计数。
            acked_count = 0
            for delivery_id in delivery_ids:
                if unacked.pop(delivery_id, None) is not None:
                    acked.add(delivery_id)
                    acked_count += 1
            return {"acked_count": acked_count}

    # ------------------------------------------------------------------
    # 设备影子：期望状态、实际状态与差异
    # ------------------------------------------------------------------

    @staticmethod
    def _shadow_snapshot_locked(device_id: str, shadow: dict) -> dict:
        """影子完整快照；深拷贝避免调用方修改进程内状态。"""
        return {
            "device_id": device_id,
            "version": shadow["version"],
            "desired": copy.deepcopy(shadow["desired"]),
            "reported": copy.deepcopy(shadow["reported"]),
            "delta": Service._shadow_delta_locked(
                shadow["desired"], shadow["reported"]
            ),
            "updated_at": shadow["updated_at"],
        }

    @staticmethod
    def _shadow_delta_locked(desired: object, reported: object) -> dict:
        """递归保留 desired 中 reported 缺失或值不同的成员。

        仅当两侧均为 JSON 对象时才向下递归；数组与其他非对象值整体比较。
        reported 独有的成员一律忽略；完全一致时返回空对象。
        """
        if not isinstance(desired, dict) or not isinstance(reported, dict):
            return {}
        delta: dict = {}
        for key, desired_value in desired.items():
            if key not in reported:
                delta[key] = copy.deepcopy(desired_value)
                continue
            reported_value = reported[key]
            if isinstance(desired_value, dict) and isinstance(reported_value, dict):
                nested = Service._shadow_delta_locked(desired_value, reported_value)
                if nested:
                    delta[key] = nested
            elif desired_value != reported_value:
                delta[key] = copy.deepcopy(desired_value)
        return delta

    @staticmethod
    def _validate_shadow_state(value: object) -> dict:
        if not isinstance(value, dict):
            raise ServiceError("state must be a JSON object")
        return value

    @staticmethod
    def _validate_expected_version(value: object) -> int:
        # bool 是 int 的子类，须显式排除；只接受非负整数。
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ServiceError("expected_version must be a non-negative integer")
        return value

    def get_shadow(self, device_id: str) -> dict:
        with self._lock:
            shadow = self._shadows.get(device_id)
            if shadow is None:
                raise ServiceError(
                    f"device {device_id!r} not found",
                    code="device_not_found",
                    status=404,
                )
            return self._shadow_snapshot_locked(device_id, shadow)

    def set_desired_shadow(self, device_id: str, payload: object) -> dict:
        data = self._require_fields(
            payload, {"state"}, optional={"expected_version"}
        )
        state = self._validate_shadow_state(data["state"])
        expected_version = None
        if "expected_version" in data:
            expected_version = self._validate_expected_version(
                data["expected_version"]
            )

        with self._lock:
            shadow = self._shadows.get(device_id)
            if shadow is None:
                raise ServiceError(
                    f"device {device_id!r} not found",
                    code="device_not_found",
                    status=404,
                )
            # 已吊销设备仍可读写 desired，此处不检查 active。
            if expected_version is not None and expected_version != shadow["version"]:
                raise ServiceError(
                    f"shadow version conflict: expected {expected_version}, "
                    f"current {shadow['version']}",
                    code="shadow_version_conflict",
                    status=409,
                )
            shadow["desired"] = copy.deepcopy(state)
            shadow["version"] += 1
            shadow["updated_at"] = _rfc3339_utc(_utc_now())
            return self._shadow_snapshot_locked(device_id, shadow)

    def report_shadow(self, session_id: str, payload: object) -> dict:
        data = self._require_fields(
            payload, {"session_token", "state"}, optional={"expected_version"}
        )
        token = data["session_token"]
        if not isinstance(token, str) or isinstance(token, bool):
            raise ServiceError("session_token must be a string")
        state = self._validate_shadow_state(data["state"])
        expected_version = None
        if "expected_version" in data:
            expected_version = self._validate_expected_version(
                data["expected_version"]
            )

        with self._lock:
            now = _utc_now()
            # 沿用会话鉴权及超时规则：未知 404、令牌错误 401、
            # 关闭或过期 409。
            session = self._authenticate_online_session_locked(
                session_id, token, now
            )
            device_id = session["device_id"]
            shadow = self._shadows.get(device_id)
            if shadow is None:
                # 在线会话必然伴随已注册设备与影子，仅作防御性处理。
                raise ServiceError(
                    f"device {device_id!r} not found",
                    code="device_not_found",
                    status=404,
                )
            if expected_version is not None and expected_version != shadow["version"]:
                raise ServiceError(
                    f"shadow version conflict: expected {expected_version}, "
                    f"current {shadow['version']}",
                    code="shadow_version_conflict",
                    status=409,
                )
            shadow["reported"] = copy.deepcopy(state)
            shadow["version"] += 1
            shadow["updated_at"] = _rfc3339_utc(now)
            return self._shadow_snapshot_locked(device_id, shadow)

    # ------------------------------------------------------------------
    # 进程内规则引擎
    # ------------------------------------------------------------------

    @staticmethod
    def _public_rule_locked(rule: dict) -> dict:
        """规则完整视图；深拷贝避免调用方修改进程内状态。"""
        return {
            "rule_id": rule["rule_id"],
            "topic_filter": rule["topic_filter"],
            "enabled": rule["enabled"],
            "condition": copy.deepcopy(rule["condition"]),
            "action": copy.deepcopy(rule["action"]),
        }

    def create_rule(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        unknown = set(payload) - {
            "rule_id", "topic_filter", "enabled", "condition", "action"
        }
        if unknown:
            raise ServiceError(f"unknown field(s): {', '.join(sorted(unknown))}")
        for field in ("rule_id", "topic_filter", "enabled", "condition", "action"):
            if field not in payload:
                raise ServiceError(f"missing required field: {field}")
        rule_id = _validate_rule_id(payload["rule_id"])
        topic_filter = _validate_topic_filter(payload["topic_filter"])
        enabled = payload["enabled"]
        if not isinstance(enabled, bool):
            raise ServiceError("enabled must be a boolean")
        condition = _validate_rule_condition(payload["condition"])
        action = _validate_rule_action(payload["action"])

        with self._lock:
            if rule_id in self._rules:
                raise ServiceError(
                    f"rule {rule_id!r} already exists",
                    code="rule_already_exists",
                    status=409,
                )
            rule = {
                "rule_id": rule_id,
                "topic_filter": topic_filter,
                "enabled": enabled,
                # 深拷贝隔离调用方持有的请求对象。
                "condition": copy.deepcopy(condition),
                "action": copy.deepcopy(action),
                # 预切分过滤器层，评估时直接参与匹配。
                "filter_layers": _split_topic_layers(topic_filter),
            }
            self._rules[rule_id] = rule
            self._rule_order.append(rule_id)
            return self._public_rule_locked(rule)

    def list_rules(self) -> dict:
        with self._lock:
            return {
                "rules": [
                    self._public_rule_locked(self._rules[rule_id])
                    for rule_id in self._rule_order
                ]
            }

    def set_rule_enabled(self, rule_id: str, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        unknown = set(payload) - {"enabled"}
        if unknown:
            raise ServiceError(f"unknown field(s): {', '.join(sorted(unknown))}")
        if "enabled" not in payload:
            raise ServiceError("missing required field: enabled")
        enabled = payload["enabled"]
        if not isinstance(enabled, bool):
            raise ServiceError("enabled must be a boolean")

        with self._lock:
            rule = self._rules.get(rule_id)
            if rule is None:
                raise ServiceError(
                    f"rule {rule_id!r} not found",
                    code="rule_not_found",
                    status=404,
                )
            rule["enabled"] = enabled
            return self._public_rule_locked(rule)

    def delete_rule(self, rule_id: str) -> dict:
        with self._lock:
            if rule_id not in self._rules:
                raise ServiceError(
                    f"rule {rule_id!r} not found",
                    code="rule_not_found",
                    status=404,
                )
            del self._rules[rule_id]
            self._rule_order.remove(rule_id)
            return {"deleted": True}

    # ------------------------------------------------------------------
    # 设备命令：下发、领取与确认
    # ------------------------------------------------------------------

    def _mint_command_id_locked(self) -> str:
        command_id = secrets.token_urlsafe(18)
        while command_id in self._issued_command_ids:
            command_id = secrets.token_urlsafe(18)
        self._issued_command_ids.add(command_id)
        return command_id

    @staticmethod
    def _public_command_locked(command: dict) -> dict:
        """命令完整快照；深拷贝避免调用方修改进程内状态。"""
        return {
            "command_id": command["command_id"],
            "device_id": command["device_id"],
            "command_name": command["command_name"],
            "payload": copy.deepcopy(command["payload"]),
            "ttl_seconds": command["ttl_seconds"],
            "status": command["status"],
            "delivery_count": command["delivery_count"],
            "created_at": _rfc3339_utc(command["created_at"]),
            "expires_at": _rfc3339_utc(command["expires_at"]),
            "completed_at": (
                _rfc3339_utc(command["completed_at"])
                if command["completed_at"] is not None
                else None
            ),
            "result": copy.deepcopy(command["result"]),
        }

    @staticmethod
    def _expire_command_locked(command: dict, now: datetime) -> None:
        """到达 expires_at 的非终态命令转为 expired，不再可领取或确认。"""
        if command["status"] in COMMAND_NON_TERMINAL and now > command["expires_at"]:
            command["status"] = "expired"

    def _create_command_locked(
        self,
        device_id: str,
        command_name: str,
        command_payload: object,
        ttl_seconds: int,
        now: datetime,
    ) -> dict:
        """在锁内创建一条 queued 命令记录（调用方负责设备存在与状态检查）。"""
        command = {
            "command_id": self._mint_command_id_locked(),
            "device_id": device_id,
            "command_name": command_name,
            "payload": copy.deepcopy(command_payload),
            "ttl_seconds": ttl_seconds,
            "status": COMMAND_QUEUED,
            "delivery_count": 0,
            "created_at": now,
            "expires_at": now + timedelta(seconds=ttl_seconds),
            "completed_at": None,
            "result": None,
            # 当前领取该命令的会话；仅 queued/delivered 状态下有意义。
            "owner_session_id": None,
        }
        self._commands[command["command_id"]] = command
        self._device_command_ids.setdefault(device_id, []).append(
            command["command_id"]
        )
        return command

    def create_command(self, device_id: str, payload: object) -> dict:
        data = self._require_fields(
            payload, {"command_name", "payload", "ttl_seconds"}
        )
        command_name = _validate_command_name(data["command_name"])
        # payload 可为任意 JSON 值（null、标量、数组或对象），原样保留。
        command_payload = data["payload"]
        ttl_seconds = _validate_ttl_seconds(data["ttl_seconds"])

        with self._lock:
            device = self._devices.get(device_id)
            if device is None:
                raise ServiceError(
                    f"device {device_id!r} not found",
                    code="device_not_found",
                    status=404,
                )
            if not device["active"]:
                raise ServiceError(
                    f"device {device_id!r} is revoked",
                    code="device_revoked",
                    status=409,
                )
            command = self._create_command_locked(
                device_id, command_name, command_payload, ttl_seconds, _utc_now()
            )
            return self._public_command_locked(command)

    def get_command(self, device_id: str, command_id: str) -> dict:
        with self._lock:
            if device_id not in self._devices:
                raise ServiceError(
                    f"device {device_id!r} not found",
                    code="device_not_found",
                    status=404,
                )
            command = self._commands.get(command_id)
            if command is None or command["device_id"] != device_id:
                raise ServiceError(
                    f"command {command_id!r} not found",
                    code="command_not_found",
                    status=404,
                )
            self._expire_command_locked(command, _utc_now())
            return self._public_command_locked(command)

    def poll_commands(self, session_id: str, payload: object) -> dict:
        data = self._require_fields(payload, {"session_token", "max_commands"})
        token = data["session_token"]
        if not isinstance(token, str) or isinstance(token, bool):
            raise ServiceError("session_token must be a string")
        max_commands = _validate_max_commands(data["max_commands"])

        with self._lock:
            now = _utc_now()
            # 沿用会话鉴权及保活规则：未知 404、令牌错误 401、已离线 409。
            session = self._authenticate_online_session_locked(
                session_id, token, now
            )
            commands: list[dict] = []
            remaining = max_commands
            # 按创建顺序领取本设备命令；单个命令在任一时刻至多归属一个
            # 在线会话（锁内完成判定与归属转移，并发领取不会双重归属）。
            for command_id in self._device_command_ids.get(session["device_id"], []):
                if remaining == 0:
                    break
                command = self._commands[command_id]
                self._expire_command_locked(command, now)
                if command["status"] == COMMAND_QUEUED:
                    # 首次领取：转为 delivered，计数为一，dup 为 false。
                    command["status"] = COMMAND_DELIVERED
                    command["delivery_count"] = 1
                    command["owner_session_id"] = session["session_id"]
                    dup = False
                elif command["status"] == COMMAND_DELIVERED:
                    owner_id = command["owner_session_id"]
                    if owner_id == session["session_id"]:
                        # 确认前由同一会话重领：计数不变，dup 为 true。
                        dup = True
                    else:
                        owner = self._sessions.get(owner_id)
                        if owner is not None:
                            self._expire_if_timed_out_locked(owner, now)
                        if owner is not None and owner["state"] == SESSION_ONLINE:
                            # 仍归属另一在线会话，本会话不得领取。
                            continue
                        # 领取会话已超时或被替换：归属转移，计数加一。
                        command["owner_session_id"] = session["session_id"]
                        command["delivery_count"] += 1
                        dup = True
                else:
                    # 终态命令（succeeded/failed/expired/cancelled）不再领取。
                    continue
                item = self._public_command_locked(command)
                item["dup"] = dup
                commands.append(item)
                remaining -= 1
            return {"commands": commands}

    def ack_command(self, session_id: str, command_id: str, payload: object) -> dict:
        data = self._require_fields(payload, {"session_token", "status", "result"})
        token = data["session_token"]
        if not isinstance(token, str) or isinstance(token, bool):
            raise ServiceError("session_token must be a string")
        status = data["status"]
        if (
            not isinstance(status, str)
            or isinstance(status, bool)
            or status not in COMMAND_ACK_STATUSES
        ):
            raise ServiceError("status must be 'succeeded' or 'failed'")
        # result 可为任意 JSON 值，原样保存。
        result = data["result"]

        with self._lock:
            now = _utc_now()
            # 沿用会话鉴权及保活规则：未知 404、令牌错误 401、已离线 409。
            session = self._authenticate_online_session_locked(
                session_id, token, now
            )
            command = self._commands.get(command_id)
            # 其他设备的命令对本会话不可见，与不存在同等处理。
            if command is None or command["device_id"] != session["device_id"]:
                raise ServiceError(
                    f"command {command_id!r} not found",
                    code="command_not_found",
                    status=404,
                )
            self._expire_command_locked(command, now)
            current = command["status"]
            if current in COMMAND_ACK_STATUSES:
                # 已确认：相同确认幂等返回当前快照，内容冲突报错。
                if current == status and _json_equal(command["result"], result):
                    return self._public_command_locked(command)
                raise ServiceError(
                    f"command {command_id!r} already completed",
                    code="command_already_completed",
                    status=409,
                )
            if current == "expired":
                raise ServiceError(
                    f"command {command_id!r} is expired",
                    code="command_expired",
                    status=409,
                )
            if current == "cancelled":
                raise ServiceError(
                    f"command {command_id!r} already completed",
                    code="command_already_completed",
                    status=409,
                )
            # 首次确认只接受当前领取会话；未投递（含投递给其他会话）拒绝。
            if (
                current == COMMAND_QUEUED
                or command["owner_session_id"] != session["session_id"]
            ):
                raise ServiceError(
                    f"command {command_id!r} not delivered",
                    code="command_not_delivered",
                    status=409,
                )
            command["status"] = status
            command["result"] = copy.deepcopy(result)
            command["completed_at"] = now
            return self._public_command_locked(command)

    # ------------------------------------------------------------------
    # 设备组：创建、查询与整体替换成员
    # ------------------------------------------------------------------

    @staticmethod
    def _public_group(group: dict) -> dict:
        """组完整视图；成员列表拷贝避免调用方修改进程内状态。"""
        return {
            "group_id": group["group_id"],
            "device_ids": list(group["device_ids"]),
            "version": group["version"],
        }

    def create_group(self, payload: object) -> dict:
        data = self._require_fields(payload, {"group_id", "device_ids"})
        group_id = _validate_group_id(data["group_id"])
        device_ids = _validate_device_ids(data["device_ids"])

        with self._lock:
            if group_id in self._groups:
                raise ServiceError(
                    f"device group {group_id!r} already exists",
                    code="group_already_exists",
                    status=409,
                )
            for device_id in device_ids:
                if device_id not in self._devices:
                    raise ServiceError(
                        f"device {device_id!r} not found",
                        code="device_not_found",
                        status=404,
                    )
            group = {
                "group_id": group_id,
                "device_ids": device_ids,
                "version": 1,
            }
            self._groups[group_id] = group
            return self._public_group(group)

    def get_group(self, group_id: str) -> dict:
        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise ServiceError(
                    f"device group {group_id!r} not found",
                    code="group_not_found",
                    status=404,
                )
            return self._public_group(group)

    def replace_group_members(self, group_id: str, payload: object) -> dict:
        data = self._require_fields(
            payload, {"device_ids"}, optional={"expected_version"}
        )
        device_ids = _validate_device_ids(data["device_ids"])
        expected_version = None
        if "expected_version" in data:
            expected_version = self._validate_expected_version(
                data["expected_version"]
            )

        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise ServiceError(
                    f"device group {group_id!r} not found",
                    code="group_not_found",
                    status=404,
                )
            # 任一设备未注册即整体失败，组保持不变。
            for device_id in device_ids:
                if device_id not in self._devices:
                    raise ServiceError(
                        f"device {device_id!r} not found",
                        code="device_not_found",
                        status=404,
                    )
            if expected_version is not None and expected_version != group["version"]:
                raise ServiceError(
                    f"group version conflict: expected {expected_version}, "
                    f"current {group['version']}",
                    code="group_version_conflict",
                    status=409,
                )
            # 整体替换成员；即使成员完全相同版本也照常加一。
            group["device_ids"] = device_ids
            group["version"] += 1
            return self._public_group(group)

    # ------------------------------------------------------------------
    # 命令批次：对受理时的组成员快照下发命令
    # ------------------------------------------------------------------

    def _mint_batch_id_locked(self) -> str:
        batch_id = secrets.token_urlsafe(18)
        while batch_id in self._issued_batch_ids:
            batch_id = secrets.token_urlsafe(18)
        self._issued_batch_ids.add(batch_id)
        return batch_id

    @staticmethod
    def _public_batch_created(batch: dict) -> dict:
        """批次受理视图：按成员顺序排列的 device_id 与 command_id。"""
        return {
            "batch_id": batch["batch_id"],
            "group_id": batch["group_id"],
            "group_version": batch["group_version"],
            "request_id": batch["request_id"],
            "commands": [
                {"device_id": device_id, "command_id": command_id}
                for device_id, command_id in zip(
                    batch["device_ids"], batch["command_ids"]
                )
            ],
        }

    def create_command_batch(self, group_id: str, payload: object) -> dict:
        data = self._require_fields(
            payload,
            {"command_name", "payload", "ttl_seconds", "request_id"},
            optional={"expected_group_version"},
        )
        command_name = _validate_command_name(data["command_name"])
        # payload 可为任意 JSON 值（null、标量、数组或对象），原样保留。
        command_payload = data["payload"]
        ttl_seconds = _validate_ttl_seconds(data["ttl_seconds"])
        request_id = _validate_request_id(data["request_id"])
        expected_group_version = None
        if "expected_group_version" in data:
            expected_group_version = self._validate_expected_group_version(
                data["expected_group_version"]
            )

        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise ServiceError(
                    f"device group {group_id!r} not found",
                    code="group_not_found",
                    status=404,
                )
            # 同组重复 request_id：内容相同幂等返回原批次（锁内判定与创建，
            # 并发相同请求至多创建一个批次）；内容不同拒绝且不产生任何命令。
            requests = self._group_request_ids.setdefault(group_id, {})
            existing_batch_id = requests.get(request_id)
            if existing_batch_id is not None:
                existing = self._batches[existing_batch_id]
                if (
                    existing["command_name"] == command_name
                    and _json_equal(existing["payload"], command_payload)
                    and existing["ttl_seconds"] == ttl_seconds
                ):
                    return self._public_batch_created(existing)
                raise ServiceError(
                    f"request {request_id!r} conflicts with an existing batch",
                    code="batch_request_conflict",
                    status=409,
                )
            if (
                expected_group_version is not None
                and expected_group_version != group["version"]
            ):
                raise ServiceError(
                    f"group version conflict: expected {expected_group_version}, "
                    f"current {group['version']}",
                    code="group_version_conflict",
                    status=409,
                )
            # 受理时快照成员；含已吊销成员则整体失败，不创建任何命令。
            member_ids = list(group["device_ids"])
            for device_id in member_ids:
                device = self._devices.get(device_id)
                if device is None or not device["active"]:
                    raise ServiceError(
                        f"device group {group_id!r} contains revoked device "
                        f"{device_id!r}",
                        code="group_contains_revoked_device",
                        status=409,
                    )
            now = _utc_now()
            command_ids: list[str] = []
            for device_id in member_ids:
                command = self._create_command_locked(
                    device_id, command_name, command_payload, ttl_seconds, now
                )
                command_ids.append(command["command_id"])
            batch = {
                "batch_id": self._mint_batch_id_locked(),
                "group_id": group_id,
                # 受理时采用的组版本；后续成员替换不影响本批次。
                "group_version": group["version"],
                "request_id": request_id,
                "command_name": command_name,
                "payload": copy.deepcopy(command_payload),
                "ttl_seconds": ttl_seconds,
                "device_ids": member_ids,
                "command_ids": command_ids,
            }
            self._batches[batch["batch_id"]] = batch
            requests[request_id] = batch["batch_id"]
            return self._public_batch_created(batch)

    def get_command_batch(self, group_id: str | None, batch_id: str) -> dict:
        with self._lock:
            if group_id is not None and group_id not in self._groups:
                raise ServiceError(
                    f"device group {group_id!r} not found",
                    code="group_not_found",
                    status=404,
                )
            batch = self._batches.get(batch_id)
            if batch is None or (
                group_id is not None and batch["group_id"] != group_id
            ):
                raise ServiceError(
                    f"command batch {batch_id!r} not found",
                    code="batch_not_found",
                    status=404,
                )
            now = _utc_now()
            commands: list[dict] = []
            counts = {
                "queued": 0,
                "delivered": 0,
                "succeeded": 0,
                "failed": 0,
                "expired": 0,
                "cancelled": 0,
            }
            # 按创建时成员顺序返回子命令当前快照，并汇总各状态数量。
            for command_id in batch["command_ids"]:
                command = self._commands[command_id]
                self._expire_command_locked(command, now)
                commands.append(self._public_command_locked(command))
                counts[command["status"]] += 1
            return {
                "batch_id": batch["batch_id"],
                "group_id": batch["group_id"],
                "group_version": batch["group_version"],
                "request_id": batch["request_id"],
                "commands": commands,
                "counts": counts,
            }

    @staticmethod
    def _validate_expected_group_version(value: object) -> int:
        # bool 是 int 的子类，须显式排除；只接受非负整数。
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ServiceError(
                "expected_group_version must be a non-negative integer"
            )
        return value
