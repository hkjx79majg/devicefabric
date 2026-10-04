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

- `POST /v1/device-sessions`：请求体为 `{"device_id": ..., "credential": ..., "client_id": ..., "keepalive_seconds": ...}`，可选 `clean_start` 字段仅接受 JSON 布尔值，缺省按 `true` 处理；非布尔值返回 `400`（`invalid_request`），且不关闭旧会话、不改变任何持久状态。`client_id` 校验规则同 `device_id`；`keepalive_seconds` 为 5 至 3600 的整数。当前凭据有效且设备 active 时返回 `201`，包含唯一不可预测的 `session_id`、仅此次返回的 `session_token`，以及 `device_id`、`client_id`、`connected_at`、`last_seen_at`、`expires_at`（UTC RFC 3339）和 `online` 状态。同设备同 `client_id` 重连时新会话取代旧会话，旧会话变为 `closed`（reason 为 `replaced`）。设备不存在、凭据错误、凭据已轮换或设备已吊销统一返回 `401`（`invalid_credential`）。
  - `clean_start` 缺省或为 `true`：沿用既有临时会话，订阅、待收消息、未确认投递与确认历史在会话离线后立即失效；若同一 `(device_id, client_id)` 此前建立过持久会话，则先清除其全部持久状态，再建立临时会话。
  - `clean_start` 为 `false`：以 `device_id` 与 `client_id` 的组合标识一个进程内持久会话。首次使用该组合时建立空状态；会话因心跳超时、被同组合重连替换或设备吊销而离线后，旧 `session_id` 与 `session_token` 仍按既有规则失效，但持久状态（订阅过滤器、QoS 1 待投递消息、未确认投递与确认历史）保留。同组合再次以 `false` 连接时复用该状态，被替换的在线会话照常关闭，重连响应结构与临时会话一致。以同组合 `true`（或缺省）连接会清除该持久状态；吊销设备清除该设备的全部持久会话，凭据轮换不清除；进程重启清空全部数据。
- `POST /v1/device-sessions/{session_id}/heartbeat`：请求体为 `{"session_token": ...}`。在线会话返回 `200` 并刷新 `last_seen_at` 与 `expires_at`；token 不匹配返回 `401`（`invalid_session_token`），对 `closed` 或 `expired` 会话心跳返回 `409`（`session_not_online`），失败请求不刷新时间。
- `GET /v1/device-sessions/{session_id}`：返回 `200` 与不含 `session_token` 的会话快照（含 `state` 与 `reason`）；未知会话返回 `404`（`session_not_found`）。

读取或心跳时，超过 `expires_at` 的在线会话转为 `expired`（reason 为 `keepalive_timeout`），不可恢复。吊销设备时其在线会话立即变为 `closed`（reason 为 `device_revoked`），吊销响应与幂等语义不变。

## 主题路由

订阅与消息仅保存在当前进程内，服务重启即清空。主题（topic）与主题过滤器（topic_filter）均为 1-256 个 Unicode 码点，按 `/` 分层且每层非空，禁止 NUL。topic 不得含通配符；过滤器中 `+` 只能独占一层并恰好匹配一层，`#` 只能独占最后一层、最多出现一次，可匹配零层或多层。

