// @ts-check
"use strict";
/**
 * 接口返回的通用记录（console_read.entry）。
 * @typedef {{ id: string|number, kind: string, title: string, status: string, occurred_at: string|null, summary: string, detail: Record<string, any> }} ConsoleItem
 * @typedef {{ items: ConsoleItem[], next_cursor: string|number|null, observed_at: string, meta?: Record<string, any> }} Collection
 * @typedef {{ key: string, label: string, group?: string, type: string, value: any, active_value?: any, active_source?: string, effect?: string, nullable?: boolean, configured?: boolean, description?: string, minimum?: number, maximum?: number, schema?: Record<string, any> }} ConfigField
 * @typedef {{ version: number, active_version: number, startup_version?: number, pending_restart: boolean, pending_reload?: boolean, requires_reapproval?: boolean, approval_reason?: string, hot_reload_enabled?: boolean, error?: string, workers?: Array<{worker: string, active_version: number, pending_version: number|null, reason: string|null, observed_at: string}>, fields: ConfigField[], history: Array<{version: number, created_at: string, fields: string[]}>, cloud_budget?: any }} Configuration
 * @typedef {{ cls?: string, symbol?: string, type?: "button"|"submit", disabled?: boolean, label?: string }} ButtonOptions
 * @typedef {{ method?: string, body?: any, signal?: AbortSignal, authenticate?: boolean, validate?: (value: any) => boolean }} RequestOptions
 */
