import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../chat/models.dart';
import '../chat/session.dart';
import 'providers.dart';
import 'theme.dart';

const _wideBreakpoint = 860.0;

class ChatApplication extends StatefulWidget {
  const ChatApplication({super.key, this.developmentNotice});
  final String? developmentNotice;
  @override
  State<ChatApplication> createState() => _ChatApplicationState();
}

class _ChatApplicationState extends State<ChatApplication> {
  late final router = GoRouter(
    routes: [
      GoRoute(
        path: '/',
        builder: (_, _) => ChatShell(notice: widget.developmentNotice),
      ),
      GoRoute(
        path: '/chat/:bot',
        builder: (_, state) => ChatShell(
          selected: state.pathParameters['bot'],
          notice: widget.developmentNotice,
        ),
      ),
    ],
  );
  @override
  Widget build(BuildContext context) => MaterialApp.router(
    title: 'Companion',
    debugShowCheckedModeBanner: false,
    routerConfig: router,
    theme: warmTheme(),
  );
  @override
  void dispose() {
    router.dispose();
    super.dispose();
  }
}

class ChatShell extends ConsumerWidget {
  const ChatShell({super.key, this.selected, this.notice});
  final String? selected;
  final String? notice;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final bots = ref.watch(botsProvider);
    return Scaffold(
      backgroundColor: Colors.transparent,
      body: WarmBackground(
        child: bots.when(
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (_, _) => const Center(child: Text('无法读取本地 Bot 列表。')),
          data: (items) {
            final bot = items.where((b) => b.id == selected).firstOrNull;
            return LayoutBuilder(
              builder: (context, constraints) =>
                  constraints.maxWidth >= _wideBreakpoint
                  ? _WideShell(bots: items, bot: bot, notice: notice)
                  : _NarrowShell(bots: items, bot: bot, notice: notice),
            );
          },
        ),
      ),
    );
  }
}

