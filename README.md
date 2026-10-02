# DeviceFabric

这是一个面向物联网的物联网设备接入、编排与治理平台。长期目标是提供设备注册与身份签发、MQTT 风格主题路由、QoS 与离线消息、物模型与影子状态、规则引擎、时序数据落盘、固件/OTA 分发和多租户隔离，把设备接入与治理沉淀为可复用服务。

仓库采用 Python。冻结基线提供进程健康检查；设备注册与身份凭据生命周期已在此基础上实现。后续能力必须通过独立题目逐步实现；每个题目都应定义可观察的公共行为、兼容边界和失败语义，不得依赖未公开内部 API。

## 启动

```bash
PYTHONPATH=src python3 -m devicefabric.server --host 127.0.0.1 --port 8080
```

服务默认监听 `127.0.0.1:8080`，可通过 `DEVICEFABRIC_ADDR` 修改。`GET /healthz` 返回 JSON 健康状态。

## 设备注册与凭据

数据仅保存在当前进程内，进程退出即清空。

- `POST /v1/devices`：请求体为 `{"device_id": ..., "display_name": ...}`。`device_id` 为 1-64 个 ASCII 字母、数字、点、下划线或短横线；`display_name` 为 1-128 个 Unicode 字符。成功返回 `201`、设备对象以及仅此次可见的非空 `credential`，对象包含 `active`、UTC RFC 3339 的 `created_at` 和值为 `1` 的 `credential_version`。重复注册返回 `409`（`device_already_exists`），原记录不变且不签发凭据。
- `GET /v1/devices/{device_id}`：返回 `200` 与不含凭据的设备对象；设备不存在返回 `404`（`device_not_found`）。
- `POST /v1/device-auth`：请求体为 `{"device_id": ..., "credential": ...}`。当前凭据且设备 active 时返回 `200 {"authenticated": true}`；凭据错误、已轮换失效或设备已吊销统一返回 `401`（`invalid_credential`）。
- `POST /v1/devices/{device_id}/credential/rotate`：返回 `200`、新凭据与恰好加一的版本，旧凭据立即失效；设备已吊销返回 `409`（`device_revoked`）。
- `POST /v1/devices/{device_id}/revoke`：首次将状态置为 `revoked` 并返回 `200`；重复调用仍返回 `200` 且版本不变。

非 JSON、非对象、缺少必填字段、含未知字段或字段越界均返回 `400`（`invalid_request`）且不产生状态；查询、轮换或吊销不存在的设备返回 `404`（`device_not_found`）。错误体统一为 `{"error": {"code": ..., "message": ...}}`。

## 连接会话与心跳

会话仅保存在当前进程内，服务重启即清空。

- `POST /v1/device-sessions`：请求体为 `{"device_id": ..., "credential": ..., "client_id": ..., "keepalive_seconds": ...}`。`client_id` 校验规则同 `device_id`；`keepalive_seconds` 为 5 至 3600 的整数。当前凭据有效且设备 active 时返回 `201`，包含唯一不可预测的 `session_id`、仅此次返回的 `session_token`，以及 `device_id`、`client_id`、`connected_at`、`last_seen_at`、`expires_at`（UTC RFC 3339）和 `online` 状态。同设备同 `client_id` 重连时新会话取代旧会话，旧会话变为 `closed`（reason 为 `replaced`）。设备不存在、凭据错误、凭据已轮换或设备已吊销统一返回 `401`（`invalid_credential`）。
- `POST /v1/device-sessions/{session_id}/heartbeat`：请求体为 `{"session_token": ...}`。在线会话返回 `200` 并刷新 `last_seen_at` 与 `expires_at`；token 不匹配返回 `401`（`invalid_session_token`），对 `closed` 或 `expired` 会话心跳返回 `409`（`session_not_online`），失败请求不刷新时间。
- `GET /v1/device-sessions/{session_id}`：返回 `200` 与不含 `session_token` 的会话快照（含 `state` 与 `reason`）；未知会话返回 `404`（`session_not_found`）。

读取或心跳时，超过 `expires_at` 的在线会话转为 `expired`（reason 为 `keepalive_timeout`），不可恢复。吊销设备时其在线会话立即变为 `closed`（reason 为 `device_revoked`），吊销响应与幂等语义不变。

## 主题路由

订阅与消息仅保存在当前进程内，服务重启即清空。主题（topic）与主题过滤器（topic_filter）均为 1-256 个 Unicode 码点，按 `/` 分层且每层非空，禁止 NUL。topic 不得含通配符；过滤器中 `+` 只能独占一层并恰好匹配一层，`#` 只能独占最后一层、最多出现一次，可匹配零层或多层。

- `POST /v1/device-sessions/{session_id}/subscriptions`：请求体为 `{"session_token": ..., "topic_filter": ...}`。在线会话返回 `200 {"topic_filter": ...}`；重复订阅同一过滤器幂等，不产生副本。
- `POST /v1/device-sessions/{session_id}/publish`：请求体为 `{"session_token": ..., "topic": ..., "payload": ...}`。`payload` 可为任意 JSON 值（null、标量、数组或对象）。返回 `202`、唯一的 `message_id` 与 `matched_count`（实际入队的在线会话数）。同一会话即使被多个过滤器命中也只入队一份；允许发布给自身。消息包含 `message_id`、`topic`、`payload`、`publisher_device_id` 和 UTC RFC 3339 的 `published_at`。
- `POST /v1/device-sessions/{session_id}/messages/poll`：请求体为 `{"session_token": ..., "max_messages": ...}`，`max_messages` 为 1 至 100 的整数。返回 `200 {"messages": [...]}`，消息按发布顺序排列并在返回后移出队列；空队列返回空数组。

三个入口对未知会话返回 `404`（`session_not_found`），令牌错误返回 `401`（`invalid_session_token`），令牌正确但会话已 `closed` 或 `expired` 返回 `409`（`session_not_online`）。路由前按既有保活规则处理超时，且只投递给在线会话。非 JSON 对象、字段缺失或多余、非法主题/过滤器、非法 `max_messages` 均返回 `400`（`invalid_request`），且不改变订阅、队列或消费位置。会话因超时、重连替换或设备吊销离线后，其订阅与未取消息立即失效，新会话不继承，也不保存离线消息。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前测试覆盖健康检查基线与设备注册、凭据认证、轮换、吊销、连接会话、心跳保活以及 MQTT 风格主题订阅、发布与拉取的成功与失败语义。影子状态与规则引擎仍留待后续任务从已冻结事实出发独立设计并验证。
