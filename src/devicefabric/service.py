"""Core service surface for DeviceFabric.

除健康检查外，本模块实现设备注册与身份凭据生命周期（注册、查询、
认证、轮换与吊销），以及进程内连接会话与心跳保活。所有数据仅保存
在当前进程内，进程退出即清空。
"""

from __future__ import annotations

import copy
import hmac
import json
import math
import os
import re
import secrets
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

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
COMMAND_STATUSES = (
    COMMAND_QUEUED,
    COMMAND_DELIVERED,
    "succeeded",
    "failed",
    "expired",
    "cancelled",
)
COMMAND_NON_TERMINAL = frozenset({COMMAND_QUEUED, COMMAND_DELIVERED})
COMMAND_ACK_STATUSES = frozenset({"succeeded", "failed"})

REQUEST_ID_MIN = 1
REQUEST_ID_MAX = 64

FIRMWARE_VERSION_MIN = 1
FIRMWARE_VERSION_MAX = 128
DOWNLOAD_URL_MIN = 1
DOWNLOAD_URL_MAX = 2048
# SHA-256 校验和：64 个十六进制字符（大小写均可）。
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")

FIRMWARE_UPDATE_QUEUED = "queued"
FIRMWARE_UPDATE_DELIVERED = "delivered"
FIRMWARE_UPDATE_STATUSES = (
    FIRMWARE_UPDATE_QUEUED,
    FIRMWARE_UPDATE_DELIVERED,
    "installed",
    "failed",
    "cancelled",
)
FIRMWARE_UPDATE_NON_TERMINAL = frozenset(
    {FIRMWARE_UPDATE_QUEUED, FIRMWARE_UPDATE_DELIVERED}
)
FIRMWARE_ACK_STATUSES = frozenset({"installed", "failed"})

TELEMETRY_POINTS_MIN = 1
TELEMETRY_POINTS_MAX = 500
TELEMETRY_RESOLUTIONS = frozenset({"raw", "60", "300", "3600"})
TELEMETRY_RAW_MAX_SECONDS = 24 * 3600
TELEMETRY_DOWNSAMPLED_MAX_SECONDS = 31 * 24 * 3600
TELEMETRY_PATH_ENV = "DEVICEFABRIC_TELEMETRY_PATH"
# 按设备隔离的固定窗口限流配置：每个 UTC 自然分钟内每台设备可受理的发布
# 请求数 / 遥测点数。未设置或值为 0 时关闭对应限流。
PUBLISH_RATE_LIMIT_ENV = "DEVICEFABRIC_PUBLISH_RATE_LIMIT"
TELEMETRY_POINT_RATE_LIMIT_ENV = "DEVICEFABRIC_TELEMETRY_POINT_RATE_LIMIT"
# 固定窗口长度（秒）：UTC 自然分钟；Retry-After 取值范围 1..60。
RATE_WINDOW_SECONDS = 60
RATE_WINDOW_MICROSECONDS = RATE_WINDOW_SECONDS * 1_000_000

AUDIT_RETENTION = 10000
AUDIT_LIMIT_MIN = 1
AUDIT_LIMIT_MAX = 100
AUDIT_LIMIT_DEFAULT = 50
# 审计查询的整数参数：仅接受无符号十进制数字串。
_AUDIT_INT_RE = re.compile(r"[0-9]+\Z")

# RFC 3339 时间戳：日期-时间以 T/t 分隔，必须携带 Z/z 或 ±HH:MM 时区偏移。
_RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})\Z"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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


class RateLimitError(ServiceError):
    """固定窗口配额耗尽：429 rate_limit_exceeded，携带 Retry-After 秒数。

    retry_after 为到下一个 UTC 分钟边界的向上取整秒数，范围 1 至
    RATE_WINDOW_SECONDS。
    """

    code = "rate_limit_exceeded"
    status = 429

    def __init__(self, message: str, retry_after: int):
        super().__init__(message, code=self.code, status=self.status)
        self.retry_after = retry_after


def _parse_rate_limit(raw: str | None) -> int | None:
    """解析限流环境变量：未设置或值为 0 表示关闭（返回 None）。

    仅接受十进制非负整数；负数、小数或其他无法解析的内容不构成有效配额，
    按关闭处理（None），避免非法配置中断服务启动。
    """
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value > 0 else None


def _retry_after_for_window(now_micros: int) -> int:
    """距下一个 UTC 分钟边界的向上取整秒数（窗口起点以纪元微秒对齐）。"""
    position = now_micros % RATE_WINDOW_MICROSECONDS
    remaining = RATE_WINDOW_MICROSECONDS - position
    seconds = (remaining + 999_999) // 1_000_000
    return max(1, min(RATE_WINDOW_SECONDS, seconds))


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


def _validate_identifier(value: object, field: str) -> str:
    """通用标识校验：规则同 device_id，错误信息以 field 命名。"""
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError(f"{field} must be a string")
    if not DEVICE_ID_MIN <= len(value) <= DEVICE_ID_MAX or not DEVICE_ID_RE.match(value):
        raise ServiceError(
            f"{field} must be 1-64 ASCII letters, digits, dots, underscores or hyphens"
        )
    return value


def _validate_device_id_list(value: object) -> list[str]:
    """device_ids 必须是标识符数组，各项合法且无重复，顺序保留。"""
    if not isinstance(value, list):
        raise ServiceError("device_ids must be an array")
    device_ids = [_validate_device_id(item) for item in value]
    if len(set(device_ids)) != len(device_ids):
        raise ServiceError("device_ids must not contain duplicates")
    return device_ids


def _validate_request_id(value: object) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("request_id must be a string")
    if not REQUEST_ID_MIN <= len(value) <= REQUEST_ID_MAX:
        raise ServiceError(f"request_id must be {REQUEST_ID_MIN}-{REQUEST_ID_MAX} characters")
    return value