- `POST /v1/device-sessions/{session_id}/subscriptions`：请求体为 `{"session_token": ..., "topic_filter": ...}`。在线会话返回 `200 {"topic_filter": ...}`；重复订阅同一过滤器幂等，不产生副本，也不再次回放。在线会话首次成功添加某个过滤器后，会立即把当时匹配的保留消息快照加入其队列：按各主题最近一次保留发布的先后顺序排列，同次回放中每个精确 topic 只入队一份；新增不同过滤器即使与已有过滤器重叠，仍独立回放当前快照。回放消息沿用原消息的 `message_id`、`topic`、`payload`、`publisher_device_id` 与 `published_at`，并额外携带 `retained` 为 `true`。QoS 0 回放拉取后即移除且不携带 `qos`；QoS 1 回放携带 `qos` 为 `1`，并为目标会话生成独立 `delivery_id`，首次 `dup` 为 `false`，后续重投、背压与确认沿用既有语义。失败订阅不回放；回放只进入当前在线会话，不为离线设备保存，也不跨重连继承。以 `clean_start: false` 重连的持久会话只让既有订阅立即生效，不触发保留消息回放；在线期间首次新增某个过滤器时，仍按上述规则回放当时的保留快照。
- `POST /v1/device-sessions/{session_id}/publish`：请求体为 `{"session_token": ..., "topic": ..., "payload": ...}`，可选 `qos` 字段仅接受整数 `0` 或 `1`，缺省按 QoS 0 处理，其他取值返回 `400`（`invalid_request`）且不产生任何投递。可选 `retain` 字段缺省按 `false` 处理，显式提供时只接受 JSON 布尔值，否则返回 `400`（`invalid_request`）且不改变实时队列或保留状态。`payload` 可为任意 JSON 值（null、标量、数组或对象）。返回 `202`、唯一的 `message_id` 与 `matched_count`（实际入队的在线会话数）。同一会话即使被多个过滤器命中也只入队一份；允许发布给自身。实时消息包含 `message_id`、`topic`、`payload`、`publisher_device_id` 和 UTC RFC 3339 的 `published_at`，不携带 `retained` 字段。`retain` 为 `true` 且 `payload` 非 `null` 时，按精确 topic 保存该消息（沿用其 QoS）并覆盖旧值，即使 `matched_count` 为零也保存；`retain` 为 `true` 且 `payload` 为 `null` 时，消息仍实时投递，同时删除该 topic 的保留值，不存在也成功；`retain` 为 `false` 不改变保留状态。保留状态仅存在当前进程内，重启后清空；发布会话离线或发布设备被吊销均不删除保留消息。
- `POST /v1/device-sessions/{session_id}/messages/poll`：请求体为 `{"session_token": ..., "max_messages": ...}`，`max_messages` 为 1 至 100 的整数。返回 `200 {"messages": [...]}`；空队列返回空数组。QoS 0 消息按发布顺序排列、保持原有字段，并在返回后移出队列。QoS 1 消息为每个命中的在线会话生成独立且不可预测的 `delivery_id`，拉取结果在原字段之外包含 `qos`、`delivery_id` 与 `dup`：某 `delivery_id` 首次被拉取时 `dup` 为 `false`，确认前的后续拉取按原发布顺序重投且 `dup` 为 `true`，`message_id` 在重投时保持不变。未确认消息优先于尚未首次交付的消息，二者共同受 `max_messages` 限制，较早的未确认消息因此形成自然背压。
- `POST /v1/device-sessions/{session_id}/messages/ack`：请求体为 `{"session_token": ..., "delivery_ids": [...]}`，`delivery_ids` 为 1 至 100 个不重复字符串。成功返回 `200 {"acked_count": ...}` 并原子地移除相应未确认消息，`acked_count` 仅为本次新确认的数量；重复确认本会话已确认过的标识幂等成功且不增加计数。任一标识从未属于该会话时返回 `404`（`delivery_not_found`），整个请求不确认任何消息。

四个入口对未知会话返回 `404`（`session_not_found`），令牌错误返回 `401`（`invalid_session_token`），令牌正确但会话已 `closed` 或 `expired` 返回 `409`（`session_not_online`）。路由前按既有保活规则处理超时，且只投递给在线会话。非 JSON 对象、字段缺失或多余、非法主题/过滤器、非法 `max_messages`、非法 `qos`、非法 `retain` 或非法 `delivery_ids` 均返回 `400`（`invalid_request`），且不改变订阅、队列、未确认集合、消费位置或保留状态。临时会话（`clean_start` 缺省或为 `true`）因超时、重连替换或设备吊销离线后，其订阅、待首次投递消息、未确认消息与确认历史立即失效，新会话不继承，也不保存离线消息；不同会话之间的确认状态互不影响。

### 持久会话的离线消息