class _WideShell extends StatelessWidget {
  const _WideShell({required this.bots, this.bot, this.notice});
  final List<BotProfile> bots;
  final BotProfile? bot;
  final String? notice;
  @override
  Widget build(BuildContext context) => SafeArea(
    child: Padding(
      padding: const EdgeInsets.fromLTRB(24, 12, 24, 24),
      child: Column(
        children: [
          _Toolbar(notice: notice),
          Expanded(
            child: Center(
              child: ConstrainedBox(
                constraints: const BoxConstraints(
                  maxWidth: 1560,
                  maxHeight: 940,
                ),
                child: Glass(
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      const _Rail(),
                      Container(
                        width: 258,
                        decoration: const BoxDecoration(
                          color: Warm.panel,
                          border: Border(right: BorderSide(color: Warm.line)),
                        ),
                        child: BotList(bots: bots, selected: bot?.id),
                      ),
                      Expanded(
                        child: ColoredBox(
                          color: Warm.panel,
                          child: bot == null
                              ? const EmptyChat()
                              : ChatPane(key: ValueKey(bot!.id), bot: bot!),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ],
      ),
    ),
  );
}

class _NarrowShell extends ConsumerWidget {
  const _NarrowShell({required this.bots, this.bot, this.notice});
  final List<BotProfile> bots;
  final BotProfile? bot;
  final String? notice;
  @override
  Widget build(BuildContext context, WidgetRef ref) => SafeArea(
    child: Column(
      children: [
        if (notice != null)
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
            child: _NoticeBadge(notice!),
          ),
        Expanded(
          child: bot == null
              ? Column(
                  children: [
                    Padding(
                      padding: const EdgeInsets.fromLTRB(20, 16, 12, 4),
                      child: Row(
                        children: [
                          const Expanded(child: _ListTitle()),
                          _AddBotButton(onPressed: () => addBot(context, ref)),
                        ],
                      ),
                    ),
                    Expanded(
                      child: BotList(bots: bots, selected: null, header: false),
                    ),
                  ],
                )
              : ChatPane(key: ValueKey(bot!.id), bot: bot!, compact: true),
        ),
      ],
    ),
  );
}

class _Toolbar extends StatelessWidget {
  const _Toolbar({this.notice});
  final String? notice;
  @override
  Widget build(BuildContext context) => SizedBox(
    height: 52,
    child: Row(
      children: [
        const Icon(Icons.spa_outlined, color: Warm.brand, size: 22),
        const SizedBox(width: 10),
        const Text(
          'Companion',
          style: TextStyle(
            color: Warm.brand,
            fontSize: 19,
            fontWeight: FontWeight.w600,
          ),
        ),
        const Padding(
          padding: EdgeInsets.symmetric(horizontal: 10),
          child: Text('/', style: TextStyle(color: Color(0x59685649))),
        ),
        const Text('私聊', style: TextStyle(color: Warm.brand, fontSize: 13)),
        const Spacer(),
        if (notice != null) _NoticeBadge(notice!),
      ],
    ),
  );
}

class _NoticeBadge extends StatelessWidget {
  const _NoticeBadge(this.text);
  final String text;
  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 13, vertical: 7),
    decoration: BoxDecoration(
      color: const Color(0x88FFFFFF),
      borderRadius: BorderRadius.circular(30),
      border: Border.all(color: const Color(0xBBFFFFFF)),
    ),
    child: Text(
      text,
      maxLines: 2,
      overflow: TextOverflow.ellipsis,
      style: const TextStyle(fontSize: 11, color: Color(0xFF75685D)),
    ),
  );
}

class _Rail extends ConsumerWidget {
  const _Rail();
  @override
  Widget build(BuildContext context, WidgetRef ref) => SizedBox(
    width: 84,
    child: Padding(
      padding: const EdgeInsets.fromLTRB(9, 22, 9, 16),
      child: Column(
        children: [
          Container(
            width: 46,
            height: 46,
            margin: const EdgeInsets.only(bottom: 18),
            decoration: BoxDecoration(
              color: Warm.accent,
              borderRadius: BorderRadius.circular(14),
              boxShadow: const [
                BoxShadow(
                  color: Warm.accentSoft,
                  blurRadius: 12,
                  offset: Offset(0, 4),
                ),
              ],
            ),
            child: const Icon(Icons.spa_outlined, color: Colors.white),
          ),
          const _RailItem(
            icon: Icons.chat_bubble_outline_rounded,
            label: '对话',
            selected: true,
          ),
          const SizedBox(height: 10),
          _RailItem(
            key: const ValueKey('add_bot'),
            icon: Icons.add_rounded,
            label: '添加',
            tooltip: '添加 Bot',
            onPressed: () => addBot(context, ref),
          ),
        ],
      ),
    ),
  );
}

class _RailItem extends StatelessWidget {
  const _RailItem({
    super.key,
    required this.icon,
    required this.label,
    this.tooltip,
    this.selected = false,
    this.onPressed,
  });
  final IconData icon;
  final String label;
  final String? tooltip;
  final bool selected;
  final VoidCallback? onPressed;
  @override
  Widget build(BuildContext context) {
    final color = selected ? Colors.white : const Color(0xFF68605B);
    final item = Pressable(
      onPressed: onPressed,
      child: Container(
        width: 66,
        height: 64,
        decoration: BoxDecoration(
          color: selected ? Warm.accent : Colors.transparent,
          borderRadius: BorderRadius.circular(16),
          boxShadow: selected
              ? const [
                  BoxShadow(
                    color: Warm.accentSoft,
                    blurRadius: 12,
                    offset: Offset(0, 4),
                  ),
                ]
              : null,
        ),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(icon, color: color, size: 23),
            const SizedBox(height: 5),
            Text(label, style: TextStyle(fontSize: 11, color: color)),
          ],
        ),
      ),
    );
    return Semantics(
      button: onPressed != null,
      selected: selected,
      label: tooltip ?? label,
      child: tooltip == null ? item : Tooltip(message: tooltip!, child: item),
    );
  }
}

/// 120 ms press feedback (scale to 0.97), disabled under reduced motion.
class Pressable extends StatefulWidget {
  const Pressable({super.key, required this.child, this.onPressed});
  final Widget child;
  final VoidCallback? onPressed;
  @override
  State<Pressable> createState() => _PressableState();
}

class _PressableState extends State<Pressable> {
  bool down = false;
  @override
  Widget build(BuildContext context) {
    final scale = down && !Motion.reduced(context) ? 0.97 : 1.0;
    return MouseRegion(
      cursor: widget.onPressed == null
          ? SystemMouseCursors.basic
          : SystemMouseCursors.click,
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTapDown: widget.onPressed == null
            ? null
            : (_) => setState(() => down = true),
        onTapCancel: () => setState(() => down = false),
        onTapUp: (_) => setState(() => down = false),
        onTap: widget.onPressed,
        child: AnimatedScale(
          scale: scale,
          duration: Motion.press,
          child: widget.child,
        ),
      ),
    );
  }
}