/* Companion 主人控制台。
   只调用同源 /v1/console；写操作带 CSRF 与幂等键，结果不明时不换键重试。
   页面通过 DOM 构建（不拼接 HTML），样式只经 CSSOM 设置，满足 style-src 'self'。 */
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function el(tag, attributes = {}, ...children) {
  const svg = ["svg", "path", "circle", "g", "line", "text", "rect", "polyline"].includes(tag);
  const node = svg ? document.createElementNS("http://www.w3.org/2000/svg", tag) : document.createElement(tag);
  for (const [key, value] of Object.entries(attributes || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "class") node.setAttribute("class", value);
    else if (key === "text") node.textContent = value;
    else if (key === "style" && typeof value === "object") for (const [name, v] of Object.entries(value)) node.style.setProperty(name, v);
    else if (["checked", "value", "disabled", "selected"].includes(key) && !svg) node[key] = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const PATHS = {
  home: "M4 11 12 4l8 7v9h-5v-6H9v6H4z", timeline: "M6 4v16M6 7h12M6 12h8M6 17h10",
  memory: "M12 3a6 6 0 0 0-3.5 10.9V17h7v-3.1A6 6 0 0 0 12 3zM9.5 20.5h5",
  diary: "M6 3h11a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM8 3v18M11 8h4M11 12h4",
  book: "M4 5c3-1.5 5.5-1.5 8 0v15c-2.5-1.5-5-1.5-8 0zM20 5c-3-1.5-5.5-1.5-8 0v15c2.5-1.5 5-1.5 8 0z",
  smile: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM8.5 14.5c1.8 2 5.2 2 7 0M9 9.5h.01M15 9.5h.01",
  mic: "M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21",
  qq: "M12 3a5 5 0 0 0-5 5v2l-2 4c1 .5 1.6.3 2 0 .3 2 1.3 3.4 2.6 4.3L8 20h8l-1.6-1.7c1.3-.9 2.3-2.3 2.6-4.3.4.3 1 .5 2 0l-2-4V8a5 5 0 0 0-5-5z",
  pulse: "M3 12h4l2.5-6 5 12 2.5-6H21",
  gear: "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM19.4 13.5l1.6 1.2-2 3.4-1.9-.7a7 7 0 0 1-2 1.2L14.8 21h-4l-.3-2.4a7 7 0 0 1-2-1.2l-1.9.7-2-3.4 1.6-1.2a7 7 0 0 1 0-2.4L4.6 9.9l2-3.4 1.9.7a7 7 0 0 1 2-1.2L10.8 3h4l.3 2.4a7 7 0 0 1 2 1.2l1.9-.7 2 3.4-1.6 1.2a7 7 0 0 1 0 2.4z",
  toggle: "M8 7h8a5 5 0 0 1 0 10H8A5 5 0 0 1 8 7zM8 9.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4",
  refresh: "M20 11a8 8 0 0 0-14.6-4.5M4 4v4h4M4 13a8 8 0 0 0 14.6 4.5M20 20v-4h-4",
  logout: "M10 4H5v16h5M14 8l4 4-4 4M18 12H9", menu: "M4 7h16M4 12h16M4 17h16", close: "M6 6l12 12M18 6 6 18",
  check: "M5 12.5 10 17l9-10", warn: "M12 4 2.8 20h18.4zM12 10v4.5M12 17.5h.01", arrow: "M5 12h14M13 6l6 6-6 6",
  left: "M15 6l-6 6 6 6", right: "M9 6l6 6-6 6", edit: "M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4",
  trash: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3", send: "M4 12 20 4l-6 16-3-7z",
  coin: "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16zM9.5 9.5h4a1.5 1.5 0 0 1 0 3h-3a1.5 1.5 0 0 0 0 3h4M12 7.5v1.5M12 16v1",
  pause: "M8 5v14M16 5v14", lock: "M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 0 1 7 0v3", file: "M6 3h8l4 4v14H6zM14 3v4h4",
  spark: "M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6",
};
function icon(name, cls = "i") {
  return el("svg", { class: cls, viewBox: "0 0 24 24", "aria-hidden": "true" }, el("path", { d: PATHS[name] || PATHS.spark }));
}

/** @type {Array<[string, Array<[string, string, string]>]>} */
const ROUTES = [
  ["日常", [["overview", "今天", "home"], ["activity", "活动时间线", "timeline"], ["thinking", "思考与决策", "spark"], ["memories", "记忆", "memory"], ["life", "日记与成长", "diary"], ["self", "情绪与偏好", "smile"]]],
  ["内容", [["content", "阅读与文件", "book"], ["memes", "表情素材", "smile"], ["voice", "语音与节点", "mic"], ["account-actions", "QQ 资料行为", "qq"]]],
  ["管理", [["caps", "能力与开关", "toggle"], ["tools", "执行任务", "file"], ["diagnostics", "运行诊断", "pulse"], ["configuration", "配置", "gear"]]],
];
const ROUTE_NAME = Object.fromEntries(ROUTES.flatMap(([, list]) => list.map(([key, name]) => [key, name])));

const STATUS = {
  READY: ["可用", "ok"], ATTENTION: ["需要留意", "warn"], ACTIVE: ["已启用", "run"], ENABLED: ["已启用", "ok"],
  DISABLED: ["未启用", "off"], UNAVAILABLE: ["不可用", "bad"], PAUSED: ["已暂停", "warn"], STOPPED: ["已停止", "off"],
  RECORDED: ["有运行记录", "ok"], STALE: ["记录已过期", "warn"], UNOBSERVED: ["尚无记录", "off"], OBSERVED: ["已观测", "ok"],
  QUEUED: ["排队中", "run"], PENDING: ["待处理", "run"], RUNNING: ["处理中", "run"], RETRY_WAIT: ["等待重试", "warn"],
  SUCCEEDED: ["已完成", "ok"], COMPLETED: ["已完成", "ok"], COMPLETE: ["已完成", "ok"], COMMITTED: ["已保存", "ok"],
  SETTLED: ["已结算", "ok"], SENT: ["已发送", "ok"], ACK: ["已送达", "ok"], UNKNOWN: ["结果不明", "warn"],
  FAILED: ["失败", "bad"], ERROR: ["发生错误", "bad"], CANCELLED: ["已取消", "off"], SUPERSEDED: ["已被新输入接续", "off"],
  RECALLED: ["已撤回", "off"], CONFIRMED: ["已确认", "ok"], HYPOTHESIS: ["待确认", "warn"], FORGOTTEN: ["已忘记", "off"],
  RETRACTED: ["已撤销", "off"], DELETED: ["已删除", "off"], REDACTED: ["已清除", "off"], EXPIRED: ["已过期", "off"],
  PROPOSED: ["待接受", "warn"], REVIEW: ["待审核", "warn"], QUARANTINED: ["已隔离", "bad"], INVALID: ["已失效", "bad"],
  INDEXED: ["已索引", "ok"], FETCHED: ["已读取", "ok"], DISCOVERED: ["已发现", "run"], WATCHED: ["已有分析", "ok"],
  PARTIAL: ["部分完成", "warn"], RESERVED: ["已预留", "run"], WAITING: ["等待中", "run"], NOT_CONFIGURED: ["未配置", "off"],
  DELIVERY_UNCERTAIN: ["投递结果不明", "warn"], BLOCKED: ["受阻", "warn"], DRAFT: ["草稿", "off"],
  ONLINE: ["近期有联系", "ok"], OFFLINE: ["联系已过期", "off"], BUSY: ["有未结束任务", "run"], TRANSCRIBED: ["已转写", "ok"],
  LEASED: ["已领取", "run"], SENDING: ["正在发送", "run"], UNVERIFIED: ["需要核对", "warn"], FRESH: ["近期已观测", "ok"],
  REVIEWED: ["审核有效", "ok"], REVOKED: ["已撤销", "off"], AVAILABLE: ["可执行", "ok"], REJECTED: ["未通过", "bad"],
  SKIPPED: ["本轮未改动", "off"], PLANNING: ["正在规划", "run"], MATCHED: ["与计划一致", "ok"], CHANGED: ["与计划不同", "warn"],
  ACCEPTED: ["已接受", "ok"], DONE: ["已完成", "ok"], SIGNED_OUT: ["已退出", "off"],
  CANDIDATE: ["待接受", "warn"], WAITING_INPUT: ["等待输入", "warn"], WAITING_RESOURCE: ["等待资源", "warn"],
  WAITING_DECISION: ["等待决定", "warn"], EXECUTION_UNCERTAIN: ["执行结果不明", "warn"],
  ACCEPT_AS_HYPOTHESIS: ["已作为假设接受", "ok"], REJECT: ["已拒绝", "off"], NEED_MORE_EVIDENCE: ["需要更多证据", "warn"],
};
const statusOf = s => STATUS[String(s || "").toUpperCase()] || [s ? "其他状态" : "未记录", "off"];
function tag(s, label) { const [text, tone] = statusOf(s); return el("span", { class: "tag " + tone }, label || text); }

const ACTIVITY_TITLES = {
  "event.accepted": "收到你的一条消息", "console.login": "你登录了控制台", "console.operation": "控制台操作已记录",
  "configuration.saved": "保存了新的配置版本", "cloud_budget.bound": "建立了云端预算记录", "cloud_budget.configured": "更新了云端预算",
  "cloud_budget.native_settled": "同步了一笔已结算费用", "model.not_dispatched": "模型请求没有发出", "model.video_rate_correction": "修正了视频计费",
  "memory.conflict": "发现需要核对的记忆", "memory.control": "记忆管理操作已记录", "memory.recall_invalid": "一次记忆检索未通过核验",
  "reading.owner_requested": "收到你的阅读请求", "reply.superseded": "回复由新消息接续", "diary.recompute_requested": "请求重新整理日记",
  "diary.committed": "日记已保存", "diary.failed": "日记整理失败", "diary.cancelled": "日记整理已取消",
  "delivery.result": "消息投递有了结果", "delivery.owner_reconciled": "你核对了一条投递", "delivery.account_not_sent": "账号动作没有发出",
  "job.succeeded": "一项任务已完成", "poke.accepted": "收到拍一拍", "poke.completed": "拍一拍已完成", "poke.failed": "拍一拍失败",
  "goal.proposed": "她提出了一个目标", "goal.completed": "一个目标已完成", "goal.reflection_withdrawn": "一条反思已撤回",
  "affect.observed": "记录了一条交流偏好", "social.plan_started": "开始规划资料改动", "social.plan_decided": "资料改动计划已确定",
  "social.plan_failed": "资料改动计划失败", "social.action_reserved": "资料改动已预留额度", "social.action_dispatched": "资料改动已发出",
  "social.action_result": "资料改动有了平台回执", "social.avatar_reviewed": "头像素材已审核", "social.profile_observed": "观测了一次 QQ 资料",
  "social.runtime_cancelled": "资料行为已取消", "bili_video.scheduled": "安排了 B 站视频分析", "bili_video.unavailable": "B 站视频暂时无法分析",
  "bili_video.clips_declined": "视频片段未纳入分析", "source_grant.read_revoked": "停止了一项来源读取", "consent.granted": "授予了一项许可",
  "consent.revoked": "撤回了一项许可", "tool.proposed": "提出了一个工具任务", "tool.revoked": "撤回了一个工具任务",
  "tool.cancelled_before_dispatch": "工具任务在执行前取消", "live_text.handoff_aborted": "实时窗口交接已中止",
};
const activityTitle = item => ACTIVITY_TITLES[item.title] || item.title || "未命名记录";
/** @type {Array<[string, string, string[]]>} */
const LANES = [
  ["chat", "对话", ["event.", "reply.", "delivery.", "poke.", "live_text.", "job."]],
  ["auto", "主动与自主", ["social.", "goal.", "tool.", "affect.", "wake.", "proactive."]],
  ["read", "阅读与内容", ["reading.", "bili_video.", "source_grant.", "consent.", "content."]],
  ["mind", "记忆与日记", ["memory.", "diary.", "consolidation.", "reflection."]],
  ["cost", "费用", ["cloud_budget.", "model."]],
  ["owner", "你的操作", ["console.", "configuration."]],
];
const laneOf = title => (LANES.find(([, , prefixes]) => prefixes.some(p => String(title).startsWith(p))) || ["owner"])[0];
const LANE_ICON = { chat: "send", auto: "spark", read: "book", mind: "memory", cost: "coin", owner: "gear" };

const CAP_NAMES = {
  "memory.manage": "记忆管理", "memory.recall": "记忆检索", diary: "日记", consolidation: "夜间整理", reflection: "反思",
  wake: "自主活动调度", affect: "交流偏好", expression: "表情回应", expression_learning: "表达习惯学习", goals: "目标与承诺",
  proactive: "主动私聊", social: "社交语义判断", "reading.bilibili": "B 站内容读取", "reading.heybox": "小黑盒内容读取",
  "reading.web": "网页阅读", exploration: "内容发现", "bilibili.video": "B 站视频分析", "qq.files": "私人文件",
  "qq.voice.asr": "语音转写", "qq.voice.tts": "语音回复", "qq.poke": "回应拍一拍", "qq.poke.native": "回拍一下",
  "qq.account.qzone_publish": "QQ 空间发布", "qq.account.nickname": "改昵称", "qq.account.signature": "改签名",
  "qq.account.avatar": "改头像", "qq.profile.observe": "资料观测", "qq.images": "私聊图片理解", "qq.animation": "私聊动图理解",
  "model.understand_video": "视频理解模型", "unicode.emoji": "Unicode 表情", "qq.face_names": "QQ 表情名称",
  "qq.face_images": "QQ 表情图像", "memes.catalog": "表情目录", "memes.community_import": "社区表情获取",
  "memes.search": "表情搜索", "memes.semantic": "表情语义理解", "media.voice": "网页语音会话", "media.camera": "摄像头",
  "media.android": "安卓设备", executive: "执行任务",
};
/** @type {Array<[string, string[]]>} */
const CAP_GROUPS = [
  ["记忆与日记", ["memory.manage", "memory.recall", "diary", "consolidation", "reflection", "goals", "affect"]],
  ["主动与自主", ["proactive", "wake", "exploration", "social", "qq.poke", "qq.poke.native"]],
  ["阅读与内容", ["reading.web", "reading.bilibili", "reading.heybox", "bilibili.video", "model.understand_video", "qq.images", "qq.animation", "qq.files"]],
  ["表达与表情", ["expression", "expression_learning", "unicode.emoji", "qq.face_names", "qq.face_images", "memes.catalog", "memes.semantic", "memes.search", "memes.community_import"]],
  ["语音与媒体", ["qq.voice.asr", "qq.voice.tts", "media.voice", "media.camera", "media.android"]],
  ["QQ 资料与执行", ["qq.profile.observe", "qq.account.signature", "qq.account.nickname", "qq.account.avatar", "qq.account.qzone_publish", "executive"]],
];
/* 能力对应的配置项。只显示服务端实际返回的字段；executive 等部署保管的项不在其中。 */
const CAP_FIELDS = {
  "memory.manage": ["memory_enabled"], "memory.recall": ["semantic_min_score", "extraction_min_events", "extraction_idle_seconds", "emerging_min_sessions", "stable_min_days", "memory_daily_micro_usd", "raw_event_retention_days"],
  diary: ["diary_enabled", "diary_generate_hour", "diary_min_source_events", "diary_max_source_events", "diary_max_body_chars", "diary_max_output_tokens", "diary_catchup_days", "diary_max_attempts", "diary_daily_micro_usd"],
  consolidation: ["consolidation_enabled", "consolidation_local_hour", "consolidation_catchup_days"],
  reflection: ["reflection_enabled", "reflection_daily_max_suggestions", "reflection_min_sources", "reflection_max_daily_calls", "reflection_max_output_tokens", "reflection_daily_micro_usd", "reflection_monthly_micro_usd"],
  goals: ["goals_enabled", "goals_max_active", "goal_max_attempts"], affect: ["affect_enabled", "affect_decay_minutes", "affect_max_evidence"],
  proactive: ["proactive_enabled", "proactive_interval_seconds", "proactive_idle_seconds", "proactive_daily_limit", "quiet_start", "quiet_end"],
  wake: ["wake_enabled", "wake_min_interval_minutes", "wake_max_daily_runs", "autonomous_daily_micro_usd"],
  exploration: ["exploration_enabled", "exploration_daily_candidate_limit", "exploration_daily_deep_reads", "exploration_daily_requests", "exploration_monthly_requests", "exploration_max_response_bytes", "exploration_daily_bytes"],
  social: ["social_enabled", "social_semantic_threshold"], "qq.poke": ["poke_policy"], "qq.poke.native": ["poke_policy"],
  "reading.web": ["web_reading_enabled", "web_reading_hosts", "content_enabled"], "reading.bilibili": ["observation_enabled", "content_enabled"], "reading.heybox": ["observation_enabled", "content_enabled"],
  "bilibili.video": ["bili_video"], "model.understand_video": ["bili_video", "video_models"], "qq.images": ["content"], "qq.animation": ["content"], "qq.files": ["private_files"],
  expression: ["expression_enabled", "expression_cooldown_minutes", "expression_selection_probability", "expression_meme_categories"],
  expression_learning: ["expression_learning_enabled"], "unicode.emoji": ["expression_enabled"], "qq.face_names": ["expression_enabled"], "qq.face_images": ["expression_enabled"],
  "memes.catalog": ["meme_discovery_enabled"], "memes.search": ["meme_discovery_enabled", "meme_discovery_daily_queries"],
  "memes.community_import": ["meme_discovery_enabled", "meme_discovery_daily_download_bytes", "meme_discovery_daily_storage_bytes"],
  "memes.semantic": ["meme_semantics_enabled", "meme_caption_daily_micro_usd", "meme_caption_max_output_tokens"],
  "qq.voice.asr": ["voice_enabled", "voice"], "qq.voice.tts": ["voice_enabled", "voice"], "media.voice": ["voice_enabled"], "media.camera": ["camera_enabled"], "media.android": ["android_enabled"],
  "qq.profile.observe": ["social_autonomy"], "qq.account.signature": ["social_autonomy"], "qq.account.nickname": ["social_autonomy"], "qq.account.avatar": ["social_autonomy"], "qq.account.qzone_publish": ["social_autonomy"],
};
/* 可以立即暂停的能力对应 /mode 的 target */
const CAP_RUNTIME = { diary: "diary", wake: "life", proactive: "proactive" };
const REASONS = {
  FEATURE_DISABLED: "配置里没有开启", DEMO_MODE: "当前是演示模式", PAUSED: "运行时已暂停", DIARY_PAUSED: "日记已暂停", LIFE_PAUSED: "自主活动已暂停",
  PROACTIVE_PAUSED_OR_QUIET: "主动私聊已暂停或处于免打扰", CONTENT_DISABLED: "内容读取没有开启", READING_DISABLED: "阅读没有开启",
  DAILY_BUDGET_EXHAUSTED: "今日额度已用完", MONTHLY_BUDGET_EXHAUSTED: "本月额度已用完", MODEL_DAILY_BUDGET_EXHAUSTED: "模型今日额度已用完",
  MODEL_MONTHLY_BUDGET_EXHAUSTED: "模型本月额度已用完", SCENARIO_BUDGET_DISABLED: "这个场景没有分配额度", SCENARIO_CALL_LIMIT: "已到调用次数上限",
  BUDGET_RECONCILIATION_REQUIRED: "费用需要先核对", MODEL_USAGE_UNKNOWN: "有结果不明的模型调用", DELIVERY_RECONCILIATION_REQUIRED: "有投递需要先核对",
  READER_NOT_CONFIGURED: "读取程序没有配置", READER_UNAVAILABLE: "读取程序不可用", READER_OBSERVATION_STALE: "读取程序很久没有联系",
  WEB_HOST_SCOPE_REQUIRED: "没有设置允许读取的网站", SOURCE_GRANT_REQUIRED: "需要你授予读取授权", LIVE_APPROVAL_REQUIRED: "需要当前窗口的授权",
  LIVE_APPROVAL_EXPIRED: "窗口授权已过期", VOICE_SCOPE_REQUIRED: "语音需要授权范围", VOICE_NODE_SCOPE_REQUIRED: "语音节点需要授权范围",
  VOICE_CONFIG_INVALID: "语音配置无效", SOCIAL_AUTONOMY_DISABLED: "QQ 资料行为没有开启", SOCIAL_LIVE_SCOPE_REQUIRED: "需要当前窗口的授权",
  SOCIAL_REVIEWED_AVATAR_REQUIRED: "需要审核过的头像素材", EXECUTOR_NOT_CONFIGURED: "没有配置执行端", MEDIA_ADAPTER_NOT_CONFIGURED: "没有配置媒体服务",
  PINNED_PARSER_IMAGE_REQUIRED: "需要固定版本的解析镜像", PRIVATE_FILE_ADAPTER_UNAVAILABLE: "文件解析通道不可用", OWNER_IDENTITY_REQUIRED: "需要确认主人身份",
  POKE_COOLDOWN: "拍一拍冷却中", POKE_RATE_LIMIT: "拍一拍已到次数上限", GOALS_AND_EXPLORATION_REQUIRED: "需要先开启目标与内容发现",
  TOOL_NOT_EXPOSED_THIS_TURN: "这轮对话没有开放", STATIC_FACE_IMAGES_PAUSED: "QQ 表情图像已暂停",
};

/* ================= 状态与接口 ================= */
const SECRET_KEY = /password|cookie|authorization|api_key|access_token|refresh_token|csrf|secret|credential/i;
const state = {
  session: null, epoch: 0, generation: 0, route: "overview", sub: "", data: null, controller: null,
  tz: undefined, updatedAt: null, toastTimer: null, searchTimer: null,
  actKind: "all", memTab: "review", memQ: "", diaryDay: null, capQ: "", capF: "all", capCollapsed: new Set(),
  capMobile: false, contentF: "all", draft: new Map(), configSave: null, budgetDraft: new Map(), budgetSave: null,
  deliverySaves: new Map(), cfgQ: "", currency: "CNY", fx: 8000000, actFrom: "", actTo: "", memView: "list", gSel: null, voiceTab: "in", cogKind: "all", cogSel: null, cogTab: "overview", cogMore: null, meterScale: "call", meterDrawn: null, contentTab: "items", expanded: new Set(),
};

class ApiError extends Error {
  constructor(code, status = 0, fields = [], uncertain = false) {
    super(code); this.code = code; this.status = status; this.fields = fields; this.uncertain = uncertain;
  }
}
const api = {
  attempts: new Map(),
  /** @param {string} path @param {RequestOptions} [options] @returns {Promise<any>} */
  async request(path, { method = "GET", body, signal, authenticate = true, validate } = {}) {
    const mutation = method !== "GET";
    const headers = { Accept: "application/json" };
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timeout = setTimeout(abort, 15000);
    let attempt;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    try {
      if (mutation) {
        if (!globalThis.crypto?.randomUUID || !globalThis.crypto?.subtle) throw new ApiError("CONSOLE_HTTPS_REQUIRED");
        // 同一内容的重复提交沿用同一个幂等键，结果不明时不会变成第二次操作。
        const bytes = new TextEncoder().encode(JSON.stringify([method, path, body]));
        const hash = await crypto.subtle.digest("SHA-256", bytes);
        attempt = [...new Uint8Array(hash)].map(v => v.toString(16).padStart(2, "0")).join("");
        if (!this.attempts.has(attempt)) this.attempts.set(attempt, crypto.randomUUID());
        headers["Idempotency-Key"] = this.attempts.get(attempt);
        if (state.session) headers["X-CSRF-Token"] = state.session.csrf_token;
      }
      const response = await fetch("/v1/console" + path, {
        method, headers, credentials: "same-origin", cache: "no-store", redirect: "error",
        signal: controller.signal, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      let value = {};
      if (response.status !== 204) {
        try { value = await response.json(); }
        catch { throw new ApiError("CONSOLE_RESPONSE_INVALID", response.status, [], mutation); }
      }
      if (!response.ok) {
        if (response.status === 401 && authenticate) clearSession("会话已过期，请重新登录。");
        throw new ApiError(typeof value?.error === "string" ? value.error : "CONSOLE_REQUEST_FAILED",
          response.status, Array.isArray(value?.fields) ? value.fields : []);
      }
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new ApiError("CONSOLE_RESPONSE_INVALID", response.status, [], mutation);
      if (validate && !validate(value)) throw new ApiError("CONSOLE_RESPONSE_INVALID", response.status, [], mutation);
      if (attempt) this.attempts.delete(attempt);
      return value;
    } catch (error) {
      if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
      if (error instanceof ApiError) throw error;
      throw new ApiError(controller.signal.aborted ? "CONSOLE_TIMEOUT" : "CONSOLE_NETWORK_ERROR", 0, [], mutation);
    } finally { clearTimeout(timeout); signal?.removeEventListener("abort", abort); }
  },
  /** @param {string} path @param {AbortSignal} [signal] */
  get(path, signal) { return this.request(path, { signal }); },
  /** @param {string} path @param {any} body @param {string} [method] @param {(value: any) => boolean} [validate] */
  write(path, body, method = "POST", validate) { return this.request(path, { method, body, validate }); },
};

function errorText(error) {
  const messages = {
    MEMORY_DISABLED: "长期记忆没有开启。", JARGON_TERM_NOT_FOUND: "这个说法已经不在了。", STYLE_CANDIDATE_NOT_FOUND: "这条表达候选已经处理过或已过期。", STYLE_CANDIDATE_RISK_REJECT_ONLY: "这条候选带风险标记或来源理解不一致，只能拒绝。",
    CONSOLE_NETWORK_ERROR: "连接中断了，请检查网络后重试。", CONSOLE_TIMEOUT: "服务器响应超时，请稍后重试。",
    CONSOLE_RESPONSE_INVALID: "服务器返回的数据不完整，请刷新核对。", CONSOLE_HTTPS_REQUIRED: "请通过 HTTPS 打开控制台后再操作。",
    CONSOLE_LOGIN_RATE_LIMIT: "密码错误次数过多，请 5 分钟后再试。", PROACTIVE_DISABLED_IN_CONFIG: "主动私聊在配置里是关闭的，先在配置里开启。",
    DIARY_NOT_FOUND: "这一天没有日记。", IDEMPOTENCY_CONFLICT: "同一个操作编号对应了不同内容，请刷新后重试。",
    PRIVACY_CHANGED: "记录的隐私版本刚刚变化，请刷新后重新读取。", COGNITION_CURSOR_UNAVAILABLE: "翻页位置已失效，请刷新后从最新记录开始。",
    INVALID_COGNITION_QUERY: "记录查询条件不正确。", INVALID_COGNITION_SELECTION: "记录查询条件不正确。",
    CONSOLE_DISABLED: "控制台没有开启。", CONSOLE_OWNER_CREDENTIAL_REQUIRED: "控制台还没有配置主人凭据。",
  };
  let message = messages[error.code];
  if (!message && error.status === 401) message = "密码不对，或者会话已过期，请重新登录。";
  if (!message && error.status === 403) message = "这个操作没有通过校验，请刷新页面后重试。";
  if (!message && error.status === 409) message = "记录或配置已经变化，请刷新后重新核对。";
  if (!message && [400, 422].includes(error.status)) message = "有填写内容不符合要求，请检查后重试。";
  if (!message && error.status === 429) message = "操作太频繁或额度不足，请稍后再试。";
  if (!message && error.status === 503) message = "服务暂时不可用，请到运行诊断里查看。";
  if (!message) message = "操作没有完成，请稍后重试。";
  if (error.uncertain) message = "操作结果还没确认，请先刷新核对。再次提交相同内容会沿用原来的操作编号。";
  return message + "（" + (typeof error.code === "string" ? error.code : "CONSOLE_REQUEST_FAILED") + "）";
}

/* ================= 格式 ================= */
const valid = v => typeof v === "string" && Number.isFinite(Date.parse(v));
function fmt(value, options) {
  if (!valid(value)) return "时间未记录";
  return new Intl.DateTimeFormat("zh-CN", { hour12: false, timeZone: state.tz, ...options }).format(new Date(value));
}
const fmtTime = v => fmt(v, { hour: "2-digit", minute: "2-digit" });
const fmtDate = v => fmt(v, { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
const localDay = v => valid(v) ? new Intl.DateTimeFormat("en-CA", { timeZone: state.tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(v)) : "";
function localMinutes(v) {
  const parts = new Intl.DateTimeFormat("en-GB", { timeZone: state.tz, hour: "2-digit", minute: "2-digit", hour12: false }).formatToParts(new Date(v));
  const get = t => Number(parts.find(p => p.type === t)?.value || 0);
  return (get("hour") % 24) * 60 + get("minute");
}
const hm = m => String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0");
const count = v => typeof v === "number" && Number.isFinite(v) ? v.toLocaleString("zh-CN") : "—";
const yuan = v => typeof v === "number" && Number.isFinite(v) ? "¥" + (v / 1e6).toFixed(2) : "—";
/* 模型账本按美元记账；显示时可以按配置汇率换算成人民币 */
function money(microUsd, unit = "usd") {
  if (typeof microUsd !== "number" || !Number.isFinite(microUsd)) return "—";
  if (unit !== "usd") return yuan(microUsd);
  if (state.currency === "CNY") { const cny = microUsd * state.fx / 1e6; return "¥" + (cny / 1e6).toFixed(cny % 10000 ? 4 : 2); }
  return "$" + (microUsd / 1e6).toFixed(microUsd % 10000 ? 4 : 2);
}
const usd = v => money(v, "usd");
function duration(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "未记录";
  if (seconds >= 86400 && seconds % 86400 === 0) return seconds / 86400 + " 天";
  if (seconds >= 3600 && seconds % 3600 === 0) return seconds / 3600 + " 小时";
  if (seconds >= 60 && seconds % 60 === 0) return seconds / 60 + " 分钟";
  return seconds.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) + " 秒";
}
function bytes(v) {
  if (typeof v !== "number" || !Number.isFinite(v)) return "未记录";
  return v >= 1048576 ? (v / 1048576).toFixed(1) + " MB" : v >= 1024 ? (v / 1024).toFixed(1) + " KB" : v + " 字节";
}
function safeUrl(value) {
  if (typeof value !== "string") return null;
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null; }
  catch { return null; }
}
function safeValue(value, depth = 0) {
  if (depth > 4) return "…";
  if (value === null || value === undefined) return "未记录";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value !== "object") return String(value).slice(0, 8000);
  if (Array.isArray(value)) return value.slice(0, 30).map(v => safeValue(v, depth + 1)).join("、");
  return Object.entries(value).filter(([k]) => !SECRET_KEY.test(k)).slice(0, 30).map(([k, v]) => k + "：" + safeValue(v, depth + 1)).join("\n");
}
const shortId = v => String(v || "").slice(-8);

/* ================= 通用组件 ================= */
function head(title, desc, actions) {
  return el("div", { class: "page-head" }, el("div", {}, el("h1", {}, title), desc ? el("p", {}, desc) : null),
    actions ? el("div", { class: "actions" }, actions) : null);
}
function sheet(title, body, link, cls = "") {
  return el("section", { class: "sheet " + cls }, title ? el("div", { class: "sheet-head" }, el("h2", {}, title), link || null) : null,
    el("div", { class: "sheet-body" }, body));
}
const linkTo = (route, text) => el("a", { class: "link", href: "#" + route }, text, icon("arrow"));
/** @param {string} text @param {(event: MouseEvent) => any} onclick @param {ButtonOptions} [options] */
function btn(text, onclick, { cls = "", symbol, type = "button", disabled = false, label } = {}) {
  return el("button", { type, class: "btn " + cls, onclick, disabled, "aria-label": label }, symbol ? icon(symbol) : null, text);
}
function empty(title, text, retry) {
  return el("div", { class: "sheet empty", role: "status" }, el("h3", {}, title), el("p", {}, text),
    retry ? btn("重新加载", () => loadRoute(), { symbol: "refresh" }) : null);
}
function kv(label, value) { return el("div", { class: "kv" }, el("span", {}, label), value instanceof Node ? value : el("span", {}, value)); }
function toast(text) {
  clearTimeout(state.toastTimer);
  const t = $("#toast"); t.replaceChildren(icon("check"), document.createTextNode(text)); t.hidden = false;
  state.toastTimer = setTimeout(() => { t.hidden = true; }, 4500);
}
function notice(text, retry = false) {
  const n = $("#notice"); n.hidden = !text; n.replaceChildren();
  if (text) {
    n.append(icon("warn"), el("span", {}, text));
    if (retry) n.append(btn("重试", () => loadRoute(), { cls: "sm" }));
  }
}
function setConnection(kind, text) { const c = $("#conn"); c.className = "conn " + kind; c.lastElementChild.textContent = text; }

function openDialog(title, content, { wide = false } = {}) {
  const d = $("#dlg");
  d.classList.toggle("wide", wide);
  d.replaceChildren(el("div", { class: "dlg-head" }, el("h2", {}, title),
    el("button", { class: "icon-btn", type: "button", "aria-label": "关闭", onclick: () => d.close() }, icon("close"))),
    el("div", { class: "dlg-body" }, content));
  if (!d.open) d.showModal();
  requestAnimationFrame(() => $("input:not([type=hidden]), textarea, select", d)?.focus());
}
const closeDialog = () => $("#dlg").open && $("#dlg").close();
function formError(form, text) {
  let field = $(".form-error", form);
  if (!field) { field = el("p", { class: "form-error", role: "alert" }); form.append(field); }
  field.textContent = text; field.hidden = !text;
}
async function perform(form, button, operation, success) {
  const label = button.textContent, epoch = state.epoch;
  button.disabled = true; button.textContent = "正在处理…"; formError(form, "");
  try {
    const result = await operation();
    if (epoch !== state.epoch || !state.session) return;
    await success(result);
  } catch (error) {
    if (epoch === state.epoch && state.session) { if (form.isConnected) formError(form, errorText(error)); else notice(errorText(error)); }
  } finally { if (button.isConnected) { button.disabled = false; button.textContent = label; } }
}
function drawer(title, sub, body) {
  const d = $("#drawer");
  d.replaceChildren(el("div", { class: "drawer-head" }, el("div", {}, el("h2", {}, title), sub ? el("p", { class: "note" }, sub) : null),
    el("button", { class: "icon-btn", type: "button", "aria-label": "关闭", onclick: closeDrawer }, icon("close"))),
    el("div", { class: "drawer-body" }, body));
  $("#drawer-scrim").hidden = false; d.classList.add("open"); d.setAttribute("aria-hidden", "false");
  setTimeout(() => $(".drawer-head .icon-btn", d)?.focus(), 30);
}
function closeDrawer() { const d = $("#drawer"); d.classList.remove("open"); d.setAttribute("aria-hidden", "true"); $("#drawer-scrim").hidden = true; }
function detailList(detail, skip = []) {
  return Object.entries(detail || {}).filter(([k, v]) => !skip.includes(k) && !SECRET_KEY.test(k) && v !== null && v !== undefined && v !== "")
    .map(([k, v]) => {
      const url = /url|source_page/.test(k) ? safeUrl(v) : null;
      const value = url ? el("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, String(v)) :
        /_bytes$/.test(k) ? bytes(v) : /_seconds$/.test(k) ? duration(v) : /_at$/.test(k) && valid(v) ? fmtDate(v)
        : typeof v === "string" && /^[A-Z][A-Z_]+$/.test(v) ? (STATUS[v] ? statusOf(v)[0] : reasonText(v)) : safeValue(v);
      return kv(DETAIL_LABELS[k] || subLabel(k), value);
    });
}
const DETAIL_LABELS = {
  entity_id: "关联对象", trace_id: "追踪编号", subject: "主体", memory_kind: "类型", needs_review: "需要核对", evidence_ids: "来源消息",
  local_date: "日期", version: "版本", commitment_state: "承诺状态", updated_at: "更新时间", owner_event_id: "你的消息", output_event_id: "输出记录",
  read_request_id: "读取请求", usage_request_id: "用量记录", reserved_bytes: "预留空间", asset_sha256: "素材摘要", source_sha256: "来源摘要",
  state: "状态", completed_at: "完成时间", count: "次数", billed_micro_usd: "已计费用", action_type: "投递类型",
  last_seen_at: "最近联系", resource_state: "上次资源状态", active_job_id: "关联任务", node_id: "处理节点", capabilities: "已配置能力",
  operation: "处理类型", task_state: "处理状态", partial: "音频不完整", input_bytes: "输入大小", input_duration_seconds: "输入时长",
  result_bytes: "结果大小", delivery_state: "投递状态", message_id: "消息编号", deadline_at: "处理截止", result_expires_at: "结果有效期",
  manifest_state: "配置核验", plan_state: "计划状态", receipt_state: "平台回执", dispatched_at: "开始发送", expires_at: "有效期至",
  revoked_at: "撤销时间", license_label: "使用许可", usage_conditions: "使用条件", purpose: "用途", dimensions: "尺寸", size_bytes: "大小",
  field: "资料项目", baseline_value: "最近观测内容", observed_at: "观测时间", freshness_seconds: "观测有效时长", freshness_state: "观测新鲜度",
  last_action_at: "最近相关操作", source_page: "来源页面", source_url: "来源链接", state_label: "状态说明", last_error_code: "最近原因",
};

/* ================= 会话、导航与读取 ================= */
const LOADERS = {
  overview: async signal => {
    const [o, a, d, c] = await Promise.allSettled([api.get("/overview", signal),
      api.get("/activity?limit=60", signal), api.get("/diagnostics", signal), api.get("/configuration", signal)]);
    if (o.status !== "fulfilled") throw o.reason;
    if (o.value.budget?.timezone) state.tz = o.value.budget.timezone;
    return { ...o.value, recent: a.status === "fulfilled" ? a.value : null, diagnostics: d.status === "fulfilled" ? d.value : null,
      configuration: c.status === "fulfilled" ? c.value : null };
  },
  activity: async (signal, more) => {
    // 按日期筛选时继续往前翻页，直到越过开始日期（最多 20 页）。
    let page = await api.get("/activity?limit=60" + (more ? "&before=" + encodeURIComponent(more) : ""), signal);
    const items = [...(page.items || [])];
    for (let n = 0; state.actFrom && page.next_cursor && n < 20; n++) {
      const oldest = items.at(-1); if (oldest && localDay(oldest.occurred_at) < state.actFrom) break;
      page = await api.get("/activity?limit=60&before=" + encodeURIComponent(page.next_cursor), signal);
      items.push(...(page.items || []));
    }
    return { ...page, items };
  },
  memories: signal => state.memTab === "jargon" ? api.get("/jargon", signal) : state.memTab === "recall"
    ? (state.memQ.trim() ? api.get("/memories/recall?q=" + encodeURIComponent(state.memQ.trim()), signal) : Promise.resolve({ items: [], query: "" }))
    : api.get("/memories?limit=60&review=" + (state.memTab === "review") + (state.memQ.trim() ? "&q=" + encodeURIComponent(state.memQ.trim()) : ""), signal),
  life: async signal => {
    const [life, goals, reflections, topics] = await Promise.all([
      api.get("/life", signal), api.get("/goals", signal), api.get("/reflections", signal),
      api.get("/life/topics", signal),
    ]);
    return { ...life, goals: goals.items, reflections: reflections.items, topics };
  },
  content: async signal => {
    const [content, grants, watched] = await Promise.allSettled([api.get("/content", signal), api.get("/source-grants", signal), api.get("/watch-ledger", signal)]);
    if (content.status !== "fulfilled") throw content.reason;
    return { ...content.value, grants: settled(grants), watched: settled(watched) };
  },
  memes: signal => api.get("/memes", signal),
  tools: async signal => { const v = await api.get("/tools", signal); return { tasks: Array.isArray(v.items) ? v.items : [], enabled: v.enabled === true }; },
  self: async signal => {
    const [affect, interests, expression, styles, mood] = await Promise.allSettled([api.get("/affect", signal), api.get("/interests", signal), api.get("/expression-learning", signal), api.get("/style-candidates", signal), api.get("/mood", signal)]);
    return { affect: settled(affect), interests: settled(interests), expression: settled(expression), styles: settled(styles), mood: settled(mood) };
  },
  voice: signal => api.get("/voice", signal),
  "account-actions": async signal => {
    const [account, publications, topics] = await Promise.allSettled([
      api.get("/account-actions", signal), api.get("/qzone/publications", signal), api.get("/life/topics", signal)]);
    return { ...settled(account), publications: settled(publications), publicationTopics: settled(topics) };
  },
  diagnostics: signal => api.get("/diagnostics", signal),
  thinking: async signal => {
    // 隐私版本变化时整页重读一次，不把不同代次的记录拼在一起
    for (let attempt = 0; ; attempt++) {
      try {
        const pages = await Promise.all(COG_LIST.map(k => cognitionPage(k, signal)));
        if (new Set(pages.map(p => p.privacy_epoch)).size > 1) throw new ApiError("PRIVACY_CHANGED", 409);
        const data = Object.fromEntries(COG_LIST.map((k, i) => [k, pages[i]]));
        const located = await locateCognition(data, signal);
        if (located.locate?.epoch !== undefined && located.locate.epoch !== pages[0].privacy_epoch) throw new ApiError("PRIVACY_CHANGED", 409);
        return located;
      } catch (error) {
        if (error.code !== "PRIVACY_CHANGED" || attempt > 0) throw error;
      }
    }
  },
  caps: async signal => {
    const [o, c, a] = await Promise.all([api.get("/overview", signal), api.get("/configuration", signal),
      api.get("/autonomy", signal).catch(e => { if (e.name === "AbortError") throw e; return null; })]);
    return { ...o, configuration: c, autonomy: a };
  },
  configuration: async signal => { const c = await api.get("/configuration", signal); const fx = c.cloud_budget?.config?.usd_to_cny_micro; if (Number.isSafeInteger(fx) && fx > 0) state.fx = fx; return { configuration: c }; },
};
const PAGES = {};
const DRAFT_ROUTES = ["caps", "configuration"];

function clearSession(message = "") {
  state.epoch += 1; state.generation += 1; state.controller?.abort();
  state.session = null; state.data = null; state.draft.clear(); state.budgetDraft.clear(); state.deliverySaves.clear();
  state.memQ = ""; state.capQ = ""; state.cfgQ = ""; api.attempts.clear(); clearTimeout(state.searchTimer);
  closeDialog(); closeDrawer();
  $("#page").replaceChildren(); $("#notice").hidden = true; $("#toast").hidden = true;
  $("#app").hidden = true; $("#boot").hidden = true; $("#login").hidden = false;
  $("#pw").value = ""; $("#login-err").textContent = message; $("#login-err").hidden = !message;
}
function acceptSession(value) {
  if (typeof value.csrf_token !== "string" || !value.csrf_token || !valid(value.expires_at)) throw new ApiError("CONSOLE_RESPONSE_INVALID");
  state.session = { csrf_token: value.csrf_token, expires_at: value.expires_at };
  $("#login").hidden = true; $("#boot").hidden = true; $("#app").hidden = false; $("#pw").value = "";
  updateSessionLeft();
  return navigate();
}
function updateSessionLeft() {
  if (!state.session) return;
  const left = Math.max(0, Math.round((Date.parse(state.session.expires_at) - Date.now()) / 60000));
  $("#session-left").textContent = left > 0 ? "剩余 " + left + " 分钟" : "即将过期";
}

function renderNav() {
  const o = state.nav || {};
  $("#nav").replaceChildren(...ROUTES.flatMap(([group, list]) => [el("p", { class: "nav-group" }, group), ...list.map(([key, name, ic]) => {
    const badge = key === "memories" ? o.review : key === "diagnostics" ? o.problems : 0;
    return el("a", { href: "#" + key, "aria-current": state.route === key ? "page" : null }, icon(ic), el("span", {}, name),
      badge ? el("span", { class: "badge" + (key === "diagnostics" ? " bad" : "") }, String(badge)) : null);
  })]));
  const p = $("#presence"), paused = o.paused === true;
  p.classList.toggle("paused", paused);
  p.replaceChildren(el("span", { class: "orb", "aria-hidden": "true" }),
    el("strong", {}, o.mode === "demo" ? "演示模式" : o.ready === false ? "需要留意" : "正式运行"),
    el("span", {}, o.worker ? (statusOf(o.worker)[0] === "有运行记录" ? "工作进程有运行记录" : "工作进程：" + statusOf(o.worker)[0]) : "正在读取状态"),
    // replaceChildren 会把 null 当成文字“null”，没有说明时不放这一段
    ...(o.note ? [el("p", {}, o.note)] : []));
}
function updateNavFrom(overview) {
  if (!overview?.runtime) return;
  const caps = overview.capabilities?.capabilities || [];
  const paused = ["diary", "wake", "proactive"].every(id => caps.find(c => c.id === id)?.state !== "READY");
  state.nav = {
    review: overview.counts?.review_memories || 0, mode: overview.runtime.mode, worker: overview.runtime.worker?.state,
    ready: overview.runtime.state === "READY", paused,
    problems: (overview.counts?.unknown_calls || 0) + (state.nav?.uncertain || 0),
    note: overview.runtime.state === "READY" ? "运行状态正常。" : "有些状态需要留意，打开运行诊断查看。",
  };
  if (overview.budget?.timezone) state.tz = overview.budget.timezone;
  if (Number.isSafeInteger(overview.budget?.config?.usd_to_cny_micro) && overview.budget.config.usd_to_cny_micro > 0) state.fx = overview.budget.config.usd_to_cny_micro;
}

async function navigate() {
  const [route, sub = ""] = (location.hash.slice(1) || "overview").split("/");
  if (!ROUTE_NAME[route]) { location.hash = "overview"; return; }
  if (route !== state.route && state.draft.size && !DRAFT_ROUTES.includes(route) && !confirm("配置还有没保存的修改。要放弃修改并离开吗？")) {
    history.replaceState(null, "", "#" + state.route); return;
  }
  if (route !== state.route) {
    if (!DRAFT_ROUTES.includes(route)) state.draft.clear();
    state.data = null; state.capMobile = false; closeDrawer();
  }
  // 思考与决策按编号定位时，换一个编号需要重新读取
  const same = route === state.route && state.data && !(route === "thinking" && decodeURIComponent(sub) !== state.sub);
  state.route = route; state.sub = decodeURIComponent(sub);
  document.title = ROUTE_NAME[route] + " · Companion 主人控制台";
  $("#crumb").replaceChildren(el("b", {}, ROUTE_NAME[route]));
  $("#side").classList.remove("open");
  renderNav();
  if (!state.session) return;
  if (same) { render({ keepScroll: true }); return; }
  await loadRoute({ reset: true });
}

async function loadRoute({ reset = false, more = false } = {}) {
  if (!state.session) return;
  if (!reset && state.draft.size && DRAFT_ROUTES.includes(state.route) && !confirm("刷新会丢弃没保存的配置修改，继续吗？")) return;
  if (!reset) state.draft.clear();
  const generation = ++state.generation, epoch = state.epoch, route = state.route;
  state.cogMore = null;
  state.controller?.abort(); state.controller = new AbortController();
  const signal = state.controller.signal;
  if (!state.data) $("#page").replaceChildren(el("div", { class: "loading", role: "status" }, el("span", { class: "spin" }), "正在读取…"));
  $("#page").setAttribute("aria-busy", "true"); $("#refresh").disabled = true; setConnection("loading", "正在同步"); notice("");
  try {
    let value = await LOADERS[route](signal, more ? state.data?.next_cursor : undefined);
    if (generation !== state.generation || epoch !== state.epoch || !state.session) return;
    if (value.items !== undefined && (!Array.isArray(value.items) || value.items.some(i => !i || !["string", "number"].includes(typeof i.id) || typeof i.status !== "string"))) {
      throw new ApiError("CONSOLE_RESPONSE_INVALID");
    }
    if (more && state.data?.items) {
      const unique = new Map(state.data.items.map(i => [String(i.id), i]));
      for (const i of value.items) unique.set(String(i.id), i);
      value = { ...value, items: [...unique.values()] };
    }
    if (route === "overview" || route === "caps") updateNavFrom(value);
    if (route === "memories" && Number.isSafeInteger(value.meta?.pending_count) && value.meta.pending_count >= 0) {
      state.nav = { ...(state.nav || {}), review: value.meta.pending_count };
    }
    if (route === "overview" && value.diagnostics?.items) state.nav.uncertain = value.diagnostics.items.filter(i => i.status === "DELIVERY_UNCERTAIN").length;
    state.data = value; state.updatedAt = new Date().toISOString();
    renderNav(); render({ keepScroll: more });
    setConnection("", "已连接");
  } catch (error) {
    if (error.name === "AbortError" || generation !== state.generation || epoch !== state.epoch || !state.session) return;
    setConnection("off", navigator.onLine ? "同步失败" : "网络已断开");
    if (state.data) notice("数据没有更新。" + errorText(error), true);
    else $("#page").replaceChildren(empty(navigator.onLine ? "暂时读不到数据" : "网络已断开", errorText(error), true));
  } finally {
    if (generation === state.generation) { $("#page").setAttribute("aria-busy", "false"); $("#refresh").disabled = false; }
  }
}

/** 分段按钮放不下时右边渐隐，提示还能横向滑动 */
function segFade(seg) {
  const update = () => seg.classList.toggle("more-r", seg.scrollLeft + seg.clientWidth < seg.scrollWidth - 1);
  update();
  if (!seg.dataset.fade) { seg.dataset.fade = "1"; seg.addEventListener("scroll", update, { passive: true }); }
}
function render({ keepScroll = false } = {}) {
  if (!state.session || !state.data) return;
  const y = scrollY, active = /** @type {HTMLInputElement|null} */ (document.activeElement), focusId = active?.id, caret = active?.selectionStart;
  // 页面内可以单独滚动的区域（例如能力列表）在重绘后回到原来的位置。
  const inner = Object.fromEntries($$("[data-keep-scroll]").map(n => [n.dataset.keepScroll, n.scrollTop]));
  const focusKey = active?.dataset?.k;
  const body = PAGES[state.route](state.data), bar = saveBar();
  $("#page").replaceChildren(body, el("footer", { class: "foot-note" }, el("span", {}, "Companion Core 主人控制台"),
    el("span", {}, "本页读取于 " + fmtTime(state.updatedAt))), ...(bar ? [bar] : []));
  $("#page").classList.toggle("has-savebar", !!$("#page .savebar"));
  if (keepScroll) for (const n of $$("[data-keep-scroll]")) if (n.dataset.keepScroll in inner) n.scrollTop = inner[n.dataset.keepScroll];
  PAGES[state.route].after?.();
  for (const seg of $$(".seg")) segFade(seg);
  if (keepScroll) window.scrollTo(0, y); else window.scrollTo(0, 0);
  const target = focusId ? document.getElementById(focusId) : focusKey ? $(`[data-k="${CSS.escape(focusKey)}"]`) : null;
  if (target && target !== document.body) { target.focus({ preventScroll: true }); if (caret != null && target.setSelectionRange) try { target.setSelectionRange(caret, caret); } catch {} }
}
const rerender = () => render({ keepScroll: true });

/* ================= 今天 ================= */
const tipOf = new WeakMap();
function cfgValue(configuration, key) { return configuration?.fields?.find(f => f.key === key)?.value; }
PAGES.overview = data => {
  const now = data.observed_at || new Date().toISOString(), today = data.budget?.local_day || localDay(now);
  const nowMin = localMinutes(now), hour = Math.floor(nowMin / 60);
  const greet = hour < 6 ? "夜深了" : hour < 11 ? "早上好" : hour < 14 ? "中午好" : hour < 18 ? "下午好" : "晚上好";
  const dateText = fmt(now, { month: "long", day: "numeric", weekday: "long" });
  const items = (data.recent?.items || []).filter(i => valid(i.occurred_at) && localDay(i.occurred_at) === today);
  const chats = items.filter(i => i.title === "event.accepted").length;
  return el("div", {},
    head(`${greet}，今天是 ${dateText}`, data.recent ? `今天收到你 ${chats} 条消息，记录了 ${items.length} 件事。` : "活动记录暂时没读到，其他状态照常显示。",
      btn("管理主动行为", openModeDialog, { symbol: "pause" })),
    dayStrip(items, nowMin, data), todoList(data),
    el("div", { class: "grid-2" },
      el("div", { class: "stack" }, budgetCard(data.budget, data.counts), recentCard(data.recent)),
      el("div", { class: "stack" }, capsCard(data.capabilities?.capabilities || []), tokenCard(data.model))));
};
/** 今日 Token 统计：只汇总今天、实际用量已记录的模型调用，分段规则与“请求计量”相同 */
function tokenCard(page) {
  const more = el("a", { class: "link", href: "#thinking", onclick: () => { state.cogKind = "model"; state.cogSel = null; } }, "查看明细", icon("arrow"));
  if (!page) return sheet("今日 Token 统计", el("p", { class: "note" }, "模型调用记录暂时读不到，可以稍后刷新。"), more, "tok");
  const { calls, observed_calls: observed, cache_calls: cacheCalls, uncertain_calls: unknown, cached: hit } = page;
  const partial = page.complete !== true;
  const parts = [["cached", hit], ["fresh", page.fresh], ["unknown", page.unknown_input], ["output", page.output]];
  const shown = parts.filter(([k, v]) => v > 0 || (k === "output" && observed));
  const total = parts.reduce((s, [, v]) => s + v, 0), cacheIn = hit + page.fresh;
  const anim = state.tokenAnim !== state.updatedAt && motionOk();
  const stat = (n, v, sub, count) => el("div", { class: "tok-stat" }, el("span", {}, n), el("b", { class: "num", "data-count": count ?? null }, v), sub ? el("small", {}, sub) : null);
  if (!calls && !partial) return sheet("今日 Token 统计", el("p", { class: "note" }, "今天还没有模型调用。"), more, "tok");
  return sheet("今日 Token 统计", [
    partial ? el("div", { class: "alert" }, icon("warn"), el("span", {}, "今天的调用没有全部读到，下面的数字不完整。")) : null,
    el("div", { class: "m-donut" }, donut(shown, tk(total), observed ? (partial ? "已读到的总量" : "实际总量") : calls ? "未记录" : partial ? "未读完" : "今天还没有", observed ? total : null),
      observed ? meterLegend(shown, total, { cached: "缓存命中", fresh: "未命中缓存", unknown: "缓存未记录", output: "输出" })
        : el("p", { class: "note" }, calls ? "今天的调用都没有有效的实际用量回执。" : partial ? "还没读到今天的调用，不能确定今天有没有调用。" : "今天还没有模型调用。")),
    el("div", { class: "tok-stats" },
      stat("模型调用", partial ? (calls ? "≥ " + calls : "未读完") : String(calls), observed < calls ? "实际用量已记录 " + observed + " 次" : null),
      stat("缓存命中", cacheIn > 0 ? pctText(hit, cacheIn) : "未记录", cacheCalls && cacheCalls < observed ? "按 " + cacheCalls + " 次已记录的计算" : null),
      stat("已计或预留", partial && !calls ? "未读完" : (partial ? "≥ " : "") + usd(page.billed_micro_usd), unknown ? unknown + " 次结果不明" : partial ? "只含已读到的调用" : "按配置费率估算")),
    el("p", { class: "note mt" }, (partial ? "已读到 " + calls + " 次今天的调用，更早的部分没有读完。" : "")
      + "上下文分区（系统提示词、工具定义等）没有保存，这里不做拆分。")], more, "meter tok" + (anim ? " anim" : ""));
}
function dayStrip(items, nowMin, data) {
  const pct = m => (m / 1440 * 100).toFixed(3) + "%";
  const qs = cfgValue(data.configuration, "quiet_start"), qe = cfgValue(data.configuration, "quiet_end");
  const bands = Number.isInteger(qs) && Number.isInteger(qe) ? (qs > qe ? [[qs, 24], [0, qe]] : qs < qe ? [[qs, qe]] : []) : [];
  let d = 0;
  const lanes = LANES.map(([key, name]) => {
    const own = items.filter(i => laneOf(i.title) === key).map(i => ({ m: localMinutes(i.occurred_at), i })).sort((a, b) => a.m - b.m);
    const groups = own.reduce((gs, e) => { const g = gs.at(-1); if (g && e.m - g.at(-1).m <= 20) g.push(e); else gs.push([e]); return gs; }, []);
    const marks = groups.map(g => {
      const first = g[0], last = g.at(-1), bad = g.some(e => ["DELIVERY_UNCERTAIN", "FAILED", "UNKNOWN"].includes(e.i.status) || /failed|unavailable|invalid|conflict/.test(e.i.title));
      const mark = el("button", { type: "button", class: "mark " + key + (bad ? " bad" : "") + (g.length > 1 ? " group" : ""),
        "aria-label": g.map(e => hm(e.m) + " " + activityTitle(e.i)).join("；"),
        style: { left: pct(first.m), "--d": String(d++), ...(g.length > 1 ? { width: `calc(${((last.m - first.m) / 1440 * 100).toFixed(3)}% + 6px)` } : {}) },
        onclick: () => { location.hash = "activity"; } }, g.length > 1 ? el("b", {}, String(g.length)) : null);
      tipOf.set(mark, g.map(e => [hm(e.m), activityTitle(e.i), statusOf(e.i.status)[0]]));
      return mark;
    });
    return [el("div", { class: "lane-label" }, name), el("div", { class: "lane" }, marks)];
  });
  const truncated = data.recent?.next_cursor && items.length >= (data.recent.items || []).length;
  return el("section", { class: "day enter", "aria-label": "今天发生的事" },
    el("div", { class: "day-head" }, el("h2", {}, "今天发生的事"),
      el("span", { class: "sum" }, "悬停或聚焦色块查看详情，现在是 ", el("b", {}, hm(nowMin)))),
    el("div", { class: "day-scroll" }, el("div", { class: "day-grid" }, lanes, el("div"),
      el("div", { class: "ruler" }, [0, 3, 6, 9, 12, 15, 18, 21, 24].map(h => el("span", { style: { left: (h / 24 * 100) + "%" } }, String(h).padStart(2, "0")))),
      el("div", { class: "day-overlay" },
        bands.map(([a, z], i) => el("div", { class: "quiet", style: { left: pct(a * 60), width: pct((z - a) * 60) } }, i === 0 ? el("span", {}, "免打扰") : null)),
        el("div", { class: "now", style: { left: pct(nowMin) } }, el("b", {}, "现在 " + hm(nowMin))),
        el("div", { class: "future", style: { left: pct(nowMin) } })))),
    !items.length ? el("p", { class: "note day-empty" }, data.recent ? "今天还没有记录。" : "活动记录暂时读不到。") : null,
    truncated ? el("p", { class: "note day-empty" }, "只显示了最近 60 条记录，更早的在活动时间线里。") : null);
}
PAGES.overview.after = () => { const day = $(".day"); const sc = $(".day-scroll"), tok = $(".sheet.tok");
  if (state.tokenAnim !== state.updatedAt) { state.tokenAnim = state.updatedAt; if (tok) countUp(tok); }
  if (sc && innerWidth <= 860 && sc.scrollWidth > sc.clientWidth) sc.scrollLeft = sc.scrollWidth * .7 - sc.clientWidth * .7;
  setTimeout(() => day?.classList.remove("enter"), 1600); };
function todoList(data) {
  const out = [], counts = data.counts || {}, b = data.budget || {};
  const uncertain = (data.diagnostics?.items || []).filter(i => i.status === "DELIVERY_UNCERTAIN");
  if (uncertain.length) out.push(["bad", uncertain.length, "条投递结果不明", "不会自动重发,请到 QQ 里看看是否已出现", "去核对", "diagnostics"]);
  if (counts.review_memories) out.push(["", counts.review_memories, "条记忆可以核对", "查看候选内容，或核对相互矛盾的说法", "去确认", "memories"]);
  if (counts.companion_proposals) out.push(["info", counts.companion_proposals, "条她的调整提议", "她想少打扰或休息一下,等你批准", "去看看", "caps/wake"]);
  if (counts.pending_goals) out.push(["", counts.pending_goals, "个目标等你决定", "她提出了新目标,需要你接受或取消", "去看看", "life"]);
  if (counts.pending_reflections) out.push(["", counts.pending_reflections, "条反思等你批准", "每日反思已完成,需要你批准或拒绝", "去看看", "life"]);
  if (counts.unknown_calls) out.push(["", counts.unknown_calls, "次模型调用结果不明", "相关费用会一直占用额度,直到核对完成", "查看诊断", "diagnostics"]);
  if (b.reconciliation_required || b.restore_reconciliation_required) out.push(["bad", "!", "费用记录需要核对", b.admission_blocked ? "云调用已暂停,需核对独立账本" : "请核对独立费用记录", "查看诊断", "diagnostics"]);
  if (b.fx_expired) out.push(["bad", "!", "汇率已过期", "过期后不会发起新的云调用", "更新汇率", "configuration/budget"]);
  if (data.runtime?.worker?.state && data.runtime.worker.state !== "RECORDED") out.push(["info", "!", "聊天处理进程没有近期记录", "进程可能没有启动或已经停止", "查看诊断", "diagnostics"]);
  if (!out.length) return el("div", { class: "todo-clear" }, icon("check"), "没有需要你处理的事。");
  return el("div", { class: "todo", "aria-label": "等你处理" }, out.map(([tone, n, title, sub, action, route]) =>
    el("div", { class: "todo-item " + tone }, el("span", { class: "n" }, String(n)), el("h3", {}, title), el("p", {}, sub),
      el("a", { class: "btn sm", href: "#" + route }, action))));
}
function budgetCard(b, counts = {}) {
  if (!b || b.currency !== "CNY") return sheet("今日云端费用", el("p", { class: "note" }, "费用记录还没有人民币口径。"));
  const limit = b.daily_limit_micro_cny, pct = v => limit > 0 ? Math.min(100, Math.max(0, v / limit * 100)) + "%" : "0%";
  const reconcile = b.reconciliation_required || b.restore_reconciliation_required;
  return sheet("今日云端费用", [
    reconcile ? el("div", { class: "alert bad" }, icon("warn"), el("span", {}, (b.restore_reconciliation_required ? "备份恢复后的费用记录可能不完整" : "费用记录需要核对") + (b.admission_blocked ? "，云调用已暂停。" : "。"))) : null,
    b.unknown_micro_cny > 0 ? el("div", { class: "alert" }, icon("warn"), el("span", {}, "有结果不明的费用，它们会一直占用额度，直到核对完成。")) : null,
    el("div", { class: "money-big" }, el("b", { class: "num" }, yuan(b.remaining_micro_cny)), el("span", {}, "今天还能用，限额 " + yuan(limit))),
    el("div", { class: "bar", role: "img", "aria-label": `已结算 ${yuan(b.settled_micro_cny)}，已预留 ${yuan(b.reserved_micro_cny)}，结果不明 ${yuan(b.unknown_micro_cny)}` },
      el("i", { class: "s", style: { width: pct(b.settled_micro_cny) } }), el("i", { class: "r", style: { width: pct(b.reserved_micro_cny) } }), el("i", { class: "u", style: { width: pct(b.unknown_micro_cny) } })),
    el("div", { class: "legend" }, [["已结算", b.settled_micro_cny, "var(--ink)"], ["已预留", b.reserved_micro_cny, "#8b90c4"], ["结果不明", b.unknown_micro_cny, "var(--amber)"]]
      .map(([n, v, c]) => el("div", { style: { "--c": c } }, n, el("b", {}, yuan(v))))),
    b.over_limit ? el("p", { class: "form-error" }, "总占用已超出限额，之后的预留会受限。") : null,
    el("div", { class: "queues" }, el("div", {}, el("b", {}, count(counts.pending_jobs)), el("span", {}, "排队中的任务")),
      el("div", {}, el("b", {}, count(counts.pending_deliveries)), el("span", {}, "待发送消息")),
      el("div", {}, el("b", {}, count(counts.review_memories)), el("span", {}, "待确认记忆"))),
    el("p", { class: "note mt" }, "按配置费率和汇率估算，不等于服务商的最终账单。")], linkTo("configuration/budget", "调整预算"));
}
function recentCard(recent) {
  if (!recent) return sheet("最近发生", el("p", { class: "note" }, "活动记录暂时读不到，可以稍后刷新。"));
  const items = (recent.items || []).slice(0, 6);
  return sheet("最近发生", items.length ? el("ul", { class: "rows recent" }, items.map(i => el("li", {},
    el("span", { class: "ic" }, icon(LANE_ICON[laneOf(i.title)])), el("span", { class: "txt" }, el("b", {}, activityTitle(i)), el("span", {}, LANES.find(l => l[0] === laneOf(i.title))[1])),
    el("time", {}, fmtTime(i.occurred_at))))) : el("p", { class: "note" }, "还没有记录。"), linkTo("activity", "全部活动"));
}
function capsCard(caps) {
  const on = caps.filter(c => !["UNAVAILABLE", "DISABLED"].includes(c.state));
  return sheet("能力状态", [el("div", { class: "caps" }, on.map(c => kv(CAP_NAMES[c.id] || c.id, tag(c.state)))),
    el("p", { class: "note mt" }, `另有 ${caps.length - on.length} 项没有开启或不可用。“可用”表示本地准入条件允许，不代表远端在线或已经执行。`)], linkTo("caps", "管理能力"));
}

/* ================= 活动时间线 ================= */
/* 正常完成的状态不在每一行重复显示；出问题的状态照常标出 */
const TL_QUIET = new Set(["RECORDED", "SUCCEEDED", "COMPLETED", "COMPLETE", "COMMITTED", "SETTLED", "SENT", "ACK", "DONE", "OBSERVED"]);
/** 同一天里连续、标题和状态都相同的记录合成一行，点开逐条查看 */
function tlRuns(list) {
  const runs = [];
  for (const i of list) {
    const last = runs[runs.length - 1]?.[0];
    if (last && last.title === i.title && last.status === i.status) runs[runs.length - 1].push(i); else runs.push([i]);
  }
  return runs;
}
PAGES.activity = data => {
  const items = (data.items || []).filter(i => (state.actKind === "all" || laneOf(i.title) === state.actKind)
    && (!state.actFrom || localDay(i.occurred_at) >= state.actFrom) && (!state.actTo || localDay(i.occurred_at) <= state.actTo));
  const today = localDay(new Date().toISOString()), dayAgo = n => localDay(new Date(Date.now() - n * 864e5).toISOString());
  const presets = [["all", "全部日期", "", ""], ["today", "今天", today, today], ["yesterday", "昨天", dayAgo(1), dayAgo(1)], ["week", "近 7 天", dayAgo(6), today]];
  const preset = presets.find(([, , f, t]) => f === state.actFrom && t === state.actTo)?.[0] || "custom";
  const setRange = (f, t) => { state.actFrom = f; state.actTo = t; loadRoute({ reset: true }); };
  const dateBar = el("div", { class: "toolbar" }, el("div", { class: "seg", role: "group", "aria-label": "按日期筛选" },
      presets.map(([k, n, f, t]) => el("button", { type: "button", "aria-pressed": String(preset === k), onclick: () => setRange(f, t) }, n))),
    el("label", { class: "date-pick" }, el("span", {}, "从"), el("input", { class: "input", type: "date", id: "act-from", value: state.actFrom, max: today, onchange: e => setRange(e.target.value, state.actTo) })),
    el("label", { class: "date-pick" }, el("span", {}, "到"), el("input", { class: "input", type: "date", id: "act-to", value: state.actTo, max: today, onchange: e => setRange(state.actFrom, e.target.value) })));
  const days = new Map();
  for (const i of items) { const d = localDay(i.occurred_at) || "未记录"; if (!days.has(d)) days.set(d, []); days.get(d).push(i); }
  const todayKey = localDay(new Date().toISOString());
  const label = d => d === todayKey ? "今天" : d === localDay(new Date(Date.now() - 864e5).toISOString()) ? "昨天" : d.slice(5).replace("-", "月") + "日";
  return el("div", {}, head("活动时间线", "按时间倒序记录每一次接收、发送、保存和操作。点开一条可以看到它的编号。"), dateBar,
    el("div", { class: "toolbar" }, el("div", { class: "seg", role: "group", "aria-label": "按类型筛选" },
      [["all", "全部"], ...LANES.map(([k, n]) => [k, n])].map(([k, n]) => el("button", { type: "button", "aria-pressed": String(state.actKind === k), onclick: () => { state.actKind = k; rerender(); } }, n)))),
    items.length ? [...days].map(([d, list]) => el("section", { class: "tl-day" },
      el("div", { class: "tl-date" }, el("b", {}, label(d)), el("span", {}, d)),
      el("div", { class: "tl-list" }, tlRuns(list).map(run => {
        const i = run[0], lane = laneOf(i.title), many = run.length > 1;
        const first = fmtTime(run[run.length - 1].occurred_at), last = fmtTime(i.occurred_at);
        const when = first === last ? last : first + "–" + last;
        const b = el("button", { type: "button", class: "tl-item" + (many ? " run" : ""), "aria-expanded": "false", style: { "--k": `var(--lane-${lane})` },
          onclick: () => b.setAttribute("aria-expanded", String(b.getAttribute("aria-expanded") !== "true")) },
          el("time", {}, when), el("span", {}, el("b", {}, activityTitle(i), many ? el("span", { class: "chip" }, "连续 " + run.length + " 条") : null),
            el("span", { class: "meta" }, el("span", {}, LANES.find(l => l[0] === lane)[1]))),
          TL_QUIET.has(i.status) ? el("span") : tag(i.status),
          el("span", { class: "tl-more" }, many ? el("ol", { class: "tl-run" }, run.map(r => el("li", {}, el("time", {}, fmtTime(r.occurred_at)), detailList(r.detail))))
            : detailList(i.detail)));
        return b;
      })))) : empty("这里还没有记录", state.actFrom || state.actTo ? "这段日期里没有符合条件的记录。" : state.actKind === "all" ? "发生新的接收、发送或操作后，会出现在这里。" : "换一个类型看看。"),
    data.next_cursor ? el("div", { class: "more" }, btn("加载更早的记录", e => { /** @type {HTMLButtonElement} */ (e.currentTarget).disabled = true; loadRoute({ more: true }); })) : null);
};

/* ================= 记忆 ================= */
const MEM_KIND = { preference: "偏好", fact: "事实", plan: "计划", habit: "习惯", relation: "关系", profile: "资料", event: "事件", communication: "沟通", goal: "目标" };
const MEM_SUBJECT = { user: "关于你", owner: "关于你", self: "关于她", companion: "关于她" };
function memSearch(v) { state.memQ = v; clearTimeout(state.searchTimer); state.searchTimer = setTimeout(() => loadRoute({ reset: true }), 300); }
const JARGON_STATE = { CANDIDATE: ["还在观察", "run"], INFERRED: ["已推测含义", "ok"], NOT_JARGON: ["是普通说法", "off"] };
function jargonView(data) {
  const items = data.items || [];
  return el("div", {}, data.enabled ? null : el("p", { class: "note mb" }, "黑话学习没有开启（配置页“学习主人的黑话”）。"),
    el("p", { class: "note mb" }, "从你的私聊里学到的说法。含义是她根据你以前的用法猜的，只用来理解你，不会拿来模仿。来源消息被遗忘时会一起删除。"),
    items.length ? el("ul", { class: "rows" }, items.map(t => { const [label, tone] = JARGON_STATE[t.state] || [t.state, "off"];
      return el("li", { class: "block" },
        el("div", { class: "row-gap" }, el("b", {}, t.term), el("span", { class: "tag " + tone }, label),
          el("span", { class: "note" }, "用过 " + count(t.uses) + " 次，" + fmtDate(t.updated_at) + " 更新")),
        t.meaning ? el("p", { class: "mt" }, "可能指：" + t.meaning) : null,
        el("div", { class: "row-gap mt" }, btn("忘掉这个说法", () => jargonForget(t), { cls: "sm danger", symbol: "trash" })));
    })) : empty("还没有学到黑话", "你在私聊里用到自己特有的说法后，会先出现在这里观察，用到几次后再推测含义。"));
}
function jargonForget(t) {
  const form = el("form", { class: "dlg-form" }, el("p", {}, "“" + t.term + "”以后不再学习，也不再用来理解你的消息。聊天记录不受影响。"));
  const submit = el("button", { type: "submit", class: "btn danger" }, "忘掉");
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    perform(form, submit, () => api.write("/jargon/forget", { term: t.term }, "POST", r => typeof r.state === "string"),
      async () => { closeDialog(); toast("已忘掉"); await loadRoute({ reset: true }); });
  });
  openDialog("忘掉“" + t.term + "”？", form);
}
function recallView(data) {
  const items = data.items || [];
  if (!state.memQ.trim()) return empty("输入一句话试试", "按聊天时的同一套检索排序，显示她会想起哪些记忆、为什么。不调用模型。");
  if (!items.length) return empty("没有会被想起的记忆", "这句话和现有记忆既没有字面重合，语义相似度也低于 " + (data.semantic_min_score ?? "阈值") + "。");
  return el("ol", { class: "rows" }, items.map((m, i) => {
    const r = m.recall || {};
    const why = [r.exact ? "原文包含这句话" : null, r.lexical_rank ? "关键词第 " + r.lexical_rank + " 名" : null,
      r.semantic_rank ? "语义第 " + r.semantic_rank + " 名" : null].filter(Boolean);
    return el("li", { class: "block" },
      el("div", { class: "row-gap" }, el("b", {}, (i + 1) + ". " + m.content), tag(m.status), m.needs_review ? el("span", { class: "chip" }, "有冲突") : null),
      el("span", { class: "meta-line" }, why.map(w => el("span", { class: "chip" }, w)),
        el("span", { class: "note num" }, "相似度 " + (r.similarity ?? "—") + "，融合分 " + (r.fused_score ?? "—")),
        el("span", { class: "note" }, "来自 " + (m.evidence_ids || []).length + " 条消息")));
  }));
}
PAGES.memories = data => {
  const items = data.items || [];
  const searchInput = el("input", { class: "input", id: "mem-q", type: "search", maxlength: "100", placeholder: "搜索记忆内容", value: state.memQ,
    oninput: e => { if (e.isComposing) return; memSearch(e.target.value); }, oncompositionend: e => memSearch(e.target.value) });
  return el("div", {}, head("记忆", "候选记忆已自动保存，可在对话中参考。确认表示你认可其内容；忘记会同时清除来源和由此衍生的内容。"),
    el("div", { class: "toolbar" }, el("div", { class: "seg", role: "group" },
      [["review", "待确认"], ["all", "全部"], ["recall", "召回测试"], ["jargon", "黑话"]].map(([k, n]) => el("button", { type: "button", "aria-pressed": String(state.memTab === k), onclick: () => { state.memTab = k; loadRoute({ reset: true }); } },
        n, k === "review" && state.nav?.review ? el("span", { class: "count" }, String(state.nav.review)) : null))),
      el("label", { class: "search" }, icon("search"), searchInput), el("span", { class: "spacer" }),
      el("div", { class: "seg", role: "group", "aria-label": "视图" }, [["list", "列表", "timeline"], ["graph", "关系图", "spark"]].map(([k, n, ic]) =>
        el("button", { type: "button", "aria-pressed": String(state.memView === k), onclick: () => { state.memView = k; rerender(); } }, icon(ic), n)))),
    state.memTab === "jargon" ? jargonView(data) : state.memTab === "recall" ? recallView(data) : state.memView === "graph" ? memoryGraph(items) : items.length ? el("div", { class: "mem-list" }, items.map(m => {
      const d = m.detail || {}, review = m.status !== "CONFIRMED" || d.needs_review;
      return el("article", { class: "mem" + (review ? " review" : "") },
        el("div", {}, el("h3", {}, m.title), el("div", { class: "meta" }, tag(m.status), d.memory_kind ? el("span", { class: "chip" }, MEM_KIND[d.memory_kind] || "其他") : null,
          d.subject ? el("span", {}, MEM_SUBJECT[d.subject] || "") : null, el("span", {}, "更新于 " + fmtDate(m.occurred_at)),
          Array.isArray(d.evidence_ids) ? el("span", {}, "来自 " + d.evidence_ids.length + " 条消息") : null),
          d.needs_review ? el("div", { class: "alert mt" }, icon("warn"), el("span", {}, "这条记忆和其他说法可能冲突，需要你核对。")) : null),
        el("div", { class: "acts" }, m.status !== "CONFIRMED" ? btn("确认", () => memoryAction(m, "confirm"), { cls: "sm primary", symbol: "check" }) : null,
          btn("纠正", () => memoryAction(m, "correct"), { cls: "sm", symbol: "edit" }), btn("忘记", () => memoryAction(m, "forget"), { cls: "sm danger", symbol: "trash" })));
    })) : empty(state.memQ ? "没有匹配的记忆" : state.memTab === "review" ? "没有待确认的记忆" : "还没有记忆",
      state.memQ ? "换个关键词试试。" : "新提取的候选会显示在“待确认”中，也可以在“全部”查看。"));
};
function memoryAction(item, decision) {
  const titles = { confirm: "确认这条记忆", correct: "纠正这条记忆", forget: "忘记这条记忆？" };
  const form = el("form", { class: "dlg-form" });
  let input;
  if (decision === "forget") form.append(el("p", {}, "“" + item.title + "”"), el("ul", { class: "consequence" },
    el("li", {}, "清除这条记忆和它引用的原始消息"), el("li", {}, "失效可能复述它的近期回复、摘要与自我观察"),
    el("li", {}, "从旧备份恢复时也会重新执行这次遗忘"), el("li", {}, "已经发到 QQ 的消息无法撤回")));
  else if (decision === "correct") {
    input = el("textarea", { class: "input", id: "memory-correction", required: true, rows: "4", maxlength: "500", value: item.title });
    form.append(el("p", { class: "muted" }, "会创建一条替代记录，原记录保留修订关系，不会被直接覆盖。"),
      el("label", { class: "field" }, el("span", {}, "原内容"), el("input", { class: "input", value: item.title, disabled: true })),
      el("label", { class: "field" }, el("span", {}, "正确的内容"), input));
  } else form.append(el("p", {}, "“" + item.title + "”"), el("p", { class: "muted" }, "确认会把这条记忆标为你已认可。"));
  const submit = el("button", { type: "submit", class: "btn " + (decision === "forget" ? "danger solid" : "primary") },
    decision === "forget" ? "忘记这条记忆" : decision === "correct" ? "保存纠正" : "确认");
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    const body = { decision, ...(input ? { corrected_content: input.value.trim() } : {}) };
    if (input && !body.corrected_content) { formError(form, "请填写正确的内容。"); return; }
    perform(form, submit, () => api.write("/memories/" + encodeURIComponent(item.id) + "/decision", body, "POST",
      r => typeof r.memory_id === "string" && typeof r.state === "string" && (r.memory_id === String(item.id) || decision === "correct" && String(r.supersedes) === String(item.id))),
      async r => { closeDialog(); toast({ confirm: "已确认", correct: "已保存纠正", forget: "已忘记" }[decision] + "（" + statusOf(r.state)[0] + "）"); await loadRoute({ reset: true }); });
  });
  openDialog(titles[decision], form);
}

/* ================= 日记与成长 ================= */
const WEEK = ["日", "一", "二", "三", "四", "五", "六"];
PAGES.life = data => {
  const items = data.items || [];
  const diaries = items.filter(i => i.kind === "diary" && /^\d{4}-\d{2}-\d{2}$/.test(i.detail?.local_date || ""));
  const days = new Set(diaries.map(i => i.detail.local_date));
  if (!state.diaryDay || !/^\d{4}-\d{2}-\d{2}$/.test(state.diaryDay)) state.diaryDay = diaries[0]?.detail.local_date || localDay(new Date().toISOString());
  const [y, mo] = state.diaryDay.split("-").map(Number), first = new Date(Date.UTC(y, mo - 1, 1)).getUTCDay(), total = new Date(Date.UTC(y, mo, 0)).getUTCDate();
  const today = localDay(new Date().toISOString());
  const cells = [...Array(first)].map(() => el("span"));
  for (let d = 1; d <= total; d++) {
    const key = `${y}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`, has = days.has(key);
    cells.push(el("button", { type: "button", class: (has ? "has" : "") + (key === today ? " today" : ""), "aria-pressed": String(key === state.diaryDay),
      "aria-label": `${mo}月${d}日${has ? "，有日记" : ""}`, onclick: () => { state.diaryDay = key; rerender(); } }, String(d)));
  }
  const shift = n => { const dt = new Date(Date.UTC(y, mo - 1 + n, 1)); state.diaryDay = dt.toISOString().slice(0, 10); rerender(); };
  const goals = data.goals || [];
  const reflections = data.reflections || [];
  const paper = el("article", { class: "paper", id: "diary-paper", "aria-live": "polite" }, el("p", { class: "note" }, "正在读取日记…"));
  queueMicrotask(() => loadDiary(state.diaryDay, paper));
  return el("div", {}, head("日记与成长", "日记由独立进程在自然日结束后整理,只根据核验过的真实互动书写。", btn("暂停或恢复写日记", openModeDialog, { symbol: "pause" })),
    el("div", { class: "diary-wrap" },
      el("div", { class: "stack" },
        el("section", { class: "sheet cal" }, el("div", { class: "cal-head" },
          el("button", { type: "button", class: "icon-btn", "aria-label": "上个月", onclick: () => shift(-1) }, icon("left")), el("b", {}, `${y} 年 ${mo} 月`),
          el("button", { type: "button", class: "icon-btn", "aria-label": "下个月", onclick: () => shift(1) }, icon("right"))),
          el("div", { class: "cal-grid" }, WEEK.map(w => el("span", { class: "wd" }, w)), cells),
          el("div", { class: "cal-legend" }, el("span", {}, el("span", { class: "tag ok" }), "最近 14 篇日记"), el("span", {}, "其他日期也可以点开查看")))),
      paper),
    // 目标、反思、话题并排三列，宽度不够时依次换行，不留大块空白
    el("div", { class: "life-grid" },
      sheet("目标与承诺", goals.length ? el("div", { class: "life-list" }, capped("goals", byRank(goals, goalRank).map(g => goalCard(g)), 3, "div")) : el("p", { class: "note" }, "还没有目标。"), countNote(goals.length, 20)),
      sheet("最近的反思", reflections.length ? el("div", { class: "life-list" }, capped("reflections", byRank(reflections, r => r.state === "PENDING" ? 0 : 1).map(r => reflectionCard(r)), 3, "div")) : el("p", { class: "note" }, "还没有反思记录。"), countNote(reflections.length, 20)),
      topicsSheet(data.topics)));
};
async function loadDiary(day, paper) {
  const epoch = state.epoch;
  try {
    const r = await api.get("/diary/" + day);
    if (epoch !== state.epoch || !paper.isConnected) return;
    if (r.local_date !== day || typeof r.body_md !== "string") throw new ApiError("CONSOLE_RESPONSE_INVALID");
    paper.replaceChildren(el("div", { class: "paper-head" }, el("div", {}, el("h2", {}, r.title || day + " 的日记"),
      el("p", {}, (Number.isInteger(r.version) ? "第 " + r.version + " 版，" : "") + r.body_md.length + " 字")),
      el("div", { class: "actions" }, btn("修订", () => diaryAction(day, "correction", r.body_md), { cls: "sm", symbol: "edit" }),
        btn("删除", () => diaryAction(day, "forget", ""), { cls: "sm danger", symbol: "trash" }))),
      el("div", { class: "paper-body" }, r.body_md.split(/\n\s*\n/).filter(Boolean).map(p => el("p", {}, p))),
      el("div", { class: "paper-foot" }, el("span", {}, "只有你能查看")));
  } catch (error) {
    if (epoch !== state.epoch || !paper.isConnected) return;
    paper.replaceChildren(el("div", { class: "empty" }, el("h3", {}, error.code === "DIARY_NOT_FOUND" ? day + " 没有日记" : "日记暂时读不到"),
      el("p", {}, error.code === "DIARY_NOT_FOUND" ? "这一天没有写日记，或者已经删除。" : errorText(error))));
  }
}
function diaryAction(day, operation, current) {
  const form = el("form", { class: "dlg-form" });
  const input = operation === "correction" ? el("textarea", { class: "input serif", id: "diary-correction", required: true, rows: "12", value: current }) : null;
  if (input) form.append(el("p", { class: "muted" }, "修订会保留之前的版本，并标记为你修订。"), el("label", { class: "field" }, el("span", {}, "正文"), input));
  else form.append(el("ul", { class: "consequence" }, el("li", {}, "清除这天的正文和所有修订版本"), el("li", {}, "从旧备份恢复时，这天的副本也会被清除"), el("li", {}, "不会删除这天的对话记录和记忆")));
  const submit = el("button", { type: "submit", class: "btn " + (input ? "primary" : "danger solid") }, input ? "保存修订" : "删除这天的日记");
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    const body = input ? { text: input.value.trim() } : {};
    if (input && !body.text) { formError(form, "请填写日记正文。"); return; }
    perform(form, submit, () => api.write("/diary/" + day + "/" + operation, body, "POST",
      r => typeof r.state === "string" && Boolean(r.state) || typeof r.entry_id === "string" && Number.isInteger(r.version)),
      async () => { closeDialog(); toast(input ? "已保存修订" : "已删除这天的日记"); await loadRoute({ reset: true }); });
  });
  openDialog(input ? "修订 " + day + " 的日记" : "删除 " + day + " 的日记？", form, { wide: !!input });
}
function openModeDialog() {
  // 当前状态来自 overview.runtime.modes；旧后端没有这个字段时显示“状态未知”，开关仍可用
  const modes = state.data?.runtime?.modes || null;
  const targets = [
    ["proactive", "主动私聊", "她会在合适的时候主动找你聊天", "暂停后会取消还没发出的主动消息"],
    ["life", "自主活动", "她自己安排阅读、整理和生活话题", "暂停后不再安排新的活动，已接受的任务继续处理"],
    ["diary", "写日记", "每天整理当天的日记", "暂停后不再整理新的日记，已有日记不受影响"]];
  const form = el("form", { class: "dlg-form mode-form" }, el("p", { class: "muted" }, "只影响她主动做的事，你发来的消息始终会回复。改动保存后立即生效，不需要重启。"));
  const submit = el("button", { type: "submit", class: "btn primary", disabled: true }, "保存");
  const draft = new Map();
  const rows = targets.map(([key, name, sub, pauseNote]) => {
    // 配置文件没开启时，控制台不能恢复，开关锁定为关
    const m = modes?.[key], known = m && typeof m.paused === "boolean", locked = m?.available === false;
    const now = known ? !m.paused && !locked : null;
    const status = el("span", { class: "mode-state" }), note = el("p", { class: "mode-note", hidden: true }, icon("warn"), el("span", {}, pauseNote));
    const sw = el("button", { type: "button", class: "switch", role: "switch", "aria-label": name, disabled: !!locked });
    const row = el("div", { class: "mode-row" }, el("div", { class: "mode-txt" }, el("b", {}, name), el("span", {}, sub)), status, sw);
    const paint = () => {
      const on = draft.has(key) ? draft.get(key) : now, changed = draft.has(key);
      sw.setAttribute("aria-checked", String(on === true));
      status.textContent = locked ? "配置文件里未开启" : changed ? (on ? "将恢复" : "将暂停") : now == null ? "状态未知" : now ? "进行中" : "已暂停";
      status.className = "mode-state" + (changed ? " changed" : now === false ? " off" : "");
      row.classList.toggle("changed", changed);
      note.hidden = !(changed && on === false);
      submit.disabled = draft.size === 0;
    };
    sw.addEventListener("click", () => {
      const cur = draft.has(key) ? draft.get(key) : now, next = !(cur === true);
      if (now != null && next === now) draft.delete(key); else draft.set(key, next);
      paint();
    });
    paint();
    form.append(el("div", { class: "mode-item" }, row, note));
    return key;
  });
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    const changes = rows.filter(k => draft.has(k)).map(k => ({ target: k, enabled: draft.get(k) }));
    if (!changes.length) { formError(form, "没有改动。"); return; }
    perform(form, submit, async () => { for (const c of changes) await api.write("/mode", c, "POST", r => typeof r.enabled === "boolean" || typeof r.state === "string"); },
      async () => { closeDialog(); toast("已保存，立即生效"); await loadRoute({ reset: true }); });
  });
  openDialog("管理主动行为", form);
}
/* ================= 阅读与文件 ================= */
const CONTENT_KIND = { link: ["链接", "#3b4070"], qq_image: ["图片", "#2f8a62"], link_image: ["文章图", "#6e9bd0"], file: ["文件", "#6c7190"], video: ["视频", "#fb7299"] };
PAGES.content = data => {
  const items = (data.items || []).filter(i => state.contentF === "all" || (state.contentF === "running" ? ["run"].includes(statusOf(i.status)[1]) : ["warn", "bad"].includes(statusOf(i.status)[1])));
  const tabs = el("div", { class: "seg", role: "group", "aria-label": "内容或授权" }, [["items", "内容记录"], ["watched", "她看过的"], ["grants", "来源授权"]].map(([k, n]) =>
    el("button", { type: "button", "aria-pressed": String(state.contentTab === k), onclick: () => { state.contentTab = k; rerender(); } }, n)));
  if (state.contentTab === "grants") return el("div", {}, head("阅读与文件", "读取程序登记的来源授权。撤回只停止之后的读取，遗忘会同时清除按授权读到的内容。"),
    el("div", { class: "toolbar" }, tabs), grantsView(data.grants));
  if (state.contentTab === "watched") return el("div", {}, head("阅读与文件", "她读完你分享的视频或文章后记下的看法。只在聊天里合适时提起，不当作线下经历。"),
    el("div", { class: "toolbar" }, tabs), watchedView(data.watched));
  return el("div", {}, head("阅读与文件", "你分享的链接、图片和文件。外部内容只作为证据进入对话，不能改变权限或人格。"),
    el("div", { class: "toolbar" }, tabs, el("div", { class: "seg" }, [["all", "全部"], ["running", "进行中"], ["attention", "需要留意"]].map(([k, n]) =>
      el("button", { type: "button", "aria-pressed": String(state.contentF === k), onclick: () => { state.contentF = k; rerender(); } }, n)))),
    items.length ? el("section", { class: "sheet table-wrap" }, el("table", { class: "t" }, el("thead", {}, el("tr", {}, el("th", {}, "类型"), el("th", {}, "内容"), el("th", {}, "状态"), el("th", { class: "r" }, "时间"))),
      el("tbody", {}, items.map(i => { const [k, c] = CONTENT_KIND[i.kind] || ["内容", "#6c7190"];
        return el("tr", { tabindex: "0", onclick: () => contentDrawer(i), onkeydown: e => { if (e.key === "Enter") contentDrawer(i); } },
          el("td", {}, el("span", { class: "src", style: { background: c } }, k)), el("td", { class: "title-cell" }, i.title, i.summary ? el("small", {}, reasonText(i.summary)) : null),
          el("td", {}, tag(i.status)), el("td", { class: "r muted" }, fmtDate(i.occurred_at))); }))))
      : empty("还没有内容记录", state.contentF === "all" ? "你在私聊里分享链接、图片或文件后，会出现在这里。" : "换个筛选条件看看。"));
};
const WATCH_DEPTH = { media_sampled: "看了抽样画面", subtitle: "看了字幕", comments: "看了评论", text: "读了正文", title_only: "只看了标题" };
const WATCH_PLATFORM = { bilibili: "B站", heybox: "小黑盒" };
const WATCH_RATING = { 2: "很喜欢", 1: "还不错", 0: "一般", "-1": "不太喜欢", "-2": "很不喜欢" };
function watchedView(w) {
  if (!w || w.error) return sheet("她看过的", failNote("观看记录", w?.error));
  const items = w.items || [];
  return sheet("她看过的", [
    w.enabled ? null : el("p", { class: "note mb" }, "观看记录没有开启（配置页“观看记录与看法”）。下面是开启期间留下的记录。"),
    items.length ? el("ul", { class: "rows" }, items.map(i => el("li", { class: "block" },
      el("div", { class: "row-gap" }, el("b", {}, i.title || "（没有标题）"), el("span", { class: "chip" }, WATCH_PLATFORM[i.platform] || i.platform || "来源"),
        WATCH_RATING[i.rating] ? el("span", { class: "chip" }, WATCH_RATING[i.rating]) : null),
      i.take ? el("p", { class: "mt" }, i.take) : null,
      el("span", { class: "meta-line" }, el("span", { class: "note" }, (WATCH_DEPTH[i.depth] || i.depth || "") + "，" + fmtDate(i.watched_at)),
        (i.topic_tags || []).map(t => el("span", { class: "chip" }, t))))))
      : el("p", { class: "note" }, "还没有观看记录。开启后，她读完你分享的视频或文章会在这里记下看法。")], countNote(items.length, 20));
}
function contentDrawer(i) {
  drawer(i.title, fmtDate(i.occurred_at), [el("div", { class: "mb" }, tag(i.status)), i.summary ? el("p", { class: "mb" }, "原因：" + reasonText(i.summary)) : null,
    kv("记录编号", el("span", { class: "num" }, String(i.id))), detailList(i.detail)]);
}