`clean_start: false` 建立的会话离线期间（无同组合在线会话），普通发布与规则动作的消息按当时持久订阅匹配：仅 QoS 1 消息加入该组合的离线队列，QoS 0 不保存；每个持久组合即使被多个过滤器命中同一消息也只保存一份；每条离线保存为该目标生成独立的 `delivery_id`，且不计入发布响应的 `matched_count`（该计数仍只统计实际入队的在线会话）。

同组合再次以 `clean_start: false` 重连后，既有订阅立即生效，待收消息可由既有拉取与确认入口消费：消息保留原 `message_id`、主题、载荷、发布者与 `published_at`；曾拉取但未确认的消息继续以相同 `delivery_id` 且 `dup` 为 `true` 按原发布顺序优先重投，从未拉取的消息首次返回时 `dup` 为 `false`，整体保持原发布顺序；确认历史同样保留，重复确认幂等。会话在线期间已入队但未拉取的 QoS 0 消息（含 QoS 0 保留回放）不属于持久状态，离线时丢弃。

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

## 设备命令

命令仅保存在当前进程内，服务重启即清空。凭据轮换不影响命令；设备吊销时全部非终态命令变为 `cancelled`，已终态命令不变。

- `POST /v1/devices/{device_id}/commands`：请求体为 `{"command_name": ..., "payload": ..., "ttl_seconds": ...}`，三个字段均必填且不得含未知字段。`command_name` 沿用 `device_id` 标识规则（1-64 个 ASCII 字母、数字、点、下划线或短横线）；`payload` 可为任意 JSON 值；`ttl_seconds` 为 5 至 86400 的整数。成功返回 `202` 与命令完整快照：唯一不可预测的 `command_id`、`queued` 状态、`delivery_count` 为 `0`、`created_at` 与 `expires_at`（UTC RFC 3339）、`completed_at` 与 `result` 为 `null`。设备不存在返回 `404`（`device_not_found`），已吊销返回 `409`（`device_revoked`）。
- `POST /v1/device-sessions/{session_id}/commands/poll`：请求体为 `{"session_token": ..., "max_commands": ...}`，`max_commands` 为 1 至 100 的整数。在线会话按创建顺序领取本设备命令，返回 `200 {"commands": [...]}`，每项在完整快照之外携带 `dup`。首次领取将状态改为 `delivered`、`delivery_count` 置一且 `dup` 为 `false`；确认前由同一会话重领时计数不变、`dup` 为 `true`。领取会话超时或被替换后，另一在线会话可重领，`delivery_count` 加一且 `dup` 为 `true`；仍归属其他在线会话的命令不可领取，并发领取不会双重归属。终态命令不再领取。
- `POST /v1/device-sessions/{session_id}/commands/{command_id}/ack`：请求体为 `{"session_token": ..., "status": ..., "result": ...}`，`status` 只接受 `succeeded` 或 `failed`，`result` 可为任意 JSON 值。首次确认只接受当前领取会话，保存终态、结果与 `completed_at` 并返回 `200` 完整快照；相同确认幂等返回 `200`，内容冲突返回 `409`（`command_already_completed`），未投递（含投递给其他会话）返回 `409`（`command_not_delivered`），命令不存在或属于其他设备返回 `404`（`command_not_found`）。
- `GET /v1/devices/{device_id}/commands/{command_id}`：返回 `200` 与完整快照；设备不存在返回 `404`（`device_not_found`），命令不存在或属于其他设备返回 `404`（`command_not_found`）。
- 到达 `expires_at` 的非终态命令在读取、领取或确认时转为 `expired`，不再可领取，确认返回 `409`（`command_expired`）。

轮询与确认沿用现有会话鉴权及保活语义：未知会话 `404`（`session_not_found`）、令牌错误 `401`（`invalid_session_token`）、会话已 `closed` 或 `expired` 返回 `409`（`session_not_online`）。非 JSON 对象、字段缺失或多余、`command_name`/`ttl_seconds`/`max_commands`/`status` 非法均返回 `400`（`invalid_request`），且不改变任何命令状态。

## 设备组与组命令批次

设备组与批次仅保存在当前进程内，服务重启即清空。设备吊销**不**将其移出任何组。

### 设备组