class _ListTitle extends StatelessWidget {
  const _ListTitle();
  @override
  Widget build(BuildContext context) => const Text(
    '对话',
    style: TextStyle(
      fontSize: 25,
      fontWeight: FontWeight.w600,
      color: Warm.text,
    ),
  );
}

class _AddBotButton extends StatelessWidget {
  const _AddBotButton({required this.onPressed});
  final VoidCallback onPressed;
  @override
  Widget build(BuildContext context) => IconButton(
    key: const ValueKey('add_bot'),
    tooltip: '添加 Bot',
    onPressed: onPressed,
    color: Warm.accent,
    icon: const Icon(Icons.add_rounded),
  );
}

class EmptyChat extends StatelessWidget {
  const EmptyChat({super.key});
  @override
  Widget build(BuildContext context) => const Center(
    child: Padding(
      padding: EdgeInsets.all(32),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.chat_bubble_outline_rounded, size: 44, color: Warm.muted),
          SizedBox(height: 14),
          Text(
            '选择一个 Bot 开始聊天',
            style: TextStyle(fontSize: 17, color: Warm.textSoft),
          ),
          SizedBox(height: 8),
          Text(
            '每个 Bot 保留自己的会话和消息。',
            style: TextStyle(fontSize: 12, color: Color(0xFF9B8877)),
          ),
        ],
      ),
    ),
  );
}

class BotList extends ConsumerWidget {
  const BotList({
    super.key,
    required this.bots,
    this.selected,
    this.header = true,
  });
  final List<BotProfile> bots;
  final String? selected;
  final bool header;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final title = header
        ? const Padding(
            padding: EdgeInsets.fromLTRB(20, 22, 20, 14),
            child: Align(alignment: Alignment.centerLeft, child: _ListTitle()),
          )
        : null;
    if (bots.isEmpty) {
      return Column(
        children: [
          ?title,
          const Expanded(
            child: Center(
              child: Padding(
                padding: EdgeInsets.all(28),
                child: Text(
                  '点击“添加”，连接你的第一个伙伴。',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: Color(0xFF9B8877), fontSize: 13),
                ),
              ),
            ),
          ),
        ],
      );
    }
    return Column(
      children: [
        ?title,
        Expanded(
          child: ListView.builder(
            padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
            itemCount: bots.length,
            itemBuilder: (context, index) {
              final bot = bots[index];
              final state =
                  ref.watch(chatStateProvider(bot)).value ?? const ChatState();
              final preview = state.turns.lastOrNull;
              final text = preview == null
                  ? state.status
                  : preview.redacted
                  ? '消息已删除'
                  : preview.replies
                            .where((r) => r.text.isNotEmpty)
                            .lastOrNull
                            ?.text ??
                        preview.text;
              final active = bot.id == selected;
              final time = preview == null
                  ? null
                  : sentAt(preview.createdAt, short: true);
              return Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Semantics(
                  selected: active,
                  button: true,
                  child: Pressable(
                    key: ValueKey('bot_${bot.id}'),
                    onPressed: () => context.go('/chat/${bot.id}'),
                    child: Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 10,
                        vertical: 13,
                      ),
                      decoration: BoxDecoration(
                        color: active
                            ? Warm.conversationActive
                            : Colors.transparent,
                        borderRadius: BorderRadius.circular(17),
                      ),
                      child: Row(
                        children: [
                          InitialAvatar(id: bot.id, name: bot.name),
                          const SizedBox(width: 11),
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Row(
                                  children: [
                                    Expanded(
                                      child: Text(
                                        bot.name,
                                        maxLines: 1,
                                        overflow: TextOverflow.ellipsis,
                                        style: const TextStyle(
                                          fontSize: 15,
                                          fontWeight: FontWeight.w600,
                                        ),
                                      ),
                                    ),
                                    if (time != null)
                                      Text(
                                        time,
                                        style: const TextStyle(
                                          fontSize: 10,
                                          color: Color(0xFF7E827E),
                                        ),
                                      ),
                                  ],
                                ),
                                const Text(
                                  'AI 伙伴',
                                  style: TextStyle(
                                    fontSize: 10,
                                    height: 1.7,
                                    color: Color(0xFF6D7C76),
                                  ),
                                ),
                                Text(
                                  text,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    fontSize: 12,
                                    color: Color(0xFF7B7770),
                                  ),
                                ),
                              ],
                            ),
                          ),
                          if (state.unread > 0) ...[
                            const SizedBox(width: 6),
                            Badge(label: Text('${state.unread}')),
                          ],
                        ],
                      ),
                    ),
                  ),
                ),
              );
            },
          ),
        ),
      ],
    );
  }
}