/* ================= 表情素材 ================= */
const hue = h => parseInt(String(h || "0").slice(0, 6), 16) % 360;
PAGES.memes = data => {
  const items = data.items || [];
  return el("div", {}, head("表情素材", "表情先核验来源，再由模型理解含义，之后才会进入索引供回复选用。这里显示每次理解任务的结果。"),
    items.length ? el("div", { class: "memes" }, items.map(m => el("button", { type: "button", class: "meme", onclick: () => drawer("表情素材理解", fmtDate(m.occurred_at),
      [el("div", { class: "mb" }, tag(m.status)), m.summary ? el("p", { class: "mb" }, "原因：" + reasonText(m.summary)) : null, kv("任务编号", el("span", { class: "num" }, String(m.id))), detailList(m.detail)]) },
      el("div", { class: "pic", style: { background: `hsl(${hue(m.detail?.asset_sha256)} 55% 90%)` } }, tag(m.status), el("span", { class: "hash num" }, String(m.detail?.asset_sha256 || "").slice(0, 8) || "—")),
      el("div", { class: "cap" }, el("b", {}, "素材 " + (String(m.detail?.asset_sha256 || "").slice(0, 6) || shortId(m.id))), el("span", {}, fmtDate(m.occurred_at))))))
      : empty("还没有表情理解记录", "启用表情语义理解后，理解过的素材会出现在这里。"));
};

/* ================= 语音与节点 ================= */
/* 字幕：接口在 detail.transcript 返回时显示（你发的是转写，她发的是合成的原文）。
   超过三行先折叠，可以展开全文；没有返回时明确说明，不用状态说明冒充字幕。 */
function subtitle(j) {
  const text = typeof j.detail?.transcript === "string" ? j.detail.transcript.trim() : "";
  if (!text) return el("p", { class: "vtext none" }, "字幕没有在控制台保留");
  const p = el("p", { class: "vtext clamp" }, text);
  const long = text.length > 90 || text.split("\n").length > 3;
  if (!long) return p;
  const toggle = btn("展开全文", () => { const open = p.classList.toggle("clamp"); toggle.textContent = open ? "展开全文" : "收起"; }, { cls: "sm quiet vmore" });
  return el("div", { class: "vsub" }, p, j.detail?.transcript_truncated ? el("p", { class: "note" }, "字幕较长，控制台只保留了前 " + text.length + " 字。") : null, toggle);
}
const mmss = s => Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0");
PAGES.voice = data => {
  const meta = data.meta || {}, items = data.items || [], nodes = items.filter(i => i.kind === "voice_node"), jobs = items.filter(i => i.kind === "voice_job");
  const policy = meta.reply_policy || {}, out = state.voiceTab === "out";
  const mine = jobs.filter(j => j.detail?.operation !== "tts"), hers = jobs.filter(j => j.detail?.operation === "tts");
  const list = out ? hers : mine;
  const stat = (label, value, sub) => el("div", { class: "vstat" }, el("span", {}, label), el("b", {}, value), sub ? el("small", {}, sub) : null);
  return el("div", {}, head("语音与节点", "语音节点负责听写和合成。联系记录过期不等于设备离线，只代表最近没有收到节点的消息。"),
    el("div", { class: "vstats" }, stat("语音能力", meta.enabled ? "已开启" : "没有开启", meta.enabled ? null : "在能力与开关里开启"),
      stat("近期有联系的节点", count(meta.nodes_recent) + " / " + count(meta.nodes_total)), stat("等待和处理中", count(meta.pending_jobs)),
      stat("投递待核对", count(meta.uncertain_deliveries)),
      stat("她的语音回复", policy.enabled ? (policy.mode === "voice_input" ? "只在你发语音时" : "短回复可以用语音") : "没有开启", policy.enabled ? "最多 " + count(policy.max_chars) + " 字" : null)),
    nodes.length ? el("div", { class: "nodes" }, nodes.map(n => el("section", { class: "node" }, el("h3", {}, n.title, tag(n.status, n.detail?.state_label)),
      kv("最近联系", fmtDate(n.detail?.last_seen_at)), kv("能力", (n.detail?.capabilities || []).map(c => ({ asr: "转写", tts: "合成" })[c] || "其他").join("、") || "未记录"),
      kv("配置核验", statusOf(n.detail?.manifest_state)[0])))) : null,
    el("section", { class: "sheet" }, el("div", { class: "sheet-head" }, el("div", { class: "seg", role: "tablist" },
        el("button", { type: "button", "aria-pressed": String(!out), onclick: () => { state.voiceTab = "in"; rerender(); } }, "你发的", el("span", { class: "count" }, String(mine.length))),
        el("button", { type: "button", "aria-pressed": String(out), onclick: () => { state.voiceTab = "out"; rerender(); } }, "她发的", el("span", { class: "count" }, String(hers.length))))),
      list.length ? el("ul", { class: "vlist" }, list.map(j => {
        const secs = j.detail?.input_duration_seconds, seed = [...String(j.id)].reduce((h, ch) => (h * 31 + ch.charCodeAt(0)) >>> 0, 7);
        const wave = Array.from({ length: 40 }, (_, k) => 4 + Math.round(Math.abs(Math.sin(k * 1.37 + seed % 97) * Math.cos(k * .31 + seed % 13)) * 22));
        return el("li", { class: "vrow gone" + (out ? " out" : "") },
          el("span", { class: "vplay", title: "录音不在控制台保留", "aria-label": "录音不在控制台保留" }, icon("lock")),
          el("div", { class: "vmain" }, el("div", { class: "vtop" }, el("span", { class: "vwave", "aria-hidden": "true" }, wave.map(h => el("i", { style: { height: h + "px" } }))),
              el("span", { class: "vtime num" }, typeof secs === "number" ? mmss(secs) : "—")),
            subtitle(j),
            el("div", { class: "vmeta" }, el("span", {}, fmtDate(j.occurred_at)), j.detail?.node_id ? el("span", {}, j.detail.node_id) : null, tag(j.status, j.detail?.state_label),
              j.detail?.delivery_state ? tag(j.detail.delivery_state) : null, j.detail?.partial ? el("span", {}, "音频不完整") : null,
              j.summary ? el("span", { class: "muted" }, j.summary) : null)));
      }))
        : el("p", { class: "sheet-body note" }, out ? "还没有她的语音回复。" : "还没有你发的语音。"),
      el("p", { class: "sheet-body note vnote" }, "录音在任务结束后清除，不能回放。字幕由接口返回时才显示。")));
};

/* ================= QQ 资料行为 ================= */
PAGES["account-actions"] = data => {
  const meta = data.meta || {}, items = data.items || [];
  const profile = f => items.find(i => i.kind === "account_profile" && i.detail?.field === f);
  const plans = items.filter(i => i.kind === "account_plan"), assets = items.filter(i => i.kind === "account_asset");
  const nick = profile("nickname"), sig = profile("signature"), av = profile("avatar");
  return el("div", {}, head("QQ 资料行为", "她对自己 QQ 资料的改动。计划、平台回执和实际观测分开记录，平台返回成功不等于资料已经变了。"),
    el("section", { class: "sheet qcompare" }, el("div", { class: "sheet-head" }, el("h2", {}, "平台上现在的资料"), el("span", { class: "note" }, nick?.detail?.observed_at ? "观测于 " + fmtDate(nick.detail.observed_at) : "还没有观测")),
      el("div", { class: "sheet-body qpair" }, el("div", { class: "qcard" }, el("span", { class: "qav ph", "aria-hidden": "true" }, av?.detail?.baseline_value ? "已记录" : "—"),
        el("div", { class: "qinfo" }, el("b", {}, nick?.detail?.baseline_value || "昵称尚未观测"), el("span", {}, sig?.detail?.baseline_value || "签名尚未观测"),
          el("small", {}, "观测可能是协议缓存，修改结果以平台回执为准"))))),
    Array.isArray(meta.limits) ? sheet("次数与冷却", el("div", { class: "limits" }, meta.limits.map(l => el("div", { class: "limit" }, el("b", {}, l.title), tag(l.state),
      el("span", { class: "note" }, "每天最多 " + count(l.daily_limit) + " 次，间隔 " + duration(l.cooldown_seconds)), l.reason ? el("span", { class: "note" }, l.reason) : null)))) : null,
    meta.policy_problem ? el("div", { class: "alert bad mt" }, icon("warn"), el("span", {}, meta.policy_problem)) : null,
    el("h2", { class: "section-title" }, "改动计划"),
    plans.length ? plans.map(p => el("section", { class: "sheet acct" }, el("div", { class: "acct-top" }, el("b", {}, p.title), el("span", { class: "row-gap" }, el("span", { class: "note" }, fmtDate(p.occurred_at)), tag(p.status, p.detail?.state_label))),
      el("div", { class: "compare" }, el("div", {}, el("h4", {}, "计划状态"), el("p", {}, statusOf(p.detail?.plan_state)[0])), el("div", {}, el("h4", {}, "平台回执"), el("p", {}, statusOf(p.detail?.receipt_state)[0]))),
      p.summary ? el("div", { class: "sheet-body acct-foot" }, el("span", { class: "note" }, p.summary)) : null)) : el("p", { class: "note" }, "还没有改动计划。"),
    assets.length ? [el("h2", { class: "section-title" }, "头像素材"), el("div", { class: "nodes" }, assets.map(a => el("section", { class: "node" }, el("h3", {}, a.title, tag(a.status)),
      detailList(a.detail, ["asset_sha256", "review_receipt_sha256"]))))] : null,
    publicationSection(data.publications, data.publicationTopics));
};

function publicationSection(value, topics) {
  const used = new Set((value?.items || []).map(p => p.source_job_id));
  const drafts = (topics?.items || []).flatMap(t => (t.results || []).filter(r => r.qzone_draft && !used.has(r.qzone_draft.draft_id)));
  const labels = { WAITING_APPROVAL: "待你审阅", PENDING: "已批准，等待发送", LEASED: "已领取", SENDING: "发送中",
    SENT: "平台已接受", DELIVERY_UNCERTAIN: "发布结果待核对", CANCELLED: "已取消", UNAVAILABLE: "当前不可发布", UNVERIFIED: "发布记录待核对", FAILED: "平台拒绝" };
  return sheet("QQ 空间公开发布", [
    el("p", { class: "note mb" }, "逐稿审阅全文、来源和有效期，再批准为所有人可见。生成草稿和保存待审稿都不会发布。"),
    value?.error ? failNote("公开发布", value.error) : (value?.items || []).map(p => el("article", { class: "topic" },
      el("div", { class: "topic-head" }, el("b", {}, labels[p.state] || "发布记录"), el("span", { class: "note" }, "有效期至 " + fmtDate(p.expires_at))),
      p.text ? el("p", { class: "vtext" }, p.text) : null,
      p.receipt?.resource_id ? kv("平台说说编号", p.receipt.resource_id) : null,
      p.state === "WAITING_APPROVAL" ? btn("审阅公开稿", () => publicationDialog(p), { cls: "sm primary" }) : null,
      ["PENDING", "LEASED", "SENDING", "DELIVERY_UNCERTAIN"].includes(p.state)
        ? btn("撤销批准", () => publicationDecision(p, "REVOKE"), { cls: "sm danger" }) : null)),
    drafts.length ? el("div", { class: "stack mt" }, el("h3", {}, "可准备的空间草稿"), drafts.map(r => el("div", { class: "row-gap" },
      el("span", {}, String(r.qzone_draft.text).slice(0, 100)), btn("准备公开发布", () => publicationPrepare(r.qzone_draft), { cls: "sm" })))) : null]);
}

function publicationPrepare(draft) {
  const input = el("textarea", { class: "input", rows: 8, value: draft.text, required: true, maxLength: 5000 });
  const form = el("form", { class: "dlg-form publication-review" }, el("p", {}, "保存后还需审阅来源并批准，才会安排公开发布。"),
    el("label", { class: "field" }, el("span", {}, "完整正文"), input));
  const submit = el("button", { type: "submit", class: "btn primary" }, "保存待审稿");
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    perform(form, submit, () => api.write("/qzone/publications", { job_id: draft.draft_id,
      expected_content_sha256: draft.content_sha256, text: input.value }, "POST", r => r.state === "WAITING_APPROVAL"),
      async () => { closeDialog(); toast("已保存待审稿"); await loadRoute({ reset: true }); });
  });
  openDialog("准备公开发布", form, { wide: true });
}

function publicationDialog(p) {
  const input = el("textarea", { class: "input", rows: 8, value: p.text, required: true, maxLength: 5000 });
  const consent = el("input", { type: "checkbox" });
  const form = el("form", { class: "dlg-form publication-review" }, kv("发布账号", p.bot_qq_id), kv("受众", "所有人可见"),
    kv("有效期至", fmtDate(p.expires_at)), el("label", { class: "field" }, el("span", {}, "完整正文"), input),
    el("h3", {}, "本稿来源快照"),
    el("div", { class: "stack" }, (p.sources || []).map(s => el("section", {}, el("b", {}, s.title),
      el("p", { class: "vtext" }, s.text), el("small", { class: "note" }, "来源指纹：" + s.source_sha256),
      safeUrl(s.url) ? el("a", { href: safeUrl(s.url), target: "_blank", rel: "noopener noreferrer" }, "查看来源") : null))),
    el("label", { class: "row-gap" }, consent, el("span", {}, "我确认这份稿件可以公开分享上述来源")));
  const submit = el("button", { type: "submit", class: "btn primary", disabled: true }, "批准公开发布");
  const update = () => { const changed = input.value !== p.text; submit.textContent = changed ? "保存修改，重新审阅" : "批准公开发布"; submit.disabled = !input.value.trim() || (!changed && !consent.checked); };
  input.addEventListener("input", () => { consent.checked = false; update(); });
  consent.addEventListener("change", update);
  form.append(el("div", { class: "dlg-acts" }, btn("拒绝此稿", () => publicationDecision(p, "REJECT"), { cls: "danger" }), submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    const changed = input.value !== p.text;
    if (!changed && !consent.checked) { formError(form, "请先确认本稿的来源分享范围。"); return; }
    const path = "/qzone/publications/" + encodeURIComponent(p.plan_id);
    const body = changed ? { expected_payload_sha256: p.payload_sha256, text: input.value }
      : { expected_payload_sha256: p.payload_sha256, decision: "APPROVE", source_sharing_confirmed: true };
    perform(form, submit, () => api.write(path + (changed ? "" : "/decision"), body, changed ? "PUT" : "POST", r => typeof r.state === "string"),
      async () => { closeDialog(); toast(changed ? "修改已保存，请重新审阅" : "已批准本稿公开发布"); await loadRoute({ reset: true }); });
  });
  openDialog("审阅公开发布稿", form, { wide: true });
}

function publicationDecision(p, decision) {
  const form = el("form", { class: "dlg-form" }, el("p", {}, decision === "REVOKE"
    ? "撤销后停止新发送。已经开始的发布仍需核对平台结果，已发布的说说不会因此删除。" : "拒绝后不会发布这份稿件。"));
  const submit = el("button", { type: "submit", class: "btn danger" }, decision === "REVOKE" ? "撤销批准" : "拒绝此稿");
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    perform(form, submit, () => api.write("/qzone/publications/" + encodeURIComponent(p.plan_id) + "/decision",
      { expected_payload_sha256: p.payload_sha256, decision }, "POST", r => typeof r.state === "string"),
      async () => { closeDialog(); toast(decision === "REVOKE" ? "批准已撤销" : "已拒绝此稿"); await loadRoute({ reset: true }); });
  });
  openDialog(decision === "REVOKE" ? "撤销公开发布批准" : "拒绝公开稿", form);
}

/* ================= 运行诊断 ================= */
const SCENARIOS = [["chat", "对话"], ["reply", "对话"], ["memory", "记忆整理"], ["diary", "日记"], ["reflection", "反思"], ["meme_caption", "表情理解"],
  ["social_autonomy", "QQ 资料行为"], ["content_vision", "图片理解"], ["content_video", "视频分析"], ["video", "视频分析"], ["media", "媒体会话"], ["tool", "工具任务"],
  ["wake", "自主活动"], ["autonomous", "自主活动"], ["exploration", "内容发现"], ["consolidation", "夜间整理"], ["goal", "目标"], ["affect", "交流偏好"], ["expression", "表达学习"]];
const scenarioName = s => (SCENARIOS.find(([k]) => String(s).startsWith(k)) || [, "其他场景"])[1];
const DELIVERY_ACTIONS = { "qq.private.reply": "私聊回复", "qq.private.proactive": "主动分享", "qq.private.poke": "拍一拍" };
PAGES.diagnostics = data => {
  const items = data.items || [], workers = items.filter(i => i.kind === "runtime"), usage = items.filter(i => i.kind === "budget");
  const problems = items.filter(i => !["runtime", "budget"].includes(i.kind));
  return el("div", {}, head("运行诊断", "进程记录、需要处理的问题和各场景的用量。这里不会显示私人内容、cookie 或密钥。"),
    sheet("工作进程", el("div", { class: "procs" }, workers.map(w => el("div", { class: "proc" }, el("b", {}, w.title), tag(w.status),
      el("small", {}, w.occurred_at ? "最近一次 " + fmtDate(w.occurred_at) : "还没有运行记录"))))),
    el("div", { class: "grid-2 mt-lg" },
      sheet("需要处理的问题", problems.length ? el("ul", { class: "rows" }, problems.map(p => el("li", { class: "prob" },
        el("span", {}, tag(p.status)), el("div", { class: "grow" }, el("b", {}, p.kind === "outbox" ? (DELIVERY_ACTIONS[p.detail?.action_type] || "消息投递") : p.kind === "jobs" ? "任务" : "内容读取"),
          el("p", { class: "note" }, p.summary ? reasonText(p.summary) : "没有原因代码"), el("p", { class: "num faint" }, String(p.id) + "，" + fmtDate(p.occurred_at)),
          p.kind === "outbox" && p.status === "DELIVERY_UNCERTAIN" ? reconcileBox(p) : null)))) : el("p", { class: "note" }, "没有需要处理的问题。")),
      sheet("用量明细", usage.length ? el("table", { class: "t" }, el("thead", {}, el("tr", {}, el("th", {}, "场景"), el("th", { class: "r" }, "次数"), el("th", {}, "状态"), el("th", { class: "r" }, "金额"))),
        el("tbody", {}, usage.map(u => el("tr", { class: "static" }, el("td", {}, scenarioName(u.title)), el("td", { class: "r num" }, count(u.detail?.count)), el("td", {}, tag(u.status)), el("td", { class: "r num" }, usd(u.detail?.billed_micro_usd))))))
        : el("p", { class: "note" }, "还没有模型用量记录。"))));
};
function reconcileBox(item) {
  const d = item.detail || {};
  if (!(d.can_reconcile === true && d.reconciliation_prepared === true && /^msg_[a-f0-9]{32}$/.test(String(item.id)) && DELIVERY_ACTIONS[d.action_type])) {
    return el("div", { class: "reconcile-note" }, el("b", {}, "需要先准备核对"), el("p", {}, typeof d.reconciliation_reason === "string" ? d.reconciliation_reason : "请先停止发送进程并准备核对凭证，完成后刷新此页。"));
  }
  const form = el("form", { class: "reconcile" }, el("p", { class: "note" }, "到 QQ 私聊里看看这条消息是否出现，再记录结果。这里不会重新发送。"));
  const choices = [["SENT", "QQ 里能看到，已送达", "primary"], ["CANCELLED", "QQ 里没有，结束核对", "danger"]];
  const buttons = choices.map(([outcome, label, cls]) => btn(label, async e => {
    if (state.deliverySaves.has(item.id)) return;
    state.deliverySaves.set(item.id, outcome); buttons.forEach(b => { b.disabled = true; });
    try {
      await perform(form, e.currentTarget, () => api.write("/deliveries/" + encodeURIComponent(item.id) + "/reconcile", { outcome }, "POST", r => r.state === outcome),
        async () => { toast(outcome === "SENT" ? "已记录为已送达" : "已结束核对，不会补发"); await loadRoute({ reset: true }); });
    } finally { state.deliverySaves.delete(item.id); if (form.isConnected) buttons.forEach(b => { b.disabled = false; }); }
  }, { cls: "sm " + cls }));
  form.addEventListener("submit", e => e.preventDefault());
  form.append(el("div", { class: "row-gap" }, buttons));
  return form;
}

