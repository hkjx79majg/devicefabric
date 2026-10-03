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

- `POST /v1/device-sessions`：请求体为 `{"device_id": ..., "credential": ..., "client_id": ..., "keepalive_seconds": ...}`，可选 `clean_start` 字段缺省按 `true` 处理，显式提供时只接受 JSON 布尔值；非布尔值返回 `400`（`invalid_request`），且不得关闭旧会话或改变任何持久状态。`client_id` 校验规则同 `device_id`；`keepalive_seconds` 为 5 至 3600 的整数。当前凭据有效且设备 active 时返回 `201`，包含唯一不可预测的 `session_id`、仅此次返回的 `session_token`，以及 `device_id`、`client_id`、`connected_at`、`last_seen_at`、`expires_at`（UTC RFC 3339）和 `online` 状态。同设备同 `client_id` 重连时新会话取代旧会话，旧会话变为 `closed`（reason 为 `replaced`）。设备不存在、凭据错误、凭据已轮换或设备已吊销统一返回 `401`（`invalid_credential`）。
  - `clean_start` 未提供或为 `true`：按现有临时会话处理。会话离线（超时、重连替换或设备吊销）后其订阅、待取消息、未确认消息与确认历史立即丢弃，新会话不继承。同组合以 `true` 连接时，先清除该 `(device_id, client_id)` 组合的全部持久状态，再建立全新临时会话。
  - `clean_start` 为 `false`：以 `device_id` 与 `client_id` 的组合标识一个进程内持久会话。首次使用该组合时建立空状态；后续通过订阅入口成功添加的过滤器、QoS 1 待投递消息、未确认消息及确认历史都归属于该持久会话。以 `false` 重连时复用并保留状态、关闭被替换的在线会话，原订阅立即生效且不触发保留消息回放；旧 `session_id` 与 `session_token` 仍按现有规则失效。持久状态在该组合没有在线会话期间继续保留，直到同组合以 `clean_start` 为 `true` 重连或设备被吊销；凭据轮换不清除，进程重启仍清空全部数据。
- `POST /v1/device-sessions/{session_id}/heartbeat`：请求体为 `{"session_token": ...}`。在线会话返回 `200` 并刷新 `last_seen_at` 与 `expires_at`；token 不匹配返回 `401`（`invalid_session_token`），对 `closed` 或 `expired` 会话心跳返回 `409`（`session_not_online`），失败请求不刷新时间。
- `GET /v1/device-sessions/{session_id}`：返回 `200` 与不含 `session_token` 的会话快照（含 `state` 与 `reason`）；未知会话返回 `404`（`session_not_found`）。

读取或心跳时，超过 `expires_at` 的在线会话转为 `expired`（reason 为 `keepalive_timeout`），不可恢复。吊销设备时其在线会话立即变为 `closed`（reason 为 `device_revoked`），吊销响应与幂等语义不变。

## 主题路由

订阅与消息仅保存在当前进程内，服务重启即清空。主题（topic）与主题过滤器（topic_filter）均为 1-256 个 Unicode 码点，按 `/` 分层且每层非空，禁止 NUL。topic 不得含通配符；过滤器中 `+` 只能独占一层并恰好匹配一层，`#` 只能独占最后一层、最多出现一次，可匹配零层或多层。

