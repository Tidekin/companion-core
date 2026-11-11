# 伙伴自主更新形象：Core 设计稿（V5，待确认）

状态：草案，尚未实现。依据《伙伴自主更新形象 / Core 与交互规格 V5》（2026-11-11）和暖光交互样板。按 AGENTS.md，这是新机制，需主人确认后再实现。本文只覆盖自研应用；同步到 QQ 等第三方是另一项能力，不在范围内。

## 目标

伙伴可以自己决定修改显示名和头像，Core 负责能力边界、素材处理、原子提交、审计和跨设备同步。用户默认不需要逐次批准，可以分别关闭"自行更新名字"和"自行更新头像"，可以给伙伴设备注，可以恢复历史版本。

三个概念始终分开：

| 概念 | 字段 | 谁能改 |
|---|---|---|
| 身份 | `companion_id`（不可变） | 无人，首次初始化时生成 |
| 显示资料 | `display_name`、`avatar_asset_id`、`profile_revision` | 伙伴（受开关约束）、主人恢复、系统安全替换 |
| 用户备注 | `viewer_alias` | 只有主人 |

改名或换头像不新建伙伴，不改 `companion_id`，不清空历史，不扩大记忆范围，不改人格提示或工具权限。

## 现有机制复用

| 需要 | 复用 | 说明 |
|---|---|---|
| bot 调用的幂等与结果未知 | `agent_tool_calls`（按 `job_id, tool_call_id` 记录，STARTED/UNKNOWN） | 规格里的 `idempotencyKey` 由服务端用这两项组成，不信任模型传入 |
| 调用主体与权限 | `check_conversation` + 当前 owner authority | 调用方由服务端认证上下文确定 |
| 图片字节存放 | 生图的做法：字节放在数据库旁的独立目录，库里只存引用和哈希 | 不进事件日志或备份 |
| 名字文本校验 | `social_autonomy_protocol._profile_text` | 再加长度、双向控制符和身份混淆检查 |
| 设备同步 | 原生聊天 `/events` SSE 通道 | 增加 `event: profile` |
| 主人侧接口认证 | 原生聊天现有会话、设备签名、CSRF | 与 `/messages` 相同 |
| 遗忘与保留期 | `privacy` 擦除流程、保留期清理 | 头像资源和审计按公布的保留策略处理 |

QQ 改资料（`social_autonomy` 的 `qq.profile.*` 和 `account_actions`）依赖外部账号探测、Outbox 投递和 Bridge 回执，原生场景不需要，不复用其流程。

## 数据模型（新增迁移 `pg0005_companion_profile`）

```text
runtime_meta['companion_id']            首次初始化生成，之后只读
companion_profile                       单行
  companion_id, revision, display_name, avatar_asset_id, updated_at
companion_profile_settings              单行
  auto_name (默认 true), auto_avatar (默认 true), ask_each_time (默认 false),
  notify ('none' 默认), viewer_alias, settings_revision, updated_at
companion_profile_changes               只追加
  change_id, old_revision, new_revision, fields (name/avatar/both),
  old_name, new_name, old_avatar_asset_id, new_avatar_asset_id,
  actor_type ('companion' | 'owner' | 'system'),
  origin_job_id, origin_tool_call_id, origin_operation_id,
  idempotency_key UNIQUE, note, restored_from_change_id, created_at
companion_avatar_assets
  asset_id, sha256, mime, width, height, byte_length,
  source ('existing' | 'owner_upload' | 'generated'), provenance_json,
  state ('PREPARING' | 'READY' | 'FAILED' | 'REJECTED' | 'ERASED'),
  safety_receipt_json, source_event_id, created_at, expires_at
```

迁移时用当前人格名初始化 `display_name`，`revision=1`，`actor_type='system'` 写第一条记录。迁移和初始化可以重复执行：已有 `companion_id` 时不覆盖。