/* ================= 配置字段（能力页与配置页共用一份草稿） ================= */
/* 服务端标签里的计价单位由输入框旁的单位显示，这里去掉，避免选人民币时仍写着美元 */
const cleanLabel = label => label.replace(/（微美元[^）]*）/g, "").replace(/（毫秒）/g, "");
const cfgFields = () => (state.data?.configuration?.fields || []).filter(f => typeof f.key === "string" && typeof f.label === "string").map(f => ({ ...f, label: cleanLabel(f.label) }));
const CAP_KEYS = new Set(Object.values(CAP_FIELDS).flat());
const capOfKey = key => Object.keys(CAP_FIELDS).find(id => CAP_FIELDS[id].includes(key));
/* 未配置的对象型设置开启时使用的默认值，与 reply_segments.SegmentedReplyConfig 的默认值一致 */
const NULL_DEFAULTS = { segmented_reply: { max_segments: 3, min_chars: 12, interval_ms: 1500, mode: "semantic" } };
/* 后端在默认值时会省略的子项，编辑时补出来显示（按句子模式时 mode 不写入） */
const SHOW_DEFAULTS = { segmented_reply: { mode: "sentence" } };
/* 常见表情分类。实际可用的分类取决于 Bridge 里导入的表情包，可以另外添加 */
const MEME_CATEGORY_PRESETS = ["happy", "like", "surprised", "morning", "sleep", "sad", "angry", "shy", "tired", "thinking", "love", "cry", "thanks", "sorry", "hug", "ok"];
function searchBox(id, value, placeholder, onValue) {
  // 输入法组字期间不重绘页面，否则中文无法输入。
  const input = el("input", { class: "input", id, type: "search", placeholder, value,
    oninput: e => { if (e.isComposing) return; onValue(e.target.value); }, oncompositionend: e => onValue(e.target.value) });
  return el("label", { class: "search" }, icon("search"), input);
}
const fieldOf = key => cfgFields().find(f => f.key === key);
const draftVal = f => state.draft.has(f.key) ? state.draft.get(f.key) : f.value;
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
/* 金额字段：模型账本按微美元记账，选人民币时按配置汇率换算显示，保存时换算回微美元 */
function unitFor(key, type) {
  const usdScale = state.currency === "CNY" ? 1e12 / state.fx : 1e6, usdPre = state.currency === "CNY" ? "¥" : "$";
  if (/micro_usd$/.test(key)) return { scale: usdScale, pre: usdPre, money: true };
  if (/micro_cny$/.test(key)) return { scale: 1e6, pre: "¥", money: true };
  if (/_rate$/.test(key) && type === "integer") return { scale: usdScale, pre: usdPre, unit: "/ 百万 Token", money: true };
  if (/_ms$/.test(key)) return { scale: 1, unit: "毫秒" };
  if (/_seconds$/.test(key)) return { scale: 1, unit: "秒" };
  if (/_minutes$/.test(key)) return { scale: 1, unit: "分钟" };
  if (/_days$/.test(key)) return { scale: 1, unit: "天" };
  if (/_bytes$/.test(key)) return { scale: 1e6, unit: "MB" };
  if (/tokens$/.test(key)) return { scale: 1, unit: "Token" };
  if (/chars$/.test(key)) return { scale: 1, unit: "字" };
  return { scale: 1 };
}
const unitOf = f => unitFor(f.key, f.type);
const isHour = f => f.type === "integer" && /(_hour|^quiet_start|^quiet_end)$/.test(f.key);
const enumOf = f => Array.isArray(f.schema?.enum) ? f.schema.enum : null;
const minOf = f => f.minimum ?? f.schema?.minimum ?? f.schema?.exclusiveMinimum;
const maxOf = f => f.maximum ?? f.schema?.maximum ?? f.schema?.exclusiveMaximum;
function fmtNum(key, type, v) { const u = unitFor(key, type); return (u.pre || "") + (+(v / u.scale).toFixed(u.money ? 4 : 2)).toLocaleString("zh-CN") + (u.unit ? " " + u.unit : ""); }
function fmtField(f, v) {
  if (v === null || v === undefined) return "未配置";
  if (f.type === "secret") return "新密钥";
  if (f.key === "web_reading_hosts" && same(v, ["*"])) return "全部公网域名";
  if (f.type === "boolean") return v ? "开" : "关";
  if (isHour(f)) return String(v).padStart(2, "0") + ":00";
  if (["integer", "number"].includes(f.type)) return fmtNum(f.key, f.type, v);
  if (typeof v === "string") return VALUE_NAMES[v] || v;
  if (Array.isArray(v)) return v.join("、") || "（空）";
  if (typeof v === "object") return "已修改其中的设置";
  return String(v);
}
function setDraft(f, v) {
  if (f.type === "secret" ? v === "" : same(v, f.value)) state.draft.delete(f.key); else state.draft.set(f.key, v);
  rerender();
}
function setIn(obj, path, value) {
  const copy = JSON.parse(JSON.stringify(obj ?? {})); let cur = copy;
  for (const k of path.slice(0, -1)) { if (cur[k] === null || typeof cur[k] !== "object") cur[k] = {}; cur = cur[k]; }
  cur[path.at(-1)] = value; return copy;
}
function fieldError(node, text) { const p = $(".pf-err", node.closest(".pf, .sub-row")); if (p) { p.textContent = text; p.hidden = !text; } }
/** @param {{ id: string, key: string, type: string, value: any, nullable?: boolean, lo?: number, hi?: number, onValue: (value: any) => void }} options */
function numberInput({ id, key, type, value, nullable, lo, hi, onValue }) {
  const u = unitFor(key, type);
  const input = el("input", { class: "input", id, "data-k": id, inputmode: "decimal", value: value === null || value === undefined ? "" : String(+(value / u.scale).toFixed(u.money ? 6 : 4)),
    placeholder: nullable ? "未配置" : undefined, onchange: e => {
      const raw = e.target.value.trim();
      if (raw === "" && nullable) { onValue(null); return; }
      const n = Number(raw) * u.scale, v = type === "integer" ? Math.round(n) : n;
      if (raw === "" || !Number.isFinite(n) || n < 0 && lo === undefined || lo !== undefined && v < lo || hi !== undefined && v > hi) {
        fieldError(e.target, "请填写" + (lo !== undefined ? "不小于 " + fmtNum(key, type, lo) : "") + (hi !== undefined ? (lo !== undefined ? "、" : "") + "不大于 " + fmtNum(key, type, hi) : "") + "的数值。");
        return;
      }
      onValue(v);
    } });
  return el("span", { class: "unit" + (u.pre ? " has-pre" : "") }, u.pre ? el("i", {}, u.pre) : null, input, u.unit ? el("em", {}, u.unit) : null);
}
function control(f, only) {
  const v = draftVal(f), id = "cf-" + f.key.replace(/[^a-z0-9_]/gi, "-");
  if (f.key === "conversation_guidance") return el("textarea", { class: "input", id, rows: "7", "data-k": id,
    "aria-label": f.label, maxlength: f.schema?.anyOf?.find(x => x.type === "string")?.maxLength,
    placeholder: "留空使用所选内置策略的交流指导", value: v ?? "",
    oninput: e => setDraft(f, e.target.value.trim() ? e.target.value : null) });
  if (f.key === "video_models") return videoModelsControl(f, v);
  if (f.key === "expression_meme_categories") return categoryPicker(f, v || []);
  if (f.key === "web_reading_hosts") return hostPicker(f, v || []);
  if (f.type === "boolean") return el("button", { type: "button", class: "switch", role: "switch", id, "aria-checked": String(Boolean(v)), "data-k": id, "aria-label": f.label, onclick: () => setDraft(f, !draftVal(f)) });
  if (enumOf(f)) return el("select", { class: "input", id, "data-k": id, onchange: e => setDraft(f, JSON.parse(e.target.value)) },
    enumOf(f).map(o => el("option", { value: JSON.stringify(o), selected: same(o, v) }, VALUE_NAMES[o] || String(o))));
  if (isHour(f)) return el("select", { class: "input hour", id, "data-k": id, onchange: e => setDraft(f, Number(e.target.value)) },
    [...Array(24)].map((_, h) => el("option", { value: String(h), selected: h === v }, String(h).padStart(2, "0") + ":00")));
  if (["integer", "number"].includes(f.type)) return numberInput({ id, key: f.key, type: f.type, value: v, nullable: f.nullable, lo: minOf(f), hi: maxOf(f), onValue: x => setDraft(f, x) });
  if (f.type === "secret") return el("input", { class: "input", type: "password", id, "data-k": id, autocomplete: "new-password",
    placeholder: f.configured ? "已设置，留空保持不变" : "未设置", onchange: e => setDraft(f, e.target.value) });
  if (f.type === "array" && (Array.isArray(v) ? v.every(x => typeof x === "string") : true)) return listControl(id, v || [], list => setDraft(f, list));
  if (f.type === "object" && NULL_DEFAULTS[f.key]) {
    const on = v !== null && v !== undefined;
    const sw = el("div", { class: "kv toggle-row" }, el("span", {}, on ? "已开启" : "没有开启"),
      el("button", { type: "button", class: "switch", role: "switch", id, "data-k": id, "aria-checked": String(on), "aria-label": f.label,
        onclick: () => setDraft(f, on ? null : { ...NULL_DEFAULTS[f.key] }) }));
    return on ? el("div", { class: "obj" }, sw, objectEditor(f, { ...SHOW_DEFAULTS[f.key], ...v }, [], true)) : sw;
  }
  if (f.type === "object" && v === null) return el("span", { class: "note" }, "未配置，在部署配置里设置");
  if (["object", "array"].includes(f.type)) return state.jsonEdit?.has(f.key) || Array.isArray(v) ? jsonControl(f, id, v) : objectEditor(f, v, [], false, only);
  return el("input", { class: "input", id, "data-k": id, value: v ?? "", onchange: e => setDraft(f, e.target.value) });
}
function jsonControl(f, id, v) {
  return el("textarea", { class: "input code", id, rows: "6", spellcheck: "false", "data-k": id, value: JSON.stringify(v, null, 2), onchange: e => {
    let parsed; try { parsed = JSON.parse(e.target.value); } catch { fieldError(e.target, "格式不对，请检查括号和引号。"); return; }
    if (parsed === null && f.nullable) { setDraft(f, null); return; }
    if (!parsed || typeof parsed !== "object" || (f.type === "array") !== Array.isArray(parsed)) { fieldError(e.target, "填写的类型不对。"); return; }
    setDraft(f, parsed);
  } });
}
/* 对象配置按子项显示：开关、数字、文字、名单，嵌套的设置组缩进显示 */
function objectEditor(f, value, path, bare = false, only = null) {
  const parent = path.length ? path.at(-1) : f.key;
  const rows = Object.entries(value || {}).filter(([k]) => !only || path.length || only.includes(k)).map(([k, v]) => {
    const p = [...path, k], id = "cf-" + [f.key, ...p].join("-").replace(/[^a-z0-9_-]/gi, "-"), label = subLabel(k);
    const put = x => setDraft(f, setIn(draftVal(f), p, x));
    let ctl;
    if (isSensitiveSub(k)) ctl = el("span", { class: "note" }, v ? "已设置，在部署配置里更换" : "未配置，在部署配置里设置");
    else if (k === "nodes" && v && typeof v === "object" && !Array.isArray(v)) ctl = el("span", { class: "note" }, Object.keys(v).length + " 个节点，在部署配置里设置");
    else if (typeof v === "boolean") ctl = el("button", { type: "button", class: "switch", role: "switch", id, "data-k": id, "aria-checked": String(v), "aria-label": label, onclick: () => put(!v) });
    else if (typeof v === "number") ctl = numberInput({ id, key: k, type: Number.isInteger(v) ? "integer" : "number", value: v, onValue: put });
    else if (typeof v === "string" && SUB_ENUMS[k]?.[parent]) ctl = el("select", { class: "input", id, "data-k": id, onchange: e => put(e.target.value) },
      SUB_ENUMS[k][parent].map(o => el("option", { value: o, selected: o === v }, VALUE_NAMES[o] || o)));
    else if (typeof v === "string") ctl = el("input", { class: "input", id, "data-k": id, value: v, onchange: e => put(e.target.value) });
    else if (Array.isArray(v) && v.every(x => typeof x === "string")) ctl = listControl(id, v, put);
    else if (v && typeof v === "object") return el("div", { class: "sub-group" }, el("p", { class: "sub-title" }, label), objectEditor(f, v, p));
    else ctl = el("span", { class: "note" }, v === null ? "未配置" : "在部署配置里设置");
    return el("div", { class: "sub-row" }, el("span", { class: "sub-l" }, label), el("span", { class: "sub-c" }, ctl), el("p", { class: "pf-err", role: "alert", hidden: true }));
  });
  if (path.length || bare) return el("div", { class: "sub-list" }, rows);
  return el("div", { class: "obj" }, el("div", { class: "sub-list" }, rows),
    btn("以 JSON 编辑", () => { state.jsonEdit = state.jsonEdit || new Set(); state.jsonEdit.add(f.key); rerender(); }, { cls: "sm quiet" }));
}
/* 与 config.exact_web_hosts 相同的规则：小写精确域名，不支持通配符、IP 和内网后缀 */
const HOST_RE = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
function normHost(raw) {
  let x = raw.trim().toLowerCase();
  if (!x) return [null, "请输入域名。"];
  if (x.includes("://")) { try { x = new URL(x).hostname; } catch { return [null, "这不是有效的网址。"]; } }
  x = x.replace(/\/.*$/, "").replace(/\.$/, "");
  if (x.includes("*")) return [null, "不支持通配符，请逐个填写域名。"];
  if (/^[\d.]+$/.test(x) || x.includes(":")) return [null, "不支持 IP 地址，请填写域名。"];
  if (/\.(localhost|local|internal)$/.test(x)) return [null, "不支持内网地址。"];
  if (x.length > 253 || !HOST_RE.test(x)) return [null, "域名格式不对，例如 sspai.com。"];
  return [x, null];
}
function hostPicker(f, list) {
  const all = same(list, ["*"]);
  const scope = el("label", {}, "读取范围", el("select", { class: "input", id: "cf-web-scope",
    onchange: e => setDraft(f, e.target.value === "all" ? ["*"] : []) },
    el("option", { value: "exact", selected: !all }, "指定网站"),
    el("option", { value: "all", selected: all }, "全部公网域名")));
  if (all) return el("div", { class: "listed" }, scope,
    el("p", { class: "note" }, "允许读取公开网站；内网地址仍会被拦截，读取次数和流量仍受现有额度限制。"));
  const input = el("input", { class: "input", id: "cf-web-hosts", placeholder: "输入域名或粘贴网址，例如 sspai.com", maxlength: "300" });
  const err = el("p", { class: "list-err", role: "alert", hidden: true });
  const fail = m => { err.textContent = m; err.hidden = false; input.focus(); };
  const add = () => { const [x, m] = normHost(input.value); if (m) return fail(m); if (list.includes(x)) return fail(x + " 已经在名单里。");
    if (list.length >= 32) return fail("最多 32 个网站。"); setDraft(f, [...list, x]); };
  input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); add(); } });
  return el("div", { class: "listed" }, scope, el("ul", {}, list.length ? list.map((x, i) => el("li", {}, el("span", { class: "num" }, x),
      el("button", { type: "button", class: "icon-btn", "aria-label": "移除 " + x, onclick: () => setDraft(f, list.filter((_, j) => j !== i)) }, icon("close"))))
      : el("li", { class: "none" }, "名单是空的，网页阅读会保持不可用。")),
    el("div", { class: "list-add" }, input, btn("添加", add, { cls: "sm" }), el("small", { class: "num" }, list.length + " / 32")), err,
    el("p", { class: "note" }, "只读这些网站，跳转到名单外的地址会被拦下。必须是精确域名：sspai.com 不包含 www.sspai.com，子域名要单独添加。"));
}
function categoryPicker(f, list) {
  const all = [...new Set([...MEME_CATEGORY_PRESETS, ...list])];
  const input = el("input", { class: "input", id: "cf-meme-cat", placeholder: "其他分类，例如 hungry", maxlength: "32" });
  const err = el("p", { class: "list-err", role: "alert", hidden: true });
  const add = () => { const x = input.value.trim().toLowerCase(); if (!x) return;
    if (!/^[a-z][a-z0-9_-]{0,31}$/.test(x)) { err.textContent = "分类名只能用小写英文字母、数字、短横线和下划线，并以字母开头。"; err.hidden = false; return; }
    if (!list.includes(x)) setDraft(f, [...list, x]); };
  input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); add(); } });
  return el("div", { class: "cats" }, el("div", { class: "cat-grid" }, all.map(c => el("button", { type: "button", class: "cat" + (list.includes(c) ? " on" : ""), "aria-pressed": String(list.includes(c)),
      onclick: () => setDraft(f, list.includes(c) ? list.filter(x => x !== c) : [...list, c]) }, list.includes(c) ? icon("check") : null, VALUE_NAMES[c] || c))),
    el("div", { class: "list-add" }, input, btn("添加", add, { cls: "sm" })), err,
    el("p", { class: "note" }, `已选 ${list.length} 个，最多 12 个生效。实际能用的分类取决于 Bridge 里导入的表情包。`));
}
function listControl(id, list, onList) {
  const input = el("input", { class: "input", id, "aria-label": "添加一项", placeholder: "输入后按回车添加", maxlength: "64" });
  const err = el("p", { class: "list-err", role: "alert", hidden: true });
  const add = () => { const x = input.value.trim(); if (!x) return; if (list.includes(x)) { err.textContent = x + " 已经在里面了。"; err.hidden = false; return; } onList([...list, x]); };
  input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); add(); } });
  return el("div", { class: "listed" }, el("ul", {}, list.length ? list.map((x, i) => el("li", {}, el("span", {}, VALUE_NAMES[x] || x),
    el("button", { type: "button", class: "icon-btn", "aria-label": "移除 " + x, onclick: () => onList(list.filter((_, j) => j !== i)) }, icon("close")))) : el("li", { class: "none" }, "还是空的")),
    el("div", { class: "list-add" }, input, btn("添加", add, { cls: "sm" })), err);
}
function fieldRow(f, label, only) {
  const changed = state.draft.has(f.key), wide = ["object", "array"].includes(f.type) || f.key === "conversation_guidance";
  const active = f.active_value !== undefined && !same(f.value, f.active_value) && f.type !== "secret";
  return el("div", { class: "pf" + (wide ? " wide" : "") + (changed ? " changed" : ""), id: "pf-" + f.key },
    el("div", { class: "pf-l" }, el("b", {}, label || f.label), f.description ? el("span", {}, f.description) : null,
      el("span", {}, f.key === "turn_taking" && f.hot_members?.includes("silence_enabled") ? "收尾消息可以不回：下一轮聊天生效。其余设置需要重启" : f.effect === "batch_boundary" ? "等待时间从下一批消息生效。合并开关需要重启" : f.effect === "task_boundary" ? "下一任务边界生效" : f.effect === "restart_and_reapproval" ? "需要重启并核对审批" : "需要重启"),
      active ? el("span", { class: "pf-active" }, (f.active_source === "inbox" ? "最近一批消息的设置：" : f.active_source === "worker" ? "聊天 Worker 新任务值：" : "API 启动值：") + fmtField(f, f.active_value)) : null),
    el("div", { class: "pf-c" }, control(f, only), f.nullable && f.type !== "secret" && !NULL_DEFAULTS[f.key] && draftVal(f) !== null ? btn("设为未配置", () => setDraft(f, null), { cls: "sm quiet" }) : null),
    el("p", { class: "pf-err", role: "alert", hidden: true }));
}
function currencySwitch() {
  return el("div", { class: "seg", role: "group", "aria-label": "金额显示" }, [["CNY", "人民币"], ["USD", "美元"]].map(([k, n]) =>
    el("button", { type: "button", "aria-pressed": String(state.currency === k), onclick: () => { state.currency = k; rerender(); } }, n)));
}
function saveBar() {
  if (!state.draft.size || !DRAFT_ROUTES.includes(state.route) || !state.data?.configuration) return null;
  const c = state.data.configuration, next = c.version + 1;
  return el("div", { class: "savebar" }, el("p", {}, el("b", { class: "num" }, String(state.draft.size)), ` 项修改没保存。保存后成为 v${next}，生效方式见各字段说明。`),
    btn("放弃修改", () => { state.draft.clear(); rerender(); }, { cls: "quiet" }), btn("查看修改", reviewDrawer, { cls: "quiet" }),
    btn("保存为 v" + next, saveDraft, { cls: "primary" }));
}
function diffLines(f, v) {
  if (f.key === "video_models") return [[f.label, f.value.selected || "部署默认",
    (v.selected || "部署默认") + " · " + Object.keys(v.candidates).length + " 个候选配置"]];
  if (f.type === "object" && f.value && v && typeof v === "object" && !Array.isArray(v)) {
    const out = [], walk = (a, b, path) => { for (const k of new Set([...Object.keys(a || {}), ...Object.keys(b || {})])) {
      const x = a?.[k], y = b?.[k];
      if (x && y && typeof x === "object" && typeof y === "object" && !Array.isArray(x)) walk(x, y, [...path, k]);
      else if (!same(x, y)) out.push([[f.label, ...path.map(subLabel), subLabel(k)].join(" › "), typeof x === "number" ? fmtNum(k, "number", x) : limitValue(k, x), typeof y === "number" ? fmtNum(k, "number", y) : limitValue(k, y)]); } };
    walk(f.value, v, []); return out;
  }
  return [[f.label, f.type === "secret" ? "原密钥" : fmtField(f, f.value), fmtField(f, v)]];
}
function diffList(undo) {
  return el("ul", { class: "diff" }, [...state.draft].flatMap(([k, v]) => { const f = fieldOf(k); if (!f) return [];
    return diffLines(f, v).map(([name, a, b], i) => el("li", {}, el("b", {}, name), el("span", {}, el("s", {}, a), " 改为 ", el("ins", {}, b)),
      undo && i === 0 ? btn("撤销", () => { state.draft.delete(k); rerender(); state.draft.size ? reviewDrawer() : closeDrawer(); }, { cls: "sm quiet" }) : null)); }));
}
function reviewDrawer() { drawer("待保存的修改", state.draft.size + " 项，可以逐项撤销", diffList(true)); }
function saveDraft() {
  const c = state.data.configuration, changes = Object.create(null);
  for (const [k, v] of state.draft) { const f = fieldOf(k); if (f && !(f.type === "secret" && v === "")) changes[k] = v; }
  if (!Object.keys(changes).length) { toast("没有需要保存的修改"); return; }
  const hot = c.hot_reload_enabled && (c.restart_fields || []).every(k => Object.hasOwn(changes, k)) &&
    Object.entries(changes).every(([k, value]) => {
      const f = fieldOf(k);
      return ["task_boundary", "batch_boundary"].includes(f?.effect) && (!f.hot_members ||
        Object.keys(value || {}).every(member => f.hot_members.includes(member) || same(value[member], f.active_value?.[member])));
    });
  const form = el("form", { class: "dlg-form" }, el("p", { class: "muted" }, hot
    ? "保存后，新任务或新一批消息会校验并使用新设置。已开始的任务和消息批次使用原设置。"
    : "此版本包含需要重启的设置，整版等待重启；涉及费用或能力策略时还需核对原审批。"), diffList(false));
  const submit = el("button", { type: "submit", class: "btn primary" }, "保存 " + Object.keys(changes).length + " 项修改");
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    if (state.configSave) return;
    state.configSave = true;
    perform(form, submit, () => api.write("/configuration", { expected_version: c.version, changes }, "PUT",
      r => Number.isInteger(r.version) && Array.isArray(r.fields) && Number.isInteger(r.active_version)), async r => {
      const budget = state.data.configuration.cloud_budget;
      state.data.configuration = { ...r, cloud_budget: r.cloud_budget || budget };
      for (const k of Object.keys(changes)) state.draft.delete(k);
      closeDialog(); closeDrawer(); rerender(); toast(`已保存为 v${r.version}，${configurationStatus(r)}`);
    }).finally(() => { state.configSave = null; });
  });
  openDialog("保存为 v" + (c.version + 1), form);
}

/* ================= 能力与开关 ================= */
const capList = () => state.data?.capabilities?.capabilities || [];
const capFieldsOf = id => (CAP_FIELDS[id] || []).map(fieldOf).filter(Boolean);
/* 列表里的提示：火花表示交给了她决定，黄点表示有她的提议等你批准，空心圈表示有没保存的修改 */
function capMarks(id) {
  const a = state.data?.autonomy, d = (a?.capabilities || []).find(x => x.capability === id);
  const proposals = (a?.adjustments || []).filter(x => x.capability === id && x.state === "PROPOSED").length;
  return [d && d.mode !== "owner" ? el("span", { class: "auto-ic", title: d.mode === "self" ? "她在范围内自己调整" : "她提议，你批准" }, icon("spark")) : null,
    proposals ? el("span", { class: "pdot", title: proposals + " 条提议等你批准" }) : null,
    capFieldsOf(id).some(f => state.draft.has(f.key)) || state.autoDraft?.[id] ? el("span", { class: "ddot", title: "有没保存的修改" }) : null];
}
/* 多项能力共用一组设置时，每项只显示和自己相关的子项 */
const PROFILE_LIMITS = ["profile_daily_limit", "profile_cooldown_seconds"];
const CAP_SUB = {
  "qq.profile.observe": { social_autonomy: ["enabled", "scope", "profile_baseline_seconds", "decision_interval_seconds", "model_daily_calls", "model_daily_micro_usd", "max_pending_plans", "plan_ttl_seconds"] },
  "qq.account.signature": { social_autonomy: ["signature_enabled", "max_signature_chars", ...PROFILE_LIMITS] },
  "qq.account.nickname": { social_autonomy: ["nickname_enabled", "max_nickname_chars", ...PROFILE_LIMITS] },
  "qq.account.avatar": { social_autonomy: ["avatar_enabled", "max_asset_bytes", "max_asset_dimension", ...PROFILE_LIMITS] },
  "qq.account.qzone_publish": { social_autonomy: ["qzone_enabled", "qzone_daily_limit", "qzone_cooldown_seconds", "max_publish_chars", "max_source_refs", "max_source_chars"] },
  "qq.poke": { poke_policy: ["enabled", "mode", "coalesce_seconds", "cooldown_seconds", "daily_limit", "max_coalesced", "max_text_chars", "max_pending_seconds"] },
  "qq.poke.native": { poke_policy: ["allow_native", "cooldown_seconds", "daily_limit"] },
  "qq.voice.asr": { voice: ["asr", "job_ttl_seconds", "max_pending_jobs", "max_private_blob_bytes"] },
  "qq.voice.tts": { voice: ["reply", "result_ttl_seconds", "max_pending_jobs"] },
  "bilibili.video": { bili_video: ["enabled", "deadline_seconds", "max_concurrent", "max_context_chars", "sampling"] },
  "model.understand_video": { bili_video: ["media"] },
  "qq.images": { content: ["use_chat_model_for_vision", "max_images_per_event", "max_image_bytes", "max_daily_images", "max_daily_image_bytes", "image_daily_micro_usd", "vision"] },
  "qq.animation": { content: ["use_chat_model_for_vision", "max_image_bytes", "max_daily_images", "max_daily_image_bytes"] },
};
const CAP_SUB_LABEL = { social_autonomy: "相关设置", poke_policy: "相关设置", voice: "相关设置", bili_video: "相关设置", content: "相关设置" };
/* 每项能力的总开关：配置键，或对象配置里的子项路径。共用一个开关的能力会同时变化 */
const CAP_SWITCH = {
  "memory.manage": "memory_enabled", "memory.recall": "memory_enabled", diary: "diary_enabled", consolidation: "consolidation_enabled",
  reflection: "reflection_enabled", goals: "goals_enabled", affect: "affect_enabled", proactive: "proactive_enabled", wake: "wake_enabled",
  exploration: "exploration_enabled", social: "social_enabled", "qq.poke": "poke_policy.enabled", "qq.poke.native": "poke_policy.allow_native",
  "reading.web": "web_reading_enabled", "reading.bilibili": "observation_enabled", "reading.heybox": "observation_enabled",
  "bilibili.video": "bili_video.enabled", "model.understand_video": "bili_video.enabled", "qq.images": "content_enabled", "qq.animation": "content_enabled",
  "qq.files": "private_files.enabled", expression: "expression_enabled", expression_learning: "expression_learning_enabled",
  "unicode.emoji": "expression_enabled", "qq.face_names": "expression_enabled", "qq.face_images": "expression_enabled",
  "memes.catalog": "meme_discovery_enabled", "memes.search": "meme_discovery_enabled", "memes.community_import": "meme_discovery_enabled",
  "memes.semantic": "meme_semantics_enabled", "qq.voice.asr": "voice.asr.enabled", "qq.voice.tts": "voice.reply.enabled",
  "media.voice": "voice_enabled", "media.camera": "camera_enabled", "media.android": "android_enabled",
  "qq.profile.observe": "social_autonomy.enabled", "qq.account.signature": "social_autonomy.signature_enabled",
  "qq.account.nickname": "social_autonomy.nickname_enabled", "qq.account.avatar": "social_autonomy.avatar_enabled",
  "qq.account.qzone_publish": "social_autonomy.qzone_enabled",
};
function capMain(id) {
  const spec = CAP_SWITCH[id]; if (!spec) return null;
  const [key, ...path] = spec.split("."), f = fieldOf(key); if (!f) return null;
  const read = () => { let v = draftVal(f); for (const k of path) v = v?.[k]; return Boolean(v); };
  if (path.length && (draftVal(f) === null || typeof draftVal(f) !== "object")) return null;
  return { f, read, toggle: () => setDraft(f, path.length ? setIn(draftVal(f), path, !read()) : !read()),
    shared: Object.entries(CAP_SWITCH).filter(([other, k]) => k === spec && other !== id).map(([other]) => CAP_NAMES[other] || other) };
}
function capSwitch(id, name, small) {
  const m = capMain(id);
  if (!m) return el("span", { class: "sw-lock", title: "开关在部署配置里" }, icon("lock"));
  return el("button", { type: "button", class: "switch" + (small ? " sm" : ""), role: "switch", "aria-checked": String(m.read()), "data-k": "sw-" + id,
    "aria-label": name + "总开关", title: m.shared.length ? "和" + m.shared.join("、") + "共用这个开关" : null, onclick: m.toggle });
}
/* 这些能力的开关或服务地址由部署配置保管，浏览器不能修改 */
const CAP_DEPLOY = {
  "media.voice": "网页语音会话的服务商地址、费用上限和会话时长在服务器部署配置的 [media] 段设置。",
  executive: "执行任务的端点和凭据在服务器部署配置的 [executive] 段设置，控制台不能修改。",
  "reading.bilibili": "B 站读取适配器和账号授权在部署环境里配置。", "reading.heybox": "小黑盒读取适配器在部署环境里配置。",
  "model.understand_video": "视频模型在「模型与密钥」中配置，新任务可切换到已授权的模型。",
  "qq.images": "付费图片理解的审批窗口（paid_model.image_input）在部署配置里设置。",
};
PAGES.caps = data => {
  const caps = capList(), known = new Set(CAP_GROUPS.flatMap(g => g[1]));
  const groups = [...CAP_GROUPS, ["其他", caps.map(c => c.id).filter(id => !known.has(id))]];
  const q = state.capQ.trim();
  const pick = c => (state.capF === "all" || (state.capF === "on" ? !["DISABLED", "UNAVAILABLE"].includes(c.state) : state.capF === "off" ? ["DISABLED", "UNAVAILABLE"].includes(c.state) : ["PAUSED", "ATTENTION", "BLOCKED"].includes(c.state)))
    && (!q || (CAP_NAMES[c.id] || "").includes(q) || (c.summary || "").includes(q) || capFieldsOf(c.id).some(f => f.label.includes(q)));
  const sel = caps.find(c => c.id === state.sub) || caps.find(pick) || caps[0];
  const filterCount = f => { const keep = state.capF; state.capF = f; const n = caps.filter(pick).length; state.capF = keep; return n; };
  const index = groups.map(([name, ids]) => {
    const list = ids.map(id => caps.find(c => c.id === id)).filter(Boolean), shown = list.filter(pick);
    if (!shown.length) return null;
    const open = q || !state.capCollapsed.has(name);
    return el("div", { class: "ci-group" }, el("button", { type: "button", class: "ci-ghead", "aria-expanded": String(Boolean(open)), onclick: () => { state.capCollapsed.has(name) ? state.capCollapsed.delete(name) : state.capCollapsed.add(name); rerender(); } },
      icon("right", "i chev"), el("span", {}, name), el("small", {}, list.filter(c => !["DISABLED", "UNAVAILABLE"].includes(c.state)).length + "/" + list.length)),
      open ? shown.map(c => {
        const main = capMain(c.id), dot = main && !main.read() ? "off" : statusOf(c.state)[1];
        return el("div", { class: "ci-row" + (c === sel ? " sel" : "") + (["DISABLED", "UNAVAILABLE"].includes(c.state) ? " off" : "") },
          el("a", { class: "ci-name", href: "#caps/" + encodeURIComponent(c.id), "aria-current": c === sel ? "true" : null, onclick: () => { state.capMobile = true; state.capScrolled = c.id; state.capTop = innerWidth <= 860; } },
            el("span", { class: "dot " + dot, title: statusOf(c.state)[0] }), el("span", { class: "nm" }, CAP_NAMES[c.id] || "未命名能力"),
            ...capMarks(c.id)),
          capSwitch(c.id, CAP_NAMES[c.id] || "", true));
      }) : null);
  });
  return el("div", {}, head("能力与开关", "左边找能力，右边看它的状态和参数。总开关和参数保存为新版本后重启生效；暂停立即生效。", currencySwitch()),
    el("div", { class: "caps2" + (state.capMobile ? " show-detail" : "") },
      el("aside", { class: "cap-index", "aria-label": "能力列表" },
        searchBox("cap-q", state.capQ, "搜索能力或参数", v => { state.capQ = v; rerender(); }),
        el("div", { class: "ci-filter", role: "group", "aria-label": "筛选" }, [["all", "全部"], ["on", "已开启"], ["off", "未开启"], ["warn", "需留意"]].map(([k, n]) =>
          el("button", { type: "button", "aria-pressed": String(state.capF === k), onclick: () => { state.capF = k; rerender(); } }, n + " ", el("span", { class: "num" }, String(filterCount(k)))))),
        el("div", { class: "ci-list", "data-keep-scroll": "caps" }, index.some(Boolean) ? index : el("div", { class: "empty" }, el("h3", {}, "没有符合条件的能力"), el("p", {}, "换个关键词，或选择“全部”。")))),
      el("section", { class: "cap-detail", "data-keep-scroll": "cap-" + (sel ? sel.id : "") }, sel ? capDetail(sel) : el("p", { class: "note" }, "还没有能力状态。"))));
};
/* 只在第一次打开（例如从别处链接进来）时把选中的能力滚进列表可见范围，不移动整页 */
PAGES.caps.after = () => {
  if (state.capTop) { state.capTop = false; window.scrollTo(0, 0); }
  if (state.capScrolled === state.sub) return; state.capScrolled = state.sub;
  const s = $(".ci-row.sel"), list = $(".ci-list"); if (!s || !list) return;
  const top = s.offsetTop - list.offsetTop;
  if (top < list.scrollTop || top > list.scrollTop + list.clientHeight - s.offsetHeight) list.scrollTop = top - list.clientHeight / 3;
};
function capDetail(c) {
  const name = CAP_NAMES[c.id] || "未命名能力", fields = capFieldsOf(c.id), main = capMain(c.id), rt = CAP_RUNTIME[c.id];
  const paused = (c.reason_codes || []).some(r => /PAUSED/.test(r));
  const limits = Object.entries(c.limits || {}).filter(([, v]) => v !== null && v !== undefined && v !== "");
  const reasons = [...new Set((c.reason_codes || []).map(reasonText))];
  return el("div", {}, btn("全部能力", () => { state.capMobile = false; rerender(); }, { cls: "sm quiet cd-back", symbol: "left" }),
    el("header", { class: "cd-head" }, el("div", {}, el("h2", {}, name), el("p", {}, c.summary || "")),
      el("div", { class: "cd-switch" }, tag(c.state), el("label", {}, el("span", {}, "总开关"), capSwitch(c.id, name)),
        main?.shared.length ? el("span", { class: "note" }, "和" + main.shared.join("、") + "共用") : !main ? el("span", { class: "note" }, "在部署配置里开关") : null)),
    reasons.length ? el("ul", { class: "needs" }, reasons.map(r => el("li", { class: "no" }, icon("close"), el("span", {}, r)))) : null,
    CAP_DEPLOY[c.id] ? el("p", { class: "lockline" }, icon("lock"), el("span", {}, CAP_DEPLOY[c.id])) : null,
    el("div", { class: "cd-stats" },
      el("div", {}, el("span", {}, "额度与限制"), limits.length ? limits.map(([k, v]) => el("p", { class: "lim-line" }, el("small", {}, limitName(k)), el("b", { class: "num" }, limitValue(k, v))))
        : el("small", {}, "这项能力没有单独的额度")),
      el("div", {}, el("span", {}, "运行状态"), el("b", {}, statusOf(c.state)[0]), rt ? btn(paused ? "恢复" : "暂停", () => runtimeToggle(rt, paused), { cls: "sm", symbol: paused ? "refresh" : "pause" }) : null,
        el("small", {}, rt ? "立即生效，不需要重启" : "没有单独的暂停")),
      el("div", {}, el("span", {}, "由谁决定"), el("b", {}, (() => { const d = (state.data?.autonomy?.capabilities || []).find(x => x.capability === c.id);
          return d ? (AUTO_MODES.find(m => m[0] === d.mode) || AUTO_MODES[0])[1] : "只由我控制"; })()),
        el("small", {}, "调用方式：" + ((c.invocation || []).map(x => ({ automatic: "她自动使用", owner_command: "你的指令", tool: "工具调用", owner: "你发起" })[x] || "其他方式").join("、") || "未记录")))),
    ["proactive", "diary", "consolidation"].includes(c.id) ? el("section", { class: "cd-sec" }, el("h3", {}, "作息时间"), scheduleStrip()) : null,
    autonomySection(c.id),
    el("section", { class: "cd-sec" }, el("h3", {}, "参数", el("span", {}, fields.length ? "保存为新版本后生效" : "")),
      fields.length ? el("div", { class: "pf-list" }, fields.map(f => CAP_SUB[c.id]?.[f.key] ? fieldRow(f, CAP_SUB_LABEL[f.key], CAP_SUB[c.id][f.key]) : fieldRow(f))) : el("p", { class: "note" }, CAP_DEPLOY[c.id] ? "这项能力的设置都在部署配置里。" : "这项能力没有可以在控制台修改的参数。")));
}
/* ================= 交给她决定 ================= */
const AUTO_MODES = [["owner", "只由我控制", "她不能改这项能力的任何设置。"], ["propose", "她提议，我批准", "她提出调整和理由，你批准后才生效。"],
  ["self", "她在范围内自己调整", "在你划定的范围内她可以直接调整，事后可以撤销。"]];
