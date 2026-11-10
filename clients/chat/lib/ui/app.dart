import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../chat/models.dart';
import '../chat/session.dart';
import 'providers.dart';

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
    theme: ThemeData(
      useMaterial3: true,
      scaffoldBackgroundColor: const Color(0xFFF4F6F8),
      colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFF466A8A)),
      inputDecorationTheme: const InputDecorationTheme(
        border: OutlineInputBorder(),
      ),
    ),
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
    return bots.when(
      loading: () =>
          const Scaffold(body: Center(child: CircularProgressIndicator())),
      error: (_, _) =>
          const Scaffold(body: Center(child: Text('无法读取本地 Bot 列表。'))),
      data: (items) {
        final bot = items.where((b) => b.id == selected).firstOrNull;
        return LayoutBuilder(
          builder: (context, constraints) {
            final wide = constraints.maxWidth >= 860;
            final list = BotList(bots: items, selected: selected);
            return Scaffold(
              appBar: AppBar(
                leading: !wide && bot != null
                    ? IconButton(
                        tooltip: '返回聊天列表',
                        onPressed: () => context.go('/'),
                        icon: const Icon(Icons.arrow_back),
                      )
                    : null,
                title: Text(wide || bot == null ? 'Companion' : bot.name),
                actions: [
                  IconButton(
                    key: const ValueKey('add_bot'),
                    tooltip: '添加 Bot',
                    onPressed: () => addBot(context, ref),
                    icon: const Icon(Icons.add),
                  ),
                  const SizedBox(width: 12),
                ],
              ),
              body: Column(
                children: [
                  if (notice != null)
                    MaterialBanner(
                      content: Text(notice!),
                      actions: const [SizedBox.shrink()],
                    ),
                  Expanded(
                    child: wide
                        ? Row(
                            children: [
                              SizedBox(width: 280, child: list),
                              const VerticalDivider(width: 1),
                              Expanded(
                                child: bot == null
                                    ? const EmptyChat()
                                    : ChatPane(key: ValueKey(bot.id), bot: bot),
                              ),
                            ],
                          )
                        : bot == null
                        ? list
                        : ChatPane(key: ValueKey(bot.id), bot: bot),
                  ),
                ],
              ),
            );
          },
        );
      },
    );
  }
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
          Icon(Icons.chat_bubble_outline, size: 48),
          SizedBox(height: 16),
          Text('选择一个 Bot 开始聊天', style: TextStyle(fontSize: 22)),
          SizedBox(height: 8),
          Text('每个 Bot 保留自己的会话和消息。'),
        ],
      ),
    ),
  );
}