Future<void> addBot(BuildContext context, WidgetRef ref) async {
  var name = '';
  var endpoint = '';
  String? error;
  await showDialog<void>(
    context: context,
    builder: (dialogContext) => StatefulBuilder(
      builder: (context, setState) => AlertDialog(
        title: const Text('添加 Bot'),
        content: SizedBox(
          width: 420,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Align(
                alignment: Alignment.centerLeft,
                child: Text(
                  'Companion Core',
                  style: TextStyle(color: Warm.muted, fontSize: 13),
                ),
              ),
              const SizedBox(height: 16),
              TextField(
                key: const ValueKey('bot_name'),
                onChanged: (value) => name = value,
                autofocus: true,
                decoration: const InputDecoration(labelText: 'Bot 名称'),
                maxLength: 80,
              ),
              const SizedBox(height: 12),
              TextField(
                key: const ValueKey('bot_endpoint'),
                onChanged: (value) => endpoint = value,
                keyboardType: TextInputType.url,
                decoration: const InputDecoration(labelText: 'HTTPS 服务地址'),
              ),
              if (error != null)
                Padding(
                  padding: const EdgeInsets.only(top: 12),
                  child: Row(
                    children: [
                      const Icon(
                        Icons.error_outline_rounded,
                        size: 18,
                        color: Warm.warning,
                      ),
                      const SizedBox(width: 6),
                      Expanded(
                        child: Text(
                          error!,
                          style: const TextStyle(color: Warm.warning),
                        ),
                      ),
                    ],
                  ),
                ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: const Text('取消'),
          ),
          FilledButton(
            key: const ValueKey('save_bot'),
            onPressed: () async {
              try {
                if (name.trim().isEmpty) {
                  throw const FormatException('请填写 Bot 名称。');
                }
                final bot = BotProfile(
                  id: randomId(),
                  name: name.trim(),
                  endpoint: coreOrigin(endpoint),
                );
                await ref.read(repositoryProvider).addBot(bot);
                if (dialogContext.mounted) Navigator.pop(dialogContext);
              } on FormatException catch (value) {
                if (dialogContext.mounted) {
                  setState(() => error = value.message);
                }
              } catch (_) {
                if (dialogContext.mounted) {
                  setState(() => error = '无法保存；请检查此地址是否已经添加。');
                }
              }
            },
            child: const Text('添加'),
          ),
        ],
      ),
    ),
  );
}

class ChatPane extends ConsumerStatefulWidget {
  const ChatPane({super.key, required this.bot, this.compact = false});
  final BotProfile bot;
  final bool compact;
  @override
  ConsumerState<ChatPane> createState() => _ChatPaneState();
}

class _ChatPaneState extends ConsumerState<ChatPane> {
  final input = TextEditingController();
  final scroll = ScrollController();
  Timer? draftTimer;
  String lastDraft = '';
  bool sending = false;
  bool initialized = false;
  // Items present when the conversation first renders, or already shown, never
  // replay their entrance when rebuilt or scrolled back into view.
  Set<String>? shown;
  late final BotSession session;

  @override
  void initState() {
    super.initState();
    session = ref.read(registryProvider).session(widget.bot);
    final active = session;
    active.loaded.then((_) {
      if (!mounted) return;
      input.text = lastDraft = active.draft;
      initialized = true;
    });
  }