const AUTO_FIELDS = { pause: "暂停到第二天 08:00", wake_max_daily_runs: "每天最多运行", wake_min_interval_minutes: "两次之间至少间隔" };
const ADJ_STATE = { PROPOSED: ["待你批准", "warn"], ACTIVE: ["生效中", "ok"], REJECTED: ["已拒绝", "off"], REVERTED: ["已撤销", "off"], EXPIRED: ["已结束", "off"],
  SUPERSEDED: ["已被新的调整替代", "off"], DISCARDED: ["恢复备份后作废", "off"] };
const AUTO_LOCKED = "这项能力会改动对外可见的内容、私人文件或费用账本，只能由你控制。";
function autoLabel(field) { return field === "pause" ? "暂停到第二天 08:00" : (fieldOf(field)?.label || AUTO_FIELDS[field] || subLabel(field)); }
function autoValue(field, v) { if (field === "pause") return "暂停"; const u = unitFor(field, Number.isInteger(v) ? "integer" : "number"); return (u.pre || "") + (+(v / u.scale).toFixed(u.money ? 4 : 2)) + (u.unit ? " " + u.unit : ""); }
function autonomySection(id) {
  const a = state.data?.autonomy;
  if (!a) return el("section", { class: "cd-sec" }, el("h3", {}, "交给她决定"), el("p", { class: "note" }, "授权接口暂时不可用，刷新后再试。"));
  const cap = (a.capabilities || []).find(x => x.capability === id);
  if (!cap) return el("section", { class: "cd-sec" }, el("h3", {}, "交给她决定"), el("p", { class: "lockline" }, icon("lock"), el("span", {}, AUTO_LOCKED)));
  const base = { mode: cap.mode, allow_pause: cap.allow_pause, ranges: cap.ranges || {} };
  const draft = state.autoDraft?.[id] || JSON.parse(JSON.stringify(base));
  const set = patch => { state.autoDraft = { ...(state.autoDraft || {}), [id]: { ...draft, ...patch } }; rerender(); };
  const changed = !!state.autoDraft?.[id] && JSON.stringify(state.autoDraft[id]) !== JSON.stringify(base);
  const rangeRow = field => {
    const lim = cap.limits?.[field]; if (!lim) return null;
    const floor = lim.kind === "floor", key = floor ? "min" : "max", on = !!draft.ranges[field];
    const type = Number.isInteger(lim.configured) ? "integer" : "number", u = unitFor(field, type);
    const show = v => +(v / u.scale).toFixed(u.money ? 4 : 6);
    const dflt = floor ? (type === "integer" ? Math.floor(lim.configured / 2) : lim.configured / 2) : lim.configured * 2;
    return el("li", { class: on ? "on" : "" }, el("label", { class: "ck" }, el("input", { type: "checkbox", checked: on, onchange: e => {
        const ranges = { ...draft.ranges }; if (e.target.checked) ranges[field] = { [key]: dflt }; else delete ranges[field]; set({ ranges }); } }),
        el("span", {}, autoLabel(field))),
      on ? el("span", { class: "rng" }, floor ? "最低" : "最高", u.pre ? el("i", {}, u.pre) : null,
        el("input", { class: "input", inputmode: "decimal", value: String(show(draft.ranges[field][key])), "aria-label": autoLabel(field),
          onchange: e => { const n = Number(e.target.value) * u.scale; if (!Number.isFinite(n)) return; set({ ranges: { ...draft.ranges, [field]: { [key]: type === "integer" ? Math.round(n) : n } } }); } }),
        u.unit ? el("em", {}, u.unit) : null, el("span", { class: "muted" }, (floor ? "到你的设置 " : "从你的设置 ") + autoValue(field, lim.configured) + (floor ? "" : " 起")))
        : el("span", { class: "muted" }, "你的设置 " + autoValue(field, lim.configured)));
  };
  const wired = new Set(cap.wired || []), notWired = cap.fields.filter(f => !wired.has(f));
  const adjustments = (a.adjustments || []).filter(x => x.capability === id).slice(0, 8);
  return el("section", { class: "cd-sec auto-sec" }, el("h3", {}, "交给她决定", el("span", {}, "保存后立即生效，不需要重启")),
    el("div", { class: "mode-pick", role: "radiogroup", "aria-label": "由谁决定" }, AUTO_MODES.map(([m, n, d]) =>
      el("button", { type: "button", role: "radio", "aria-checked": String(draft.mode === m), onclick: () => set({ mode: m }) }, el("b", {}, n), el("span", {}, d)))),
    draft.mode !== "owner" ? [
      cap.fields.includes("pause") ? el("label", { class: "ck big" }, el("input", { type: "checkbox", checked: draft.allow_pause, onchange: e => set({ allow_pause: e.target.checked }) }),
        el("span", {}, el("b", {}, "允许她暂停这项能力"), el("small", {}, "最长到第二天 08:00，到时自动恢复。她不能恢复你暂停的，也不能打开你关掉的能力。"))) : null,
      Object.keys(cap.limits || {}).length ? [el("p", { class: "sub2" }, "她可以调整的参数"), el("ul", { class: "ranges" }, Object.keys(cap.limits).map(rangeRow))] : null,
      el("p", { class: "note mt" }, "她只能让这项能力少运行：减少次数和额度、拉长间隔或暂停，不能增加次数或费用。她的调整必须引用你们的原话作为理由。从备份恢复后，她的调整全部作废。"),
      notWired.length ? el("p", { class: "lockline" }, icon("warn"), el("span", {}, (wired.size ? "其中" + notWired.map(autoLabel).join("、") : "这项能力") + "的运行逻辑还没有接入她的调整，授权可以先保存，接入后生效。")) : null] : null,
    changed ? el("div", { class: "row-gap mt" }, btn("保存授权", () => saveAutonomy(id, state.autoDraft[id]), { cls: "sm primary" }),
      btn("还原", () => { delete state.autoDraft[id]; rerender(); }, { cls: "sm quiet" })) : null,
    adjustments.length ? [el("p", { class: "sub2" }, "她的调整"), el("ul", { class: "clog" }, adjustments.map(x => { const [label, tone] = ADJ_STATE[x.state] || ["其他状态", "off"];
      return el("li", { class: x.state === "PROPOSED" ? "prop" : "" }, el("span", { class: "who it" }, "她"),
        el("div", {}, el("b", {}, x.field === "pause" ? "暂停这项能力" : `${autoLabel(x.field)}改为 ${autoValue(x.field, x.value)}`),
          x.reason ? el("p", {}, "理由：" + x.reason) : null, el("small", {}, fmtDate(x.created_at) + (x.expires_at && x.state === "ACTIVE" ? "，" + fmtDate(x.expires_at) + " 自动恢复" : ""))),
        el("div", { class: "clog-r" }, el("span", { class: "tag " + tone }, label),
          x.state === "PROPOSED" ? el("span", {}, btn("批准", () => decideAdjustment(x, "approve"), { cls: "sm primary" }), btn("拒绝", () => decideAdjustment(x, "reject"), { cls: "sm danger" })) : null,
          x.state === "ACTIVE" ? btn("撤销", () => decideAdjustment(x, "revert"), { cls: "sm" }) : null));
    }))] : null);
}
async function saveAutonomy(id, body) {
  try { await api.write("/autonomy/" + id, body, "PUT", r => r.state === "COMPLETE"); delete state.autoDraft[id]; toast("已保存授权，立即生效"); await loadRoute({ reset: true }); }
  catch (error) { notice(errorText(error)); }
}
async function decideAdjustment(x, decision) {
  try { await api.write("/autonomy/adjustments/" + encodeURIComponent(x.override_id) + "/decision", { decision }, "POST", r => typeof r.state === "string");
    toast({ approve: "已批准，立即生效", reject: "已拒绝", revert: "已撤销，回到你的设置" }[decision]); await loadRoute({ reset: true }); }
  catch (error) { notice(errorText(error)); }
}
function runtimeToggle(target, paused) {
  const name = { diary: "写日记", life: "自主活动", proactive: "主动私聊" }[target];
  const form = el("form", { class: "dlg-form" }, el("p", {}, paused ? `恢复${name}。` : `暂停${name}。`), el("p", { class: "muted" }, "立即生效，不需要重启。你发来的消息始终会回复。"));
  const submit = el("button", { type: "submit", class: "btn primary" }, paused ? "恢复" : "暂停");
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => { e.preventDefault();
    perform(form, submit, () => api.write("/mode", { target, enabled: paused }, "POST", r => typeof r.enabled === "boolean" || r.state === "COMPLETE"),
      async () => { closeDialog(); toast(paused ? "已恢复，立即生效" : "已暂停，立即生效"); await loadRoute({ reset: true }); }); });
  openDialog((paused ? "恢复" : "暂停") + name, form);
}

/* ================= 配置 ================= */
const GROUPS = [["model", "模型与计价"], ["memory", "记忆与日记"], ["autonomy", "主动与自主"], ["content", "阅读与文件"], ["expression", "表达与表情"], ["system", "运行与控制台"]];
const RATE_KEYS = ["input_rate", "cached_input_rate", "output_rate"];
const MODEL_TAIL = ["timeout_seconds", "max_output_tokens", "context_soft_tokens", "max_recent_messages"];
const SCHEDULE_KEYS = ["quiet_start", "quiet_end", "diary_generate_hour", "consolidation_local_hour"];
const isMoneyKey = k => /_(daily|monthly)_micro_usd$|^monthly_limit_micro_usd$/.test(k);
PAGES.configuration = data => {
  const c = data.configuration;
  if (!c || !Array.isArray(c.fields) || !Number.isInteger(c.version) || !Number.isInteger(c.active_version)) return empty("配置数据不完整", "请刷新核对，现在无法安全编辑配置。", true);
  const q = state.cfgQ.trim(), fields = cfgFields();
  const search = searchBox("cfg-q", state.cfgQ, "搜索设置，例如 冷却、上限、时间", v => { state.cfgQ = v; rerender(); });
  search.classList.add("wide-search");
  const top = [head("配置", "保存为加密的新版本；允许热加载的整版设置在下一任务边界校验，已有任务保持原版本。", currencySwitch()), el("div", { class: "toolbar" }, search)];
  if (q) {
    const hits = fields.filter(f => (f.label + (f.description || "") + f.key).includes(q));
    return el("div", {}, top, sheet(`“${q}”的搜索结果`, hits.length ? el("div", { class: "pf-list" }, hits.map(f => CAP_KEYS.has(f.key)
      ? el("div", { class: "pf" }, el("div", { class: "pf-l" }, el("b", {}, f.label), el("span", {}, "属于“" + (CAP_NAMES[capOfKey(f.key)] || "能力") + "”")),
          el("div", { class: "pf-c" }, el("a", { class: "btn sm", href: "#caps/" + encodeURIComponent(capOfKey(f.key)) }, "去能力与开关修改")))
      : fieldRow(f))) : el("div", { class: "empty" }, el("h3", {}, "没有找到这个设置"), el("p", {}, "试试“冷却”“上限”“时间”这类词。")), el("span", { class: "note" }, hits.length + " 项")));
  }
  const special = new Set([...RATE_KEYS, ...MODEL_TAIL, ...fields.filter(f => isMoneyKey(f.key) || f.type === "secret").map(f => f.key)]);
  // 属于某项能力的开关和参数只在“能力与开关”里出现，配置页只放系统级设置。
  const rest = fields.filter(f => !special.has(f.key) && !CAP_KEYS.has(f.key));
  const groups = GROUPS.filter(([g]) => rest.some(f => (f.group || "system") === g));
  const extra = [...new Set(rest.map(f => f.group || "system"))].filter(g => !GROUPS.some(([k]) => k === g)).map(g => [g, "其他设置"]);
  const sections = [["model", "模型与密钥"], ["money", "费用与预算"],
    ...[...groups, ...extra].filter(([g]) => g !== "model")];
  const dirtyIn = id => ({ model: [...RATE_KEYS, ...MODEL_TAIL, ...fields.filter(f => f.type === "secret").map(f => f.key), ...rest.filter(f => f.group === "model").map(f => f.key)],
    money: fields.filter(f => isMoneyKey(f.key) && !CAP_KEYS.has(f.key)).map(f => f.key) }[id] || rest.filter(f => (f.group || "system") === id).map(f => f.key)).some(k => state.draft.has(k));
  // 版本状态始终在上方；下面一次只显示目录里选中的一节。草稿跨节保留，目录上的圆点标出哪节有修改。
  const cur = sections.some(([id]) => id === state.sub) ? state.sub : "model";
  const section = cur === "model" ? modelSection(rest.filter(f => f.group === "model"))
    : cur === "money" ? moneySection(c) : el("section", { class: "sheet cfg-sec", id: "cfg-" + cur },
      el("div", { class: "sheet-head" }, el("h2", {}, sections.find(([id]) => id === cur)[1])),
      el("div", { class: "sheet-body pf-list" }, rest.filter(f => (f.group || "system") === cur).map(f => fieldRow(f))));
  return el("div", {}, top, el("div", { class: "cfg2" },
    el("nav", { class: "cfg-toc", "aria-label": "配置目录" }, sections.map(([id, n]) => el("a", { href: "#configuration/" + id, "data-toc": "cfg-" + id,
      "aria-current": id === cur ? "true" : null,
      onclick: e => { e.preventDefault(); history.replaceState(null, "", "#configuration/" + id); state.sub = id; rerender();
        const main = $(".cfg2"); if (main && main.getBoundingClientRect().top < 0) main.scrollIntoView({ block: "start" }); } }, n,
      dirtyIn(id) || (id === "money" && state.budgetDraft.size) ? el("span", { class: "dirty", "aria-label": "有修改" }) : null))),
    el("div", { class: "cfg-main" }, versionSection(c), section)));
};
function versionSection(c) {
  const hist = [...(c.history || [])].filter(h => Number.isInteger(h.version)).sort((a, b) => a.version - b.version).slice(-6);
  const label = v => v === c.active_version ? ["active", "聊天新任务生效版本"] : v === c.version ? ["pending", "已保存"] : ["old", "历史"];
  return el("section", { class: "sheet cfg-sec", id: "cfg-version" }, el("div", { class: "sheet-head" }, el("h2", {}, "版本")), el("div", { class: "sheet-body" },
    el("ol", { class: "vchain" }, hist.map(h => { const [cls, text] = label(h.version);
      return el("li", { class: cls }, el("button", { type: "button", onclick: () => {
        const detail = el("div", {}, el("ul", { class: "diff" }, (h.fields || []).map(k => el("li", {}, el("b", {}, fieldOf(k)?.label || "已移除的设置")))),
          btn("重新选择此版本", () => restoreConfiguration(h.version)));
        drawer("v" + h.version + " 的修改", fmtDate(h.created_at), detail);
      } },
        el("b", { class: "num" }, "v" + h.version), el("span", {}, text), el("small", {}, fmtDate(h.created_at) + "，" + (h.fields || []).length + " 项修改"))); }),
      !hist.length ? el("li", { class: "active" }, el("span", {}, el("b", { class: "num" }, "v" + c.active_version), el("span", {}, "生效中"), el("small", {}, "还没有通过控制台保存过修改"))) : null,
      !c.pending_restart ? el("li", { class: "next" }, el("span", {}, el("b", { class: "num" }, "v" + (c.version + 1)), el("span", {}, state.draft.size ? state.draft.size + " 项修改未保存" : "暂无修改"))) : null),
    el("div", { class: "apply" }, el("div", {}, el("h3", {}, `v${c.version} 已保存 · ${configurationStatus(c)}`),
      el("p", { class: "note" }, c.hot_reload_enabled ? "新任务使用 Worker 各自报告的默认版本；运行中、重试和工具等待恢复的任务保持原版本。" : "热加载已关闭，使用进程启动时读取的配置。"),
      c.error ? el("p", { role: "alert" }, c.error) : null,
      c.approval_reason ? el("p", { role: "alert" }, c.approval_reason) : null,
      !(c.workers || []).length ? el("p", {}, "尚无 Worker 生效报告；API 启动版本 v" + (c.startup_version ?? c.active_version)) :
        el("ul", {}, c.workers.map(w => el("li", {}, `${w.worker}：新任务 v${w.active_version}；${w.pending_version == null ? "已生效" : "等待 v" + w.pending_version}${w.reason ? "；" + w.reason : ""}；报告于 ${fmtDate(w.observed_at)}`))))),
    btn("重新选择启动基础配置", () => restoreConfiguration(0), { cls: "sm quiet" }),
    c.pending_restart ? el("div", { class: "apply" }, el("div", {}, el("h3", {}, c.requires_reapproval ? "需要重启并重新审批" : "需要重启"), el("p", { class: "note" }, "保存不会扩展原有授权；完成原发布与审批流程后再启用任务。"),
      el("ol", { class: "steps" }, el("li", {}, "停止聊天、日记、内容和生活进程"), el("li", {}, "做一次加密备份"), el("li", {}, "如果在 QQ 私人窗口期内，先完成窗口交接"), el("li", {}, "重启 API 和各个进程")))) : null));
}
function configurationStatus(c) {
  if (c.error) return "校验失败，保留上一有效版本";
  if (c.requires_reapproval) return c.pending_restart ? "需要重新审批与重启" : "需要重新审批";
  if (c.pending_restart) return "需要重启";
  if (c.pending_reload) return "等待任务边界";
  if (!(c.workers || []).length) return "启动配置，等待 Worker 报告";
  return "已生效（新任务）";
}
function restoreConfiguration(version) {
  const c = state.data.configuration;
  const form = el("form", { class: "dlg-form" }, el("p", {}, `将 v${version} 的设置重新校验并保存为 v${c.version + 1}。现有任务继续使用原版本；当前授权、来源和费用审批仍需满足。`));
  const submit = el("button", { type: "submit", class: "btn primary" }, "重新选择并保存");
  form.append(submit);
  form.addEventListener("submit", e => {
    e.preventDefault();
    perform(form, submit, () => api.write("/configuration", { expected_version: c.version, restore_version: version }, "PUT",
      r => Number.isInteger(r.version) && Array.isArray(r.fields)), r => {
      state.data.configuration = r; state.draft.clear(); closeDialog(); closeDrawer(); rerender();
      toast(`已保存为 v${r.version}，${configurationStatus(r)}`);
    });
  });
  openDialog("重新选择配置版本", form);
}
function videoModelsControl(f, config) {
  const candidates = config.candidates, saved = f.value.candidates, approved = f.authorized || [];
  const active = name => approved.includes(name) && same(candidates[name], saved[name]);
  const update = (name, field, value) => setDraft(f, { ...config,
    candidates: { ...candidates, [name]: { ...candidates[name], [field]: value } } });
  const nameInput = el("input", { class: "input", placeholder: "候选名称", "aria-label": "新候选名称" });
  const labels = { provider: "提供方", model: "模型名称", endpoint: "HTTPS 请求地址", key: "密钥",
    input_rate: "输入费率", cached_input_rate: "缓存输入费率", output_rate: "输出费率" };
  return el("div", { class: "obj" },
    el("label", {}, "新任务使用", el("select", { class: "input", onchange: e =>
      setDraft(f, { ...config, selected: e.target.value || null }) },
      el("option", { value: "", selected: !config.selected }, "部署默认 · " + (f.default_model || "未配置")),
      Object.keys(candidates).map(name => el("option", { value: name, selected: config.selected === name,
        disabled: !active(name) }, name + (active(name) ? " · 已授权" : " · 待授权"))))),
    Object.entries(candidates).map(([name, route]) => el("fieldset", { class: "obj" },
      el("legend", {}, name + (active(name) ? " · 已授权" : " · 待授权")),
      Object.entries(labels).map(([key, label]) => el("label", {}, label,
        el("input", { class: "input", type: key === "key" ? "password" : key.endsWith("_rate") ? "number" : "text",
          value: key === "key" ? "" : route[key], autocomplete: key === "key" ? "new-password" : "off",
          placeholder: key === "key" && saved[name] ? "留空保留已保存的密钥" : undefined,
          onchange: e => update(name, key, key.endsWith("_rate") ? Number(e.target.value) : e.target.value) }))),
      f.approvals?.[name] && !active(name) ? el("details", {}, el("summary", {}, "已保存候选的付费授权参数"),
        el("pre", {}, JSON.stringify(f.approvals[name], null, 2))) : null)),
    el("div", { class: "actions" }, nameInput, btn("添加候选", () => {
      const name = nameInput.value.trim();
      if (!/^[a-zA-Z0-9_-]{1,48}$/.test(name) || name in candidates) {
        fieldError(nameInput, "请输入唯一的英文、数字、下划线或连字符名称"); return;
      }
      setDraft(f, { ...config, candidates: { ...candidates, [name]: { provider: "", model: "",
        endpoint: "", key: "", input_rate: 1, cached_input_rate: 1, output_rate: 1 } } });
    }, { cls: "sm" })), el("p", { class: "note" }, "费率单位为微美元／百万 token。先保存候选，授权后可切换。"));
}
function modelSection(other) {
  const rate = k => fieldOf(k), rates = RATE_KEYS.map(rate).filter(Boolean);
  const ctx = fieldOf("context_soft_tokens"), out = fieldOf("max_output_tokens"), inR = rate("input_rate"), outR = rate("output_rate");
  const est = ctx && out && inR && outR ? (draftVal(ctx) * draftVal(inR) + draftVal(out) * draftVal(outR)) / 1e6 : null;
  const order = ["provider", "model_url", "model_name"], main = [...order.map(fieldOf), ...other.filter(f => !order.includes(f.key) && f.key !== "video_models")].filter(Boolean);
  const key = fieldOf("model_key"), videoKey = fieldOf("video_model_key");
  return el("section", { class: "sheet cfg-sec", id: "cfg-model" }, el("div", { class: "sheet-head" }, el("h2", {}, "模型与密钥"), el("span", { class: "note" }, "主回复只使用这一个模型")),
    el("div", { class: "sheet-body" }, el("h3", { class: "sub first" }, "对话模型"),
      el("div", { class: "pf-list" }, main.map(f => fieldRow(f, f.key === "model_url" ? "请求地址" : f.key === "model_name" ? "模型名称" : f.key === "provider" ? "服务类型" : undefined)),
        key ? fieldRow(key, "密钥") : null),
      videoKey ? [el("h3", { class: "sub" }, "视频理解模型"), el("div", { class: "pf-list" }, fieldRow(videoKey, "部署默认密钥"),
        fieldOf("video_models") ? fieldRow(fieldOf("video_models")) : null)] : null,
      el("p", { class: "note mt" }, "密钥只能写入，任何地方都不会显示原值。传输令牌、主人控制令牌和读取适配器凭据在部署环境里更换；主人控制令牌同时用来加密配置版本，直接更换会导致旧版本无法解密。"),
      rates.length ? [el("h3", { class: "sub" }, "费率", el("span", {}, "按服务商公布的峰值价格填写，价格未知时会停止自主活动。")),
        el("div", { class: "prices" }, rates.map(f => el("div", { class: "price" + (state.draft.has(f.key) ? " changed" : "") }, el("span", {}, ({ input_rate: "输入", cached_input_rate: "缓存命中输入", output_rate: "输出" })[f.key]), control(f))))] : null,
      el("div", { class: "pf-list" }, MODEL_TAIL.map(fieldOf).filter(Boolean).map(f => fieldRow(f))),
      est !== null ? el("p", { class: "estimate" }, icon("coin"), el("span", {}, "按现在的设置，一轮上下文用满的对话最多约 ", el("b", { class: "num" }, money(est)),
        state.currency === "CNY" ? "（按 1 美元 = ¥" + (state.fx / 1e6).toFixed(2) + " 换算）" : "")) : null));
}
function scheduleStrip() {
  const fs = SCHEDULE_KEYS.map(fieldOf).filter(Boolean); if (!fs.length) return null;
  const v = k => { const f = fieldOf(k); return f ? draftVal(f) : null; };
  const qs = v("quiet_start"), qe = v("quiet_end"), x = h => (h / 24 * 100) + "%";
  const bands = Number.isInteger(qs) && Number.isInteger(qe) ? (qs > qe ? [[qs, 24], [0, qe]] : qs < qe ? [[qs, qe]] : []) : [];
  // 标记按时间排开，相隔不到 4 小时的错开到下一行，文字不压线也不互相重叠。
  const rowsUsed = [];
  const pin = (k, name, cls, on) => {
    if (!Number.isInteger(v(k))) return null;
    const h = v(k); let row = 0; while (rowsUsed.some(([r, at]) => r === row && Math.abs(at - h) < 4)) row++; rowsUsed.push([row, h]);
    const right = h >= 19;
    return el("div", { class: "pin " + cls + (on === false ? " off" : "") + (right ? " right" : ""), style: { left: x(h + .5), top: (10 + row * 26) + "px" } },
      el("i"), el("span", {}, name + " " + String(h).padStart(2, "0") + ":00"));
  };
  const now = localMinutes(new Date().toISOString());
  return el("div", { class: "cd-clock" }, el("div", { class: "clock", role: "img", "aria-label": "免打扰与整理时间" },
      bands.map(([a, z]) => el("div", { class: "cband", style: { left: x(a), width: x(z - a) } })), el("div", { class: "cnow", style: { left: (now / 1440 * 100) + "%" } }),
      pin("diary_generate_hour", "日记", "diary", fieldOf("diary_enabled") ? draftVal(fieldOf("diary_enabled")) : undefined),
      pin("consolidation_local_hour", "夜间整理", "cons", fieldOf("consolidation_enabled") ? draftVal(fieldOf("consolidation_enabled")) : undefined),
      el("div", { class: "cticks" }, [0, 3, 6, 9, 12, 15, 18, 21, 24].map(h => el("span", { style: { left: x(h) } }, String(h).padStart(2, "0"))))),
      el("p", { class: "note clock-note" }, "斜纹是免打扰时段：不发主动消息，回复照常。灰色标记表示这项整理没有开启。"));
}
function moneySection(c) {
  const monthly = fieldOf("monthly_limit_micro_usd"), scen = cfgFields().filter(f => isMoneyKey(f.key) && f.key !== "monthly_limit_micro_usd" && !CAP_KEYS.has(f.key));
  return el("section", { class: "sheet cfg-sec", id: "cfg-money" }, el("div", { class: "sheet-head" }, el("h2", {}, "费用与预算")),
    el("div", { class: "sheet-body" }, c.cloud_budget?.config ? cloudBudget(c.cloud_budget) : null,
      el("div", { class: "budget-block" }, el("div", { class: "bb-head" }, el("div", {}, el("h3", {}, "模型账本"),
        el("p", { class: "note" }, "所有模型调用每月的总额度。各能力自己的每日额度在“能力与开关”里调整。" + (state.currency === "CNY" ? "金额按 1 美元 = ¥" + (state.fx / 1e6).toFixed(2) + " 换算。" : ""))), el("span", { class: "chip" }, "新版本生效")),
        monthly ? fieldRow(monthly) : null,
        scen.length ? el("table", { class: "t scen" }, el("thead", {}, el("tr", {}, el("th", {}, "场景"), el("th", { class: "r" }, "额度"))),
          el("tbody", {}, scen.map(f => el("tr", { class: "static" + (state.draft.has(f.key) ? " changed" : "") }, el("td", {}, el("b", {}, f.label)), el("td", { class: "r" }, control(f), el("p", { class: "pf-err", role: "alert", hidden: true })))))) : null)));
}
function secretsSection(list) {
  return el("section", { class: "sheet cfg-sec", id: "cfg-secrets" }, el("div", { class: "sheet-head" }, el("h2", {}, "密钥"), el("span", { class: "note" }, "只能写入，任何地方都不会显示原值")),
    el("div", { class: "sheet-body" }, list.length ? el("div", { class: "pf-list" }, list.map(f => fieldRow(f))) : el("p", { class: "note" }, "没有可以在控制台替换的密钥。"),
      el("p", { class: "note mt" }, "传输令牌、主人控制令牌和读取适配器凭据在部署环境里更换。主人控制令牌同时用来加密配置版本，直接更换会导致旧版本无法解密。")));
}
function cloudBudget(policy) {
  /** @type {Array<[string, string, number, string, string?]>} */
  const defs = [["daily_micro_cny", "每日云端限额", 1e6, "¥"], ["video_per_item_micro_cny", "单个视频任务上限", 1e6, "¥"], ["asr_per_item_micro_cny", "单个语音任务上限", 1e6, "¥"],
    ["usd_to_cny_micro", "美元汇率（每 1 美元）", 1e6, "¥"], ["fx_source", "汇率依据", 1, ""], ["fx_validity_seconds", "汇率有效期", 86400, "", "天"]];
  const form = el("form", { class: "cloud-grid", id: "budget-form" }, defs.map(([k, n, scale, pre, unit]) => {
    const cur = policy.config[k], value = state.budgetDraft.has(k) ? state.budgetDraft.get(k) : typeof cur === "number" ? String(+(cur / scale).toFixed(6)) : cur === "operator_planning_estimate" ? "运营估算" : cur ?? "";
    const input = el("input", { class: "input", id: "budget-" + k, value, inputmode: k === "fx_source" ? undefined : "decimal", maxlength: k === "fx_source" ? "256" : undefined,
      oninput: e => { state.budgetDraft.set(k, e.target.value); $("#budget-save").disabled = false; } });
    return el("label", { class: "field" }, el("span", {}, n), k === "fx_source" ? input : el("span", { class: "unit" + (pre ? " has-pre" : "") }, pre ? el("i", {}, pre) : null, input, unit ? el("em", {}, unit) : null));
  }));
  const save = btn("更新云端预算", () => saveBudget(policy, defs, form, save), { cls: "sm primary", disabled: !state.budgetDraft.size });
  save.id = "budget-save";
  return el("div", { class: "budget-block" }, el("div", { class: "bb-head" }, el("div", {}, el("h3", {}, "云端人民币预算"),
      el("p", { class: "note" }, "保存后立即约束之后的云调用，不生成配置版本。不会重置已有费用、预留和结果不明的调用，进行中的任务继续用开始时的上限和汇率。")), el("span", { class: "chip" }, "立即生效")),
    form, el("div", { class: "bb-acts" }, save, state.budgetDraft.size ? btn("还原", () => { state.budgetDraft.clear(); rerender(); }, { cls: "sm quiet" }) : null),
    el("div", { class: "pf-list mt" }, kv("预算版本", "v" + policy.version), kv("生效时间", fmtDate(policy.effective_at)), kv("汇率有效期至", fmtDate(policy.fx_valid_until)), kv("记账时区", policy.timezone || "未记录")));
}
function saveBudget(policy, defs, form, button) {
  const changes = Object.create(null);
  try {
    for (const [k, raw] of state.budgetDraft) {
      const def = defs.find(d => d[0] === k); if (!def) continue;
      const [, n, scale] = def; let value;
      if (k === "fx_source") { value = String(raw).trim(); if (value === "运营估算") value = "operator_planning_estimate"; if (!value || value.length > 256) throw new Error("请填写有效的汇率依据。"); }
      else if (scale === 1e6) { const m = String(raw).trim().match(/^(\d+)(?:\.(\d{0,6}))?$/); if (!m) throw new Error(n + "：请填写最多 6 位小数的数字。"); value = Number(m[1]) * 1e6 + Number((m[2] || "").padEnd(6, "0")); }
      else { const s = Number(raw) * scale; value = Math.round(s); if (raw === "" || !Number.isFinite(s) || Math.abs(s - value) > 1e-5) throw new Error(n + "：请填写能换算为整秒的时长。"); }
      if (typeof value === "number" && (!Number.isSafeInteger(value) || value < 0)) throw new Error(n + "：数值超出允许范围。");
      if (value !== policy.config[k]) changes[k] = value;
    }
  } catch (error) { formError(form, error.message); return; }
  if (!Object.keys(changes).length) { formError(form, "没有需要保存的预算修改。"); return; }
  if (state.budgetSave) return;
  state.budgetSave = true;
  perform(form, button, () => api.write("/cloud-budget", { expected_version: policy.version, changes }, "PUT", r => Number.isInteger(r.version) && r.config && typeof r.config === "object"),
    async r => { state.data.configuration.cloud_budget = r; if (Number.isSafeInteger(r.config.usd_to_cny_micro)) state.fx = r.config.usd_to_cny_micro; state.budgetDraft.clear(); rerender(); toast("已更新云端预算，立即生效"); })
    .finally(() => { state.budgetSave = null; });
}

/* ================= 启动与全局事件 ================= */
function openPalette() {
  const p = $("#palette"), pages = ROUTES.flatMap(([, list]) => list.map(([k, n, i]) => ({ k, n, i })));
  const list = el("ul", { id: "pal-list" });
  const draw = q => {
    const hits = pages.filter(x => x.n.includes(q));
    list.replaceChildren(...(hits.length ? hits.map((x, j) => el("li", {}, el("button", { type: "button", class: j === 0 ? "sel" : "", onclick: () => { location.hash = x.k; p.close(); } }, icon(x.i), el("span", {}, x.n))))
      : [el("li", { class: "empty" }, "没有找到“" + q + "”")]));
  };
  const input = el("input", { class: "input", placeholder: "跳转到页面", "aria-label": "跳转到页面",
    oninput: e => draw(e.target.value.trim()), onkeydown: e => { if (e.key === "Enter") $("button", list)?.click(); } });
  p.replaceChildren(input, list); draw(""); p.showModal(); input.focus();
}
const tip = () => $("#tip");
function showTip(target) {
  const lines = tipOf.get(target); if (!lines) return;
  const r = target.getBoundingClientRect(), t = tip();
  t.replaceChildren(...lines.map(([time, title, st]) => el("div", { class: "tip-line" }, el("b", {}, time + " " + title), el("span", {}, st))));
  t.hidden = false;
  const w = t.offsetWidth;
  t.style.left = Math.max(8, Math.min(innerWidth - w - 8, r.left + r.width / 2 - w / 2)) + "px";
  t.style.top = Math.max(8, r.top - t.offsetHeight - 8) + "px";
}
document.addEventListener("pointerover", e => { const m = /** @type {Element} */ (e.target).closest?.(".mark"); if (m) showTip(m); else tip().hidden = true; });
document.addEventListener("focusin", e => { const m = /** @type {Element} */ (e.target).closest?.(".mark"); if (m) showTip(m); else tip().hidden = true; });

$("#login-form").addEventListener("submit", async e => {
  e.preventDefault();
  const button = $("#login-submit"), err = $("#login-err");
  if (!$("#pw").value) { err.textContent = "请输入控制台密码。"; err.hidden = false; $("#pw").focus(); return; }
  button.disabled = true; err.hidden = true;
  try {
    const value = await api.request("/session", { method: "POST", body: { password: $("#pw").value }, authenticate: false,
      validate: r => typeof r.csrf_token === "string" && Boolean(r.csrf_token) && valid(r.expires_at) });
    await acceptSession(value);
  } catch (error) { err.textContent = errorText(error); err.hidden = false; }
  finally { button.disabled = false; }
});
$("#logout").addEventListener("click", async () => {
  let failure = "";
  try { await api.write("/session", {}, "DELETE"); } catch { failure = "本地显示已清除，服务器上的退出结果没有确认。请重新登录后核对。"; }
  clearSession(failure); $("#pw").focus();
});
$("#menu").addEventListener("click", () => $("#side").classList.toggle("open"));
$("#refresh").addEventListener("click", () => loadRoute());
$("#search-open").addEventListener("click", openPalette);
$("#drawer-scrim").addEventListener("click", closeDrawer);
window.addEventListener("hashchange", navigate);
window.addEventListener("scroll", () => { $("#top").classList.toggle("scrolled", scrollY > 4); }, { passive: true });
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k" && state.session) { e.preventDefault(); openPalette(); }
  if (e.key === "Escape" && $("#drawer").classList.contains("open")) closeDrawer();
  if (e.key === "Escape" && $("#side").classList.contains("open")) $("#side").classList.remove("open");
});
window.addEventListener("offline", () => { if (state.session) { setConnection("off", "网络已断开"); notice("网络已断开，页面保留上次读取的记录。", true); } });
window.addEventListener("online", () => { if (state.session && !state.draft.size) loadRoute(); });
window.addEventListener("beforeunload", e => { if (state.draft.size || state.budgetDraft.size) { e.preventDefault(); e.returnValue = ""; } });
setInterval(updateSessionLeft, 30000);
for (const node of $$("[data-icon]")) node.replaceChildren(icon(node.dataset.icon));

async function start() {
  try { await acceptSession(await api.request("/session", { authenticate: false })); }
  catch (error) { clearSession(error.status === 401 ? "" : "暂时无法恢复会话。" + errorText(error)); }
}
start();