class BotList extends ConsumerWidget {
  const BotList({super.key, required this.bots, this.selected});
  final List<BotProfile> bots;
  final String? selected;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (bots.isEmpty) {
      return const Center(
        child: Padding(
          padding: EdgeInsets.all(28),
          child: Text('点击右上角的 +，添加你的第一个 Bot。', textAlign: TextAlign.center),
        ),
      );
    }
    return ListView.builder(
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
        return Padding(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
          child: ListTile(
            key: ValueKey('bot_${bot.id}'),
            selected: bot.id == selected,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(14),
            ),
            leading: CircleAvatar(child: Text(bot.name.characters.first)),
            title: Text(bot.name, maxLines: 1, overflow: TextOverflow.ellipsis),
            subtitle: Text(text, maxLines: 1, overflow: TextOverflow.ellipsis),
            trailing: state.unread > 0
                ? Badge(label: Text('${state.unread}'))
                : null,
            onTap: () => context.go('/chat/${bot.id}'),
          ),
        );
      },
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
                child: Text('Companion Core'),
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
                  child: Text(
                    error!,
                    style: TextStyle(
                      color: Theme.of(context).colorScheme.error,
                    ),
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
  const ChatPane({super.key, required this.bot});
  final BotProfile bot;
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
            scroll.animateTo(
              scroll.position.maxScrollExtent,
              duration: const Duration(milliseconds: 180),
              curve: Curves.easeOut,
            );
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
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 12),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      widget.bot.name,
                      style: Theme.of(context).textTheme.titleLarge,
                    ),
                    Text(
                      state.status,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                  ],
                ),
              ),
              TextButton(
                key: ValueKey('connect_${widget.bot.id}'),
                onPressed: connect,
                child: Text(state.review != null ? '登记设备' : '登录'),
              ),
              PopupMenuButton<String>(
                tooltip: '会话操作',
                onSelected: (value) async {
                  if (value == 'sync') await session.sync(full: true);
                  if (value == 'disconnect') await session.disconnect();
                  if (value == 'discard') await session.discardRejected();
                  if (value == 'identity') {
                    await Clipboard.setData(
                      ClipboardData(
                        text: await session.backend.publicIdentity(),
                      ),
                    );
                    if (context.mounted) {
                      ScaffoldMessenger.of(
                        context,
                      ).showSnackBar(const SnackBar(content: Text('设备公钥已复制。')));
                    }
                  }
                },
                itemBuilder: (_) => const [
                  PopupMenuItem(value: 'sync', child: Text('同步全部历史')),
                  PopupMenuItem(value: 'identity', child: Text('复制设备公钥')),
                  PopupMenuItem(value: 'discard', child: Text('移除被拒绝的待发送消息')),
                  PopupMenuItem(value: 'disconnect', child: Text('断开连接')),
                ],
              ),
            ],
          ),
        ),
        const Divider(height: 1),
        Expanded(
          child: SelectionArea(
            child: ListView.builder(
              key: ValueKey('messages_${widget.bot.id}'),
              controller: scroll,
              padding: const EdgeInsets.all(24),
              itemCount: state.turns.length + pending.length,
              itemBuilder: (context, index) {
                if (index < state.turns.length) {
                  return TurnView(turn: state.turns[index]);
                }
                final message = pending[index - state.turns.length];
                return Column(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  children: [
                    Bubble(text: message.text, owner: true),
                    Text(
                      message.error == null
                          ? message.admitted
                                ? 'Core 已接受'
                                : '本机待发送'
                          : '提交被拒绝 · ${message.error}',
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                    const SizedBox(height: 18),
                  ],
                );
              },
            ),
          ),
        ),
        SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.all(16),
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
                      decoration: const InputDecoration(
                        hintText: '写一条消息…',
                        helperText: 'Ctrl + Enter 发送 · Enter 换行',
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
                const SizedBox(width: 12),
                Padding(
                  padding: const EdgeInsets.only(bottom: 24),
                  child: FilledButton(
                    key: ValueKey('send_${widget.bot.id}'),
                    onPressed: !state.ready || sending ? null : send,
                    child: const Text('发送'),
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

class Bubble extends StatelessWidget {
  const Bubble({super.key, required this.text, this.owner = false});
  final String text;
  final bool owner;
  @override
  Widget build(BuildContext context) => Align(
    alignment: owner ? Alignment.centerRight : Alignment.centerLeft,
    child: Container(
      constraints: const BoxConstraints(maxWidth: 700),
      margin: const EdgeInsets.symmetric(vertical: 6),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: owner
            ? Theme.of(context).colorScheme.primaryContainer
            : Colors.white,
        borderRadius: BorderRadius.circular(16),
      ),
      child: Text(text, style: const TextStyle(fontSize: 16, height: 1.5)),
    ),
  );
}

class TurnView extends StatelessWidget {
  const TurnView({super.key, required this.turn});
  final ChatTurn turn;
  @override
  Widget build(BuildContext context) {
    final status = turn.uncertain
        ? '结果待核对 · 不会自动重新生成'
        : {
                'QUEUED': '等待处理',
                'RUNNING': '正在回复',
                'RETRY_WAIT': '等待重试',
                'SUCCEEDED': '已完成',
                'FAILED': '处理失败',
                'CANCELLED': '已取消',
              }[turn.state] ??
              turn.state;
    return Padding(
      padding: const EdgeInsets.only(bottom: 20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Bubble(text: turn.redacted ? '此消息已删除' : turn.text, owner: true),
          for (final reply in turn.replies.where((r) => r.text.isNotEmpty))
            Bubble(text: reply.text),
          Text(status, style: Theme.of(context).textTheme.bodySmall),
        ],
      ),
    );
  }
}