  bool entering(String id) {
    final seen = shown;
    if (seen == null || seen.contains(id)) return false;
    seen.add(id);
    return true;
  }

  Future<void> send() async {
    final text = input.text;
    if (sending ||
        text.trim().isEmpty ||
        input.value.composing.isValid && !input.value.composing.isCollapsed) {
      return;
    }
    setState(() => sending = true);
    final active = session;
    try {
      await active.send(text);
      if (lastDraft == text) {
        lastDraft = '';
        if (mounted) input.clear();
      }
      await active.setDraft(lastDraft);
      if (mounted) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (scroll.hasClients) {
            final end = scroll.position.maxScrollExtent;
            if (Motion.reduced(context)) {
              scroll.jumpTo(end);
            } else {
              scroll.animateTo(
                end,
                duration: Motion.message,
                curve: Curves.easeOut,
              );
            }
          }
        });
      }
    } on BackendError catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(error.code)));
      }
    } finally {
      if (mounted) setState(() => sending = false);
    }
  }

  Future<void> connect() async {
    final active = session;
    if (active.state.review == null) {
      var password = '';
      final credential = await showDialog<String>(
        context: context,
        builder: (context) => AlertDialog(
          title: Text('登录 ${widget.bot.name}'),
          content: SizedBox(
            width: 380,
            child: TextField(
              key: const ValueKey('owner_credential'),
              onChanged: (value) => password = value,
              autofocus: true,
              obscureText: true,
              enableSuggestions: false,
              autocorrect: false,
              decoration: const InputDecoration(labelText: '主人凭据'),
              onSubmitted: (_) => Navigator.pop(context, password),
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('取消'),
            ),
            FilledButton(
              key: const ValueKey('login_device'),
              onPressed: () => Navigator.pop(context, password),
              child: const Text('登录'),
            ),
          ],
        ),
      );
      if (credential == null || credential.isEmpty) return;
      await active.login(credential);
    }
    if (mounted && active.state.review != null) {
      final approved = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('登记此设备'),
          content: const Text(
            '允许此设备以主人身份聊天？\n\n'
            '登记会更新授权版本，旧版本下尚未完成的任务可能失效。',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('取消'),
            ),
            FilledButton(
              key: const ValueKey('approve_device'),
              onPressed: () => Navigator.pop(context, true),
              child: const Text('允许登记'),
            ),
          ],
        ),
      );
      if (approved == true) await active.approve();
    }
  }

  Future<void> sessionAction(String value) async {
    if (value == 'sync') await session.sync(full: true);
    if (value == 'disconnect') await session.disconnect();
    if (value == 'discard') await session.discardRejected();
    if (value == 'identity') {
      await Clipboard.setData(
        ClipboardData(text: await session.backend.publicIdentity()),
      );
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('设备公钥已复制。')));
      }
    }
  }

  static const _actions = [
    ('sync', Icons.sync_rounded, '同步全部历史'),
    ('identity', Icons.key_rounded, '复制设备公钥'),
    ('discard', Icons.delete_sweep_outlined, '移除被拒绝的待发送消息'),
    ('disconnect', Icons.link_off_rounded, '断开连接'),
  ];

  Widget actionsButton() {
    if (!widget.compact) {
      return PopupMenuButton<String>(
        tooltip: '会话操作',
        icon: const Icon(Icons.more_horiz_rounded, color: Warm.textSoft),
        onSelected: sessionAction,
        itemBuilder: (_) => [
          for (final (value, _, label) in _actions)
            PopupMenuItem(value: value, child: Text(label)),
        ],
      );
    }
    return IconButton(
      tooltip: '会话操作',
      icon: const Icon(Icons.more_horiz_rounded, color: Warm.textSoft),
      onPressed: () async {
        final value = await showWarmSheet<String>(
          context,
          (context) => SafeArea(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                for (final (value, icon, label) in _actions)
                  ListTile(
                    leading: Icon(icon, color: Warm.accent),
                    title: Text(label),
                    onTap: () => Navigator.pop(context, value),
                  ),
                const SizedBox(height: 8),
              ],
            ),
          ),
        );
        if (value != null) await sessionAction(value);
      },
    );
  }

  @override
  Widget build(BuildContext context) {
    final state =
        ref.watch(chatStateProvider(widget.bot)).value ?? session.state;
    if (state.unread > 0) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) unawaited(session.markViewed());
      });
    }
    final known = state.turns.map((t) => t.id).toSet();
    final pending = state.pending
        .where(
          (p) =>
              p.error != null ||
              p.eventId == null ||
              !known.contains(p.eventId),
        )
        .toList();
    // The first render establishes what was already there; only later arrivals
    // get an entrance.
    shown ??= {
      ...known,
      for (final message in pending) 'pending_${message.requestId}',
    };
    final compact = widget.compact;
    return Column(
      children: [
        Container(
          height: compact ? 68 : 86,
          padding: EdgeInsets.symmetric(horizontal: compact ? 6 : 22),
          decoration: const BoxDecoration(
            color: Color(0x22FFFFFF),
            border: Border(bottom: BorderSide(color: Warm.line)),
          ),
          child: Row(
            children: [
              if (compact)
                IconButton(
                  tooltip: '返回聊天列表',
                  onPressed: () => context.go('/'),
                  color: Warm.textSoft,
                  icon: const Icon(Icons.arrow_back_rounded),
                ),
              InitialAvatar(
                id: widget.bot.id,
                name: widget.bot.name,
                size: compact ? 38 : 44,
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      widget.bot.name,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: compact ? 16 : 18,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      state.status,
                      maxLines: compact ? 1 : 2,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontSize: 11, color: Warm.meta),
                    ),
                  ],
                ),
              ),
              TextButton(
                key: ValueKey('connect_${widget.bot.id}'),
                onPressed: connect,
                child: Text(state.review != null ? '登记设备' : '登录'),
              ),
              actionsButton(),
            ],
          ),
        ),
        Expanded(
          child: SelectionArea(
            child: ListView.builder(
              key: ValueKey('messages_${widget.bot.id}'),
              controller: scroll,
              padding: EdgeInsets.fromLTRB(
                compact ? 14 : 24,
                22,
                compact ? 14 : 24,
                18,
              ),
              itemCount: state.turns.length + pending.length,
              itemBuilder: (context, index) {
                if (index < state.turns.length) {
                  final turn = state.turns[index];
                  return TurnView(
                    key: ValueKey('turn_${turn.id}'),
                    turn: turn,
                    bot: widget.bot,
                    animate: entering(turn.id),
                  );
                }
                final message = pending[index - state.turns.length];
                final failed = message.error != null;
                return Appear(
                  key: ValueKey('pending_${message.requestId}'),
                  animate: entering('pending_${message.requestId}'),
                  child: Padding(
                    padding: const EdgeInsets.only(bottom: 18),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.end,
                      children: [
                        Bubble(text: message.text, owner: true),
                        _Meta(
                          failed
                              ? '提交被拒绝 · ${message.error}'
                              : message.admitted
                              ? 'Core 已接受'
                              : '本机待发送',
                          icon: failed ? Icons.error_outline_rounded : null,
                          warning: failed,
                          end: true,
                        ),
                      ],
                    ),
                  ),
                );
              },
            ),
          ),
        ),
        SafeArea(
          top: false,
          child: Padding(
            padding: EdgeInsets.fromLTRB(
              compact ? 10 : 16,
              8,
              compact ? 10 : 16,
              compact ? 10 : 14,
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Container(
                  padding: const EdgeInsets.fromLTRB(16, 4, 5, 4),
                  decoration: BoxDecoration(
                    color: Warm.composer,
                    borderRadius: BorderRadius.circular(28),
                    border: Border.all(color: Warm.bubbleBorder),
                    boxShadow: const [
                      BoxShadow(
                        color: Color(0x0B80664D),
                        blurRadius: 8,
                        offset: Offset(0, 3),
                      ),
                    ],
                  ),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.end,
                    children: [
                      Expanded(
                        child: CallbackShortcuts(
                          bindings: {
                            const SingleActivator(
                              LogicalKeyboardKey.enter,
                              control: true,
                            ): send,
                          },
                          child: TextField(
                            key: ValueKey('compose_${widget.bot.id}'),
                            controller: input,
                            minLines: 1,
                            maxLines: 5,
                            textInputAction: TextInputAction.newline,
                            style: const TextStyle(
                              fontSize: 15,
                              height: 1.5,
                              color: Color(0xFF51473E),
                            ),
                            decoration: const InputDecoration(
                              hintText: '写点什么…',
                              filled: false,
                              border: InputBorder.none,
                              enabledBorder: InputBorder.none,
                              focusedBorder: InputBorder.none,
                              isCollapsed: true,
                              contentPadding: EdgeInsets.symmetric(
                                vertical: 12,
                              ),
                            ),
                            onChanged: (text) {
                              lastDraft = text;
                              draftTimer?.cancel();
                              final active = session;
                              draftTimer = Timer(
                                const Duration(milliseconds: 250),
                                () => active.setDraft(text),
                              );
                            },
                          ),
                        ),
                      ),
                      const SizedBox(width: 6),
                      _SendButton(
                        key: ValueKey('send_${widget.bot.id}'),
                        onPressed: !state.ready || sending ? null : send,
                      ),
                    ],
                  ),
                ),
                if (!compact)
                  const Padding(
                    padding: EdgeInsets.fromLTRB(18, 6, 0, 0),
                    child: Text(
                      'Ctrl + Enter 发送 · Enter 换行',
                      style: TextStyle(fontSize: 10, color: Color(0xFFA39A91)),
                    ),
                  ),
              ],
            ),
          ),
        ),
      ],
    );
  }

  @override
  void dispose() {
    draftTimer?.cancel();
    if (initialized) unawaited(session.setDraft(lastDraft));
    input.dispose();
    scroll.dispose();
    super.dispose();
  }
}