/* ================= 中文标签：原因码、限额、对象配置子项 ================= */
Object.assign(REASONS, {
  BILI_VIDEO_CONCURRENCY_LIMIT: "B 站视频分析已到并发上限", BILI_VIDEO_READER_PAUSED: "B 站读取已暂停", BILI_VIDEO_READER_UNAVAILABLE: "B 站读取不可用",
  BILI_VIDEO_RECONCILIATION_REQUIRED: "B 站视频费用需要核对", BILI_VIDEO_SCOPE_REQUIRED: "B 站视频需要授权范围", BRIDGE_CAPABILITY_PAUSED: "AstrBot 这项能力已暂停",
  BRIDGE_OBSERVATION_STALE: "AstrBot 很久没有上报状态", CUMULATIVE_ACCOUNT_NOT_ENROLLED: "账号没有加入累计额度", CUMULATIVE_PAUSED: "累计额度已暂停",
  CUMULATIVE_POLICY_EXPIRED: "累计额度策略已过期", CUMULATIVE_SCOPE_REQUIRED: "累计额度需要授权范围", LIVE_IMAGE_APPROVAL_REQUIRED: "图片需要当前窗口的授权",
  LIVE_LINK_SCOPE_REQUIRED: "链接需要当前窗口的授权", LIVE_WINDOW_STOPPED: "私人窗口已停止", MEME_DISCOVERY_PRIVATE_SCOPE_REQUIRED: "表情获取需要私人授权范围",
  MODEL_LEGACY_USAGE_RECONCILIATION_REQUIRED: "历史模型费用需要核对", MODEL_PROVIDER_CONCURRENCY_EXHAUSTED: "模型服务并发已满", MODEL_USAGE_UNRECONCILED: "模型费用还没核对",
  MODEL_WINDOW_BUDGET_EXHAUSTED: "窗口额度已用完", OWNER_COMMAND_SCOPE_REQUIRED: "需要你的指令授权", PAID_IMAGE_APPROVAL_REQUIRED: "付费图片理解需要授权",
  PAID_MEME_APPROVAL_REQUIRED: "付费表情理解需要授权", PRIVATE_FILE_CONFIG_INVALID: "私人文件配置无效", PRIVATE_FILE_SCOPE_REQUIRED: "私人文件需要授权范围",
  PRIVATE_FILE_TRAFFIC_EXHAUSTED: "私人文件今日流量已用完", PRIVATE_POKE_NATIVE_DISABLED: "原生拍一拍没有开启", PRIVATE_POKE_SCOPE_REQUIRED: "拍一拍需要授权范围",
  QUARANTINED: "已隔离", READER_NOT_ACCEPTING_TASKS: "读取程序暂不接收任务", READER_OPERATION_UNAVAILABLE: "读取程序不支持这项操作",
  READ_BUDGET_UNAVAILABLE: "读取额度不可用", SCENARIO_MONTHLY_BUDGET_EXHAUSTED: "这个场景本月额度已用完", SEARCH_PROVIDER_UNOBSERVED: "搜索服务还没有上报状态",
  STATIC_FACE_OBSERVATION_STALE: "QQ 表情图像很久没有更新", VOICE_ASR_SCOPE_REQUIRED: "语音转写需要授权范围", VOICE_CAPACITY_EXCEEDED: "语音任务已满",
  VOICE_RESOURCE_RECONCILIATION_REQUIRED: "语音资源需要核对", STALE: "记录已过期", UNKNOWN: "结果不明", UNOBSERVED: "还没有观测", UNAVAILABLE: "不可用",
  BRIDGE_TIMEOUT: "连接 AstrBot 超时", MODEL_TIMEOUT: "模型请求超时", NETWORK_ERROR: "网络错误",
});
/* 未收录的原因码按词尾给出中文，界面上不出现英文代码 */
const REASON_TAILS = [["_SCOPE_REQUIRED", "需要授权范围"], ["_APPROVAL_REQUIRED", "需要授权"], ["_RECONCILIATION_REQUIRED", "需要先核对"], ["_NOT_CONFIGURED", "没有配置"],
  ["_DISABLED", "没有开启"], ["_PAUSED", "已暂停"], ["_UNAVAILABLE", "不可用"], ["_EXHAUSTED", "额度已用完"], ["_STALE", "很久没有联系"], ["_EXPIRED", "已过期"],
  ["_INVALID", "配置无效"], ["_LIMIT", "已到上限"], ["_COOLDOWN", "冷却中"], ["_TIMEOUT", "超时"], ["_FAILED", "失败"], ["_REQUIRED", "缺少前置条件"]];
function reasonText(code) {
  if (!code) return "";
  if (REASONS[code]) return REASONS[code];
  const tail = REASON_TAILS.find(([t]) => String(code).endsWith(t));
  return tail ? tail[1] : "未归类的原因";
}
const LIMIT_NAMES = {
  daily_micro_usd: "每日额度", monthly_micro_usd: "每月额度", daily_calls: "每日调用", daily_limit: "每日次数", cooldown_seconds: "冷却", mode: "模式",
  max_daily_images: "每日图片", max_daily_files: "每日文件", daily_requests: "每日请求", monthly_requests: "每月请求", daily_bytes: "每日流量",
  max_concurrent: "并发", deadline_seconds: "处理时限", max_chars: "字数上限", wait_seconds: "等待", daily_queries: "每日搜索", max_active: "同时进行",
};
const VALUE_NAMES = { auto: "自动", voice_input: "只回应语音", text_only: "只回文字", poke_only: "只拍回去", none: "不使用", qq_social_v1: "QQ 资料与空间", references_v1: "引用与转发", owner_private_v1: "仅主人私聊", isolated_container: "隔离容器", sentence: "按句子长度分段", semantic: "按语义分段（由她写的换行决定）", unlimited: "不限",
  OFF: "关闭", SHADOW: "只记录不生效", ENABLED: "开启", PREVIEW: "只预览", participation_v1: "参与式 v1", participation_v2: "参与式 v2",
  never: "不引用", occasionally: "偶尔引用", often: "经常引用", disabled: "关闭", low: "低", high: "高", max: "最高", bridge: "Bridge 管理", core: "Core 管理",
  happy: "开心", like: "喜欢", surprised: "惊讶", morning: "早安", sleep: "晚安", sad: "难过", angry: "生气", shy: "害羞", tired: "困了", thinking: "思考", love: "爱心", cry: "哭哭" };
function limitValue(k, v) {
  if (v === null || v === undefined) return "未设置";
  if (typeof v === "boolean") return v ? "开" : "关";
  if (typeof v === "string") return VALUE_NAMES[v] || v;
  if (/micro_usd/.test(k)) return money(v, "usd");
  if (/micro_cny/.test(k)) return yuan(v);
  if (/_seconds$/.test(k)) return duration(v);
  if (/_bytes$/.test(k)) return bytes(v);
  return count(v);
}
const limitName = k => LIMIT_NAMES[k] || subLabel(k);

/* 对象配置的子项名称 */
const SUB = {
  enabled: "开启", mode: "模式", scope: "范围", url: "地址", model: "模型", key: "密钥", protocol: "协议", nodes: "节点", reply: "语音回复", asr: "语音转写",
  endpoint: "接口", sizes: "可选尺寸", max_output_bytes: "单张图片上限", retention_seconds: "图片保留时长",
  proactive: "主动消息也用语音", media: "媒体处理", sampling: "抽样", limits: "资源限制", vision: "图片理解", adapter_id: "适配器", parser_image: "解析镜像",
  parser_socket: "解析通道", public_origin: "控制台地址", session_seconds: "会话时长", max_sessions: "最多同时登录", allow_native: "允许原生拍一拍",
  coalesce_seconds: "合并间隔", cooldown_seconds: "冷却", daily_limit: "每天最多", max_coalesced: "最多合并", max_text_chars: "文字上限", max_pending_seconds: "最长等待",
  max_links_per_event: "每条消息最多链接", max_images_per_event: "每条消息最多图片", max_images_per_article: "每篇文章最多图片", article_image_micro_usd: "文章图片单价",
  use_chat_model_for_vision: "用对话模型理解图片", max_image_bytes: "单张图片上限", max_daily_image_bytes: "每日图片流量", max_daily_images: "每日最多图片",
  image_daily_micro_usd: "图片理解每日额度", wait_seconds: "回复前最多等待", upload_window_seconds: "上传窗口", max_request_micro_usd: "单次请求上限",
  max_files_per_event: "每条消息最多文件", max_daily_files: "每日最多文件", max_daily_bytes: "每日流量", upload_lease_seconds: "上传租约", worker_lease_seconds: "处理租约",
  spool_retention_seconds: "暂存保留", max_file_bytes: "单个文件上限", max_expanded_bytes: "解压后上限", max_zip_entries: "压缩包最多条目", max_compression_ratio: "最大压缩比",
  max_pages: "最多页数", max_records: "最多条数", max_text_chars_file: "文字上限", max_table_depth: "表格最深层级", max_pdf_stream_bytes: "PDF 单流上限",
  cpu_seconds: "CPU 时间", wall_seconds: "总时长", memory_bytes: "内存上限", scratch_bytes: "临时空间", max_processes: "最多进程",
  signing_private_key: "签名私钥", job_ttl_seconds: "任务有效期", lease_ttl_seconds: "租约有效期", result_ttl_seconds: "结果有效期", max_pending_jobs: "排队上限",
  max_private_blob_bytes: "私有数据上限", max_chars: "字数上限", max_context_chars: "带入对话的字数", authorization_sha256: "授权摘要", asset_policy_sha256: "素材策略摘要",
  policy_version: "策略版本", qzone_enabled: "允许 QQ 空间发布", qzone_public_enabled: "允许空间公开发布（逐稿审批）", nickname_enabled: "允许改昵称", signature_enabled: "允许改签名", avatar_enabled: "允许改头像",
  qzone_daily_limit: "QQ 空间每天最多", profile_daily_limit: "资料每天最多改", qzone_cooldown_seconds: "QQ 空间冷却", profile_cooldown_seconds: "资料改动冷却",
  model_daily_calls: "模型每日调用", model_daily_micro_usd: "模型每日额度", decision_interval_seconds: "决策间隔", max_publish_chars: "发布字数上限",
  max_nickname_chars: "昵称字数上限", max_signature_chars: "签名字数上限", max_source_refs: "最多引用来源", max_source_chars: "来源字数上限", max_input_bytes: "输入上限",
  max_output_tokens: "输出上限", timeout_seconds: "超时", plan_ttl_seconds: "计划有效期", profile_baseline_seconds: "资料观测有效期", max_pending_plans: "最多待执行计划",
  max_asset_bytes: "素材大小上限", max_asset_dimension: "素材边长上限", deadline_seconds: "处理时限", max_concurrent: "并发", max_preview_frames: "最多预览帧",
  max_clips: "最多片段", max_clip_seconds: "单段时长", max_total_clip_seconds: "片段总时长", max_total_output_bytes: "输出总上限", max_width: "最大宽度", max_height: "最大高度",
  max_source_pixels: "源视频像素上限", max_source_seconds: "源视频时长上限", max_decoded_frames: "最多解码帧", max_output_frames: "最多输出帧", output_fps: "输出帧率",
  timestamp_tolerance_seconds: "时间戳容差", max_frame_gap_seconds: "最大帧间隔", min_frame_gap_seconds: "最小帧间隔", terminate_grace_seconds: "终止宽限",
  max_process_output_bytes: "进程输出上限", max_memory_bytes: "内存上限", initial_frames: "初始帧数", max_segments: "最多分段", max_segment_seconds: "单段时长",
  max_total_segment_seconds: "分段总时长", max_subtitles: "最多字幕", max_danmaku: "最多弹幕", subtitle_similarity: "字幕相似度", subtitle_duplicate_seconds: "字幕去重间隔",
  danmaku_duplicate_seconds: "弹幕去重间隔", danmaku_bin_seconds: "弹幕统计间隔", min_danmaku_support: "弹幕最少支持数", min_density_ratio: "最小密度比",
  local_window_seconds: "局部窗口", context_before_seconds: "之前的上下文", context_after_seconds: "之后的上下文", baseline_floor_per_second: "每秒基线",
  focus_terms: "关注词", max_evidence_per_selection: "每次选取最多证据", max_duration_seconds: "最长时长", provider_timeout_seconds: "服务商超时", image_adapter: "图片适配器",
  max_segments_reply: "最多分段", min_chars: "最少字数", interval_ms: "间隔（毫秒）",
  strategy: "策略", daily_micro_usd: "每日额度", daily_pairs: "每日评论对数", candidate_days: "候选有效期（天）", lookback_hours: "回看时长（小时）",
  max_attempts: "最多尝试", local_hour: "运行时刻", max_source_memories: "最多来源记忆", source_retention_days: "来源保留（天）",
  record_retention_days: "记录保留（天）", max_input_tokens: "输入上限（token）", source_limit: "来源上限", topic_ttl_seconds: "话题有效期",
  follow_up_delay_seconds: "跟进延迟", quiet_seconds: "安静时先等", active_seconds: "聊得正热时先等",
  continue_seconds: "每多一条再等", silence_enabled: "收尾消息可以不回", silence_max_chars: "算作收尾的最多字数", silence_window_seconds: "回复后多久内算收尾", follow_up_enabled: "冷场后主动接一句", follow_up_after_seconds: "冷场多久后接话", follow_up_within_seconds: "回复后多久内还接", follow_up_lively_seconds: "算作聊得正热的间隔", follow_up_daily_checks: "每天最多判断几次", media_seconds: "只发图时至少等", batch_seconds: "一批最多等", active_window_seconds: "算作聊得正热的间隔", max_daily_decisions: "每日最多决策", min_interval_seconds: "最小间隔", read_goals_enabled: "允许阅读目标",
};
const TOKENS = { max: "最多", min: "最少", daily: "每日", monthly: "每月", total: "总", seconds: "时长", bytes: "大小", chars: "字数", enabled: "开启", limit: "上限" };
const subLabel = k => SUB[k] || String(k).split("_").map(t => TOKENS[t] || "").join("") || "其他设置";
/* 与 poke_protocol / voice_config / social_autonomy_protocol 中的 Literal 保持一致 */
const SUB_ENUMS = { mode: { poke_policy: ["auto", "text_only", "poke_only", "none"], reply: ["voice_input", "auto"], segmented_reply: ["sentence", "semantic"],
    conversation_policy: ["OFF", "SHADOW", "ENABLED"], owner_runtime: ["OFF", "SHADOW", "ENABLED"], life_loop: ["OFF", "PREVIEW", "SHADOW"] },
  strategy: { conversation_policy: ["participation_v1", "participation_v2"] }, scope: { social_autonomy: ["none", "qq_social_v1"] } };
const isSensitiveSub = k => /(_sha256|private_key|_socket|parser_image|token|secret)$/.test(k) || k === "key";

/* ================= 记忆关系图 =================
   参考 AstrBot 插件 LivingMemory 的图谱视图（选中节点列出关联记忆、图谱与列表互相定位）。
   现有记忆只有类型、主体和来源消息，所以这里按“主体 → 类型 → 记忆”连接，
   并把引用同一条来源消息的记忆连起来；实体与关系抽取需要后端新增后再接入。 */
const GPOS = new Map();
function graphModel(items) {
  const nodes = [{ id: "me", label: "你", type: "self" }], edges = [];
  const herItems = items.filter(m => ["self", "companion"].includes(m.detail?.subject));
  if (herItems.length) { nodes.push({ id: "her", label: "她", type: "her" }); edges.push({ a: "me", b: "her", kind: "root" }); }
  const kinds = new Map();
  for (const m of items) {
    const root = ["self", "companion"].includes(m.detail?.subject) ? "her" : "me", kind = m.detail?.memory_kind || "other";
    const cid = root + ":" + kind;
    if (!kinds.has(cid)) { kinds.set(cid, true); nodes.push({ id: cid, label: MEM_KIND[kind] || "其他", type: "kind", root }); edges.push({ a: root, b: cid, kind: "root" }); }
    nodes.push({ id: "m:" + m.id, label: m.title.length > 12 ? m.title.slice(0, 11) + "…" : m.title, type: "mem", m, cat: cid });
    edges.push({ a: cid, b: "m:" + m.id, kind: m.detail?.needs_review ? "conflict" : m.status === "CONFIRMED" ? "solid" : "hyp" });
  }
  const byEvidence = new Map();
  for (const m of items) for (const e of m.detail?.evidence_ids || []) { if (!byEvidence.has(e)) byEvidence.set(e, []); byEvidence.get(e).push(m.id); }
  const seen = new Set();
  for (const ids of byEvidence.values()) for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) {
    const key = ids[i] + "|" + ids[j]; if (seen.has(key)) continue; seen.add(key);
    edges.push({ a: "m:" + ids[i], b: "m:" + ids[j], kind: "shared" });
  }
  return { nodes, edges };
}
function layoutGraph(nodes, edges) {
  const W = 1000, H = 620, cx = W / 2, cy = H / 2, kindsList = nodes.filter(n => n.type === "kind");
  for (const n of nodes) {
    if (GPOS.has(n.id)) continue;
    if (n.type === "self") GPOS.set(n.id, { x: cx, y: cy });
    else if (n.type === "her") GPOS.set(n.id, { x: cx + 260, y: cy - 170 });
    else if (n.type === "kind") { const k = kindsList.indexOf(n), a = k / Math.max(1, kindsList.length) * Math.PI * 2 - Math.PI / 2; GPOS.set(n.id, { x: cx + Math.cos(a) * 210, y: cy + Math.sin(a) * 160 }); }
    else { const p = GPOS.get(n.cat) || { x: cx, y: cy }, siblings = nodes.filter(x => x.cat === n.cat), k = siblings.indexOf(n), a = Math.atan2(p.y - cy, p.x - cx) + (k - (siblings.length - 1) / 2) * .55;
      GPOS.set(n.id, { x: p.x + Math.cos(a) * 120, y: p.y + Math.sin(a) * 100 }); }
  }
  for (let it = 0; it < 160; it++) for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
    const a = GPOS.get(nodes[i].id), b = GPOS.get(nodes[j].id); const dx = a.x - b.x, dy = a.y - b.y, d = Math.hypot(dx, dy) || 1;
    if (d < 92) { const push = (92 - d) / 2, ux = dx / d, uy = dy / d;
      if (nodes[i].type !== "self") { a.x += ux * push; a.y += uy * push; } if (nodes[j].type !== "self") { b.x -= ux * push; b.y -= uy * push; } }
  }
  for (const n of nodes) { const p = GPOS.get(n.id); p.x = Math.max(60, Math.min(W - 60, p.x)); p.y = Math.max(36, Math.min(H - 36, p.y)); }
}
function edgeD(e) { const a = GPOS.get(e.a), b = GPOS.get(e.b); const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2, dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1, bend = e.kind === "shared" ? 40 : 10;
  return `M${a.x.toFixed(1)} ${a.y.toFixed(1)} Q${(mx - dy / d * bend).toFixed(1)} ${(my + dx / d * bend).toFixed(1)} ${b.x.toFixed(1)} ${b.y.toFixed(1)}`; }
function memoryGraph(items) {
  if (!items.length) return empty("还没有可以画成关系图的记忆", state.memTab === "review" ? "切到“全部”看看。" : "记忆形成后会出现在这里。");
  const { nodes, edges } = graphModel(items); layoutGraph(nodes, edges);
  const sel = nodes.find(n => n.id === state.gSel) || null;
  const near = sel ? new Set([sel.id, ...edges.filter(e => e.a === sel.id || e.b === sel.id).flatMap(e => [e.a, e.b])]) : null;
  const q = state.memQ.trim();
  const svg = el("svg", { class: "graph", viewBox: "0 0 1000 620", role: "img", "aria-label": `记忆关系图，${nodes.length} 个节点` },
    edges.map(e => el("path", { class: "ge " + e.kind + (near && !(near.has(e.a) && near.has(e.b)) ? " dim" : ""), d: edgeD(e), "data-a": e.a, "data-b": e.b })),
    nodes.map(n => { const p = GPOS.get(n.id), r = n.type === "self" ? 30 : n.type === "her" ? 24 : n.type === "kind" ? 20 : 11;
      const dim = near ? !near.has(n.id) : q ? !(n.label.includes(q) || n.m?.title.includes(q)) && n.type === "mem" : false;
      return el("g", { class: `gn ${n.type}${n === sel ? " sel" : ""}${dim ? " dim" : ""}${n.m && n.m.status !== "CONFIRMED" ? " pending" : ""}`, transform: `translate(${p.x.toFixed(1)} ${p.y.toFixed(1)})`,
        tabindex: "0", role: "button", "data-gn": n.id, "aria-label": n.m ? n.m.title : n.label },
        el("circle", { r: String(r) }), el("text", { y: String(n.type === "self" || n.type === "her" ? 5 : n.type === "kind" ? 5 : r + 15), class: n.type === "mem" ? "out" : "in" }, n.label));
    }));
  bindGraphEvents(svg, edges);
  return el("div", { class: "gwrap" }, el("section", { class: "gcanvas" }, svg,
    el("div", { class: "glegend" }, el("span", {}, el("i", { class: "l solid" }), "已确认"), el("span", {}, el("i", { class: "l hyp" }), "待确认"),
      el("span", {}, el("i", { class: "l conflict" }), "需要核对"), el("span", {}, el("i", { class: "l shared" }), "来自同一条消息"), el("span", { class: "muted" }, "拖动节点调整位置"),
      btn("重新排列", () => { GPOS.clear(); rerender(); }, { cls: "sm quiet", symbol: "refresh" }))),
    el("aside", { class: "gpanel", "aria-live": "polite" }, sel ? graphPanel(sel, nodes, edges) : graphSummary(items, nodes, edges)));
}
function graphSummary(items, nodes, edges) {
  const kinds = nodes.filter(n => n.type === "kind");
  return el("div", {}, el("h3", {}, "关系概览"), el("div", { class: "gstats" },
      el("div", {}, el("b", { class: "num" }, String(items.length)), el("span", {}, "条记忆")), el("div", {}, el("b", { class: "num" }, String(kinds.length)), el("span", {}, "个类型")),
      el("div", {}, el("b", { class: "num" }, String(items.filter(m => m.status !== "CONFIRMED").length)), el("span", {}, "条待确认"))),
    el("p", { class: "note mt" }, "点一个节点，右边会列出相关的记忆，可以直接确认、纠正或忘记。"),
    el("h4", { class: "gsub" }, "各类型"), el("ul", { class: "gtop" }, kinds.map(k => el("li", {}, el("button", { type: "button", onclick: () => pick(k.id) }, el("i"), k.label,
      el("span", { class: "num" }, String(nodes.filter(n => n.cat === k.id).length)))))));
}
function graphPanel(sel, nodes, edges) {
  const mems = sel.type === "mem" ? [sel.m, ...edges.filter(e => e.kind === "shared" && (e.a === sel.id || e.b === sel.id)).map(e => nodes.find(n => n.id === (e.a === sel.id ? e.b : e.a)).m)]
    : nodes.filter(n => n.type === "mem" && (n.cat === sel.id || (sel.type !== "kind" && nodes.find(k => k.id === n.cat)?.root === sel.id))).map(n => n.m);
  return el("div", {}, el("div", { class: "gp-head" }, el("button", { type: "button", class: "icon-btn", "aria-label": "返回概览", onclick: () => pick(null) }, icon("left")),
      el("h3", {}, sel.type === "mem" ? "这条记忆" : sel.label)),
    el("p", { class: "note" }, sel.type === "mem" && mems.length > 1 ? `还有 ${mems.length - 1} 条记忆来自同一条消息` : `${mems.length} 条记忆`),
    mems.map(m => el("article", { class: "gmem" + (m.status !== "CONFIRMED" ? " review" : "") }, el("b", {}, m.title),
      el("div", { class: "meta" }, tag(m.status), m.detail?.memory_kind ? el("span", { class: "chip" }, MEM_KIND[m.detail.memory_kind] || "其他") : null, el("span", { class: "note" }, fmtDate(m.occurred_at))),
      el("div", { class: "acts" }, m.status !== "CONFIRMED" ? btn("确认", () => memoryAction(m, "confirm"), { cls: "sm primary", symbol: "check" }) : null,
        btn("纠正", () => memoryAction(m, "correct"), { cls: "sm", symbol: "edit" }), btn("忘记", () => memoryAction(m, "forget"), { cls: "sm danger", symbol: "trash" })))));
}
function pick(id) { state.gSel = id; rerender(); if (id) $(`[data-gn="${CSS.escape(id)}"]`)?.focus({ preventScroll: true }); }
function bindGraphEvents(svg, edges) {
  let drag = null;
  const pt = ev => { const m = svg.getScreenCTM?.(); if (!m) return null; const p = svg.createSVGPoint(); p.x = ev.clientX; p.y = ev.clientY; return p.matrixTransform(m.inverse()); };
  svg.addEventListener("pointerdown", ev => { const g = ev.target.closest("[data-gn]"); if (!g) return; const p = pt(ev); const o = GPOS.get(g.dataset.gn);
    drag = { id: g.dataset.gn, g, sx: p ? p.x : 0, sy: p ? p.y : 0, ox: o.x, oy: o.y, moved: false }; g.setPointerCapture?.(ev.pointerId); });
  svg.addEventListener("pointermove", ev => { if (!drag) return; const p = pt(ev); if (!p) return; const dx = p.x - drag.sx, dy = p.y - drag.sy;
    if (Math.hypot(dx, dy) > 3) drag.moved = true; if (!drag.moved) return;
    const pos = { x: Math.max(30, Math.min(970, drag.ox + dx)), y: Math.max(30, Math.min(590, drag.oy + dy)) }; GPOS.set(drag.id, pos);
    drag.g.setAttribute("transform", `translate(${pos.x.toFixed(1)} ${pos.y.toFixed(1)})`);
    $$(".ge", svg).forEach(path => { if (path.dataset.a === drag.id || path.dataset.b === drag.id) path.setAttribute("d", edgeD({ a: path.dataset.a, b: path.dataset.b, kind: path.classList.contains("shared") ? "shared" : "" })); }); });
  const end = () => { if (!drag) return; const d = drag; drag = null; if (!d.moved) pick(state.gSel === d.id ? null : d.id); };
  svg.addEventListener("pointerup", end); svg.addEventListener("pointercancel", () => { drag = null; });
  svg.addEventListener("keydown", ev => { const g = ev.target.closest("[data-gn]"); if (g && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); pick(g.dataset.gn); } });
}

/* ================= 思考与决策 ================= */
const COGNITION_KINDS = {
  wake: { name: "自主活动", ic: "timeline" },
  life: { name: "生活话题", ic: "diary" },
  goal: { name: "目标", ic: "spark" },
  diary: { name: "日记", ic: "book" },
  reflection: { name: "反思", ic: "memory" },
  nightly: { name: "夜间整理", ic: "moon" },
  model: { name: "模型调用", ic: "chip" },
  rem: { name: "REM 记录", ic: "pulse" },
};
const COGNITION_CHOICES = {
  SKIP: "跳过", EXPLORE: "读到期的目标", OBSERVE: "发现新内容",
  CONTINUE: "继续", FOLLOW_UP: "跟进", SHARE: "分享", DEFER: "推迟",
  ACCEPTED: "接受", CANCELLED: "取消", COMMITTED: "完成", SKIPPED: "跳过",
};
const COGNITION_TONE = {
  SKIP: "off", SKIPPED: "off", CANCELLED: "off", EXPIRED: "off",
  FAILED: "bad", REJECTED: "bad", UNKNOWN: "bad",
  RUNNING: "run", PENDING: "run", WAITING: "warn", WAITING_DECISION: "warn", CANDIDATE: "warn",
  WAITING_INPUT: "warn", WAITING_RESOURCE: "warn", EXECUTION_UNCERTAIN: "warn",
};
const REASON_NAMES = {
  PAUSED: "你暂停了", QUIET_HOURS: "免打扰时段", OWNER_REQUESTED_PAUSE: "你让她先别打扰",
  RESOURCE_UNAVAILABLE: "资源不可用", DAILY_LIMIT: "今日上限", COOLDOWN: "冷却中",
  NO_CANDIDATE: "无候选", GOAL_DUE: "目标到期", DISCOVERY_DUE: "可以发现内容",
  OWNER_AUTHORITY_DISABLED: "权限未开启", OWNER_AUTHORITY_EXPIRED: "权限已过期",
  PRIVACY_CHANGED: "隐私设置变化",
};

function cognitionTitle(item, kind) {
  const k = COGNITION_KINDS[kind] || { name: "认知" };
  if (kind === "wake") return item.choice ? COGNITION_CHOICES[item.choice] || item.choice : "自主活动决策";
  if (kind === "life") return item.choice ? COGNITION_CHOICES[item.choice] || item.choice : "生活话题决策";
  if (kind === "goal") return item.topic || "目标";
  if (kind === "diary") return "日记";
  if (kind === "reflection") return "每日反思";
  if (kind === "nightly") return "夜间整理";
  if (kind === "model") return "模型调用";
  if (kind === "rem") return "REM";
  return k.name;
}

function cognitionState(item) {
  return item._kind === "wake" ? item.choice : item.state || item.choice;
}
function cognitionStateName(item) {
  const value = cognitionState(item);
  return COGNITION_CHOICES[value] || statusOf(value)[0];
}

function cognitionSummary(item, kind) {
  if (kind === "wake") return item.goal_id ? `关联目标 ${item.goal_id}` : "";
  if (kind === "life") {
    const dest = { PRIVATE_DIARY: "日记", PRIVATE_MESSAGE_DRAFT: "私聊草稿", QZONE_DRAFT: "空间草稿" }[item.destination] || "";
    return dest ? `去向：${dest}` : "";
  }
  if (kind === "goal") return item.state || "";
  if (kind === "diary") return item.state || "";
  if (kind === "reflection") return item.state || "";
  if (kind === "nightly") {
    const steps = item.steps || {};
    const done = Object.values(steps).filter(s => s === "DONE" || s === "COMPLETE").length;
    const total = Object.keys(steps).length;
    return `${done} / ${total} 完成`;
  }
  if (kind === "model") {
    const u = item.usage || {};
    return u.observed ? `${u.input_tokens} 输入，${u.output_tokens} 输出` : "实际用量未记录";
  }
  return "";
}

PAGES.thinking = data => {
  const allItems = [];
  for (const kind of Object.keys(COGNITION_KINDS)) {
    const kindData = data[kind];
    if (kindData && kindData.items) {
      for (const item of pageRecords(kindData)) {
        allItems.push({ ...item, _kind: kind, _title: cognitionTitle(item, kind), _summary: cognitionSummary(item, kind) });
      }
    }
  }
  allItems.sort((a, b) => {
    const at = Date.parse(a.occurred_at), bt = Date.parse(b.occurred_at);
    return (Number.isFinite(bt) ? bt : 0) - (Number.isFinite(at) ? at : 0) || String(a.id).localeCompare(String(b.id));
  });

  const filtered = allItems.filter(i => state.cogKind === "all" || i._kind === state.cogKind);
  const sel = filtered.find(i => i._kind + ":" + i.id === state.cogSel) || filtered[0];

  const today = localDay(new Date().toISOString());
  const todayItems = allItems.filter(i => i.occurred_at && localDay(i.occurred_at) === today);
  const bounded = Object.values(data).some(page => page.truncated);
  const coverage = el("p", { class: "note" }, "统计仅包含本页加载的记录。" + (bounded ? "部分类型还有更早的记录未展示。" : ""));

  const stats = el("div", { class: "dk-stats" }, [
    ["本页今日记录", todayItems.length],
    ["已完成", todayItems.filter(i => ["DONE", "COMPLETE", "COMMITTED", "SUCCEEDED"].includes(i.state)).length],
    ["跳过", todayItems.filter(i => ["SKIP", "SKIPPED"].includes(cognitionState(i))).length],
    ["失败", todayItems.filter(i => ["FAILED", "REJECTED", "UNKNOWN"].includes(i.state)).length]
  ].map(([t, v]) => el("div", {}, el("b", { class: "num" }, String(v)), el("span", {}, t))));

  const filters = el("div", {}, el("div", { class: "toolbar" }, el("div", { class: "seg" },
    [["all", "全部"], ...Object.entries(COGNITION_KINDS).map(([k, v]) => [k, v.name])].map(([k, t]) =>
      el("button", { type: "button", "aria-pressed": String(state.cogKind === k),
        onclick: () => { state.cogKind = k; state.cogSel = null; rerender(); } }, t))), cognitionFinder()), locateNote(data));

  if (!sel) return el("div", {},
    head("思考与决策", "她每一次决定做不做，以及当时依据了哪些信息。只显示真实记录，没有的就写没有。"),
    stats, coverage, filters,
    empty("还没有认知记录", "自主活动、生活话题、目标、日记、反思等决策记录会出现在这里。"), cognitionMore());

  const k = COGNITION_KINDS[sel._kind] || { name: "认知", ic: "spark" };
  const stateName = cognitionStateName(sel);

  const tabs = [["overview", "决策记录"], ["sources", "上下文"], ["reasoning", "原始推理"]];

  const body = state.cogTab === "sources" ? [
    el("h3", { class: "dk-title" }, "本轮依据的来源"),
    el("p", { class: "note" }, "这条记录引用的消息编号，都经过权限和来源校验；来源被清除后这条记录不再显示。"),
    el("ul", { class: "dk-ctx" },
      (sel.source_event_ids || []).length > 0
        ? (sel.source_event_ids || []).map((eid, n) => el("li", {}, el("span", { class: "dk-n" }, "第 " + (n + 1) + " 条来源"),
            el("span", { class: "tag ok" }, "已核验"), el("span", { class: "note" }, "编号尾号 " + shortId(eid))))
        : [el("li", { class: "note" }, "没有来源消息")]
    )
  ] : state.cogTab === "reasoning" ? reasoningView(sel) : [
    el("h3", { class: "dk-title" }, sel._title, el("span", { class: "note" }, fmtDate(sel.occurred_at))),
    el("dl", { class: "dk-rows" }, [
      ["类型", k.name],
      ["状态", el("b", {}, stateName)],
      ["原因代码", sel.reason_code && sel.reason_code !== "NOT_RECORDED" ? (REASON_NAMES[sel.reason_code] || sel.reason_code) : "—"],
      ["摘要", sel._summary || "—"],
    ].map(([dt, dd]) => el("div", {}, el("dt", {}, dt), el("dd", {}, dd)))),
    cognitionExtra(sel)
  ];

  return el("div", {},
    head("思考与决策", "她每一次决定做不做，以及当时依据了哪些信息。只显示真实记录，没有的就写没有。"),
    stats, coverage, filters,
    ["all", "model"].includes(state.cogKind) ? meterOverview(data.model) : null,
    el("div", { class: "dk-wrap" },
      el("ul", { class: "dk-list" + (state.cogKind === "model" ? " compact" : "") + (freshLoad() ? " anim" : ""), "data-keep-scroll": "cog-list" }, filtered.map((i, n) => {
        const ik = COGNITION_KINDS[i._kind] || { name: "其他", ic: "spark" };
        const iState = cognitionState(i) || "UNKNOWN";
        const tone = COGNITION_TONE[iState] || "ok";
        const [title, sub] = i._kind === "model" ? modelRowText(i) : [i._title, fmtDate(i.occurred_at) + "，" + ik.name];
        return el("li", { style: { "--i": String(Math.min(n, 16)) } },
          el("button", { type: "button", class: i === sel ? "sel" : "", "data-record-id": i.id,
            onclick: () => { state.cogSel = i._kind + ":" + i.id; rerender(); } },
            el("span", { class: "ic" }, icon(ik.ic)),
            el("span", { class: "dk-l" }, el("b", {}, title), el("small", {}, sub)),
            el("span", { class: "tag " + tone }, cognitionStateName(i))));
      }), el("li", { class: "dk-more" }, cognitionMore())),
      el("section", { class: "dk-card" },
        el("div", { class: "dk-head" },
          el("div", {},
            el("h2", {}, "思考与决策"),
            el("p", { class: "note" }, sel._title + "，" + fmtDate(sel.occurred_at))),
          el("span", { class: "dk-ic" }, icon("spark"))),
        el("div", { class: "dk-tabs", role: "tablist" }, tabs.map(([k, t]) =>
          el("button", { type: "button", role: "tab", "aria-selected": String(state.cogTab === k),
            onclick: () => { state.cogTab = k; rerender(); } }, t))),
        el("div", { class: "dk-body" }, body))));
};

/* ================= 目标与反思 ================= */

function goalCard(g) {
  const steps = g.steps || [], actions = g.allowed_actions || [];
  return el("div", { class: "goal" },
    el("div", {}, el("div", { class: "goal-h" }, el("b", {}, g.topic), tag(g.state)),
      el("p", {}, "更新于 " + fmtDate(g.updated_at)),
      g.commitment_state ? el("p", {}, "承诺：" + statusOf(g.commitment_state)[0]) : null,
      steps.length ? el("ul", { class: "goal-steps" }, steps.map(s =>
        el("li", {}, `第 ${s.position} 步：${statusOf(s.state)[0]}`))) : null),
    el("div", { class: "goal-acts row-gap" },
      g.state === "CANDIDATE" ? btn("接受", () => goalDecision(g.goal_id, "ACCEPT"), { cls: "sm primary", symbol: "check" }) : null,
      !["DONE", "FAILED", "CANCELLED"].includes(g.state) && !actions.includes("ABANDON") ? btn("取消", () => goalDecision(g.goal_id, "CANCEL"), { cls: "sm danger", symbol: "close" }) : null,
      actions.length ? btn("续行", () => goalContinue(g), { cls: "sm", symbol: "arrow" }) : null));
}

function goalDecision(goal_id, decision) {
  const titles = { ACCEPT: "接受这个目标", CANCEL: "取消这个目标？" };
  const form = el("form", { class: "dlg-form" }, el("p", {}, decision === "ACCEPT" ? "接受后按当前权限和资源安排执行。" : "取消后停止后续执行，已有结果和结果不明的记录会保留。"));
  const submit = el("button", { type: "submit", class: "btn " + (decision === "ACCEPT" ? "primary" : "danger") }, decision === "ACCEPT" ? "接受" : "取消");
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    perform(form, submit, () => api.write("/goals/" + encodeURIComponent(goal_id) + "/decision", { decision }, "POST",
      r => typeof r.state === "string"),
      async r => { closeDialog(); toast((decision === "ACCEPT" ? "已接受" : "已取消") + "（" + statusOf(r.state)[0] + "）"); await loadRoute({ reset: true }); });
  });
  openDialog(titles[decision], form);
}