- `POST /v1/device-sessions/{session_id}/subscriptions`：请求体为 `{"session_token": ..., "topic_filter": ...}`。在线会话返回 `200 {"topic_filter": ...}`；重复订阅同一过滤器幂等，不产生副本，也不再次回放。在线会话首次成功添加某个过滤器后，会立即把当时匹配的保留消息快照加入其队列：按各主题最近一次保留发布的先后顺序排列，同次回放中每个精确 topic 只入队一份；新增不同过滤器即使与已有过滤器重叠，仍独立回放当前快照。回放消息沿用原消息的 `message_id`、`topic`、`payload`、`publisher_device_id` 与 `published_at`，并额外携带 `retained` 为 `true`。QoS 0 回放拉取后即移除且不携带 `qos`；QoS 1 回放携带 `qos` 为 `1`，并为目标会话生成独立 `delivery_id`，首次 `dup` 为 `false`，后续重投、背压与确认沿用既有语义。失败订阅不回放；回放只进入当前在线会话，不为离线设备保存，也不跨重连继承。
- `POST /v1/device-sessions/{session_id}/publish`：请求体为 `{"session_token": ..., "topic": ..., "payload": ...}`，可选 `qos` 字段仅接受整数 `0` 或 `1`，缺省按 QoS 0 处理，其他取值返回 `400`（`invalid_request`）且不产生任何投递。可选 `retain` 字段缺省按 `false` 处理，显式提供时只接受 JSON 布尔值，否则返回 `400`（`invalid_request`）且不改变实时队列或保留状态。`payload` 可为任意 JSON 值（null、标量、数组或对象）。返回 `202`、唯一的 `message_id` 与 `matched_count`（实际入队的在线会话数）。同一会话即使被多个过滤器命中也只入队一份；允许发布给自身。实时消息包含 `message_id`、`topic`、`payload`、`publisher_device_id` 和 UTC RFC 3339 的 `published_at`，不携带 `retained` 字段。`retain` 为 `true` 且 `payload` 非 `null` 时，按精确 topic 保存该消息（沿用其 QoS）并覆盖旧值，即使 `matched_count` 为零也保存；`retain` 为 `true` 且 `payload` 为 `null` 时，消息仍实时投递，同时删除该 topic 的保留值，不存在也成功；`retain` 为 `false` 不改变保留状态。保留状态仅存在当前进程内，重启后清空；发布会话离线或发布设备被吊销均不删除保留消息。
- `POST /v1/device-sessions/{session_id}/messages/poll`：请求体为 `{"session_token": ..., "max_messages": ...}`，`max_messages` 为 1 至 100 的整数。返回 `200 {"messages": [...]}`；空队列返回空数组。QoS 0 消息按发布顺序排列、保持原有字段，并在返回后移出队列。QoS 1 消息为每个命中的在线会话生成独立且不可预测的 `delivery_id`，拉取结果在原字段之外包含 `qos`、`delivery_id` 与 `dup`：某 `delivery_id` 首次被拉取时 `dup` 为 `false`，确认前的后续拉取按原发布顺序重投且 `dup` 为 `true`，`message_id` 在重投时保持不变。未确认消息优先于尚未首次交付的消息，二者共同受 `max_messages` 限制，较早的未确认消息因此形成自然背压。
- `POST /v1/device-sessions/{session_id}/messages/ack`：请求体为 `{"session_token": ..., "delivery_ids": [...]}`，`delivery_ids` 为 1 至 100 个不重复字符串。成功返回 `200 {"acked_count": ...}` 并原子地移除相应未确认消息，`acked_count` 仅为本次新确认的数量；重复确认本会话已确认过的标识幂等成功且不增加计数。任一标识从未属于该会话时返回 `404`（`delivery_not_found`），整个请求不确认任何消息。

四个入口对未知会话返回 `404`（`session_not_found`），令牌错误返回 `401`（`invalid_session_token`），令牌正确但会话已 `closed` 或 `expired` 返回 `409`（`session_not_online`）。路由前按既有保活规则处理超时，且只投递给在线会话。非 JSON 对象、字段缺失或多余、非法主题/过滤器、非法 `max_messages`、非法 `qos`、非法 `retain` 或非法 `delivery_ids` 均返回 `400`（`invalid_request`），且不改变订阅、队列、未确认集合、消费位置或保留状态。临时会话因超时、重连替换或设备吊销离线后，其订阅、待首次投递消息、未确认消息与确认历史立即失效，新会话不继承，也不保存离线消息；不同会话之间的确认状态互不影响。

### 持久会话的离线投递

`clean_start` 为 `false` 的组合没有在线会话期间（超时离线、被同组合重连替换之后），其持久订阅继续参与匹配：普通发布和规则动作只把匹配的 **QoS 1** 消息加入该持久会话的离线队列，QoS 0 消息不保存；每个持久会话即使有多个过滤器命中同一消息也只保存一份，并为该目标生成独立且不可预测的 `delivery_id`。离线保存不计入发布响应的 `matched_count`，该字段继续只统计实际入队的在线会话数；动作消息同样可以进入离线队列且不计入 `matched_count`。