class _SendButton extends StatelessWidget {
  const _SendButton({super.key, this.onPressed});
  final VoidCallback? onPressed;
  @override
  Widget build(BuildContext context) => Semantics(
    button: true,
    enabled: onPressed != null,
    label: '发送',
    child: Tooltip(
      message: '发送',
      child: Pressable(
        onPressed: onPressed,
        child: AnimatedOpacity(
          opacity: onPressed == null ? 0.5 : 1,
          duration: Motion.press,
          child: Container(
            width: 43,
            height: 43,
            decoration: const BoxDecoration(
              color: Warm.accent,
              shape: BoxShape.circle,
            ),
            child: const Icon(
              Icons.arrow_upward_rounded,
              color: Colors.white,
              size: 22,
            ),
          ),
        ),
      ),
    ),
  );
}

class _Meta extends StatelessWidget {
  const _Meta(this.text, {this.icon, this.warning = false, this.end = false});
  final String text;
  final IconData? icon;
  final bool warning;
  final bool end;
  @override
  Widget build(BuildContext context) {
    final color = warning ? Warm.warning : Warm.meta;
    return Padding(
      padding: EdgeInsets.only(top: 3, left: end ? 0 : 42, right: end ? 4 : 0),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (icon != null) ...[
            Icon(icon, size: 13, color: color),
            const SizedBox(width: 4),
          ],
          Flexible(
            child: Text(text, style: TextStyle(fontSize: 11, color: color)),
          ),
        ],
      ),
    );
  }
}