function goalContinue(g) {
  const form = el("form", { class: "dlg-form" });
  const remaining = (g.steps || []).filter(s => s.state === "WAITING_DECISION").sort((a, b) => a.position - b.position);
  const actions = g.allowed_actions || [];
  const labels = { CONTINUE: "继续执行", WAIT: "等待到期", ABANDON: "放弃目标" };
  const actionSel = el("select", { class: "input", id: "goal-action", "aria-label": "操作", required: true },
    el("option", { value: "" }, "选择操作"),
    actions.map(action => el("option", { value: action }, labels[action])));
  const stepList = el("ol", { class: "stack" });
  const renderSteps = () => stepList.replaceChildren(...remaining.map((step, index) => {
    const move = delta => { [remaining[index], remaining[index + delta]] = [remaining[index + delta], remaining[index]]; renderSteps(); };
    return el("li", { class: "row-gap" }, el("span", {}, `原第 ${step.position} 步`),
      btn("上移", () => move(-1), { cls: "sm", label: `上移第 ${step.position} 步`, disabled: index === 0 }),
      btn("下移", () => move(1), { cls: "sm", label: `下移第 ${step.position} 步`, disabled: index === remaining.length - 1 }));
  }));
  renderSteps();
  const stepsArea = el("div", { class: "field", hidden: true }, el("p", {}, "剩余步骤的执行顺序"), stepList);
  const waitArea = el("div", { class: "field", id: "goal-wait-area", hidden: true },
    el("label", {}, el("span", {}, "等待到期时间（浏览器本地时间）"), el("input", { class: "input", type: "datetime-local", id: "goal-wait-until" })));
  actionSel.addEventListener("change", () => {
    stepsArea.hidden = actionSel.value !== "CONTINUE";
    waitArea.hidden = actionSel.value !== "WAIT";
  });
  form.append(el("label", { class: "field" }, el("span", {}, "操作"), actionSel), stepsArea, waitArea,
    el("p", { class: "muted" }, "继续执行时可以调整已有步骤的顺序。等待时间最多 7 天，到期后可再次决定。放弃后保留已完成记录。"));
  const submit = el("button", { type: "submit", class: "btn primary" }, "确认");
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    const action = actionSel.value;
    if (!actions.includes(action)) { formError(form, "请选择操作。"); return; }
    if (!Number.isInteger(g.version) || g.version < 1) { formError(form, "目标版本不可用，请刷新后重试。"); return; }
    /** @type {{expected_version: number, action: string, step_order?: string[], wait_until?: string}} */
    const body = { expected_version: g.version, action };
    if (action === "CONTINUE") {
      if (!remaining.length || remaining.length > 3) { formError(form, "剩余步骤不可用，请刷新后重试。"); return; }
      body.step_order = remaining.map(step => step.step_id);
    } else if (action === "WAIT") {
      const until = $("#goal-wait-until").value;
      if (!until || !Number.isFinite(Date.parse(until))) { formError(form, "请选择有效的等待到期时间。"); return; }
      body.wait_until = new Date(until).toISOString();
    }
    perform(form, submit, () => api.write("/goals/" + encodeURIComponent(g.goal_id) + "/continuation", body, "POST",
      r => typeof r.state === "string"),
      async r => { closeDialog(); toast({ CONTINUE: "已继续执行", WAIT: "已等待到期", ABANDON: "已放弃目标" }[action] + "（" + statusOf(r.state)[0] + "）"); await loadRoute({ reset: true }); });
  });
  openDialog("续行目标：" + g.topic, form, { wide: true });
}

function reflectionCard(r) {
  const evidence = r.evidence || [];
  return el("div", { class: "reflection-card" },
    el("div", { class: "rc-head" }, el("b", {}, r.content), tag(r.state), el("span", { class: "note" }, fmtDate(r.created_at))),
    evidence.length ? el("details", { class: "refl-evidence" }, el("summary", {}, "证据（" + evidence.length + " 条原话）"),
      el("ul", {}, evidence.map(e => el("li", {}, "\"" + e.exact_quote + "\"")))
    ) : null,
    r.state === "PENDING" ? el("div", { class: "row-gap" },
      btn("作为假设接受", () => reflectionDecision(r.suggestion_id, "ACCEPT_AS_HYPOTHESIS"), { cls: "sm primary", symbol: "check" }),
      btn("需要更多证据", () => reflectionDecision(r.suggestion_id, "NEED_MORE_EVIDENCE"), { cls: "sm" }),
      btn("拒绝", () => reflectionDecision(r.suggestion_id, "REJECT"), { cls: "sm danger", symbol: "close" })) : null);
}

function reflectionDecision(suggestion_id, decision) {
  const labels = { ACCEPT_AS_HYPOTHESIS: "作为假设接受", REJECT: "拒绝", NEED_MORE_EVIDENCE: "需要更多证据" };
  const explanations = {
    ACCEPT_AS_HYPOTHESIS: "将这条建议保存为待验证假设。",
    REJECT: "拒绝这条建议，撤回由它派生的目标，保留已有证据和执行结果。",
    NEED_MORE_EVIDENCE: "将建议标为需要更多证据，并撤回由它派生的目标。",
  };
  const form = el("form", { class: "dlg-form" }, el("p", {}, explanations[decision]));
  const submit = el("button", { type: "submit", class: "btn " + (decision === "REJECT" ? "danger" : "primary") }, labels[decision]);
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    perform(form, submit, () => api.write("/reflections/" + encodeURIComponent(suggestion_id) + "/decision", { decision }, "POST",
      r => typeof r.state === "string"),
      async r => { closeDialog(); toast(statusOf(r.state)[0]); await loadRoute({ reset: true }); });
  });
  openDialog(labels[decision], form);
}

/* ================= 补齐：来源授权、执行任务、生活话题、情绪与偏好、认知分页与定位 =================
   这一段只读取已有接口：/source-grants、/tools、/life/topics、/affect、/interests、
   /expression-learning 和 /cognition 的 before、record_id。写操作都走统一的操作回执，
   只校验回执里的 state，不依赖回执带回业务编号。 */
Object.assign(STATUS, Object.fromEntries(Object.entries({
  WAITING_APPROVAL: ["等你批准", "warn"], EXECUTION_UNCERTAIN: ["执行结果不明", "warn"], DUE: ["已到期", "warn"],
  CLOSED: ["已关闭", "off"], CANDIDATE: ["候选", "warn"], EMERGING: ["正在形成", "run"], STABLE: ["已稳定", "ok"],
  EXPLORING: ["在探索", "run"], FADING: ["渐淡", "off"], SHADOW: ["只记录未执行", "off"], INVALIDATED: ["已作废", "off"],
  WAITING_INPUT: ["等待新输入", "warn"], WAITING_RESOURCE: ["等待资源", "warn"], WAITING_DECISION: ["等你决定", "warn"],
  DEFERRED: ["已推迟", "off"], SCHEDULED: ["已安排", "run"], NOT_RECORDED: ["未记录", "off"],
  APPROVED: ["已批准", "ok"],
}).filter(([k]) => !(k in STATUS))));
Object.assign(REASONS, Object.fromEntries(Object.entries({
  OWNER_REVOKED: "你撤回了", EXECUTION_UNCERTAIN: "执行结果不明", TOOL_SCOPE_CHANGED: "执行内容已变化",
  SOURCE_GRANT_REVOKED: "来源授权已撤回", OWNER_CLOSED: "你关闭了", LIFE_SOURCE_UNAVAILABLE: "来源已失效",
  CONSUMED: "她已经做了决定", LIFE_NEW_INPUT: "你发来了新消息，换成新话题", SOURCE_WITHDRAWN: "来源被撤回",
  GOAL_RESULT: "由目标结果接续", LIFE_DRAFT_EXPIRED: "草稿已过期",
}).filter(([k]) => !(k in REASONS))));
const failNote = (what, error) => el("div", { class: "alert bad" }, icon("warn"), el("span", {}, what + "暂时读不到。" + (error ? errorText(error) : "")));
/** allSettled 的结果：成功给值，失败给 { error }，取消请求时整体中止 */
function settled(result) {
  if (result.status === "fulfilled") return result.value;
  if (result.reason?.name === "AbortError") throw result.reason;
  return { error: result.reason };
}

/* ---------- 来源授权（阅读与文件页的第二个标签） ---------- */
const GRANT_SOURCE = { web: "网页", bilibili: "B 站", heybox: "小黑盒", simulation: "模拟来源" };
const grantName = g => GRANT_SOURCE[g.source] || g.source || "未记录来源";
function grantState(g) {
  if (g.forgotten_at) return ["FORGOTTEN", "已遗忘"];
  if (g.revoked_at) return ["REVOKED", "已撤回"];
  if (valid(g.expires_at) && Date.parse(g.expires_at) <= Date.now()) return ["EXPIRED", "已过期"];
  return ["ENABLED", "有效"];
}
function grantsView(grants) {
  if (!grants || grants.error) return failNote("来源授权", grants?.error);
  const items = grants.items || [];
  if (!items.length) return empty("还没有来源授权", "读取程序登记授权后会出现在这里。授权只决定能不能读，不包含账号或密码。");
  return el("div", {},
    el("p", { class: "note mb" }, "授权由读取程序登记，这里只能撤回或遗忘，不能新建或延长。"),
    el("section", { class: "sheet table-wrap" }, el("table", { class: "t" },
      el("thead", {}, el("tr", {}, el("th", {}, "来源"), el("th", {}, "状态"), el("th", {}, "版本"), el("th", {}, "有效期至"), el("th", { class: "r" }, "操作"))),
      el("tbody", {}, items.map(g => {
        const [st, label] = grantState(g);
        const expiry = g.source === "bilibili" && g.expires_at === null ? "直到撤销" : fmtDate(g.expires_at);
        return el("tr", { class: "static" },
          el("td", { class: "title-cell" }, grantName(g), el("small", {}, "读取程序 " + (g.adapter_id || "—") + "，编号 " + shortId(g.grant_id))),
          el("td", {}, tag(st, label)),
          el("td", { class: "num" }, "第 " + count(g.epoch) + " 版"),
          el("td", { class: "muted" }, g.forgotten_at ? "遗忘于 " + fmtDate(g.forgotten_at) : g.revoked_at ? "撤回于 " + fmtDate(g.revoked_at) : expiry),
          el("td", { class: "r" }, el("span", { class: "row-gap end" },
            !g.revoked_at && !g.forgotten_at ? btn("撤回", () => grantAction(g, "revoke"), { cls: "sm" }) : null,
            !g.forgotten_at ? btn("遗忘", () => grantAction(g, "forget"), { cls: "sm danger" }) : null)));
      })))),
    items.length >= (grants.limit || 100) ? el("p", { class: "note mt" }, "只显示最近 " + count(grants.limit || 100) + " 条授权。") : null);
}
function grantAction(g, action) {
  const forget = action === "forget";
  const form = el("form", { class: "dlg-form" },
    el("p", {}, grantName(g) + "的读取授权，编号 " + shortId(g.grant_id)),
    el("ul", { class: "consequence" }, forget ? [
      el("li", {}, "之后不再按这个授权读取，进行中的读取会取消"),
      el("li", {}, "清除按这个授权读到的内容，以及由这些内容产生的记忆"),
      el("li", {}, "从旧备份恢复时也会重新执行这次遗忘"),
      el("li", {}, "已经发到 QQ 的消息无法撤回")] : [
      el("li", {}, "之后不再按这个授权读取，进行中的读取会取消"),
      el("li", {}, "已经读到的内容和由此产生的记忆保留，要一并清除请用“遗忘”"),
      el("li", {}, "撤回后这个授权不能恢复，需要读取程序重新登记")]));
  const submit = el("button", { type: "submit", class: "btn " + (forget ? "danger solid" : "primary") }, forget ? "遗忘这个授权" : "撤回授权");
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    perform(form, submit, () => api.write("/source-grants/" + encodeURIComponent(g.grant_id), { action }, "POST", r => typeof r.state === "string"),
      async () => { closeDialog(); toast(forget ? "已遗忘这个授权" : "已撤回授权"); await loadRoute({ reset: true }); });
  });
  openDialog(forget ? "遗忘这个来源授权？" : "撤回这个来源授权？", form);
}

/* ---------- 执行任务 ---------- */
const TOOL_CAPS = { "windows.read_text": ["读取文件", "只读"], "windows.write_text": ["写入文件", "会改动文件"], "dsh.run": ["运行 DSH 任务", "会执行操作"] };
const TOOL_OPEN = ["WAITING_APPROVAL", "QUEUED", "LEASED", "RUNNING"];
PAGES.tools = data => {
  if (data.error) return el("div", {}, head("执行任务", ""), failNote("执行任务", data.error));
  const tasks = data.tasks || [], waiting = tasks.filter(t => t.state === "WAITING_APPROVAL").length;
  return el("div", {}, head("执行任务", "她提出的读写文件或 DSH 任务。会改动内容的任务要你看过具体内容并批准后才会执行，批准只对这里显示的这一份内容有效。"),
    data.enabled ? null : el("div", { class: "alert" }, icon("warn"), el("span", {}, "执行任务在部署配置里没有开启，现在不能批准或拒绝；已有任务仍可查看和撤回。")),
    waiting ? el("p", { class: "note mb" }, waiting + " 个任务等你批准。") : null,
    tasks.length ? el("div", { class: "stack" }, tasks.map(t => toolCard(t, data.enabled)))
      : empty("还没有执行任务", "她提出读写文件或运行 DSH 的任务后，会出现在这里。"));
};
function scopeText(label, v) {
  if (!v) return null;
  if (v.state !== "RECORDED") return kv(label, "含有可能敏感的内容，不在这里显示（" + count(v.chars) + " 字）");
  return el("div", { class: "scope-field" }, el("span", { class: "note" }, label + "，" + count(v.chars) + " 字"), el("pre", { class: "scope-text" }, v.value));
}
function scopeView(scope) {
  if (!scope || scope.state !== "RECORDED") {
    const text = scope?.sources_live === false ? "依据的私聊消息已经失效或被清除，执行内容不再显示，可以撤回这个任务。"
      : { ERASED: "执行内容已经随来源一起清除。", MISMATCH: "保存的执行内容和批准摘要对不上，不能批准，请撤回这个任务。" }[scope?.state] || "执行内容读不到。";
    return el("div", { class: "alert" + (scope?.state === "MISMATCH" ? " bad" : "") }, icon("warn"), el("span", {}, text));
  }
  const L = scope.limits || {};
  return el("div", { class: "scope-box" },
    kv("工作区", scope.workspace || "—"),
    scope.path ? kv("路径", scope.path.state === "RECORDED" ? el("code", {}, scope.path.value) : "含有可能敏感的内容，不在这里显示") : null,
    scopeText("写入内容", scope.text), scopeText("任务说明", scope.prompt),
    el("div", { class: "scope-limits" }, [["费用上限", usd(L.max_cost_micro_usd)], ["模型调用", count(L.max_model_calls) + " 次"], ["输入上限", bytes(L.max_input_bytes)],
      ["输出上限", count(L.max_output_tokens) + " token"], ["时限", duration(L.timeout_seconds)]].map(([k, v]) => el("span", {}, el("small", {}, k), el("b", {}, v)))),
    scope.sources_live ? el("p", { class: "note" }, "依据你的 " + count(scope.source_count) + " 条私聊消息，来源仍然有效。")
      : el("div", { class: "alert" }, icon("warn"), el("span", {}, "依据的私聊消息已经失效或被清除，批准和拒绝都会被拒绝，可以撤回这个任务。")));
}
function toolCard(t, enabled) {
  const [name, risk] = TOOL_CAPS[t.capability] || ["其他工具", t.risk_level ? "会改动" : "只读"];
  const reviewable = t.state === "WAITING_APPROVAL" && t.scope?.state === "RECORDED" && t.scope.sources_live;
  const visible = reviewable && ["path", "text", "prompt"].every(k => !t.scope[k] || t.scope[k].state === "RECORDED");
  return el("article", { class: "sheet tool-card" }, el("div", { class: "sheet-body" },
    el("div", { class: "tool-head" }, el("div", {}, el("h3", {}, name),
      el("p", { class: "note" }, risk + "，提出于 " + fmtDate(t.created_at) + (t.dispatched_at ? "，开始执行于 " + fmtDate(t.dispatched_at) : ""))), tag(t.state)),
    t.state === "UNKNOWN" ? el("div", { class: "alert bad" }, icon("warn"), el("span", {}, "执行结果不明：执行端可能已经动手。不会自动重试，相关费用会一直占用额度，请到执行端核对。")) : null,
    t.last_error_code ? el("p", { class: "note mb" }, "原因：" + reasonText(t.last_error_code)) : null,
    scopeView(t.scope),
    reviewable && !visible ? el("p", { class: "note" }, "具体执行内容没有完整显示，不能批准；可以拒绝或撤回任务。") : null,
    el("div", { class: "tool-foot" }, el("span", { class: "note" }, "预留 " + usd(t.reserved_micro_usd) + "，已计 " + usd(t.billed_micro_usd) + "，编号 " + shortId(t.task_id)),
      el("span", { class: "row-gap" },
        t.state === "WAITING_APPROVAL" ? btn("批准", () => toolDecision(t, "APPROVE"), { cls: "sm primary", symbol: "check", disabled: !enabled || !visible }) : null,
        t.state === "WAITING_APPROVAL" ? btn("拒绝", () => toolDecision(t, "REJECT"), { cls: "sm", disabled: !enabled || !reviewable }) : null,
        TOOL_OPEN.includes(t.state) ? btn("撤回", () => toolRevoke(t), { cls: "sm danger" }) : null))));
}
function toolDecision(t, decision) {
  const approve = decision === "APPROVE", [name] = TOOL_CAPS[t.capability] || ["这个任务"];
  const check = approve ? el("input", { type: "checkbox", id: "tool-seen" }) : null;
  const form = el("form", { class: "dlg-form" },
    approve ? el("ul", { class: "consequence" }, el("li", {}, "批准只对刚才显示的这一份内容有效，内容一变就需要重新批准"),
      el("li", {}, "批准后一小时内会执行，期间可以撤回"), el("li", {}, "执行会占用费用额度，上限见任务卡片"))
      : el("p", {}, "拒绝后这个任务不会执行，预留的额度会释放。"),
    approve ? el("label", { class: "ck" }, check, el("span", {}, "我已经看过" + name + "的具体内容")) : null);
  const submit = el("button", { type: "submit", class: "btn " + (approve ? "primary" : "danger") }, approve ? "批准执行" : "拒绝");
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    if (check && !check.checked) { formError(form, "请先确认看过具体内容。"); return; }
    perform(form, submit, () => api.write("/tools/" + encodeURIComponent(t.task_id) + "/decision", { decision, scope_sha256: t.scope_sha256 }, "POST", r => typeof r.state === "string"),
      async () => { closeDialog(); toast(approve ? "已批准，等待执行" : "已拒绝"); await loadRoute({ reset: true }); });
  });
  openDialog(approve ? "批准" + name + "？" : "拒绝" + name + "？", form);
}
function toolRevoke(t) {
  const started = ["LEASED", "RUNNING"].includes(t.state);
  const form = el("form", { class: "dlg-form" }, el("ul", { class: "consequence" },
    started ? [el("li", {}, "执行端可能已经开始，撤回不能保证没有动手"), el("li", {}, "正在执行的会记为结果不明，之后送回的结果不会被接受"), el("li", {}, "已经产生的费用保留")]
      : [el("li", {}, "这个任务不会再执行"), el("li", {}, "已有的批准一并作废")]));
  const submit = el("button", { type: "submit", class: "btn danger solid" }, "撤回任务");
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    perform(form, submit, () => api.write("/tools/" + encodeURIComponent(t.task_id) + "/revoke", {}, "POST", r => typeof r.state === "string"),
      async () => { closeDialog(); toast("已撤回"); await loadRoute({ reset: true }); });
  });
  openDialog("撤回这个执行任务？", form);
}

/* ---------- 生活话题（日记与成长页） ---------- */
const TOPIC_KIND = { conversation: "对话话题", reading: "阅读话题" };
const LIFE_ACTION = { SKIP: "这次不跟进", CONTINUE: "继续聊", EXPLORE: "去查一查", FOLLOW_UP: "回头再问", SHARE: "分享给你", DEFER: "推迟" };
const LIFE_DEST = { NONE: "只记录", PRIVATE_DIARY: "写进日记", PRIVATE_MESSAGE_DRAFT: "私聊草稿", QZONE_DRAFT: "QQ 空间草稿" };
function topicsSheet(topics) {
  if (!topics || topics.error) return sheet("生活话题", failNote("生活话题", topics?.error));
  const items = topics.items || [], candidates = topics.candidate_sources || [];
  return sheet("生活话题", [
    topics.mode === "OFF" ? el("p", { class: "note mb" }, "生活循环没有开启，下面只显示已有记录。") : null,
    items.length ? el("div", { class: "topics" }, capped("topics", byRank(items, t => ["WAITING", "DUE"].includes(t.state) ? 0 : t.state === "CLOSED" ? 1 : 2).map(topicCard), 3, "div")) : el("p", { class: "note" }, "还没有生活话题。"),
    candidates.length ? el("p", { class: "note mt" }, "另有 " + candidates.length + " 条来源可以成为新话题，由她在下一轮决定。") : null]);
}
function topicCard(t) {
  const sources = t.sources || [], results = t.results || [];
  const owner = sources.filter(s => s.kind === "owner_message"), open = ["WAITING", "DUE"].includes(t.state);
  const closeable = open || t.state === "CLOSED" && t.reason_code === "CONSUMED";
  return el("article", { class: "topic" },
    el("div", { class: "topic-head" }, el("b", {}, TOPIC_KIND[t.kind] || "话题"), tag(t.state)),
    el("p", { class: "note" }, (open ? (t.state === "DUE" ? "已到期，" : "到期于 " + fmtDate(t.due_at) + "，") + "有效期至 " + fmtDate(t.expires_at)
      : t.reason_code ? reasonText(t.reason_code) : "") + (sources.length ? "" : (t.state === "EXPIRED" ? "；来源已过期或被清除" : "；没有可以显示的来源"))),
    sources.length ? el("ul", { class: "topic-src" }, sources.slice(0, 4).map(s => el("li", {},
      el("span", { class: "chip" }, s.kind === "owner_message" ? "你说" : "她读到"), el("span", { class: "txt" }, s.text),
      safeUrl(s.url) ? el("a", { href: safeUrl(s.url), target: "_blank", rel: "noopener noreferrer" }, "原文") : null,
      el("small", { class: "note" }, fmtDate(s.occurred_at)))))
      : null,
    sources.length > 4 ? el("p", { class: "note" }, "还有 " + (sources.length - 4) + " 条来源") : null,
    results.length ? el("div", { class: "topic-res" }, results.map(r => el("div", { class: "topic-r" },
      el("div", { class: "row-gap" }, el("b", {}, LIFE_ACTION[r.action] || "其他决定"), el("span", { class: "chip" }, LIFE_DEST[r.destination] || "未记录去向"), tag(r.state)),
      r.reason ? el("p", {}, r.reason) : null, r.text ? el("blockquote", {}, r.text) : null,
      r.goal ? el("p", { class: "note" }, "关联目标：" + statusOf(r.goal.state)[0] + "，", linkTo("thinking/goal:" + r.goal.goal_id, "查看目标")) : null))) : null,
    closeable ? (owner.length ? el("div", { class: "row-gap mt" }, btn("关闭话题", () => topicClose(t, owner), { cls: "sm", symbol: "close" }))
      : el("p", { class: "note faint" }, "没有你的原话，不能在这里关闭；在私聊里说不想聊这个，她会自己收起。")) : null);
}
function topicClose(t, owner) {
  const sel = el("select", { class: "input", id: "topic-source", required: true },
    owner.map(s => el("option", { value: s.event_id }, fmtDate(s.occurred_at) + "：" + String(s.text).slice(0, 40))));
  const form = el("form", { class: "dlg-form" },
    el("ul", { class: "consequence" }, el("li", {}, "她不再跟进这个话题"), el("li", {}, "这个话题下的决定和草稿会作废，排队中的处理会取消"),
      el("li", {}, "已经发出的消息不受影响")),
    el("label", { class: "field" }, el("span", {}, "以哪句话作为关闭依据"), sel));
  const submit = el("button", { type: "submit", class: "btn danger solid" }, "关闭话题");
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    perform(form, submit, () => api.write("/life/topics/" + encodeURIComponent(t.topic_id) + "/close", { source_event_id: sel.value }, "POST", r => typeof r.state === "string"),
      async () => { closeDialog(); toast("已关闭话题"); await loadRoute({ reset: true }); });
  });
  openDialog("关闭这个话题？", form);
}

/* ---------- 情绪与偏好 ---------- */
const AFFECT_DIMS = {
  interaction_tone: ["回复风格", { neutral: "默认", concise: "简短一点", detailed: "详细一点" }],
  engagement: ["聊天意愿", { neutral: "默认", pause: "先不聊这个", continue: "继续聊" }],
  cognitive_load: ["讲解方式", { normal: "默认", stepwise: "分步骤" }],
  novelty_interest: ["新话题", { unknown: "没表态", open: "想试新话题" }],
};
const AFFECT_REASON = { OWNER_REQUESTED_CONCISE: "你说过“简短一点”", OWNER_REQUESTED_DETAIL: "你说过“详细一点”", OWNER_REQUESTED_STEPS: "你说过“分步骤说”",
  OWNER_REQUESTED_PAUSE: "你说过“先不聊了”", OWNER_REQUESTED_CONTINUE: "你说过“继续聊”", OWNER_REQUESTED_NOVELTY: "你说过“换个新话题”" };
const dimLabel = d => (AFFECT_DIMS[d] || [d || "其他"])[0];
const dimValue = (d, v) => (AFFECT_DIMS[d]?.[1] || {})[v] || v || "—";
PAGES.self = data => el("div", {}, head("情绪与偏好", "交流偏好来自你的明确反馈；她的心情来自有依据的互动和已完成经历，兴趣单独记录。"),
  el("div", { class: "stack" }, affectSheet(data.affect),
    moodSheet(data.mood),
    el("div", { class: "grid-2" }, preferenceSheet(data.interests), interestSheet(data.interests)),
    expressionSheet(data.expression), styleSheet(data.styles)));
const STYLE_TAGS = { daily_share: "日常分享", complaint: "吐槽", light_humor: "轻松玩笑", opinion: "交换看法", help_request: "求助",
  lull: "冷场", closing: "收尾", emotion: "情绪", disagreement: "意见不同" };
const STYLE_RISKS = { circle_meme: "圈子梗", regional_meme: "地域梗", misattribution: "张冠李戴", claims_experience: "声称亲身经历",
  mocks_third_party: "嘲讽他人", needs_relationship: "要很熟才合适" };
function styleSheet(x) {
  if (!x || x.error) return sheet("表达候选", failNote("表达候选", x?.error));
  const items = x.items || [];
  const field = (label, text) => text ? el("div", { class: "kv" }, el("span", {}, label), el("span", {}, text)) : null;
  return sheet("表达候选", [
    el("p", { class: "note mb" }, "从评论区提炼的说话方式。只有你批准的候选才会用在聊天里" +
      (x.live_enabled ? "，目前已开启。" : "，需要先在配置页打开“聊天时参考已批准的表达”。") + (x.enabled ? "" : "提炼没有开启，不会产生新候选。")),
    items.length ? el("ul", { class: "rows" }, items.map(c => el("li", { class: "block" },
      el("div", { class: "row-gap" }, el("b", {}, (c.context_tags || []).map(t => STYLE_TAGS[t] || t).join("、") || "表达"), tag(c.state), el("span", { class: "num note" }, c.short_id),
        el("span", { class: "note" }, "来源 " + count(c.source_count) + " 条，" + fmtDate(c.expires_at) + " 到期")),
      field("留意", c.attention_target), field("回应", c.response_action), field("适合", c.appropriate_when), field("避免", c.avoid_when),
      (c.risk_flags || []).length || c.disagreement ? el("p", { class: "note" }, "带风险标记" + ((c.risk_flags || []).length ? "（" + c.risk_flags.map(r => STYLE_RISKS[r] || r).join("、") + "）" : "") +
        (c.disagreement ? "，来源理解不一致" : "") + "，只能拒绝。") : null,
      c.state === "CANDIDATE" ? el("div", { class: "row-gap mt" },
        c.approvable ? btn("批准", () => styleDecision(c, "APPROVE"), { cls: "sm primary", symbol: "check" }) : null,
        btn("拒绝", () => styleDecision(c, "REJECT"), { cls: "sm danger", symbol: "close" })) : null)))
      : el("p", { class: "note" }, "目前没有待审或已批准的表达候选。")], countNote(items.length, 40));
}
function styleDecision(c, decision) {
  const approve = decision === "APPROVE";
  const reason = el("input", { class: "input", maxlength: "200", "aria-label": "备注", placeholder: approve ? "备注，可以不填" : "拒绝原因，可以不填" });
  const form = el("form", { class: "dlg-form" }, el("p", {}, approve ? "批准后这条表达会冻结为一个版本，开启后用于聊天。" : "拒绝后这条候选不再出现。"), reason);
  const submit = el("button", { type: "submit", class: "btn " + (approve ? "primary" : "danger") }, approve ? "批准" : "拒绝");
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    perform(form, submit, () => api.write("/style-candidates/" + encodeURIComponent(c.pattern_sha256) + "/decision",
      { decision, reason: reason.value.trim() }, "POST", r => typeof r.state === "string"),
      async r => { closeDialog(); toast(statusOf(r.state)[0]); await loadRoute({ reset: true }); });
  });
  openDialog((approve ? "批准" : "拒绝") + "表达候选 " + c.short_id, form);
}
function affectSheet(a) {
  if (!a || a.error) return sheet("你的交流偏好", failNote("交流偏好", a?.error));
  const evidence = a.evidence || [];
  return sheet("你的交流偏好", [
    a.enabled ? null : el("p", { class: "note mb" }, "交流偏好调整没有开启，下面是默认状态。重置仍然可以使用。"),
    el("div", { class: "affect-dims" }, Object.keys(AFFECT_DIMS).map(d => {
      const v = a.state?.[d], ev = evidence.find(x => x.dimension === d);
      return el("div", { class: "affect-dim" + (ev ? " set" : "") }, el("small", {}, dimLabel(d)), el("b", {}, dimValue(d, v)),
        el("span", { class: "note" }, ev ? (AFFECT_REASON[ev.reason_code] || reasonText(ev.reason_code)) + "，到 " + fmtDate(ev.expires_at) + " 失效" : "没有明确反馈"));
    })),
    el("p", { class: "note mt" }, evidence.length ? "每条反馈都来自你的私聊原话，到期后自动恢复默认。" : "最近没有明确反馈。",
      a.reset_at ? "上次重置在 " + fmtDate(a.reset_at) + "。" : ""),
    el("div", { class: "row-gap mt" }, btn("重置交流偏好", affectReset, { cls: "sm", symbol: "refresh" }))]);
}
const MOOD_REASONS = { HELPFUL_FEEDBACK: "明确的正向互动反馈", THANKS_FOR_HELP: "对已送达帮助的感谢",
  UNHELPFUL_FEEDBACK: "明确的改进反馈", COMPLETED_READ_REVIEW: "完成线上阅读后的评价",
  SHARED_IDEA_INTEREST: "主人明确觉得刚才的想法有趣", DISCUSSION_CURIOSITY: "主人明确想继续探讨问题",
  PERSPECTIVE_SURPRISE: "刚才的新角度带来明确反馈", VIEW_DISAGREEMENT: "主人明确提出观点分歧",
  DISCUSSION_PROGRESS: "主人明确反馈讨论有进展",
  VERIFIED_GOAL_COMPLETION: "有执行证据的目标完成", REVIEWED_GOAL_COMPLETION: "完成目标与阅读评价",
  OWNER_AFFECTION: "主人表达亲近或在乎（模型判断）", OWNER_HURT_BY_REPLY: "主人被刚才的回复伤到（模型判断）",
  OWNER_DISTRESS: "主人难过或压力大（模型判断）", OWNER_EXPECTATION: "主人表达期待或想念（模型判断）",
  OWNER_PLAYFUL: "主人在逗你玩（模型判断）" };
const padText = value => ["pleasure", "arousal", "dominance"].map(k => Number(value?.[k] || 0).toFixed(3)).join(" / ");
function moodSheet(m) {
  if (!m || m.error) return sheet("小肥鱼的心情", failNote("心情", m?.error));
  const signals = m.signals || [];
  return sheet("小肥鱼的心情", [
    el("p", { class: "note mb" }, !m.enabled ? "动态心情未开启，可在配置页开启。" : m.paused ? "心情滤镜已暂停。恢复后根据仍有效的来源重新计算。" : !m.active ? "当前授权不允许使用心情滤镜。" : "表达参考会自然衰减；数值不代表真实情绪体验或固定人格。"),
    el("p", { class: "note mb" }, "以下依次为愉悦度 / 唤醒度 / 支配感；0 表示没有方向性参考，也可能缺少依据。"),
    kv("即时心情 · 半衰期 3 小时", padText(m.immediate)),
    kv("近期底色 · 半衰期 3 天", padText(m.undertone)),
    kv("当前表达参考", padText(m.expression)),
    el("details", { class: "mt" }, el("summary", {}, "最近趋势与变化来源"),
      (m.trend || []).map(t => kv(fmtDate(t.at), "即时 " + padText(t.immediate) + "；底色 " + padText(t.undertone))),
      signals.length ? signals.slice(-8).reverse().map(s => el("div", { class: "mt" },
        el("b", {}, MOOD_REASONS[s.reason] || s.reason),
        el("p", { class: "note" }, fmtDate(s.at) + " · " + s.event_id + " · 初始变化 " + padText(Object.fromEntries(["pleasure", "arousal", "dominance"].map((k, i) => [k, s.delta[i]])))),
        el("p", { class: "note" }, "即时影响剩余约 " + (100 * Math.pow(2, -(Date.parse(m.evaluated_at) - Date.parse(s.at)) / 10800000)).toFixed(1) + "%")))
        : el("p", { class: "note mt" }, "暂无有效的变化来源。")),
    m.truncated ? el("p", { class: "note mt" }, "候选来源超过上限，底色暂不采用；显示有限的近期参考。") : null,
    m.reset_at ? el("p", { class: "note mt" }, "最近清除：" + fmtDate(m.reset_at)) : null,
    el("p", { class: "note mt" }, "心绪手札保存在日记与反思中，仍以原始来源为依据。"),
    el("div", { class: "row-gap mt" },
      btn(m.paused ? "恢复心情滤镜" : "暂停心情滤镜", () => moodControl(m.paused ? "resume" : "pause"), { cls: "sm" }),
      btn("重新计算心情", () => moodControl("recompute"), { cls: "sm", symbol: "refresh" }),
      btn("清除心情", () => moodControl("clear"), { cls: "sm danger" }))]);
}
function moodControl(action) {
  const titles = { pause: "暂停心情滤镜", resume: "恢复心情滤镜", recompute: "重新计算心情", clear: "清除心情" };
  const descriptions = { pause: "暂停心情对表达的影响，原始互动和阅读仍照常记录。", resume: "从仍有效的原始来源重建，按实际经过的时间衰减。", recompute: "重新核验来源并计算当前状态，保留清除时间边界。", clear: "现在及以前的来源不再影响心情，旧备份恢复也保留这次清除。日记、聊天和记忆仍保留。" };
  const form = el("form", { class: "dlg-form" }, el("p", {}, descriptions[action]));
  const submit = el("button", { type: "submit", class: "btn " + (action === "clear" ? "danger" : "primary") }, titles[action]);
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    perform(form, submit, () => api.write("/mood/control", { action }, "POST", r => typeof r.state === "string"),
      async () => { closeDialog(); toast("已" + titles[action]); await loadRoute({ reset: true }); });
  });
  openDialog(titles[action], form);
}
function affectReset() {
  const form = el("form", { class: "dlg-form" }, el("ul", { class: "consequence" },
    el("li", {}, "到现在为止的明确反馈都不再生效，之后只看新的原话"), el("li", {}, "表达习惯学习也从现在开始重新累计"),
    el("li", {}, "不会删除聊天记录和记忆"), el("li", {}, "从旧备份恢复时也会保持这次重置")));
  const submit = el("button", { type: "submit", class: "btn danger solid" }, "重置");
  form.append(el("div", { class: "dlg-acts" }, submit));
  form.addEventListener("submit", e => {
    e.preventDefault();
    perform(form, submit, () => api.write("/affect/reset", { confirm: true }, "POST", r => typeof r.state === "string"),
      async () => { closeDialog(); toast("已重置交流偏好"); await loadRoute({ reset: true }); });
  });
  openDialog("重置交流偏好？", form);
}
function evidenceDates(list) {
  const days = [...new Set((list || []).map(e => localDay(e.occurred_at)).filter(Boolean))].sort();
  return days.length ? (days.length === 1 ? days[0] : days[0] + " 到 " + days.at(-1)) : "日期未记录";
}
function preferenceSheet(p) {
  if (!p || p.error) return sheet("你的偏好", failNote("偏好档案", p?.error));
  const items = p["owner.preference"] || [];
  return sheet("你的偏好", items.length ? el("ul", { class: "rows" }, capped("prefs", items.map(m => el("li", { class: "block" }, el("b", {}, m.content),
    el("span", { class: "meta-line" }, el("span", { class: "chip" }, "来自你的 " + (m.evidence || []).length + " 条原话"), el("span", { class: "note" }, evidenceDates(m.evidence)))))
    , 5)) : el("p", { class: "note" }, "还没有确认过的偏好。在记忆页确认一条偏好后会出现在这里。"), countNote(items.length, 40));
}
function interestSheet(p) {
  if (!p || p.error) return sheet("她的兴趣", failNote("兴趣档案", p?.error));
  const items = p["self.interest"] || [];
  const rank = { STABLE: 0, EMERGING: 1, EXPLORING: 2, FADING: 3 };
  return sheet("她的兴趣", [items.length ? el("ul", { class: "rows" }, capped("interests", byRank(items, i => rank[i.status] ?? 4).map(i => el("li", { class: "block" },
    el("div", { class: "row-gap" }, el("b", {}, i.topic), tag(i.status)),
    el("span", { class: "meta-line" }, el("span", { class: "note" }, "读完 " + count(i.independent_sessions) + " 篇，分布在 " + count(i.independent_days) + " 天，证据覆盖 " + Math.round((i.confidence || 0) * 100) + "%")),
    (i.evidence || []).length ? el("details", { class: "ev" }, el("summary", {}, "看读过的内容"), el("ul", {}, i.evidence.slice(0, 8).map(e => el("li", {},
      safeUrl(e.canonical_url) ? el("a", { href: safeUrl(e.canonical_url), target: "_blank", rel: "noopener noreferrer" }, new URL(safeUrl(e.canonical_url)).hostname) : el("span", {}, "来源"),
      el("span", { class: "note" }, " " + fmtDate(e.occurred_at) + "，" + statusOf(String(e.evidence_level || "").replace(/^SIMULATED_/, ""))[0] + (String(e.evidence_level).startsWith("SIMULATED_") ? "（模拟）" : "")))))) : null)), 4))
    : el("p", { class: "note" }, "还没有兴趣记录。她完成自己的阅读目标后才会累计。"),
    el("p", { class: "note mt" }, "兴趣只说明她自己读过什么，不会当成你的偏好，也不会授予任何权限。")], countNote(items.length, 40));
}
function expressionSheet(x) {
  if (!x || x.error) return sheet("表达习惯学习", failNote("表达习惯学习", x?.error));
  const items = x.items || [];
  return sheet("表达习惯学习", [
    el("p", { class: "note mb" }, "稳定偏好可在表达习惯学习和表达能力开启、当前授权有效时用于回复；短时反馈优先。这里展示当前学习结果。"),
    items.length ? el("ul", { class: "rows" }, items.map(i => el("li", { class: "block" },
      el("div", { class: "row-gap" }, el("b", {}, dimLabel(i.dimension) + "：" + dimValue(i.dimension, i.value)), tag(i.status)),
      el("span", { class: "meta-line" }, el("span", { class: "note" }, "有 " + count(i.independent_days) + " 天的明确反馈，证据覆盖 " + Math.round((i.confidence || 0) * 100) + "%" + (i.strength ? "，影响强度 " + i.strength : ""))),
      el("div", { class: "days" }, (i.local_dates || []).map(d => el("span", { class: "chip" }, d))))))
      : el("p", { class: "note" }, "还没有可以学习的反馈。你说“简短一点”“分步骤说”这类话之后会开始累计。"),
    x.truncated ? el("p", { class: "note mt" }, "反馈太多，只看了最近 500 条，暂时不会判定为稳定。") : null,
    el("p", { class: "note mt" }, "这里是按保留期内的原话重新算出的当前结果，不另外保存逐日变化。出现相反的反馈或重置交流偏好后，会从那时重新累计。")]);
}