以同一 `device_id`、`client_id` 且 `clean_start` 为 `false` 重新连接后，原订阅立即生效且不触发保留消息回放，待收消息可由现有拉取与确认入口消费：消息保留原 `message_id`、主题、载荷、发布者与发布时间，每个目标的 `delivery_id` 保持独立；曾拉取但未确认的消息继续以相同 `delivery_id` 且 `dup` 为 `true` 优先重投，从未拉取的消息首次返回 `dup` 为 `false`，整体保持原发布顺序，确认历史同样保留（重复确认幂等）。在线期间首次成功添加新过滤器时，仍按现有规则回放当时的保留消息快照。设备吊销时清除该设备的全部持久会话；凭据轮换不清除；进程重启仍清空全部数据。

## 设备影子

影子仅保存在当前进程内，服务重启即清空。设备注册时初始化空影子；吊销、凭据轮换或会话离线均不删除影子，现有入口行为不变。

- `GET /v1/devices/{device_id}/shadow`：返回 `200` 与完整快照 `{"device_id", "version", "desired", "reported", "delta", "updated_at"}`。初始 `version` 为 `0`，`desired`、`reported`、`delta` 均为空对象，`updated_at` 为 `null`。设备不存在返回 `404`（`device_not_found`），已吊销设备仍可读取。
- `PUT /v1/devices/{device_id}/shadow/desired`：请求体为 `{"state": {...}}`，可选 `expected_version`（非负整数）。整体替换 `desired`（非合并），返回 `200` 与写入后的完整快照。每次写入 `version` 加一、`updated_at` 更新为当前 UTC RFC 3339 时间；即使新 `state` 与现有值完全相同、或写入空对象也照常递增。设备不存在返回 `404`（`device_not_found`）；已吊销设备仍可写入。
- `POST /v1/device-sessions/{session_id}/shadow/reported`：请求体为 `{"session_token": ..., "state": {...}}`，可选 `expected_version`。沿用会话鉴权与保活超时规则：未知会话 `404`（`session_not_found`）、令牌错误 `401`（`invalid_session_token`）、会话已 `closed` 或 `expired` 返回 `409`（`session_not_online`）。鉴权通过后整体替换该会话所属设备的 `reported`，返回 `200` 与完整快照，`version` 加一、`updated_at` 更新（`state` 相同也递增）。reported 仅可由归属该设备且在线的会话更新。
- `delta` 递归计算：保留 `desired` 中 `reported` 缺失或值不同的成员，取值来自 `desired`；仅当两侧同名成员均为对象时向下递归，数组及其他非对象值整体比较；`reported` 独有的成员忽略；完全一致时 `delta` 为空对象。
- 乐观并发：提供 `expected_version` 时，其值必须为非负整数且等于写入前的 `version`，否则返回 `409`（`shadow_version_conflict`）且影子不变；不提供时不做版本检查。并发的相同预期版本写入至多一个成功。
- 非 JSON 对象、缺少必填字段（desired 为 `state`，reported 为 `session_token`、`state`）、含未知字段、`state` 不是 JSON 对象或 `expected_version` 非法（非非负整数，含布尔值）均返回 `400`（`invalid_request`），影子不变。

## 规则引擎

规则仅保存在当前进程内，服务重启即清空。设备吊销、凭据轮换或会话离线均不删除规则。

- `POST /v1/rules`：请求体为 `{"rule_id": ..., "topic_filter": ..., "enabled": ..., "condition": ..., "action": ...}`，五个字段均必填且不得含未知字段。`rule_id` 沿用 `device_id` 标识规则（1-64 个 ASCII 字母、数字、点、下划线或短横线）且在进程内唯一，重复返回 `409`（`rule_already_exists`）；`topic_filter` 沿用订阅过滤器语义；`enabled` 必须是 JSON 布尔值。成功返回 `201` 与完整规则。
  - `condition` 为 `{"path": ..., "operator": ..., "value": ...}`，三个字段必填。`path` 为 1 至 16 个非空字符串组成的数组，从发布 payload 顶层逐层下钻；`operator` 仅支持 `eq`、`ne`、`gt`、`gte`、`lt`、`lte`。大小比较（后四者）的 `value` 必须是非布尔数字，否则创建返回 `400`；`eq`/`ne` 的 `value` 允许任意 JSON 值。
  - `action` 为 `{"topic": ..., "payload": ..., "qos": ...}`，三个字段必填。`topic` 为合法固定 topic（沿用发布主题规则，不得含通配符或空层）；`payload` 为任意 JSON 值（创建后与规则绑定，触发时深拷贝生成消息）；`qos` 仅接受整数 `0` 或 `1`，与原发布消息的 QoS 无关。
