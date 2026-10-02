"""Core service surface for DeviceFabric.

除健康检查外，本模块实现设备注册与身份凭据生命周期（注册、查询、
认证、轮换与吊销）、进程内连接会话与心跳保活，以及 MQTT 风格的
主题订阅、发布与消息拉取。所有数据仅保存在当前进程内，进程退出
即清空。
"""

from __future__ import annotations

import hmac
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

from . import __version__

DEVICE_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
DEVICE_ID_MIN = 1
DEVICE_ID_MAX = 64
DISPLAY_NAME_MIN = 1
DISPLAY_NAME_MAX = 128
KEEPALIVE_MIN = 5
KEEPALIVE_MAX = 3600

SESSION_ONLINE = "online"
SESSION_CLOSED = "closed"
SESSION_EXPIRED = "expired"

TOPIC_MIN = 1
TOPIC_MAX = 256
MAX_MESSAGES_MIN = 1
MAX_MESSAGES_MAX = 100


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


def _split_topic_levels(value: str, label: str) -> list[str]:
    """主题与过滤器的公共约束：1-256 个码点、斜杠分层、每层非空、禁止 NUL。"""
    if not TOPIC_MIN <= len(value) <= TOPIC_MAX:
        raise ServiceError(f"{label} must be 1-256 Unicode code points")
    if "\x00" in value:
        raise ServiceError(f"{label} must not contain NUL characters")
    levels = value.split("/")
    if any(level == "" for level in levels):
        raise ServiceError(f"{label} levels must be non-empty")
    return levels


def _validate_topic_name(value: object) -> str:
    # 发布主题不得包含任何通配符。
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("topic must be a string")
    for level in _split_topic_levels(value, "topic"):
        if "+" in level or "#" in level:
            raise ServiceError("topic must not contain wildcards")
    return value


def _validate_topic_filter(value: object) -> str:
    # 过滤器中 + 只能独占一层，# 只能独占最后一层且最多出现一次。
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("topic_filter must be a string")
    levels = _split_topic_levels(value, "topic_filter")
    for index, level in enumerate(levels):
        if "#" in level and (level != "#" or index != len(levels) - 1):
            raise ServiceError("topic_filter # must occupy the entire final level")
        if "+" in level and level != "+":
            raise ServiceError("topic_filter + must occupy an entire level")
    return value


def _topic_filter_matches(topic_filter: str, topic: str) -> bool:
    """MQTT 风格匹配：+ 匹配恰好一层，# 匹配零层或多层（含父层）。"""
    filter_levels = topic_filter.split("/")
    topic_levels = topic.split("/")
    index = 0
    for level in filter_levels:
        if level == "#":
            return True
        if index >= len(topic_levels):
            return False
        if level != "+" and level != topic_levels[index]:
            return False
        index += 1
    return index == len(topic_levels)


