"""Core service surface for DeviceFabric.

除健康检查外，本模块实现设备注册与身份凭据生命周期（注册、查询、
认证、轮换与吊销），以及进程内连接会话与心跳保活。所有数据仅保存
在当前进程内，进程退出即清空。
"""

from __future__ import annotations

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

SESSION_ONLINE = "online"
SESSION_CLOSED = "closed"
SESSION_EXPIRED = "expired"


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

    def _expire_if_timed_out_locked(self, session: dict, now: datetime) -> bool:
        """在线会话超过 expires_at 即转为 expired，不可恢复。"""
        if session["state"] == SESSION_ONLINE and now > session["expires_at"]:
            session["state"] = SESSION_EXPIRED
            session["reason"] = "keepalive_timeout"
            self._online_session_keys.pop(
                (session["device_id"], session["client_id"]), None
            )
            self._discard_session_routes_locked(session)
            return True
        return False

    def _close_session_locked(self, session: dict, reason: str) -> None:
        session["state"] = SESSION_CLOSED
        session["reason"] = reason
        self._online_session_keys.pop(
            (session["device_id"], session["client_id"]), None
        )
        self._discard_session_routes_locked(session)

    @staticmethod
    def _discard_session_routes_locked(session: dict) -> None:
        """会话离线后订阅与未取消息立即失效，且不被新会话继承。"""
        session["subscriptions"].clear()
        session["queue"].clear()

    def create_session(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        unknown = set(payload) - {"device_id", "credential", "client_id", "keepalive_seconds"}
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
                # 订阅过滤器集合（幂等、无副本）与待取消息队列。
                "subscriptions": set(),
                "queue": deque(),
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
    def _require_fields(payload: object, fields: set[str]) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        unknown = set(payload) - fields
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
            # 集合保证重复订阅幂等，不产生副本。
            session["subscriptions"].add(topic_filter)
            return {"topic_filter": topic_filter}

    def publish_message(self, session_id: str, payload: object) -> dict:
        data = self._require_fields(payload, {"session_token", "topic", "payload"})
        token = data["session_token"]
        if not isinstance(token, str) or isinstance(token, bool):
            raise ServiceError("session_token must be a string")
        topic = _validate_topic(data["topic"])
        # payload 可为 null、标量、数组或对象，原样保留。
        message_payload = data["payload"]

        with self._lock:
            now = _utc_now()
            publisher = self._authenticate_online_session_locked(
                session_id, token, now
            )
            topic_layers = _split_topic_layers(topic)
            message = {
                "message_id": self._mint_message_id_locked(),
                "topic": topic,
                "payload": message_payload,
                "publisher_device_id": publisher["device_id"],
                "published_at": _rfc3339_utc(now),
            }
            matched = 0
            for target in self._sessions.values():
                # 路由前按既有保活规则处理超时，只投递给在线会话。
                self._expire_if_timed_out_locked(target, now)
                if target["state"] != SESSION_ONLINE:
                    continue
                if not any(
                    _topic_matches_filter(topic_layers, _split_topic_layers(sub))
                    for sub in target["subscriptions"]
                ):
                    continue
                # 同一会话即使被多个过滤器命中也只入队一份。
                target["queue"].append(message)
                matched += 1
            return {"message_id": message["message_id"], "matched_count": matched}

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
            queue: deque = session["queue"]
            count = min(max_messages, len(queue))
            # 按发布顺序取出并移出队列。
            messages = [queue.popleft() for _ in range(count)]
            return {"messages": messages}