- `POST /v1/device-groups`：请求体为 `{"group_id": ..., "device_ids": [...]}`，两个字段均必填且不得含未知字段。`group_id` 沿用 `device_id` 标识规则（1-64 个 ASCII 字母、数字、点、下划线或短横线）；`device_ids` 为数组，每项沿用 `device_id` 标识规则，且不得重复（顺序保留，允许空数组）。所有成员必须是已注册设备（含已吊销设备），任一不存在返回 `404`（`device_not_found`）且不建组。成功返回 `201` 与 `{"group_id", "version": 1, "device_ids": ...}`，成员按请求顺序排列。重名返回 `409`（`group_already_exists`），原记录不变。
- `GET /v1/device-groups/{group_id}`：返回 `200` 与组快照；组不存在返回 `404`（`group_not_found`）。
- `PUT /v1/device-groups/{group_id}/members`：请求体为 `{"device_ids": [...]}`，可选非负整数 `expected_version`。整体替换成员（非合并），成功返回 `200` 与更新后的组快照，`version` 加一；即使新成员与现有成员完全相同也照常递增。提供 `expected_version` 时其值必须等于替换前的版本，否则返回 `409`（`group_version_conflict`）；组不存在返回 `404`（`group_not_found`）；成员含未注册设备返回 `404`（`device_not_found`）。任一失败均不改变组。

### 命令批次

- `POST /v1/device-groups/{group_id}/command-batches`：请求体为 `{"request_id": ..., "command_name": ..., "payload": ..., "ttl_seconds": ...}`，可带非负整数 `expected_group_version`，不得含其他未知字段。`request_id` 为 1-64 个字符的字符串；`command_name`、`payload`、`ttl_seconds` 沿用单设备命令规则。受理时对**当时成员快照**下发：成功返回 `202` 与 `{"batch_id", "group_version", "commands"}`，`batch_id` 唯一且不可预测，`group_version` 为采用的组版本，`commands` 为按成员顺序排列的 `{"device_id", "command_id"}`；空组也创建零项批次，`commands` 为空数组。子命令即普通设备命令（初始 `queued`），由既有的轮询、重领、确认与单命令查询入口处理。
  - 组不存在返回 `404`（`group_not_found`）；提供 `expected_group_version` 且与当前版本不符返回 `409`（`group_version_conflict`）；受理时成员中含已吊销设备返回 `409`（`group_contains_revoked_device`）。这些失败均不创建任何子命令或批次。
  - 幂等：同一组内相同 `request_id` 且内容（`command_name`、`payload`、`ttl_seconds`）相同的重复请求返回原批次（含原 `batch_id` 与子命令列表，HTTP `202`），不重复创建命令；内容不同返回 `409`（`batch_request_conflict`）。`request_id` 的作用域为单个组；并发的相同请求至多创建一个批次。
- `GET /v1/device-groups/{group_id}/command-batches/{batch_id}`：返回 `200` 与 `{"batch_id", "group_version", "commands", "status_counts"}`。`commands` 按批次创建时的成员顺序返回各子命令的**当前完整快照**（成员后续变化不影响旧批次）；`status_counts` 汇总 `queued`、`delivered`、`succeeded`、`failed`、`expired`、`cancelled` 六个状态的当前数量（零项批次均为 `0`）。批次不存在或属于其他组返回 `404`（`batch_not_found`）。

设备吊销按既有规则取消批次中的非终态子命令，已终态子命令不变；子命令到达 `expires_at` 时在批次查询中按既有规则转为 `expired`。非 JSON 对象、缺失或未知字段、重复成员、非法 `group_id`/`device_ids`/版本/`request_id`/命令字段均返回 `400`（`invalid_request`）且状态不变，错误体沿用 `{"error": {"code": ..., "message": ...}}`。

## 时序遥测

遥测的持久化位置由环境变量 `DEVICEFABRIC_TELEMETRY_PATH` 指定；未配置时数据仅保存在当前进程内，进程退出即清空；配置后已受理的数据点与幂等记录跨重启保留。凭据轮换、会话离线或设备吊销均不删除遥测。