变化记录不写进 `events` 表。`events` 是主人输入和观察，会进入记忆流程；资料变化是伙伴自身状态，单独一张表。聊天里的"小肥鱼更新了头像和名字"由历史接口按时间合并进时间线，不是一条消息。

## bot 工具

在聊天工具集里增加两个工具。只有 `profile_enabled=true` 且当前授权包含所需目的时才注册（目的见"待确认"第 1 项）。

### `get_own_profile()`

返回当前 `display_name`、头像是否存在、`revision`，以及两个自动更新开关的当前值。只读。

### `update_display_profile(display_name?, avatar_asset_id?, expected_revision, note?)`

1. `check_conversation` 重新确认当前任务租约和授权。
2. 在同一个写事务里：
   - 读取最新的资料和开关。
   - 本次涉及的字段如果被关闭，返回 `disabled_by_user`，并写明是哪个字段。
   - `expected_revision` 不等于当前 revision，返回 `revision_conflict` 和最新资料。
   - 规范化名字：去除首尾空白，长度 1–32 个可见字素；拒绝控制字符、双向控制符、零宽字符，以及冒充主人或系统的称呼（如"主人""系统"），拒绝时返回 `validation_failed` 和原因。
   - 头像资源必须是 `READY` 状态、没有过期、没有被擦除，否则返回 `asset_not_ready` 或 `asset_failed`。
   - 两个字段都没有实际变化，返回 `no_change`，不写记录。
   - 频率限制按服务端配置检查，超限返回 `rate_limited` 和 `retry_after`。
   - 全部通过后：更新 `companion_profile`，`revision+1`，写一条 `companion_profile_changes`（`actor_type='companion'`，`idempotency_key` 由 `job_id + tool_call_id` 组成），然后在同一事务里 NOTIFY。
3. 返回 `confirmed`，带 `new_revision`、规范化后的资料和 `change_id`。

同时修改名字和头像时，两项要么一起生效，要么都不生效。

同一个 `tool_call_id` 重试，按 `idempotency_key` 查到原记录，返回同一结果，不再写第二次。工具调用处于 UNKNOWN（例如提交后、记录结果前进程崩溃）时，恢复流程按 `idempotency_key` 核对：已提交就返回 `confirmed`，否则返回"未生效"，不盲目重新提交。

本地提交是同步完成的，所以首期没有 `pending` 状态。头像生成接入后（第三期），生成和处理在素材阶段完成，提交本身仍是同步的。

## 提示词

- 人格提示里写死的"小肥鱼"（如 `core.py:687` 的心情参考标题）改为读取当前 `display_name`。
- 上下文增加一行事实："你当前的显示名是 X"。改名后的下一轮对话生效，本轮不追溯。
- 不在提示里放审计历史，也不写入记忆。如果伙伴想记住"我改过名"，走正常的记忆流程。

## 主人侧接口（原生客户端）