class Bubble extends StatelessWidget {
  const Bubble({
    super.key,
    required this.text,
    this.owner = false,
    this.bot,
    this.time,
  });
  final String text;
  final bool owner;

  /// Real send time, shown small inside the bubble.
  final String? time;

  /// Shows the companion's avatar beside its bubble when given.
  final BotProfile? bot;
  @override
  Widget build(BuildContext context) {
    final bubble = Flexible(
      child: Container(
        constraints: const BoxConstraints(maxWidth: 560),
        margin: const EdgeInsets.symmetric(vertical: 5),
        padding: const EdgeInsets.fromLTRB(14, 10, 14, 7),
        decoration: BoxDecoration(
          color: owner ? Warm.bubbleOwner : Warm.bubble,
          borderRadius: BorderRadius.circular(17),
          border: Border.all(
            color: owner ? Warm.bubbleOwnerBorder : Warm.bubbleBorder,
          ),
          boxShadow: const [
            BoxShadow(
              color: Color(0x058C6E4E),
              blurRadius: 5,
              offset: Offset(0, 2),
            ),
          ],
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.end,
          mainAxisSize: MainAxisSize.min,
          children: [
            Align(
              alignment: Alignment.centerLeft,
              widthFactor: 1,
              child: Text(
                text,
                style: const TextStyle(
                  fontSize: 14.5,
                  height: 1.65,
                  color: Warm.text,
                ),
              ),
            ),
            if (time != null)
              Text(
                time!,
                style: const TextStyle(fontSize: 9.5, color: Color(0xFF938C84)),
              ),
          ],
        ),
      ),
    );
    return Row(
      mainAxisAlignment: owner
          ? MainAxisAlignment.end
          : MainAxisAlignment.start,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (owner) const SizedBox(width: 38),
        if (!owner && bot != null) ...[
          Padding(
            padding: const EdgeInsets.only(top: 7),
            child: InitialAvatar(id: bot!.id, name: bot!.name, size: 32),
          ),
          const SizedBox(width: 10),
        ],
        bubble,
        if (!owner) const SizedBox(width: 38),
      ],
    );
  }
}