- `POST /v1/device-sessions/{session_id}/telemetry`：请求体为 `{"session_token": ..., "request_id": ..., "points": [...]}`，三个字段均必填且不得含未知字段。`request_id` 为 1-64 个字符的非空字符串；`points` 含 1 至 500 项，每项仅含 `metric`、`timestamp`、`value` 三个字段：`metric` 沿用 `device_id` 标识规则（1-64 个 ASCII 字母、数字、点、下划线或短横线）；`timestamp` 必须是带时区的 RFC 3339 时间，按 UTC 保存；`value` 是非布尔的有限 JSON 数字。整批原子校验并写入，任一点非法则整批拒绝。成功返回 `202` 与 `{"request_id", "accepted_count"}`。
  - 幂等：同一设备相同 `request_id` 且内容相同的重复提交返回原结果（HTTP `202`），不重复写入；内容不同返回 `409`（`telemetry_request_conflict`）。`request_id` 的作用域为单台设备，不同设备互不影响。
  - 会话错误沿用既有语义：未知会话 `404`（`session_not_found`）、令牌错误 `401`（`invalid_session_token`）、会话已 `closed` 或 `expired` 返回 `409`（`session_not_online`）。非法请求返回 `400`（`invalid_request`）且不产生任何状态。配置持久化后存储失败返回 `503`（`telemetry_storage_unavailable`），该批数据与幂等记录均不可见。
- `GET /v1/devices/{device_id}/telemetry?metric=...&start=...&end=...&resolution=...`：四个查询参数均必填且各出现一次。`start`、`end` 均为带时区的 RFC 3339 时间，区间包含 `start`、不包含 `end`，且 `start` 必须早于 `end`；`resolution` 仅接受 `raw`、`60`、`300`、`3600`。返回 `200` 与 `{"device_id", "metric", "resolution", ...}`。
  - `raw` 按时间升序返回原始点（同时间按受理顺序排列），结果携带 `points`（每项含 UTC RFC 3339 的 `timestamp` 与 `value`）；raw 查询区间最长 24 小时。
  - 数值分辨率按 Unix 纪元对齐的固定窗口降采样，仅返回非空窗口，结果携带 `buckets`（每项含窗口 UTC 起始时间 `start` 及 `count`、`min`、`max`、`avg`、`last`，`last` 取窗口内最后一点，同时间取受理顺序最后者）；降采样查询区间最长 31 天。
  - 非法或越界参数返回 `400`（`invalid_request`）；设备不存在返回 `404`（`device_not_found`），已吊销设备仍可查询；无数据返回 `200` 和空结果。

## 按设备的入口限流

在线会话的消息发布与遥测提交支持按设备隔离的固定窗口限流，避免单台设备占满进程资源。两个限流相互独立，均通过环境变量配置：

- `DEVICEFABRIC_PUBLISH_RATE_LIMIT`：每台设备在一个 UTC 自然分钟内可受理的发布请求数。
- `DEVICEFABRIC_TELEMETRY_POINT_RATE_LIMIT`：每台设备在一个 UTC 自然分钟内可受理的遥测点数。

变量接受十进制非负整数；未设置或值为 `0` 时对应限流关闭，行为与未启用限流的基线完全一致。配额归属于 `device_id`：同一设备的多个会话、`client_id` 与重连共享同一额度，不同设备互不影响。窗口计数仅保存在当前进程内，服务重启即清空；遥测数据及已有幂等记录仍沿用 `DEVICEFABRIC_TELEMETRY_PATH` 的持久化语义，窗口计数不落盘。

- 一次通过完整字段校验、会话鉴权和在线状态检查的普通发布占用一个发布额度；该消息触发的全部规则动作不另行计数。
- 遥测按本批 `points` 数量原子占用额度：整批超过当前窗口剩余额度时整笔拒绝，不写入任何数据点或幂等记录。
- 同设备相同 `request_id` 且内容相同的重试返回原有 `202` 结果且不重复计数；内容不同仍优先返回 `409`（`telemetry_request_conflict`），不消耗额度。
- 非法请求、错误令牌、离线会话以及其他既有失败（含遥测落盘失败 `503`）均不消耗额度。
- 额度不足时返回 `429`（`rate_limit_exceeded`），响应体沿用统一错误体 `{"error": {"code", "message"}}`，并携带 `Retry-After` 响应头：其值为到下一个 UTC 分钟边界的向上取整秒数，范围为 1 至 60。发布被限流时，原消息、保留状态、订阅队列、离线队列及规则动作均不产生变化。
- 窗口到达 UTC 分钟边界后立即恢复完整额度；计数与受理在同一把锁内完成，并发请求下实际成功受理的发布数或遥测点数不得突破配置值。