/* ---------- 认知记录：分页、按编号定位、跨记录跳转、目标步骤 ---------- */
const COG_LIST = ["wake", "life", "goal", "diary", "reflection", "nightly", "model", "rem"];
/** 读取一页认知记录：失败时保留错误，不当成空记录 */
async function cognitionPage(kind, signal, query = "") {
  return api.get("/cognition?kind=" + kind + "&limit=30" + query, signal);
}
/** #thinking/goal:goal_xxx 定位到一条记录；不在第一页时单独读取并放在最前面 */
async function locateCognition(data, signal) {
  const m = /^([a-z]+):(.{1,160})$/.exec(state.sub || "");
  if (!m || !COG_LIST.includes(m[1])) return data;
  const [, kind, id] = m;
  state.cogKind = kind; state.cogSel = kind + ":" + id; state.cogTab = "overview";
  const page = data[kind] || { items: [] };
  if (!(page.items || []).some(i => String(i.id) === id)) {
    const one = await cognitionPage(kind, signal, "&record_id=" + encodeURIComponent(id));
    if (!(one.items || []).length) data.locate = { kind, id, missing: true, epoch: one.privacy_epoch };
    // 单独定位到的记录放在 located 里：items 保持服务端分页顺序，排序与序号按保存时间计算
    else { data[kind] = { ...page, located: one.items }; data.locate = { kind, id, found: true, epoch: one.privacy_epoch }; }
  }
  return data;
}
async function moreCognition(kind) {
  const page = state.data?.[kind];
  if (!page?.next_cursor || state.cogMore) return;
  state.cogMore = kind; rerender();
  const epoch = state.epoch, generation = state.generation;
  try {
    const next = await cognitionPage(kind, state.controller?.signal, "&before=" + encodeURIComponent(page.next_cursor));
    if (epoch !== state.epoch || generation !== state.generation || state.route !== "thinking" || !state.data) return;
    if (next.privacy_epoch !== page.privacy_epoch) { await loadRoute({ reset: true }); return; }
    const seen = new Set(page.items.map(i => String(i.id)));
    state.data[kind] = { ...next, located: page.located, items: [...page.items, ...(next.items || []).filter(i => !seen.has(String(i.id)))] };
  } catch (error) {
    // 游标失效或隐私版本变化：丢弃已加载的分页，从首页重新读取
    if (["COGNITION_CURSOR_UNAVAILABLE", "PRIVACY_CHANGED"].includes(error.code) && epoch === state.epoch && generation === state.generation) {
      state.cogMore = null; await loadRoute({ reset: true });
      notice(error.code === "PRIVACY_CHANGED" ? "记录的隐私版本变了，已按当前版本重新读取。" : "翻页位置已失效，已从最新记录重新读取。", false); return;
    }
    if (error.name !== "AbortError" && epoch === state.epoch && generation === state.generation) {
      notice("更早的" + (COGNITION_KINDS[kind]?.name || "") + "记录没有读到。" + errorText(error), false);
    }
  } finally {
    if (epoch === state.epoch && generation === state.generation) { state.cogMore = null; rerender(); }
  }
}
function cognitionMore() {
  const kinds = state.cogKind === "all" ? COG_LIST : [state.cogKind];
  const more = kinds.filter(k => state.data?.[k]?.next_cursor);
  const failed = COG_LIST.filter(k => state.data?.[k]?.error);
  return [
    failed.length ? el("div", { class: "alert bad mt" }, icon("warn"), el("span", {}, failed.map(k => COGNITION_KINDS[k]?.name || k).join("、") + "暂时读不到，列表里缺少这部分记录。")) : null,
    more.length ? el("div", { class: "row-gap mt" }, more.map(k => btn(state.cogMore === k ? "正在读取…" : "更早的" + (COGNITION_KINDS[k]?.name || "记录"),
      () => moreCognition(k), { cls: "sm quiet", disabled: !!state.cogMore }))) : null];
}
function cognitionFinder() {
  const kind = el("select", { class: "input sm-select", "aria-label": "记录类型" }, COG_LIST.map(k => el("option", { value: k, selected: state.cogKind === k }, COGNITION_KINDS[k]?.name || k)));
  const id = el("input", { class: "input", placeholder: "按编号查找", "aria-label": "记录编号", maxlength: "160" });
  return el("form", { class: "cog-find", onsubmit: e => { e.preventDefault(); const v = id.value.trim(); if (v) location.hash = "thinking/" + kind.value + ":" + encodeURIComponent(v); } },
    kind, id, btn("查找", () => {}, { cls: "sm", type: "submit", symbol: "search" }));
}
function locateNote(data) {
  const l = data.locate; if (!l || l.found) return null;
  return el("div", { class: "alert" + (l.error ? " bad" : "") }, icon("warn"),
    el("span", {}, (COGNITION_KINDS[l.kind]?.name || "记录") + " " + shortId(l.id) + (l.error ? " 暂时读不到。" + errorText(l.error) : " 没有找到，可能已被清除或来源已失效。")));
}
/** 记录之间的跳转：只链到能在控制台打开的记录 */
function cognitionLinks(sel) {
  const links = [];
  if (sel.goal_id) links.push(linkTo("thinking/goal:" + encodeURIComponent(sel.goal_id), "查看关联目标"));
  if (sel.model_request_id) links.push(linkTo("thinking/model:" + encodeURIComponent(sel.model_request_id), "查看模型调用"));
  if (sel._kind === "life" && sel.topic_id) links.push(linkTo("life", "查看生活话题"));
  if (sel._kind === "reflection") links.push(linkTo("life", "查看反思建议"));
  if (sel._kind === "diary") links.push(linkTo("life", "查看日记"));
  return links.length ? el("div", { class: "row-gap mt" }, links) : null;
}
const STEP_EXEC = { planned: ["待执行", "off"], running: ["执行中", "run"], done: ["已完成，结果已核验", "ok"], failed: ["失败", "bad"],
  closed: ["已关闭", "off"], discussed: ["只讨论过", "off"], UNKNOWN: ["结果不明", "warn"] };
const STEP_OP = { read_content: "读取内容", investigate: "调查" };
function goalStepsView(g) {
  const steps = g.steps || [], ex = g.execution || {}, [exText, exTone] = STEP_EXEC[ex.state] || ["未记录", "off"];
  return el("section", { class: "steps-view" }, el("h4", {}, "执行进度"),
    el("p", { class: "row-gap" }, el("span", { class: "tag " + exTone }, exText), el("span", { class: "note" }, "已核验完成 " + count(g.verified_completed_steps) + " / " + steps.length + " 步")),
    ex.state === "UNKNOWN" ? el("div", { class: "alert" }, icon("warn"), el("span", {}, "有一步的读取结果不明：读取程序可能已经读过，但没有能核验的回执。不会当作完成，也不会自动重试。")) : null,
    steps.length ? el("ol", { class: "steps" }, steps.map(st => {
      const [t, tone] = STEP_EXEC[st.execution?.state] || ["未记录", "off"], receipts = st.execution?.receipts || [];
      return el("li", {}, el("div", { class: "row-gap" }, el("b", {}, "第 " + st.position + " 步：" + (STEP_OP[st.operation] || "其他操作")), el("span", { class: "tag " + tone }, t), tag(st.state)),
        el("p", { class: "note" }, "尝试 " + count(st.attempts) + " / " + count(st.max_attempts) + " 次，依据 " + (st.source_event_ids || []).length + " 条来源" + (st.outcome_event_id ? "，结果记录 " + shortId(st.outcome_event_id) : "")),
        receipts.length ? el("ul", { class: "receipts" }, receipts.map(r => el("li", {}, el("span", { class: "num" }, "读取请求 " + shortId(r.request_id)), tag(r.state),
          r.attempt_state ? el("span", { class: "note" }, "读取尝试：" + statusOf(r.attempt_state)[0]) : null))) : el("p", { class: "note" }, "还没有执行回执。"),
        st.execution?.truncated ? el("p", { class: "note" }, "回执较多，只显示 20 条，结果不明的排在前面。") : null);
    })) : el("p", { class: "note" }, "这个目标没有分步计划。"));
}
function lifeDecisionView(sel) {
  const reason = sel.decision_reason || {};
  return el("section", { class: "steps-view" }, el("h4", {}, "这次决定"),
    kv("决定", LIFE_ACTION[sel.choice] || "未记录"), kv("去向", LIFE_DEST[sel.destination] || "未记录"),
    kv("理由", reason.state === "RECORDED" ? reason.text + (reason.truncated ? "…" : "") : reason.state === "REDACTED" ? "含可能敏感的内容，不显示" : "没有记录理由"),
    sel.job ? kv("处理任务", sel.job.state === "NOT_RECORDED" ? "未记录" : statusOf(sel.job.state)[0]) : null);
}
function cognitionExtra(sel) {
  return [sel._kind === "goal" ? goalStepsView(sel) : sel._kind === "life" ? lifeDecisionView(sel)
    : sel._kind === "model" ? modelMeasurementView(sel) : null, cognitionLinks(sel)];
}

/* 原始推理来自该次模型响应的原生会话快照，只作为纯文本展示。 */
const REASONING_NOTE = {
  wake: "自主活动调度按规则判断，这一步不调用模型，所以没有推理内容。",
  nightly: "夜间整理的各阶段只记录状态，阶段里的模型调用可以在“模型调用”里查看用量。",
  rem: "REM 默认使用非思考模式生成私人虚构作品；这里展示作品元数据。",
};
function reasoningView(sel) {
  const diagnosis = sel.reasoning || { state: "NOT_RECORDED" };
  if (diagnosis.state === "RECORDED" && typeof diagnosis.text === "string") {
    return [el("h3", { class: "dk-title" }, "模型推理诊断"),
      el("p", { class: "note" }, "这是服务商在本次调用中返回并保存的推理内容。" + (diagnosis.redacted ? "敏感凭据已脱敏。" : "") + (diagnosis.truncated ? "内容过长，已截断显示。" : "")),
      el("pre", { class: "scope-text mt", "aria-label": "原始推理内容" }, diagnosis.text)];
  }
  const message = sel._kind === "model" ? ({
    NOT_RETURNED: "这次调用已采集响应，但服务商没有返回有效的推理内容。",
    INCONSISTENT: "这次调用的推理记录无法校验，请检查原始会话记录。",
  }[diagnosis.state] || "这次调用没有保存原始推理，历史推理无法补回。") : "这条记录没有可以直接查看的原始推理。";
  return [el("h3", { class: "dk-title" }, "模型推理诊断"),
    el("div", { class: "alert" }, icon("warn"), el("span", {}, message)),
    sel._kind !== "model" ? el("p", { class: "note mt" }, REASONING_NOTE[sel._kind] || "她的决定和理由在“决策记录”里，依据的来源在“上下文”里。") : null,
    sel.model_request_id ? el("div", { class: "row-gap mt" }, linkTo("thinking/model:" + encodeURIComponent(sel.model_request_id), "查看这次模型调用的用量")) : null];
}

/* ================= 请求计量：模型调用总览与单次明细 =================
   只消费 cognition?kind=model 的原投影：
   - usage.observed 为真时，input/output 才是有效原回执的实际用量；否则只显示“未记录”，
     input_tokens 此时可能是预留上界，不能当作实际值。
   - 缓存只看 usage.cache_usage：RECORDED 且数值有效才显示（包括明确的 0）；
     缺字段、NOT_RECORDED 或数值组合无效都按未记录，不用 usage.cached_input_tokens 推断。
   - 费用 billed_micro_usd 始终显示，是已计或预留的责任金额，按配置费率估算。
   - 耗时、上下文分区与裁剪原因没有保存，界面写明未记录，不做拆分。 */
const METER_COLORS = { cached: "var(--m-cached)", fresh: "var(--m-fresh)", unknown: "var(--m-unknown)", output: "var(--m-output)" };
const METER_NAMES = { cached: "缓存命中输入", fresh: "未命中缓存的输入", unknown: "缓存未记录的输入", output: "输出" };
const COUNT_KINDS = { exact_local_render: "固定渲染器本地计数", provider_count: "服务商计数", estimate: "本地估算", unsupported: "计数格式不支持" };
const isCount = v => Number.isSafeInteger(v) && v >= 0;
/** 47512 → 47.5k；未知返回“未记录” */
function tk(v) {
  if (!isCount(v)) return "未记录";
  if (v < 1000) return String(v);
  if (v < 1e6) return (v / 1000).toFixed(v < 1e4 ? 2 : 1).replace(/\.?0+$/, "") + "k";
  return (v / 1e6).toFixed(2).replace(/\.?0+$/, "") + "M";
}
const pct = (part, whole) => whole > 0 ? (part / whole * 100) : 0;
const pctText = (part, whole) => { if (!(whole > 0)) return "—"; const p = pct(part, whole); return p > 0 && p < 0.1 ? "<0.1%" : p.toFixed(p >= 1 || p === 0 ? 1 : 2).replace(/\.0+$/, "") + "%"; };
/** 一次请求的可用数字：实际用量和缓存分别判定，互不推断 */
function meterOf(item) {
  const u = item.usage || {}, m = item.measurement || {};
  const actual = u.observed === true && isCount(u.input_tokens) && isCount(u.output_tokens);
  const c = u.cache_usage;
  const cacheKnown = actual && c && c.state === "RECORDED" && isCount(c.cached_input_tokens) && c.cached_input_tokens <= u.input_tokens;
  const cached = cacheKnown ? c.cached_input_tokens : null;
  return {
    actual, cacheKnown, cached,
    input: actual ? u.input_tokens : null, output: actual ? u.output_tokens : null,
    billed: isCount(u.billed_micro_usd) ? u.billed_micro_usd : null, estimate: u.is_estimate === 1 || u.is_estimate === true,
    measured: m.state === "RECORDED" && isCount(m.input_tokens) ? m.input_tokens : null,
    margin: m.state === "RECORDED" && isCount(m.reservation_margin) ? m.reservation_margin : null,
    allowance: m.state === "RECORDED" && isCount(m.output_allowance) ? m.output_allowance : null,
    countKind: m.state === "RECORDED" ? COUNT_KINDS[m.count_kind] || "未知方式" : m.state === "INCONSISTENT" ? "记录损坏或超限" : "未记录",
  };
}
/** 一次请求的分段：缓存命中 / 未命中 / 缓存未记录 / 输出 */
function meterParts(x) {
  if (!x.actual) return [];
  return x.cacheKnown
    ? [["cached", x.cached], ["fresh", x.input - x.cached], ["output", x.output]]
    : [["unknown", x.input], ["output", x.output]];
}
/** 分页记录加上单独定位的记录，按编号去重；分页里已有的以分页为准 */
function pageRecords(page) {
  const items = (page?.items || []).filter(Boolean), seen = new Set(items.map(i => String(i.id)));
  return [...items, ...(page?.located || []).filter(i => i && !seen.has(String(i.id)))];
}
/** 已加载的模型调用按保存时间从旧到新；时间相同或无效时保持服务端顺序（新到旧）的倒序 */
function modelTimeline(page) {
  const list = pageRecords(page).map((item, n) => ({ item, n, t: Date.parse(item.occurred_at) }));
  return list.sort((a, b) => (Number.isFinite(a.t) ? a.t : -Infinity) - (Number.isFinite(b.t) ? b.t : -Infinity) || b.n - a.n).map(r => r.item);
}
/** 同一任务里的第几次调用：按保存时间从旧到新数，只在已加载记录里计算 */
function jobSequence(items, sel) {
  if (!sel.job_id) return null;
  const same = items.filter(i => i.job_id === sel.job_id);
  return { index: same.findIndex(i => i.id === sel.id) + 1, total: same.length };
}

function donut(parts, center, sub, countTo) {
  const total = parts.reduce((s, [, v]) => s + v, 0), r = 42, c = 2 * Math.PI * r;
  let offset = 0;
  const arcs = total > 0 ? parts.filter(([, v]) => v > 0).map(([k, v]) => {
    const len = v / total * c, arc = el("circle", { cx: "60", cy: "60", r: String(r), fill: "none", stroke: METER_COLORS[k], "stroke-width": "14",
      "stroke-dasharray": len.toFixed(2) + " " + (c - len).toFixed(2), "stroke-dashoffset": (-offset).toFixed(2), transform: "rotate(-90 60 60)" });
    offset += len; arc.style.setProperty("--len", len.toFixed(2)); return arc;
  }) : [el("circle", { cx: "60", cy: "60", r: String(r), fill: "none", stroke: "var(--line)", "stroke-width": "14" })];
  return el("div", { class: "donut" }, el("svg", { viewBox: "0 0 120 120", role: "img", "aria-label": center + " " + sub }, arcs),
    el("div", { class: "donut-c" }, el("b", { class: "num", "data-count": countTo ?? null }, center), el("span", {}, sub)));
}
function meterLegend(parts, total, names = METER_NAMES) {
  return el("ul", { class: "m-legend" }, parts.map(([k, v]) => el("li", {},
    el("i", { style: { background: METER_COLORS[k] } }), el("span", { class: "m-name", title: METER_NAMES[k] }, names[k]),
    el("b", { class: "num" }, pctText(v, total)), el("small", { class: "num" }, "≈" + tk(v)))));
}

/** 本页已加载的模型调用总览，放在“模型调用”筛选的列表上方 */
function meterOverview(page) {
  // 新到旧，按保存时间排；单独定位的旧记录落在它自己的时间位置上
  const items = modelTimeline(page).reverse();
  if (!items.length) return null;
  const rows = items.map(i => ({ item: i, x: meterOf(i) }));
  const actual = rows.filter(r => r.x.actual), cacheRows = actual.filter(r => r.x.cacheKnown);
  const sum = (list, f) => list.reduce((s, r) => s + (f(r.x) || 0), 0);
  const parts = [["cached", sum(cacheRows, x => x.cached)], ["fresh", sum(cacheRows, x => x.input - x.cached)],
    ["unknown", sum(actual.filter(r => !r.x.cacheKnown), x => x.input)], ["output", sum(actual, x => x.output)]].filter(([, v]) => v > 0);
  const total = parts.reduce((s, [, v]) => s + v, 0);
  const cacheIn = sum(cacheRows, x => x.input), hit = sum(cacheRows, x => x.cached);
  const jobs = new Set(items.map(i => i.job_id).filter(Boolean)).size;
  const unknown = items.filter(i => ["UNKNOWN", "RESERVED"].includes(i.state)).length;
  const billed = sum(rows, x => x.billed);
  const stat = (title, value, chips) => el("div", { class: "m-stat" }, el("div", { class: "m-stat-h" }, el("span", {}, title), el("b", { class: "num" }, value)),
    chips.length ? el("div", { class: "m-chips" }, chips.map(([n, v, tone]) => el("span", { class: "m-chip" + (tone ? " " + tone : "") }, n, el("b", { class: "num" }, String(v))))) : null);
  return el("section", { class: "meter" + (freshLoad() ? " anim" : "") },
    el("div", { class: "m-grid" },
      el("div", { class: "sheet m-summary" }, el("div", { class: "sheet-head" }, el("h2", {}, "请求统计"), el("span", { class: "note" }, "本页已加载 " + items.length + " 次")),
        el("div", { class: "sheet-body m-stats" },
          stat("实际用量", actual.length + " / " + items.length, [["已记录", actual.length, "ok"], ["未记录", items.length - actual.length, items.length - actual.length ? "warn" : ""]]),
          stat("缓存观察", cacheRows.length + " / " + actual.length, [["已记录", cacheRows.length, "ok"], ["未记录", actual.length - cacheRows.length, ""]]),
          stat("任务", String(jobs), [["结果不明", unknown, unknown ? "bad" : ""]]),
          stat("费用", usd(billed), [["按配置费率估算", rows.filter(r => r.x.estimate).length, ""]]))),
      el("div", { class: "sheet m-tokens" }, el("div", { class: "sheet-head" }, el("h2", {}, "Token 统计"),
          el("span", { class: "note" }, cacheIn > 0 ? "缓存命中 " + pctText(hit, cacheIn) : actual.length ? "缓存未记录" : "")),
        el("div", { class: "sheet-body m-donut" }, donut(parts, tk(total), actual.length ? "实际总量" : "未记录", actual.length ? total : null),
          actual.length ? meterLegend(parts, total) : el("p", { class: "note" }, "本页没有有效的实际用量回执。")))),
    meterChart(rows),
    el("p", { class: "note mt" }, "统计只包含实际用量已记录的请求；缓存命中率只按缓存已记录的请求计算。费用是已计或预留的责任金额，按配置费率估算，不等于服务商账单。耗时、上下文分区与裁剪原因没有保存，这里不做拆分。"));
}

const METER_SCALES = [["call", "逐次"], ["day", "按天"], ["month", "按月"]];
/** 已加载的请求按本地日期或月份归组；轴是连续的，到今天为止，最多 30 天或 12 个月 */
function meterBuckets(rows, scale) {
  const keyOf = r => scale === "day" ? localDay(r.item.occurred_at) : localDay(r.item.occurred_at).slice(0, 7);
  const groups = new Map();
  for (const r of rows) { if (!valid(r.item.occurred_at)) continue; const k = keyOf(r); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(r); }
  if (!groups.size) return { trimmed: false, buckets: [] };
  // 窗口固定为截至今天的最近 30 天或 12 个月，再把已加载的记录填进去；更早的只计入 trimmed
  const today = localDay(new Date().toISOString()), cap = scale === "day" ? 30 : 12;
  const prev = k => {
    if (scale === "day") { const d = new Date(k + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() - 1); return d.toISOString().slice(0, 10); }
    const [y, m] = k.split("-").map(Number); return m === 1 ? (y - 1) + "-12" : y + "-" + String(m - 1).padStart(2, "0");
  };
  const keys = [scale === "day" ? today : today.slice(0, 7)];
  while (keys.length < cap) keys.unshift(prev(keys[0]));
  const trimmed = [...groups.keys()].some(k => k < keys[0]);
  return { trimmed, buckets: keys.map(key => {
    const list = groups.get(key) || [], actual = list.filter(r => r.x.actual), sum = f => actual.reduce((s, r) => s + (f(r.x) || 0), 0);
    return { key, list, actual: actual.length,
      parts: [["cached", sum(x => x.cacheKnown ? x.cached : 0)], ["fresh", sum(x => x.cacheKnown ? x.input - x.cached : 0)],
        ["unknown", sum(x => x.cacheKnown ? 0 : x.input)], ["output", sum(x => x.output)]] };
  }) };
}
function bucketLabel(key, scale, i, prev) {
  const [y, m, d] = key.split("-").map(Number);
  if (scale === "month") return i === 0 || m === 1 ? String(y).slice(2) + "年" + m + "月" : m + "月";
  return i === 0 || !prev || prev.slice(0, 7) !== key.slice(0, 7) ? m + "/" + d : String(d);
}

/** 逐次：柱子是实际输入（按缓存分段）与输出，折线是本地准入计数；按天/按月：同一时段的实际用量相加 */
function meterChart(rows) {
  const scale = state.meterScale || "call", page = state.data?.model;
  const W = 640, H = 170, L = 44, R = 8, T = 10, B = 22;
  let cols, top, trimmed = false;
  if (scale === "call") {
    const list = rows.slice(0, 30).reverse();
    top = Math.max(1, ...list.map(r => r.x.actual ? r.x.input + r.x.output : 0), ...list.map(r => r.x.measured || 0), ...list.map(r => r.x.measured != null && r.x.margin != null ? r.x.measured + r.x.margin : 0));
    cols = list.map((r, i) => ({ id: r.item.id, label: String(i + 1), parts: r.x.actual ? meterParts(r.x) : null, none: !r.x.actual, ghost: r.x.measured || 0, measured: r.x.measured,
      aria: "第 " + (i + 1) + " 次请求，" + (r.x.actual ? "实际输入 " + tk(r.x.input) + "，输出 " + tk(r.x.output) : "实际用量未记录") }));
  } else {
    const grouped = meterBuckets(rows, scale), buckets = grouped.buckets; trimmed = grouped.trimmed;
    top = Math.max(1, ...buckets.map(b => b.parts.reduce((s, [, v]) => s + v, 0)));
    // 标签从最新一根往回每隔 every 根标一次；月份在第一个标签和跨月处写出
    const step = Math.ceil(Math.max(1, buckets.length) / 16);
    let shownKey = null;
    const labels = buckets.map((b, i) => (buckets.length - 1 - i) % step === 0
      ? (() => { const t = bucketLabel(b.key, scale, shownKey == null ? 0 : 1, shownKey); shownKey = b.key; return t; })() : "");
    cols = buckets.map((b, i) => {
      const total = b.parts.reduce((s, [, v]) => s + v, 0), when = scale === "day" ? b.key : b.key.replace("-", " 年 ") + " 月";
      const ghost = b.list.reduce((t, r) => t + (r.x.measured || 0), 0);
      return { id: b.list[0]?.item.id, label: labels[i], parts: b.actual ? b.parts : null, none: b.list.length > 0 && !b.actual, ghost,
        aria: when + "，" + b.list.length + " 次请求" + (b.actual ? "，实际总量 " + tk(total) + (b.actual < b.list.length ? "，其中 " + (b.list.length - b.actual) + " 次未记录" : "") : b.list.length ? "，实际用量未记录" : "") };
    });
  }
  const n = Math.max(1, cols.length), bw = Math.min(28, (W - L - R) / n * 0.62);
  const xAt = i => L + (i + 0.5) * (W - L - R) / n, yAt = v => T + (H - T - B) * (1 - v / top);
  const grid = [0, 0.5, 1].map(f => [el("line", { x1: String(L), x2: String(W - R), y1: yAt(top * f).toFixed(1), y2: yAt(top * f).toFixed(1), class: "m-grid-l" }),
    el("text", { x: String(L - 6), y: (yAt(top * f) + 4).toFixed(1), "text-anchor": "end", class: "m-axis" }, tk(Math.round(top * f)))]);
  const every = Math.ceil(n / 16);
  const bars = cols.map((c, i) => {
    const x = xAt(i) - bw / 2, sel = c.id != null && state.cogSel === "model:" + c.id;
    const go = () => { if (c.id == null) return; state.cogSel = "model:" + c.id; rerender(); };
    let y = yAt(0);
    const segs = c.parts ? c.parts.filter(([, v]) => v > 0).map(([k, v]) => {
      const h = (H - T - B) * v / top; y -= h;
      return el("rect", { x: x.toFixed(1), y: y.toFixed(1), width: bw.toFixed(1), height: Math.max(h, 0.5).toFixed(1), fill: METER_COLORS[k] });
    }) : c.none ? [el("rect", { x: x.toFixed(1), y: Math.min(yAt(c.ghost), yAt(0) - 12).toFixed(1), width: bw.toFixed(1), height: Math.max((H - T - B) * c.ghost / top, 12).toFixed(1), class: "m-bar-none" })] : [];
    const label = (n - 1 - i) % every === 0 && c.label ? el("text", { x: xAt(i).toFixed(1), y: String(H - 6), "text-anchor": "middle", class: "m-axis" }, c.label) : null;
    if (c.id == null) return el("g", { class: "m-bar empty", style: { "--i": String(i) }, role: "img", "aria-label": c.aria }, label);
    return el("g", { class: "m-bar" + (sel ? " sel" : ""), style: { "--i": String(i) }, tabindex: "0", role: "button", onclick: go,
      onkeydown: e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } }, "aria-label": c.aria + (scale === "call" ? "" : "，查看其中最近一次") },
      el("rect", { x: (x - 3).toFixed(1), y: String(T), width: (bw + 6).toFixed(1), height: String(H - T - B), class: "m-hit" }), segs, label);
  });
  const pts = scale === "call" ? cols.map((c, i) => c.measured != null ? [xAt(i), yAt(c.measured)] : null).filter(Boolean) : [];
  const line = pts.length ? el("polyline", { points: pts.map(([a, b]) => a.toFixed(1) + "," + b.toFixed(1)).join(" "), class: "m-line", pathLength: "1" }) : null;
  const dots = pts.map(([a, b]) => el("circle", { cx: a.toFixed(1), cy: b.toFixed(1), r: "2.6", class: "m-dot" }));
  const oldest = rows.map(r => r.item.occurred_at).filter(valid).sort((a, b) => Date.parse(a) - Date.parse(b))[0];
  const hint = scale === "call" ? "从旧到新，点一根柱子查看明细" : "按已加载的 " + rows.length + " 次请求汇总" + (oldest && valid(oldest) ? "，最早 " + fmtDate(oldest) : "") + (trimmed ? "，图中只画最近 " + (scale === "day" ? "30 天" : "12 个月") : "") + "；点柱子看该时段最近一次";
  // 切换粒度时只给柱子重播生长动画
  const swap = state.meterDrawn != null && state.meterDrawn !== scale; state.meterDrawn = scale;
  return el("div", { class: "sheet m-chart" + (swap && motionOk() ? " swap" : "") },
    el("div", { class: "sheet-head" }, el("h2", {}, scale === "call" ? "逐次请求" : scale === "day" ? "每日用量" : "每月用量"),
      el("div", { class: "seg sm", role: "group", "aria-label": "柱状图粒度" }, METER_SCALES.map(([k, t]) =>
        el("button", { type: "button", "aria-pressed": String(scale === k), onclick: () => { state.meterScale = k; rerender(); } }, t)))),
    el("div", { class: "sheet-body" },
      el("p", { class: "note m-hint" }, hint, scale !== "call" && page?.next_cursor
        ? el("button", { type: "button", class: "more-btn", disabled: state.cogMore === "model", onclick: () => moreCognition("model") }, state.cogMore === "model" ? "正在加载…" : "加载更早的记录") : null),
      cols.length ? el("svg", { viewBox: `0 0 ${W} ${H}`, class: "m-svg", role: "group", "aria-label": scale === "call" ? "逐次请求的输入输出" : "按时段汇总的实际用量" }, grid, bars, line, dots)
        : el("p", { class: "note" }, "已加载的记录里没有有效时间，无法按时段汇总。"),
      el("div", { class: "m-keys" }, ["cached", "fresh", "unknown", "output"].map(k => el("span", {}, el("i", { style: { background: METER_COLORS[k] } }), METER_NAMES[k])),
        scale === "call" ? el("span", {}, el("i", { class: "k-line" }), "本地准入计数") : null, el("span", {}, el("i", { class: "k-none" }), "实际用量未记录"))));
}

/** 单次请求明细，用在“模型调用”记录的决策记录标签里 */
function modelMeasurementView(sel) {
  const x = meterOf(sel), seq = jobSequence(modelTimeline(state.data?.model), sel);
  const parts = meterParts(x), total = parts.reduce((s, [, v]) => s + v, 0);
  const pending = ["UNKNOWN", "RESERVED"].includes(sel.state);
  const chips = [["实际输入", x.actual ? tk(x.input) : "未记录"], ["输出", x.actual ? tk(x.output) : "未记录"],
    ["缓存", x.cacheKnown ? pctText(x.cached, x.input) + "（" + tk(x.cached) + "）" : "未记录"], ["费用", x.billed != null ? usd(x.billed) + (x.estimate ? " 估算" : "") : "未记录"]];
  const reserve = x.measured != null && x.margin != null ? x.measured + x.margin : null;
  const scale = Math.max(1, reserve || 0, x.measured || 0, x.actual ? x.input : 0);
  const compare = [["准入预留", reserve, "res"], ["本地输入计数", x.measured, "loc"], ["实际输入", x.actual ? x.input : null, "act"]];
  const freshOne = state.meterOne !== sel.id; state.meterOne = sel.id;
  return el("section", { class: "steps-view meter-one" + (freshOne ? " anim" : "") },
    el("div", { class: "m-one-h" }, el("h4", {}, "本次请求计量"),
      el("span", { class: "note" }, seq && seq.total > 1 ? "同一任务第 " + seq.index + " 次调用（本页共 " + seq.total + " 次）" : "同一任务里只加载到这一次调用")),
    pending ? el("div", { class: "alert" }, icon("warn"), el("span", {}, "这次请求的结果不明或仍在预留：实际用量未记录，费用按预留责任保留，不代表实际账单。")) : null,
    el("div", { class: "m-chips" }, chips.map(([n, v]) => el("span", { class: "m-chip strong" }, n, el("b", { class: "num" }, v)))),
    parts.length ? [el("div", { class: "m-seg", role: "img", "aria-label": parts.map(([k, v]) => METER_NAMES[k] + " " + tk(v)).join("，") },
        parts.filter(([, v]) => v > 0).map(([k, v]) => el("i", { style: { background: METER_COLORS[k], width: pct(v, total).toFixed(3) + "%" } }))),
      meterLegend(parts, total)] : el("p", { class: "note" }, "没有有效的实际用量回执，不显示输入输出分布。"),
    el("div", { class: "m-compare" }, el("p", { class: "m-sub" }, "本地计量与实际", el("span", { class: "note" }, x.countKind)),
      compare.map(([n, v, k]) => el("div", { class: "m-cmp" }, el("span", {}, n),
        el("div", { class: "m-track" }, v != null ? el("i", { class: k, style: { width: pct(v, scale).toFixed(2) + "%" } }) : null),
        el("b", { class: "num" }, v != null ? tk(v) : "未记录"))),
      x.allowance != null ? el("p", { class: "note" }, "输出预留 " + tk(x.allowance) + (x.actual ? "，实际输出 " + tk(x.output) : "")) : null),
    el("div", { class: "m-kv" },
      kv("计量方式", x.countKind), kv("实际输入 / 输出", x.actual ? x.input + " / " + x.output : "未记录"),
      kv("缓存命中输入", x.cacheKnown ? String(x.cached) : "未记录"), kv("已计或预留费用", x.billed != null ? usd(x.billed) : "未记录")),
    el("p", { class: "note" }, "本地计量用于准入和预留，实际用量以有效的服务商回执为准；缓存只在原回执明确返回时显示。费用按配置费率核算，不是服务商账单。"),
    el("p", { class: "note" }, "上下文区域分布与裁剪原因未记录。"));
}

/* ---------- 列表与动画 ---------- */
/** 模型调用的列表行写用量，不再重复“模型调用” */
function modelRowText(i) {
  const x = meterOf(i);
  return [x.actual ? tk(x.input) + " 输入 · " + tk(x.output) + " 输出" : "实际用量未记录",
    fmtDate(i.occurred_at) + (x.cacheKnown ? "，缓存 " + pctText(x.cached, x.input) : x.actual ? "，缓存未记录" : "")];
}
const motionOk = () => !globalThis.matchMedia || globalThis.matchMedia("(prefers-reduced-motion: no-preference)").matches;
/** 每次读取到新数据只播放一次入场动画，切换选中不重播 */
function freshLoad() { return state.meterAnim !== state.updatedAt; }
/** 数字从 0 滚到目标值；只用于入场 */
function countUp(root) {
  if (!motionOk()) return;
  for (const node of root.querySelectorAll("[data-count]")) {
    const to = Number(node.dataset.count), fmt = node.dataset.fmt === "usd" ? usd : tk, t0 = performance.now();
    if (!Number.isFinite(to) || to <= 0) continue;
    const step = now => { const k = Math.min(1, (now - t0) / 700), e = 1 - Math.pow(1 - k, 3);
      node.textContent = fmt(Math.round(to * e)); if (k < 1 && node.isConnected) requestAnimationFrame(step); };
    node.textContent = fmt(0); requestAnimationFrame(step);
  }
}
PAGES.thinking.after = () => {
  const page = $("#page");
  if (freshLoad()) { state.meterAnim = state.updatedAt; if (page) countUp(page); }
  // 选中项不在列表可见范围时，只滚动列表，不移动整页
  if (state.cogScrolled !== state.cogSel) {
    state.cogScrolled = state.cogSel;
    const list = $(".dk-list"), item = $(".dk-list button.sel");
    if (list && item && list.scrollHeight > list.clientHeight) {
      const top = item.offsetTop - list.offsetTop;
      if (top < list.scrollTop || top + item.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = Math.max(0, top - list.clientHeight / 3);
    }
  }
};

/* ---------- 长列表 ---------- */
/** 先显示前几条，其余点“展开”查看；展开状态在本次登录里保留 */
function capped(key, nodes, limit, tagName = "li") {
  const open = state.expanded.has(key), rest = nodes.length - limit;
  if (rest <= 0) return nodes;
  return [...(open ? nodes : nodes.slice(0, limit)), el(tagName, { class: "more-row" }, el("button", { type: "button", class: "more-btn", "aria-expanded": String(open),
    onclick: () => { open ? state.expanded.delete(key) : state.expanded.add(key); rerender(); } }, open ? "收起" : "展开其余 " + rest + " 条"))];
}
/** 需要你处理的排在前面，同一级保持服务端原顺序（新的在前） */
function byRank(items, rank) { return items.map((x, i) => [rank(x), i, x]).sort((a, b) => a[0] - b[0] || a[1] - b[1]).map(r => r[2]); }
function goalRank(g) {
  if (g.state === "CANDIDATE") return 0;
  if ((g.allowed_actions || []).length) return 1;
  return ["DONE", "CANCELLED", "FAILED"].includes(g.state) ? 3 : 2;
}
/** 标题旁的条数；到达服务端上限时说明只读了最近的这么多条 */
const countNote = (n, cap) => n ? el("span", { class: "note" }, n >= cap ? "最近 " + n + " 条" : n + " 条") : null;