def _validate_max_messages(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ServiceError("max_messages must be an integer")
    if not MAX_MESSAGES_MIN <= value <= MAX_MESSAGES_MAX:
        raise ServiceError(
            f"max_messages must be between {MAX_MESSAGES_MIN} and {MAX_MESSAGES_MAX}"
        )
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
        # 记录本进程签发过的全部消息标识，保证标识在进程内绝不重复。
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

    @staticmethod
    def _discard_session_messaging_locked(session: dict) -> None:
        """会话离线后其订阅与未取消息立即失效，不留存也不被继承。"""
        session["subscriptions"].clear()
        session["inbox"].clear()

    def _expire_if_timed_out_locked(self, session: dict, now: datetime) -> bool:
        """在线会话超过 expires_at 即转为 expired，不可恢复。"""
        if session["state"] == SESSION_ONLINE and now > session["expires_at"]:
            session["state"] = SESSION_EXPIRED
            session["reason"] = "keepalive_timeout"
            self._online_session_keys.pop(
                (session["device_id"], session["client_id"]), None
            )
            self._discard_session_messaging_locked(session)
            return True
        return False

    def _close_session_locked(self, session: dict, reason: str) -> None:
        session["state"] = SESSION_CLOSED
        session["reason"] = reason
        self._online_session_keys.pop(
            (session["device_id"], session["client_id"]), None
        )
        self._discard_session_messaging_locked(session)

    def _require_online_session_locked(self, session_id: str, token: str) -> dict:
        """会话令牌保护的在线会话公共判定：404 → 超时过期 → 401 → 409。"""
        session = self._sessions.get(session_id)
        if session is None:
            raise ServiceError(
                f"session {session_id!r} not found",
                code="session_not_found",
                status=404,
            )
        # 任何受令牌保护的操作都会触发超时判定；已过期会话不能恢复。
        self._expire_if_timed_out_locked(session, _utc_now())
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
                # 消息状态随会话生灭：订阅集合与待取队列不跨会话继承。
                "subscriptions": set(),
                "inbox": [],
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
            session = self._require_online_session_locked(session_id, token)
            now = _utc_now()
            session["last_seen_at"] = now
            session["expires_at"] = now + timedelta(
                seconds=session["keepalive_seconds"]
            )
            return self._public_session(session)

    # ------------------------------------------------------------------
    # 主题订阅、发布与消息拉取
    # ------------------------------------------------------------------

    def _mint_message_id_locked(self) -> str:
        message_id = secrets.token_urlsafe(16)
        while message_id in self._issued_message_ids:
            message_id = secrets.token_urlsafe(16)
        self._issued_message_ids.add(message_id)
        return message_id

    @staticmethod
    def _extract_token(payload: dict, allowed: set[str]) -> str:
        unknown = set(payload) - allowed
        if unknown:
            raise ServiceError(f"unknown field(s): {', '.join(sorted(unknown))}")
        if "session_token" not in payload:
            raise ServiceError("missing required field: session_token")
        token = payload["session_token"]
        if not isinstance(token, str):
            raise ServiceError("session_token must be a string")
        return token

    def add_subscription(self, session_id: str, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        token = self._extract_token(payload, {"session_token", "topic_filter"})
        if "topic_filter" not in payload:
            raise ServiceError("missing required field: topic_filter")
        topic_filter = _validate_topic_filter(payload["topic_filter"])

        with self._lock:
            session = self._require_online_session_locked(session_id, token)
            # 重复订阅幂等：集合语义保证不产生副本。
            session["subscriptions"].add(topic_filter)
            return {
                "session_id": session["session_id"],
                "topic_filter": topic_filter,
                "subscriptions": sorted(session["subscriptions"]),
            }

    def publish_message(self, session_id: str, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        token = self._extract_token(payload, {"session_token", "topic", "payload"})
        for field in ("topic", "payload"):
            if field not in payload:
                raise ServiceError(f"missing required field: {field}")
        topic = _validate_topic_name(payload["topic"])
        # payload 可为任意 JSON 值（null、标量、数组或对象），无需校验。
        message_payload = payload["payload"]

        with self._lock:
            publisher = self._require_online_session_locked(session_id, token)
            now = _utc_now()
            # 路由前按保活规则处理全部会话超时，只投递给在线会话。
            for session in self._sessions.values():
                self._expire_if_timed_out_locked(session, now)
            message = {
                "message_id": self._mint_message_id_locked(),
                "topic": topic,
                "payload": message_payload,
                "publisher_device_id": publisher["device_id"],
                "published_at": _rfc3339_utc(now),
            }
            matched = 0
            for session in self._sessions.values():
                if session["state"] != SESSION_ONLINE:
                    continue
                # 同一会话即使被多个过滤器命中也只入队一份。
                if any(
                    _topic_filter_matches(topic_filter, topic)
                    for topic_filter in session["subscriptions"]
                ):
                    session["inbox"].append(message)
                    matched += 1
            return {"message_id": message["message_id"], "matched_count": matched}

    def poll_messages(self, session_id: str, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object")
        token = self._extract_token(payload, {"session_token", "max_messages"})
        if "max_messages" not in payload:
            raise ServiceError("missing required field: max_messages")
        max_messages = _validate_max_messages(payload["max_messages"])

        with self._lock:
            session = self._require_online_session_locked(session_id, token)
            inbox = session["inbox"]
            # 按发布顺序取出并移出队列；空队列返回空数组。
            messages = inbox[:max_messages]
            del inbox[:max_messages]
            return {"messages": messages}