健康检查、设备与会话生命周期、消息拉取确认、命令、固件、影子、规则、设备组、审计与遥测查询入口均不受限流影响，保持既有语义。

## 变更审计日志

审计日志仅保存在当前进程内，服务重启即清空。以下变更在成功提交时原子追加一条事件：设备注册（`device.created`）、凭据轮换（`device.credential_rotated`）、吊销（`device.revoked`），规则创建（`rule.created`）、启停变更（`rule.enabled_changed`）、删除（`rule.deleted`），设备组创建（`group.created`）、成员替换（`group.members_replaced`），以及固件发布登记（`firmware_release.created`）、固件批次创建（`firmware_rollout.created`）。失败请求、读取入口与未列出的入口不记审计；幂等调用未改变状态时不追加（如重复吊销、启停值未变化）。

每条事件含 `sequence`、`occurred_at`、`action`、`resource_type`、`resource_id`：`sequence` 从 1 开始严格递增且不复用；`occurred_at` 为 UTC RFC 3339 时间；`resource_type` 取 `action` 的点号前缀；`resource_id` 取主资源标识。事件绝不包含 `credential`、`session_token`、`payload` 或 `result`。日志仅保留最近 10000 条，淘汰最旧事件后 `sequence` 继续增长。

- `GET /v1/audit-events`：查询审计日志，返回 `200` 与 `{"events": [...], "next_after": ...}`，事件按 `sequence` 升序。
  - `after`：可选非负整数，只返回 `sequence` 严格大于它的事件；未传时从现存最早事件读取。
  - `limit`：可选，1 至 100 的整数，缺省 50；过滤后按升序截取。
  - `action`、`resource_id`：可选单值精确过滤条件。
  - `next_after` 取本页最后一个 `sequence`；空页取 `after`，未传 `after` 时取 `0`。
  - `after` 早于现存最老事件的前一序号时返回 `410`（`audit_cursor_expired`）。未知、重复、类型错误或越界的查询参数返回 `400`（`invalid_request`）。

## 多租户审计与诊断导出

`devicefabric` 包公开入口提供一套与上述 HTTP 服务完全解耦的多租户审计能力：`from devicefabric import AuditStore, AuditValidationError, AuditCursorError, create_audit_store`。数据仅保存在当前进程内，不进行任何文件写入；既有设备、连接、路由与消息入口不会自动向该存储写入事件。