def _validate_firmware_version(value: object) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("version must be a string")
    if not FIRMWARE_VERSION_MIN <= len(value) <= FIRMWARE_VERSION_MAX:
        raise ServiceError(
            f"version must be {FIRMWARE_VERSION_MIN}-{FIRMWARE_VERSION_MAX} characters"
        )
    return value


def _validate_download_url(value: object) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("download_url must be a string")
    if not DOWNLOAD_URL_MIN <= len(value) <= DOWNLOAD_URL_MAX:
        raise ServiceError(
            f"download_url must be {DOWNLOAD_URL_MIN}-{DOWNLOAD_URL_MAX} characters"
        )
    return value


def _validate_sha256(value: object) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError("sha256 must be a string")
    if not SHA256_RE.match(value):
        raise ServiceError("sha256 must be 64 hexadecimal characters")
    return value


def _parse_rfc3339(value: object, field: str) -> datetime:
    """解析带时区的 RFC 3339 时间戳并归一到 UTC；缺时区或形状非法即拒绝。"""
    if not isinstance(value, str) or isinstance(value, bool):
        raise ServiceError(f"{field} must be an RFC 3339 timestamp with timezone")
    if not _RFC3339_RE.match(value):
        raise ServiceError(f"{field} must be an RFC 3339 timestamp with timezone")
    text = value
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ServiceError(
            f"{field} must be an RFC 3339 timestamp with timezone"
        ) from None
    return parsed.astimezone(timezone.utc)