class TurnView extends StatelessWidget {
  const TurnView({
    super.key,
    required this.turn,
    this.bot,
    this.animate = false,
  });
  final ChatTurn turn;
  final BotProfile? bot;
  final bool animate;
  @override
  Widget build(BuildContext context) {
    final failed = turn.state == 'FAILED';
    final status = turn.uncertain
        ? '结果待核对 · 不会自动重新生成'
        : turn.preview?.state == 'WITHDRAWN' && turn.state == 'RUNNING'
        ? '回复未完成'
        : {
                'QUEUED': '等待处理',
                'RUNNING': '正在回复',
                'RETRY_WAIT': '等待重试',
                'SUCCEEDED': '已完成',
                'FAILED': '处理失败',
                'CANCELLED': '已取消',
              }[turn.state] ??
              turn.state;
    final replies = turn.replies.where((r) => r.text.isNotEmpty).toList();
    // The send time sits in the bubble; only states other than a plain
    // completion get a status line.
    final quiet = turn.state == 'SUCCEEDED' && !turn.uncertain;
    return Appear(
      animate: animate,
      child: Padding(
        padding: const EdgeInsets.only(bottom: 20),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Bubble(
              text: turn.redacted ? '此消息已删除' : turn.text,
              owner: true,
              time: sentAt(turn.createdAt),
            ),
            for (final (index, reply) in replies.indexed)
              Appear(
                key: ValueKey('reply_${reply.id}'),
                animate: animate || index > 0,
                child: Bubble(text: reply.text, bot: index == 0 ? bot : null),
              ),
            if (turn.preview case final preview?
                when !turn.redacted && replies.isEmpty) ...[
              if (preview.text.isNotEmpty)
                Bubble(
                  key: ValueKey('preview_${turn.id}'),
                  text: preview.text,
                  bot: bot,
                ),
              if (switch (preview.state) {
                    'GENERATING' => '生成中预览',
                    'INTERRUPTED' => '回复未完成',
                    'WITHDRAWN' => '预览已撤回',
                    'WAITING' => '等待恢复原任务',
                    _ => '',
                  }
                  case final label when label.isNotEmpty)
                _Meta(label),
            ],
            if (!quiet)
              _Meta(
                status,
                icon: failed || turn.uncertain
                    ? Icons.error_outline_rounded
                    : null,
                warning: failed || turn.uncertain,
              ),
          ],
        ),
      ),
    );
  }
}

/// Local send time. [short] drops the clock on other days (conversation list).
String sentAt(DateTime value, {DateTime? now, bool short = false}) {
  final local = value.toLocal();
  final today = (now ?? DateTime.now()).toLocal();
  String two(int part) => part.toString().padLeft(2, '0');
  final time = '${two(local.hour)}:${two(local.minute)}';
  if (local.year == today.year &&
      local.month == today.month &&
      local.day == today.day) {
    return time;
  }
  if (short) {
    return local.year == today.year
        ? '${local.month}/${local.day}'
        : '${local.year}/${local.month}/${local.day}';
  }
  return local.year == today.year
      ? '${local.month}月${local.day}日 $time'
      : '${local.year}年${local.month}月${local.day}日 $time';
}