- `store.append(tenant_id, event_type, occurred_at, result, *, device_id=None, session_id=None, correlation_id=None, details=None)`：写入一条事件并返回与后续查询一致的记录。`tenant_id`、`event_type`、`result` 必须为非空字符串；`occurred_at` 必须为有效且带时区的时间（`datetime` 或 RFC 3339 字符串，内部归一化为 UTC）；`details` 必须为可 JSON 序列化的对象。任一校验失败统一抛出 `AuditValidationError` 且不产生部分记录。`sequence` 由系统生成，在每个租户内从 1 开始严格递增且永不复用；详情在写入时深拷贝，调用方之后修改原对象不影响已保存事件。
- `store.query(tenant_id, *, device_id=None, event_type=None, result=None, start=None, end=None, limit=100, cursor=None)`：必须指定租户，只返回该租户数据，支持按设备、事件类型、结果与起止时间（含边界）组合筛选，按 `sequence` 升序稳定分页，返回 `{"events": [...], "next_cursor": ...}`。`limit` 必须为 1 至 1000 的整数，否则抛出 `AuditValidationError`；无匹配数据时返回空页。`next_cursor` 为不透明字符串，记录了查询起始时的快照上界：翻页期间新写入的事件不会混入后续页，连续翻页不重复、不遗漏。游标损坏、用于其他租户、筛选条件变更或超出其快照范围时统一抛出 `AuditCursorError`。相同查询参数与相同存储状态得到相同的事件顺序与游标；任何查询结果都不泄露其他租户是否存在或拥有多少记录。
- `store.export(tenant_id, *, device_id=None, event_type=None, result=None, start=None, end=None)`：诊断导出，筛选条件与查询相同，返回 `{"content": bytes, "record_count": int, "sha256": str}`。`content` 为 UTF-8 JSON Lines，每行一个事件，顺序与完整分页查询一致；`sha256` 为整个字节内容的十六进制摘要。导出前递归遮蔽 `details` 中大小写不敏感的 `password`、`token`、`secret`、`authorization`、`credential` 键，键保留而值固定为 `[REDACTED]`；存储中的原始事件与普通查询结果不被改写。无法序列化时仍按 `AuditValidationError` 处理且不返回残缺导出；空导出产生空内容、记录数 0 与空字节串的标准 SHA-256。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前测试覆盖健康检查基线与设备注册、凭据认证、轮换、吊销、连接会话、心跳保活、MQTT 风格主题订阅、发布（QoS 0 与 QoS 1）、拉取、重投与确认、设备影子的期望/实际状态写入、差异计算、版本冲突与会话鉴权，以及规则引擎的创建、列表、启停、删除、字段校验、条件评估与动作投递（含顺序、QoS、不递归触发、回放不触发）的成功与失败语义；另覆盖 `clean_start=false` 持久会话的建立、离线 QoS 1 队列、每组合一份与独立 delivery_id、`matched_count` 口径、重连后的顺序与 dup 重投、确认历史、保留消息不回放、`clean_start=true` 清状态、非布尔值拒绝且无副作用，以及凭据轮换保留与设备吊销清除；并覆盖设备命令的下发、按创建顺序领取、dup 重领、归属转移、确认与幂等、过期与吊销取消、字段校验与会话鉴权语义；另覆盖设备组的创建、查询、整体替换、乐观版本与各类冲突（含重名、版本不符、组/设备不存在、重复或非法成员、吊销不移除成员），以及组命令批次的成员快照下发、空组零项批次、不可预测 batch_id、按成员顺序的结果、expected_group_version 冲突、含吊销成员整体拒绝、同组 request_id 幂等与内容冲突、并发至多一个批次、批次查询的子命令当前快照与六状态汇总、成员变化不影响旧批次、吊销按既有规则取消非终态子命令，以及子命令经既有轮询/重领/确认/单命令查询入口处理的端到端语义；另覆盖时序遥测的批量写入与原子校验、UTC 归一化、同设备 request_id 幂等与内容冲突、设备间隔离、会话鉴权语义、raw 升序与受理顺序排列、区间含头不含尾、纪元对齐固定窗口降采样（count/min/max/avg/last 与空窗口跳过）、查询区间上限、吊销设备可查询、凭据轮换与会话离线保留数据，以及 DEVICEFABRIC_TELEMETRY_PATH 持久化的跨重启恢复与存储失败 503 语义；另覆盖按设备固定窗口限流的发布与遥测额度占用、按 points 原子占用与整批拒绝、UTC 分钟窗口恢复、设备间隔离与同设备多会话/重连共享、幂等重试不计数与内容冲突优先、各类失败不消耗额度、429 统一错误体与 Retry-After 头、并发不突破配置值及限流关闭时的基线兼容；另覆盖变更审计日志的十类事件追加时机、幂等未变更不追加、失败与读取不记录、sequence 严格递增、分页游标（after/limit/next_after）、action 与 resource_id 精确过滤、参数校验 400、保留窗口淘汰后的 410 游标过期语义；另覆盖多租户审计与诊断导出的写入校验与序号隔离、详情深拷贝、组合筛选与起止时间、页大小校验、快照游标翻页与各类游标错误、导出内容/计数/SHA-256 一致性、递归遮蔽不改写存储，以及既有入口不产生审计事件。