def _epoch_microseconds(dt: datetime) -> int:
    """自 Unix 纪元的整数微秒数，避免浮点误差影响窗口对齐。"""
    delta = dt - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _validate_telemetry_points(value: object) -> list[dict]:
    """校验并归一化一批遥测点；任一点非法则整批拒绝。

    返回的每项含 metric、UTC datetime 形式的 ts、原样数值 value，以及
    规范化后的 UTC RFC 3339 字符串 timestamp（供幂等比较与持久化）。
    """
    if not isinstance(value, list):
        raise ServiceError("points must be an array")
    if not TELEMETRY_POINTS_MIN <= len(value) <= TELEMETRY_POINTS_MAX:
        raise ServiceError(
            f"points must contain {TELEMETRY_POINTS_MIN}-{TELEMETRY_POINTS_MAX} items"
        )
    points: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            raise ServiceError("each point must be a JSON object")
        unknown = set(item) - {"metric", "timestamp", "value"}
        if unknown:
            raise ServiceError(f"unknown field(s): {', '.join(sorted(unknown))}")
        for field in ("metric", "timestamp", "value"):
            if field not in item:
                raise ServiceError(f"missing required field: {field}")
        # metric 沿用设备标识规则。
        metric = _validate_identifier(item["metric"], "metric")
        timestamp = _parse_rfc3339(item["timestamp"], "timestamp")
        point_value = item["value"]
        if not _is_json_number(point_value) or not math.isfinite(point_value):
            raise ServiceError("value must be a finite non-boolean number")
        points.append(
            {
                "metric": metric,
                "ts": timestamp,
                "timestamp": _rfc3339_utc(timestamp),
                "value": point_value,
            }
        )
    return points


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

    def __init__(
        self,
        telemetry_path: str | None = None,
        *,
        publish_rate_limit: int | None = None,
        telemetry_point_rate_limit: int | None = None,
    ) -> None:
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
        # 进程内设备组：group_id -> {"version", "device_ids"}。成员按请求
        # 顺序保存且无重复；设备吊销不移除成员。重启即清空。
        self._groups: dict[str, dict] = {}
        # 进程内命令批次：batch_id -> 批次记录。子命令按受理时的成员快照
        # 顺序保存；组的后续变化不影响旧批次。重启即清空。
        self._batches: dict[str, dict] = {}
        # 同组请求幂等索引：(group_id, request_id) -> batch_id。
        self._group_requests: dict[tuple[str, str], str] = {}
        # 记录本进程签发过的全部批次 ID，保证批次在进程内绝不重复。
        self._issued_batch_ids: set[str] = set()
        # 进程内固件发布：release_id -> 发布记录。发布创建后不可变，
        # 不提供修改或删除。重启即清空。
        self._firmware_releases: dict[str, dict] = {}
        # 进程内固件更新：update_id -> 更新记录；另按设备保存受理顺序的
        # update_id 列表，供设备按序领取最早更新。设备吊销时非终态更新
        # 变为 cancelled。重启即清空。
        self._firmware_updates: dict[str, dict] = {}
        self._device_update_ids: dict[str, list[str]] = {}
        # 进程内固件下发批次：rollout_id -> 批次记录。成员顺序与组版本
        # 为受理时快照，组的后续变化不影响本批次。重启即清空。
        self._rollouts: dict[str, dict] = {}
        # 记录本进程签发过的全部更新 ID 与批次 ID，保证进程内绝不重复。
        self._issued_update_ids: set[str] = set()
        self._issued_rollout_ids: set[str] = set()
        # 时序遥测：按受理顺序保存的全部数据点，每项为
        # {"device_id", "metric", "ts"(UTC datetime), "value", "seq"}；
        # seq 全局单调递增，同时间戳的点按受理顺序排列。凭据轮换、会话
        # 离线或设备吊销均不删除遥测。
        self._telemetry_points: list[dict] = []
        self._telemetry_seq = 0
        # 遥测幂等记录：(device_id, request_id) -> {"accepted_count",
        # "points"(规范化请求内容)}。作用域为单台设备，设备间互不影响。
        self._telemetry_requests: dict[tuple[str, str], dict] = {}
        # 变更审计日志：仅保留最近 AUDIT_RETENTION 条，淘汰最旧事件；
        # sequence 全局单调递增且不复用，淘汰不重置。事件只含 sequence、
        # occurred_at、action、resource_type、resource_id，绝不包含
        # credential、session_token、payload 或 result。仅进程内，重启即清空。
        self._audit_events: deque = deque(maxlen=AUDIT_RETENTION)
        self._audit_seq = 0
        # 持久化位置：显式参数优先，否则读 DEVICEFABRIC_TELEMETRY_PATH；
        # 未配置时数据仅存在当前进程内，进程退出即清空。
        if telemetry_path is None:
            telemetry_path = os.environ.get(TELEMETRY_PATH_ENV)
        self._telemetry_path = telemetry_path or None
        if self._telemetry_path is not None and os.path.exists(self._telemetry_path):
            self._load_telemetry()
        # 按设备隔离的固定窗口限流。显式参数优先；否则读环境变量，未设置
        # 或值为 0 时对应限流关闭（None）。计数器仅存在当前进程内，重启即
        # 清空，且不随遥测落盘。每类限流为 (limit, {device_id: (window,
        # used)})，window 为 UTC 纪元对齐的分钟序号。
        if publish_rate_limit is None:
            publish_rate_limit = _parse_rate_limit(
                os.environ.get(PUBLISH_RATE_LIMIT_ENV)
            )
        self._publish_rate_limit = publish_rate_limit or None
        if telemetry_point_rate_limit is None:
            telemetry_point_rate_limit = _parse_rate_limit(
                os.environ.get(TELEMETRY_POINT_RATE_LIMIT_ENV)
            )
        self._telemetry_point_rate_limit = telemetry_point_rate_limit or None
        self._publish_usage: dict[str, tuple[int, int]] = {}
        self._telemetry_usage: dict[str, tuple[int, int]] = {}

    def _load_telemetry(self) -> None:
        """从持久化文件恢复遥测数据与幂等记录（启动时调用，无需持锁）。"""
        with open(self._telemetry_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        self._telemetry_seq = state["seq"]
        for entry in state["points"]:
            self._telemetry_points.append(
                {
                    "device_id": entry["device_id"],
                    "metric": entry["metric"],
                    "ts": _parse_rfc3339(entry["timestamp"], "timestamp"),
                    "value": entry["value"],
                    "seq": entry["seq"],
                }
            )
        for record in state["requests"]:
            key = (record["device_id"], record["request_id"])
            self._telemetry_requests[key] = {
                "accepted_count": record["accepted_count"],
                "points": record["points"],
            }

    def _persist_telemetry_locked(
        self,
        new_entries: list[dict],
        new_request: tuple[tuple[str, str], dict],
    ) -> None:
        """把含新批次在内的完整遥测状态原子写盘；失败抛 OSError。"""
        (request_key, request_record) = new_request
        state = {
            "version": 1,
            "seq": self._telemetry_seq + len(new_entries),
            "points": [
                {
                    "device_id": entry["device_id"],
                    "metric": entry["metric"],
                    "timestamp": _rfc3339_utc(entry["ts"]),
                    "value": entry["value"],
                    "seq": entry["seq"],
                }
                for entry in (*self._telemetry_points, *new_entries)
            ],
            "requests": [
                {
                    "device_id": key[0],
                    "request_id": key[1],
                    "accepted_count": record["accepted_count"],
                    "points": record["points"],
                }
                for key, record in (
                    *self._telemetry_requests.items(),
                    (request_key, request_record),
                )
            ],
        }
        temp_path = self._telemetry_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
        os.replace(temp_path, self._telemetry_path)

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

    def _append_audit_locked(self, action: str, resource_id: str) -> None:
        """在状态成功提交的同一锁内原子追加一条审计事件。

        resource_type 取 action 的点号前缀；occurred_at 为当前 UTC
        RFC 3339 时间。仅在调用方已完成状态变更且不会失败时调用。
        """
        self._audit_seq += 1
        self._audit_events.append(
            {
                "sequence": self._audit_seq,
                "occurred_at": _rfc3339_utc(_utc_now()),
                "action": action,
                "resource_type": action.split(".", 1)[0],
                "resource_id": resource_id,
            }
        )

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
                # 设备当前固件：初始无固件；确认 installed 后更新为对应
                # 发布（安装旧发布即回滚，不做版本比较）。仅进程内记录。
                "firmware_release_id": None,
                "firmware_version": None,
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
            self._append_audit_locked("device.created", device_id)
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
            self._append_audit_locked("device.credential_rotated", device_id)
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
            was_active = device["active"]
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
            # 吊销同时使该设备全部非终态固件更新变为 cancelled；已终态
            # （installed/failed）的更新保持不变。
            for update_id in self._device_update_ids.get(device_id, []):
                update = self._firmware_updates[update_id]
                if update["status"] in FIRMWARE_UPDATE_NON_TERMINAL:
                    update["status"] = "cancelled"
            # 幂等重吊销未改变状态，不追加审计事件。
            if was_active:
                self._append_audit_locked("device.revoked", device_id)
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

    def _acquire_rate_quota_locked(
        self,
        limit: int | None,
        usage: dict[str, tuple[int, int]],
        device_id: str,
        amount: int,
        now: datetime,
    ) -> None:
        """在已持锁状态下为某设备原子占用固定窗口额度。

        窗口按 UTC 自然分钟对齐（纪元分钟序号）；跨分钟立即恢复完整额度。
        剩余额度不足时整笔占用失败并抛 RateLimitError，不改变任何计数或
        其他状态。limit 为 None（限流关闭）时直接放行。
        """
        if limit is None:
            return
        now_micros = _epoch_microseconds(now)
        window = now_micros // RATE_WINDOW_MICROSECONDS
        current_window, used = usage.get(device_id, (window, 0))
        if current_window != window:
            used = 0
        if used + amount > limit:
            raise RateLimitError(
                "rate limit exceeded for this device in the current UTC minute window",
                _retry_after_for_window(now_micros),
            )
        usage[device_id] = (window, used + amount)

    @staticmethod
    def _rollback_rate_quota_locked(
        usage: dict[str, tuple[int, int]], device_id: str, amount: int
    ) -> None:
        """归还同一锁内刚占用且随后失败（如落盘 503）的额度。"""
        record = usage.get(device_id)
        if record is None:
            return
        window, used = record
        usage[device_id] = (window, max(0, used - amount))

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
            # 通过字段校验、会话鉴权与在线状态检查后占用一个发布额度；
            # 额度不足时原消息、保留状态、订阅队列、离线队列与规则动作均
            # 不产生变化。规则动作不另行计数。
            self._acquire_rate_quota_locked(
                self._publish_rate_limit,
                self._publish_usage,
                publisher["device_id"],
                1,
                now,
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
            self._append_audit_locked("rule.created", rule_id)
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
            # 值未变化的幂等调用不追加审计事件。
            if rule["enabled"] != enabled:
                rule["enabled"] = enabled
                self._append_audit_locked("rule.enabled_changed", rule_id)
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
            self._append_audit_locked("rule.deleted", rule_id)
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
            now = _utc_now()
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
    # 进程内设备组
    # ------------------------------------------------------------------

    @staticmethod
    def _public_group_locked(group: dict) -> dict:
        """设备组快照；成员列表按既定顺序返回副本。"""
        return {
            "group_id": group["group_id"],
            "version": group["version"],
            "device_ids": list(group["device_ids"]),
        }

    def create_group(self, payload: object) -> dict:
        data = self._require_fields(payload, {"group_id", "device_ids"})
        group_id = _validate_identifier(data["group_id"], "group_id")
        device_ids = _validate_device_id_list(data["device_ids"])

        with self._lock:
            if group_id in self._groups:
                raise ServiceError(
                    f"group {group_id!r} already exists",
                    code="group_already_exists",
                    status=409,
                )
            # 成员必须全部是已注册设备；任一不存在则整体失败、不建组。
            for device_id in device_ids:
                if device_id not in self._devices:
                    raise ServiceError(
                        f"device {device_id!r} not found",
                        code="device_not_found",
                        status=404,
                    )
            group = {
                "group_id": group_id,
                "version": 1,
                "device_ids": list(device_ids),
            }
            self._groups[group_id] = group
            self._append_audit_locked("group.created", group_id)
            return self._public_group_locked(group)

    def get_group(self, group_id: str) -> dict:
        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise ServiceError(
                    f"group {group_id!r} not found",
                    code="group_not_found",
                    status=404,
                )
            return self._public_group_locked(group)

    def replace_group_members(self, group_id: str, payload: object) -> dict:
        data = self._require_fields(
            payload, {"device_ids"}, optional={"expected_version"}
        )
        device_ids = _validate_device_id_list(data["device_ids"])
        expected_version = None
        if "expected_version" in data:
            expected_version = self._validate_expected_version(
                data["expected_version"]
            )

        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise ServiceError(
                    f"group {group_id!r} not found",
                    code="group_not_found",
                    status=404,
                )
            if expected_version is not None and expected_version != group["version"]:
                raise ServiceError(
                    f"group version conflict: expected {expected_version}, "
                    f"current {group['version']}",
                    code="group_version_conflict",
                    status=409,
                )
            # 成员必须全部存在；任一不存在则整体失败、组保持不变。
            # 吊销设备仍在注册表中，加入/保留成员不被此检查阻止。
            for device_id in device_ids:
                if device_id not in self._devices:
                    raise ServiceError(
                        f"device {device_id!r} not found",
                        code="device_not_found",
                        status=404,
                    )
            # 整体替换：即使新成员与现有成员完全相同，版本也照常加一。
            group["device_ids"] = list(device_ids)
            group["version"] += 1
            self._append_audit_locked("group.members_replaced", group_id)
            return self._public_group_locked(group)

    # ------------------------------------------------------------------
    # 设备组命令批次
    # ------------------------------------------------------------------

    def _mint_batch_id_locked(self) -> str:
        batch_id = secrets.token_urlsafe(18)
        while batch_id in self._issued_batch_ids:
            batch_id = secrets.token_urlsafe(18)
        self._issued_batch_ids.add(batch_id)
        return batch_id

    def _create_command_for_group_locked(
        self,
        device_id: str,
        command_name: str,
        command_payload: object,
        ttl_seconds: int,
        now: datetime,
    ) -> str:
        """在批次锁内为成员创建命令，返回 command_id。"""
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
            "owner_session_id": None,
        }
        self._commands[command["command_id"]] = command
        self._device_command_ids.setdefault(device_id, []).append(
            command["command_id"]
        )
        return command["command_id"]

    def create_command_batch(self, group_id: str, payload: object) -> dict:
        data = self._require_fields(
            payload,
            {"request_id", "command_name", "payload", "ttl_seconds"},
            optional={"expected_group_version"},
        )
        request_id = _validate_request_id(data["request_id"])
        command_name = _validate_command_name(data["command_name"])
        command_payload = data["payload"]
        ttl_seconds = _validate_ttl_seconds(data["ttl_seconds"])
        expected_version = None
        if "expected_group_version" in data:
            expected_version = self._validate_expected_version(
                data["expected_group_version"]
            )

        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise ServiceError(
                    f"group {group_id!r} not found",
                    code="group_not_found",
                    status=404,
                )
            # 同组重复 request_id：先于版本与成员检查进行幂等/冲突判定。
            # 并发的相同请求持同一把锁，至多创建一个批次。
            existing_id = self._group_requests.get((group_id, request_id))
            if existing_id is not None:
                existing = self._batches[existing_id]
                same_request = (
                    existing["command_name"] == command_name
                    and existing["ttl_seconds"] == ttl_seconds
                    and _json_equal(existing["payload"], command_payload)
                )
                if not same_request:
                    raise ServiceError(
                        f"request_id {request_id!r} already used with different content",
                        code="batch_request_conflict",
                        status=409,
                    )
                return self._public_batch_locked(existing)
            if expected_version is not None and expected_version != group["version"]:
                raise ServiceError(
                    f"group version conflict: expected {expected_version}, "
                    f"current {group['version']}",
                    code="group_version_conflict",
                    status=409,
                )
            members = list(group["device_ids"])
            # 受理时成员中含已吊销设备：整批拒绝，不创建任何子命令。
            for device_id in members:
                device = self._devices.get(device_id)
                if device is None or not device["active"]:
                    raise ServiceError(
                        f"group {group_id!r} contains revoked device {device_id!r}",
                        code="group_contains_revoked_device",
                        status=409,
                    )
            now = _utc_now()
            batch = {
                "batch_id": self._mint_batch_id_locked(),
                "group_id": group_id,
                "request_id": request_id,
                "command_name": command_name,
                "payload": copy.deepcopy(command_payload),
                "ttl_seconds": ttl_seconds,
                # 采用受理时的成员顺序与组版本（快照，组的后续变化不影响
                # 本批次）。
                "group_version": group["version"],
                "device_ids": members,
                "command_ids": [
                    self._create_command_for_group_locked(
                        device_id, command_name, command_payload, ttl_seconds, now
                    )
                    for device_id in members
                ],
                "created_at": now,
            }
            self._batches[batch["batch_id"]] = batch
            self._group_requests[(group_id, request_id)] = batch["batch_id"]
            return self._public_batch_locked(batch)

    def _public_batch_locked(self, batch: dict) -> dict:
        """批次受理视图：batch_id、采用的组版本与按成员顺序排列的
        device_id/command_id 对。"""
        return {
            "batch_id": batch["batch_id"],
            "group_version": batch["group_version"],
            "commands": [
                {"device_id": device_id, "command_id": command_id}
                for device_id, command_id in zip(
                    batch["device_ids"], batch["command_ids"]
                )
            ],
        }

    def _public_batch_status_locked(self, batch: dict, now: datetime) -> dict:
        """批次查询视图：子命令当前快照（按创建时成员顺序）与状态汇总。"""
        counts = {status: 0 for status in COMMAND_STATUSES}
        commands: list[dict] = []
        for command_id in batch["command_ids"]:
            command = self._commands[command_id]
            self._expire_command_locked(command, now)
            counts[command["status"]] += 1
            commands.append(self._public_command_locked(command))
        return {
            "batch_id": batch["batch_id"],
            "group_version": batch["group_version"],
            "commands": commands,
            "status_counts": counts,
        }

    def get_command_batch(self, group_id: str, batch_id: str) -> dict:
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None or batch["group_id"] != group_id:
                raise ServiceError(
                    f"batch {batch_id!r} not found",
                    code="batch_not_found",
                    status=404,
                )
            return self._public_batch_status_locked(batch, _utc_now())

    # ------------------------------------------------------------------
    # 固件 OTA：发布登记、组下发与设备领取/确认
    # ------------------------------------------------------------------

    @staticmethod
    def _public_firmware_release_locked(release: dict) -> dict:
        """发布完整视图；发布创建后不可变。"""
        return {
            "release_id": release["release_id"],
            "version": release["version"],
            "download_url": release["download_url"],
            "sha256": release["sha256"],
            "created_at": _rfc3339_utc(release["created_at"]),
        }

    def create_firmware_release(self, payload: object) -> dict:
        data = self._require_fields(
            payload, {"release_id", "version", "download_url", "sha256"}
        )
        release_id = _validate_identifier(data["release_id"], "release_id")
        version = _validate_firmware_version(data["version"])
        download_url = _validate_download_url(data["download_url"])
        sha256 = _validate_sha256(data["sha256"])

        with self._lock:
            # 发布不可变：重名一律冲突，即使内容完全相同也不覆盖。
            if release_id in self._firmware_releases:
                raise ServiceError(
                    f"firmware release {release_id!r} already exists",
                    code="firmware_release_already_exists",
                    status=409,
                )
            release = {
                "release_id": release_id,
                "version": version,
                "download_url": download_url,
                "sha256": sha256,
                "created_at": _utc_now(),
            }
            self._firmware_releases[release_id] = release
            self._append_audit_locked("firmware_release.created", release_id)
            return self._public_firmware_release_locked(release)

    def _mint_update_id_locked(self) -> str:
        update_id = secrets.token_urlsafe(18)
        while update_id in self._issued_update_ids:
            update_id = secrets.token_urlsafe(18)
        self._issued_update_ids.add(update_id)
        return update_id

    def _mint_rollout_id_locked(self) -> str:
        rollout_id = secrets.token_urlsafe(18)
        while rollout_id in self._issued_rollout_ids:
            rollout_id = secrets.token_urlsafe(18)
        self._issued_rollout_ids.add(rollout_id)
        return rollout_id

    @staticmethod
    def _public_firmware_update_locked(update: dict) -> dict:
        """更新完整快照；发布字段在受理时固化（发布本身不可变）。"""
        return {
            "update_id": update["update_id"],
            "rollout_id": update["rollout_id"],
            "device_id": update["device_id"],
            "release_id": update["release_id"],
            "version": update["version"],
            "download_url": update["download_url"],
            "sha256": update["sha256"],
            "status": update["status"],
            "created_at": _rfc3339_utc(update["created_at"]),
            "completed_at": (
                _rfc3339_utc(update["completed_at"])
                if update["completed_at"] is not None
                else None
            ),
        }

    def create_firmware_rollout(self, group_id: str, payload: object) -> dict:
        data = self._require_fields(
            payload, {"release_id"}, optional={"expected_group_version"}
        )
        release_id = _validate_identifier(data["release_id"], "release_id")
        expected_version = None
        if "expected_group_version" in data:
            expected_version = self._validate_expected_version(
                data["expected_group_version"]
            )

        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                raise ServiceError(
                    f"group {group_id!r} not found",
                    code="group_not_found",
                    status=404,
                )
            release = self._firmware_releases.get(release_id)
            if release is None:
                raise ServiceError(
                    f"firmware release {release_id!r} not found",
                    code="firmware_release_not_found",
                    status=404,
                )
            if expected_version is not None and expected_version != group["version"]:
                raise ServiceError(
                    f"group version conflict: expected {expected_version}, "
                    f"current {group['version']}",
                    code="group_version_conflict",
                    status=409,
                )
            members = list(group["device_ids"])
            # 受理时成员中含已吊销设备：整批拒绝，不创建任何更新。
            for device_id in members:
                device = self._devices.get(device_id)
                if device is None or not device["active"]:
                    raise ServiceError(
                        f"group {group_id!r} contains revoked device {device_id!r}",
                        code="group_contains_revoked_device",
                        status=409,
                    )
            now = _utc_now()
            rollout = {
                "rollout_id": self._mint_rollout_id_locked(),
                "group_id": group_id,
                "release_id": release_id,
                # 采用受理时的成员顺序与组版本（快照，组的后续变化不影响
                # 本批次）。空组也成功，仅没有成员更新。
                "group_version": group["version"],
                "device_ids": members,
                "update_ids": [],
                "created_at": now,
            }
            for device_id in members:
                update = {
                    "update_id": self._mint_update_id_locked(),
                    "rollout_id": rollout["rollout_id"],
                    "device_id": device_id,
                    "release_id": release_id,
                    "version": release["version"],
                    "download_url": release["download_url"],
                    "sha256": release["sha256"],
                    "status": FIRMWARE_UPDATE_QUEUED,
                    "created_at": now,
                    "completed_at": None,
                    # 当前领取该更新的会话；仅 queued/delivered 状态下有意义。
                    "owner_session_id": None,
                }
                self._firmware_updates[update["update_id"]] = update
                self._device_update_ids.setdefault(device_id, []).append(
                    update["update_id"]
                )
                rollout["update_ids"].append(update["update_id"])
            self._rollouts[rollout["rollout_id"]] = rollout
            self._append_audit_locked(
                "firmware_rollout.created", rollout["rollout_id"]
            )
            return {
                "rollout_id": rollout["rollout_id"],
                "release_id": release_id,
                "group_version": rollout["group_version"],
                "updates": [
                    {"device_id": device_id, "update_id": update_id}
                    for device_id, update_id in zip(
                        rollout["device_ids"], rollout["update_ids"]
                    )
                ],
            }

    def get_firmware_rollout(self, group_id: str, rollout_id: str) -> dict:
        with self._lock:
            rollout = self._rollouts.get(rollout_id)
            if rollout is None or rollout["group_id"] != group_id:
                raise ServiceError(
                    f"rollout {rollout_id!r} not found",
                    code="rollout_not_found",
                    status=404,
                )
            counts = {status: 0 for status in FIRMWARE_UPDATE_STATUSES}
            updates: list[dict] = []
            for update_id in rollout["update_ids"]:
                update = self._firmware_updates[update_id]
                counts[update["status"]] += 1
                updates.append(self._public_firmware_update_locked(update))
            return {
                "rollout_id": rollout["rollout_id"],
                "release_id": rollout["release_id"],
                "group_version": rollout["group_version"],
                "updates": updates,
                "status_counts": counts,
            }

    def poll_firmware_update(self, session_id: str, payload: object) -> dict:
        data = self._require_fields(payload, {"session_token"})
        token = data["session_token"]
        if not isinstance(token, str) or isinstance(token, bool):
            raise ServiceError("session_token must be a string")

        with self._lock:
            now = _utc_now()
            # 沿用会话鉴权及保活规则：未知 404、令牌错误 401、已离线 409。
            session = self._authenticate_online_session_locked(
                session_id, token, now
            )
            # 按受理顺序领取本设备最早的非终态更新；单个更新在任一时刻
            # 至多归属一个在线会话（锁内完成判定与归属转移）。
            for update_id in self._device_update_ids.get(session["device_id"], []):
                update = self._firmware_updates[update_id]
                if update["status"] == FIRMWARE_UPDATE_QUEUED:
                    # 首次领取：转为 delivered，dup 为 false。
                    update["status"] = FIRMWARE_UPDATE_DELIVERED
                    update["owner_session_id"] = session["session_id"]
                    dup = False
                elif update["status"] == FIRMWARE_UPDATE_DELIVERED:
                    owner_id = update["owner_session_id"]
                    if owner_id == session["session_id"]:
                        # 确认前由同一会话重领：update_id 不变，dup 为 true。
                        dup = True
                    else:
                        owner = self._sessions.get(owner_id)
                        if owner is not None:
                            self._expire_if_timed_out_locked(owner, now)
                        if owner is not None and owner["state"] == SESSION_ONLINE:
                            # 仍归属另一在线会话，本会话不得领取。
                            continue
                        # 领取会话已超时或被替换：归属转移，仍为重复投递。
                        update["owner_session_id"] = session["session_id"]
                        dup = True
                else:
                    # 终态更新（installed/failed/cancelled）不再领取。
                    continue
                item = self._public_firmware_update_locked(update)
                item["dup"] = dup
                return {"update": item}
            # 无待领取更新：200 且 update 为 null。
            return {"update": None}

    def ack_firmware_update(
        self, session_id: str, update_id: str, payload: object
    ) -> dict:
        data = self._require_fields(payload, {"session_token", "status"})
        token = data["session_token"]
        if not isinstance(token, str) or isinstance(token, bool):
            raise ServiceError("session_token must be a string")
        status = data["status"]
        if (
            not isinstance(status, str)
            or isinstance(status, bool)
            or status not in FIRMWARE_ACK_STATUSES
        ):
            raise ServiceError("status must be 'installed' or 'failed'")

        with self._lock:
            # 沿用会话鉴权及保活规则：未知 404、令牌错误 401、已离线 409。
            session = self._authenticate_online_session_locked(
                session_id, token, _utc_now()
            )
            update = self._firmware_updates.get(update_id)
            # 其他设备的更新对本会话不可见，与不存在同等处理。
            if update is None or update["device_id"] != session["device_id"]:
                raise ServiceError(
                    f"firmware update {update_id!r} not found",
                    code="firmware_update_not_found",
                    status=404,
                )
            current = update["status"]
            if current in FIRMWARE_ACK_STATUSES:
                # 已确认：相同状态幂等返回当前快照，状态冲突报错。
                if current == status:
                    return self._public_firmware_update_locked(update)
                raise ServiceError(
                    f"firmware update {update_id!r} already completed",
                    code="firmware_update_already_completed",
                    status=409,
                )
            if current == "cancelled":
                raise ServiceError(
                    f"firmware update {update_id!r} already completed",
                    code="firmware_update_already_completed",
                    status=409,
                )
            # 首次确认只接受当前领取会话；未投递（含投递给其他会话）拒绝。
            if (
                current == FIRMWARE_UPDATE_QUEUED
                or update["owner_session_id"] != session["session_id"]
            ):
                raise ServiceError(
                    f"firmware update {update_id!r} not delivered",
                    code="firmware_update_not_delivered",
                    status=409,
                )
            update["status"] = status
            update["completed_at"] = _utc_now()
            if status == "installed":
                # 安装成功即更新设备当前固件；安装旧发布即为回滚，不做
                # 版本比较。
                device = self._devices[update["device_id"]]
                device["firmware_release_id"] = update["release_id"]
                device["firmware_version"] = update["version"]
            return self._public_firmware_update_locked(update)

    # ------------------------------------------------------------------
    # 时序遥测：写入、幂等与固定窗口降采样查询
    # ------------------------------------------------------------------

    def submit_telemetry(self, session_id: str, payload: object) -> dict:
        data = self._require_fields(
            payload, {"session_token", "request_id", "points"}
        )
        token = data["session_token"]
        if not isinstance(token, str) or isinstance(token, bool):
            raise ServiceError("session_token must be a string")
        request_id = _validate_request_id(data["request_id"])
        # 整批原子校验：任一点非法则整批拒绝，不产生任何状态。
        points = _validate_telemetry_points(data["points"])

        with self._lock:
            now = _utc_now()
            # 沿用会话鉴权及保活规则：未知 404、令牌错误 401、已离线 409。
            session = self._authenticate_online_session_locked(
                session_id, token, now
            )
            device_id = session["device_id"]
            key = (device_id, request_id)
            existing = self._telemetry_requests.get(key)
            if existing is not None:
                # 同设备同 request_id：内容相同返回原结果且不重复写入，
                # 内容不同拒绝。request_id 作用域为单台设备。
                same_request = _json_equal(
                    existing["points"],
                    [
                        {
                            "metric": point["metric"],
                            "timestamp": point["timestamp"],
                            "value": point["value"],
                        }
                        for point in points
                    ],
                )
                if not same_request:
                    raise ServiceError(
                        f"request_id {request_id!r} already used with different content",
                        code="telemetry_request_conflict",
                        status=409,
                    )
                return {
                    "request_id": request_id,
                    "accepted_count": existing["accepted_count"],
                }
            # 非幂等重试的新批次：按 points 数量原子占用额度。整批超过
            # 剩余额度时不写入任何数据或幂等记录。
            point_count = len(points)
            self._acquire_rate_quota_locked(
                self._telemetry_point_rate_limit,
                self._telemetry_usage,
                device_id,
                point_count,
                now,
            )
            entries = []
            for index, point in enumerate(points, start=1):
                entries.append(
                    {
                        "device_id": device_id,
                        "metric": point["metric"],
                        "ts": point["ts"],
                        "value": point["value"],
                        "seq": self._telemetry_seq + index,
                    }
                )
            record = {
                "accepted_count": len(points),
                "points": [
                    {
                        "metric": point["metric"],
                        "timestamp": point["timestamp"],
                        "value": point["value"],
                    }
                    for point in points
                ],
            }
            if self._telemetry_path is not None:
                # 先持久化再提交内存：存储失败时数据与幂等记录均不可见。
                try:
                    self._persist_telemetry_locked(entries, (key, record))
                except OSError as exc:
                    # 既有 503 失败不消耗额度：回滚本批占用的窗口计数。
                    self._rollback_rate_quota_locked(
                        self._telemetry_usage, device_id, point_count
                    )
                    raise ServiceError(
                        "telemetry storage unavailable",
                        code="telemetry_storage_unavailable",
                        status=503,
                    ) from exc
            self._telemetry_points.extend(entries)
            self._telemetry_seq += len(entries)
            self._telemetry_requests[key] = record
            return {"request_id": request_id, "accepted_count": len(points)}

    @staticmethod
    def _parse_telemetry_query(query: str) -> dict:
        """解析并校验遥测查询参数；任一非法或越界即 400。"""
        pairs = parse_qs(query, keep_blank_values=True)
        unknown = set(pairs) - {"metric", "start", "end", "resolution"}
        if unknown:
            raise ServiceError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
        for field in ("metric", "start", "end", "resolution"):
            if field not in pairs or len(pairs[field]) != 1:
                raise ServiceError(f"parameter {field} must appear exactly once")
        metric = _validate_identifier(pairs["metric"][0], "metric")
        start = _parse_rfc3339(pairs["start"][0], "start")
        end = _parse_rfc3339(pairs["end"][0], "end")
        resolution = pairs["resolution"][0]
        if resolution not in TELEMETRY_RESOLUTIONS:
            raise ServiceError("resolution must be one of: raw, 60, 300, 3600")
        if not start < end:
            raise ServiceError("start must be earlier than end")
        limit = (
            TELEMETRY_RAW_MAX_SECONDS
            if resolution == "raw"
            else TELEMETRY_DOWNSAMPLED_MAX_SECONDS
        )
        if (end - start).total_seconds() > limit:
            raise ServiceError("query range exceeds the limit for this resolution")
        return {
            "metric": metric,
            "start": start,
            "end": end,
            "resolution": resolution,
        }

    def query_telemetry(self, device_id: str, query: str) -> dict:
        params = self._parse_telemetry_query(query)
        metric = params["metric"]
        start = params["start"]
        end = params["end"]
        resolution = params["resolution"]

        with self._lock:
            if device_id not in self._devices:
                raise ServiceError(
                    f"device {device_id!r} not found",
                    code="device_not_found",
                    status=404,
                )
            # 已吊销设备仍可查询；区间为 [start, end)。
            points = [
                point
                for point in self._telemetry_points
                if point["device_id"] == device_id
                and point["metric"] == metric
                and start <= point["ts"] < end
            ]
            # 按时间升序，同时间按受理顺序。
            points.sort(key=lambda point: (point["ts"], point["seq"]))
            result = {
                "device_id": device_id,
                "metric": metric,
                "resolution": resolution,
            }
            if resolution == "raw":
                result["points"] = [
                    {
                        "timestamp": _rfc3339_utc(point["ts"]),
                        "value": point["value"],
                    }
                    for point in points
                ]
                return result
            result["buckets"] = self._downsample_points(
                points, int(resolution)
            )
            return result

    @staticmethod
    def _downsample_points(points: list[dict], resolution_seconds: int) -> list[dict]:
        """按 Unix 纪元对齐的固定窗口聚合；仅返回非空窗口。"""
        window_micros = resolution_seconds * 1_000_000
        windows: dict[int, list[dict]] = {}
        for point in points:
            window = _epoch_microseconds(point["ts"]) // window_micros
            windows.setdefault(window, []).append(point)
        buckets: list[dict] = []
        for window in sorted(windows):
            members = windows[window]
            values = [point["value"] for point in members]
            # 窗口内最后一点：时间最晚，同时间取受理顺序最后。
            last = max(members, key=lambda point: (point["ts"], point["seq"]))
            window_start = _EPOCH + timedelta(
                microseconds=window * window_micros
            )
            buckets.append(
                {
                    "start": _rfc3339_utc(window_start),
                    "count": len(members),
                    "min": min(values),
                    "max": max(values),
                    "avg": sum(values) / len(values),
                    "last": last["value"],
                }
            )
        return buckets

    # ------------------------------------------------------------------
    # 变更审计日志
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_audit_int(value: str, field: str) -> int:
        """解析审计查询的无符号十进制整数参数；形状非法即 400。"""
        if not _AUDIT_INT_RE.match(value):
            raise ServiceError(f"parameter {field} must be a non-negative integer")
        return int(value)

    def query_audit_events(self, query: str) -> dict:
        """查询审计日志：过滤后按 sequence 升序截取一页。

        after 只返回 sequence 严格大于它的事件；limit 缺省 50，范围 1-100；
        action 与 resource_id 为可选单值精确过滤。after 早于现存最老事件
        的前一序号时返回 410（audit_cursor_expired）；未传 after 则从现存
        最早事件读取。本查询自身不记入审计。
        """
        pairs = parse_qs(query, keep_blank_values=True)
        unknown = set(pairs) - {"after", "limit", "action", "resource_id"}
        if unknown:
            raise ServiceError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
        for field, values in pairs.items():
            if len(values) != 1:
                raise ServiceError(f"parameter {field} must appear at most once")
        after = None
        if "after" in pairs:
            after = self._parse_audit_int(pairs["after"][0], "after")
        limit = AUDIT_LIMIT_DEFAULT
        if "limit" in pairs:
            limit = self._parse_audit_int(pairs["limit"][0], "limit")
            if not AUDIT_LIMIT_MIN <= limit <= AUDIT_LIMIT_MAX:
                raise ServiceError(
                    f"limit must be between {AUDIT_LIMIT_MIN} and {AUDIT_LIMIT_MAX}"
                )
        action = pairs["action"][0] if "action" in pairs else None
        resource_id = pairs["resource_id"][0] if "resource_id" in pairs else None

        with self._lock:
            events = self._audit_events
            if after is not None and events and after < events[0]["sequence"] - 1:
                raise ServiceError(
                    "audit cursor expired",
                    code="audit_cursor_expired",
                    status=410,
                )
            page: list[dict] = []
            for event in events:
                if after is not None and event["sequence"] <= after:
                    continue
                if action is not None and event["action"] != action:
                    continue
                if resource_id is not None and event["resource_id"] != resource_id:
                    continue
                page.append(dict(event))
                if len(page) == limit:
                    break
            # 空页的游标停留在 after（未传 after 时为 0）。
            next_after = (
                page[-1]["sequence"] if page else (after if after is not None else 0)
            )
            return {"events": page, "next_after": next_after}