认证与 `/messages` 相同。所有写接口都要求带上预期的 revision。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/profile` | 当前资料、开关、备注、两个 revision |
| PUT | `/profile/settings` | 修改开关和通知方式，带 `expected_settings_revision`，以服务端确认的结果为准 |
| PUT | `/profile/alias` | 设置或清除备注 |
| GET | `/profile/changes?before=&limit=` | 分页查看变化记录 |
| GET | `/profile/assets/{asset_id}` | 读取头像字节，按哈希缓存；已擦除的返回 410 |
| POST | `/profile/restore` | 参数 `change_id`、`expected_revision`、`pause_fields`。恢复写成一条新 revision（`actor_type='owner'`），不删除中间记录；`pause_fields` 是用户显式选择的，不会暗中修改开关。素材已不可用时返回原因 |

伙伴在恢复之前拿到的 `expected_revision` 会过期，因此旧请求必然冲突，不会覆盖用户的恢复。

## 同步

- `/events` 增加 `event: profile`，内容为 `{revision, profile, change}`。客户端只接受比本地新的 revision，并按 `change_id` 去重。
- 重连时先拉 `/profile` 快照，再接收事件，不重播历史头像动画。
- NOTIFY 和提交在同一事务里，不会出现"资料已变、变化记录丢失"。

## 头像来源（分期）

1. **第一期**：只做名字。头像字段保留，但工具不接受 `avatar_asset_id`。
2. **第二期**：主人上传的图片，以及伙伴已有的头像素材。上传后要通过：真实格式检测（PNG/JPEG/WebP）、大小和尺寸上限、可解码、去除元数据、居中裁成正方形并缩放到固定尺寸，状态才变为 `READY`。用户聊天里的照片和附件默认不会成为头像素材；主人在上传头像时明确选择的那张才算。
3. **第三期**：通过应用的生图能力生成伙伴自己的插画，沿用 `image_generation` 的费用和不确定结果规则；生成的图片同样要经过上面的检查。需要把用户照片发给外部服务的流程，单独确认授权。

内容安全检查的具体服务待定（见"待确认"第 4 项）。检查未通过就不提交，不靠反复生成变体来绕过。

## 遗忘与保留

- 头像资源如果来自主人上传或聊天来源，记录 `source_event_id`。遗忘该来源时资源变为 `ERASED`、文件删除；正在使用它的资料由系统换成中性占位，写一条 `actor_type='system'` 的记录。
- 变化记录里的名字是伙伴自身的显示值，默认保留；被删掉的头像只留 `asset_id` 和"图片已不可用"。

## 客户端展示

暖光客户端只在 Core 报告能力可用时才显示资料面板、变化记录和开关。没有头像时显示首字母占位。名字和头像以 `revision` 刷新，变化条用 `change_id` 去重，不用 `display_name` 做 Widget Key 或主键。

## 测试与验收（对应规格 8 条）

1. bot 无需用户发消息或批准，就能改名；第二期起能换头像，也能两项同时改（真实 PostgreSQL + 独立 Worker + 模拟模型传输）。
2. 改名后 `companion_id`、会话、草稿、记忆、授权和备注不变。
3. 开关关闭后工具返回 `disabled_by_user`。素材处理期间关闭开关，提交时会被拒绝。
4. 素材失败、审核拒绝、超时、UNKNOWN、并发冲突都不会产生假成功或只生效一半。
5. 同一 `tool_call_id` 重试、重连、多设备都不会重复写记录或回退版本。
6. 记录中的 `actor_type` 真实反映来源，伙伴、主人、系统的操作不会混写。
7. 恢复不抹掉审计；素材不可用时能说明原因；用户恢复后，伙伴持有的旧 revision 请求必然冲突。
8. 界面验收（长名字、200% 字体、读屏）在客户端切片里做。

## 实施顺序

1. 迁移 + `companion_id` + 名字更新工具 + 审计 + 开关和备注接口 + SSE 同步 + 提示词改为读取显示名 + 客户端资料面板。
2. 主人上传头像、素材处理、资产读取接口、客户端头像显示与交叉淡入。
3. 头像生成。

每期一个 PR。第一期不依赖 PR #445。

## 需要主人确认

1. **权限目的**：
   - 新增 `profile` 目的：需要重新批准一次授权，旧版本下未完成的任务可能失效。
   - 复用现有的 `expression` 目的：不需要重新授权，但"表达"的含义会扩大。
   - 我倾向新增 `profile`，权限边界更清楚，代价是一次重新授权。
2. **频率限制默认值**：建议名字每天最多 1 次、两次至少间隔 3 天；头像每天最多 1 次。
3. **提示词**：是否同意把写死的"小肥鱼"改成读取当前显示名，并在上下文里告诉伙伴当前名字。
4. **头像内容安全**：第二期用哪种检查服务（本地模型或外部接口），以及检查不可用时是否一律拒绝。
5. **保留期**：变化记录和旧头像各保留多久。建议记录长期保留，不再使用的头像资源保留 180 天。