- `GET /v1/rules`：返回 `200 {"rules": [...]}`，按创建顺序列出全部规则（含已禁用与已启用）；无规则时返回 `{"rules": []}`。
- `PUT /v1/rules/{rule_id}/enabled`：请求体只接受 `{"enabled": true}` 或 `{"enabled": false}`，多余字段、缺失字段或非布尔值均返回 `400`（`invalid_request`）。成功返回 `200` 与更新后的完整规则；规则不存在返回 `404`（`rule_not_found`）。
- `DELETE /v1/rules/{rule_id}`：删除规则并保持其余规则的相对顺序，返回 `200 {"deleted": true}`；规则不存在返回 `404`（`rule_not_found`）。

### 规则评估语义

在线会话发布通过全部字段校验、会话鉴权与保活检查后，系统按创建顺序评估当时全部已启用规则；未通过校验或鉴权（含未知会话、令牌错误、会话离线、非法主题、非法 qos/retain 等）时不评估任何规则。对每条规则，当发布 topic 命中 `topic_filter` 且 `condition` 命中 payload 时，以 `action` 的 `topic`/`payload`/`qos`、原发布设备的 `publisher_device_id` 和一个全新唯一的 `message_id` 生成一条普通非保留实时消息（携带当前 UTC RFC 3339 的 `published_at`，不携带 `retained` 字段），按现有订阅匹配、QoS、delivery_id、背压重投与确认语义投递给当时在线的会话。

- 原消息先于全部动作消息入队；同一次发布命中多条规则时，每条规则各生成一条消息，并按规则创建顺序入队。
- 动作消息不再触发任何规则（即使其 topic 命中某条规则的过滤器），动作投递不计入发布响应的 `matched_count`，发布响应的 `message_id` 与 `matched_count` 口径保持不变。
- 条件读取：`path` 任一段在 payload 中缺失，或下钻途中遇到非对象值（数组、标量或 null）即不命中；读取到的终点值允许任意 JSON 类型。`eq`/`ne` 按 JSON 值深度比较（对象按键与值递归、数组逐项比较，布尔值不与数字相等）；大小比较仅在读取值也是非布尔数字时进行，否则不命中。
- 禁用中的规则不参与评估；重新启用后立即生效。保留消息回放（新订阅时的快照回放）不触发规则；带 `retain: true` 的实时发布仍正常评估规则，但其动作消息为普通非保留消息，不写入保留存储。

非 JSON 对象、字段缺失或多余、`rule_id`/`topic_filter`/`enabled`/`condition`/`action` 任一非法均返回 `400`（`invalid_request`），且不改变任何规则或消息队列。错误体统一为 `{"error": {"code": ..., "message": ...}}`。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前测试覆盖健康检查基线与设备注册、凭据认证、轮换、吊销、连接会话、心跳保活、MQTT 风格主题订阅、发布（QoS 0 与 QoS 1）、拉取、重投与确认、设备影子的期望/实际状态写入、差异计算、版本冲突与会话鉴权，以及规则引擎的创建、列表、启停、删除、字段校验、条件评估与动作投递（含顺序、QoS、不递归触发、回放不触发）的成功与失败语义；另覆盖 `clean_start` 持久会话的建连与重连、订阅与确认历史保留、离线 QoS 1 队列（QoS 0 不保存、多过滤器去重、顺序、`dup` 与 `delivery_id` 语义、规则动作入队）、重连不回放保留消息而在线新订阅仍回放、`clean_start` 为 `true` 清空、非布尔值 `400` 且无副作用、设备吊销清除与凭据轮换保留。
