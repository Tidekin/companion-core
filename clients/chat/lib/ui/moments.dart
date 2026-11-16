import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../chat/core_backend.dart';
import '../chat/models.dart';
import 'avatar.dart';
import 'providers.dart';
import 'theme.dart';

String momentTime(DateTime value, DateTime now) {
  final local = value.toLocal();
  final age = now.difference(value);
  String two(int part) => part.toString().padLeft(2, '0');
  if (age.inMinutes < 1) return '刚刚';
  if (age.inHours < 1) return '${age.inMinutes} 分钟前';
  if (age.inDays < 1 && now.toLocal().day == local.day) {
    return '今天 ${two(local.hour)}:${two(local.minute)}';
  }
  return '${local.month}月${local.day}日 ${two(local.hour)}:${two(local.minute)}';
}

const _triggers = {'LIFE': '她自己想说的', 'KEEPSAKE': '牢记了一件事'};

String _failure(Object error) => error is BackendError
    ? switch (error.code) {
        'MOMENT_NOT_FOUND' => '这条动态已经不在了，已刷新。',
        'CONNECTION_UNAVAILABLE' => '连接不上，请稍后再试。',
        _ => '操作没有完成（${error.code}）。',
      }
    : '操作没有完成。';

/// "她的动态": owner-only first-person posts, newest first, with likes.
class MomentsPanel extends ConsumerStatefulWidget {
  const MomentsPanel({super.key, required this.bot, this.onClose});
  final BotProfile bot;
  final VoidCallback? onClose;
  @override
  ConsumerState<MomentsPanel> createState() => _MomentsPanelState();
}

class _MomentsPanelState extends ConsumerState<MomentsPanel> {
  List<Moment>? items;
  List<Moment> memories = const [];
  bool memoriesOpen = true;
  String? error;
  StreamSubscription<void>? updates;

  ChatBackend get _backend =>
      ref.read(registryProvider).session(widget.bot).backend;

  @override
  void initState() {
    super.initState();
    updates = _backend.momentUpdates.listen((_) => load());
    load();
  }

  Future<void> load() async {
    try {
      final feed = await _backend.moments();
      final past = await _backend.momentMemories();
      if (!mounted) return;
      setState(() {
        items = feed;
        memories = past;
        error = null;
      });
    } catch (failure) {
      if (mounted) setState(() => error = _failure(failure));
    }
  }

  Future<void> run(Future<void> Function() action) async {
    try {
      await action();
    } catch (failure) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(_failure(failure))));
      }
    }
    await load();
  }

  Future<void> toggleLike(Moment moment) async {
    final liked = !moment.liked;
    setState(() {
      items = [
        for (final item in items ?? <Moment>[])
          item.id == moment.id ? item.withLiked(liked) : item,
      ];
    });
    await run(() => _backend.likeMoment(moment.id, liked));
  }

  Future<void> forget(Moment moment) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('忘记这条动态？'),
        content: const Text('这条动态会被彻底清除，从旧备份恢复时也会重新清除。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('取消'),
          ),
          FilledButton(
            key: const ValueKey('confirm_forget_moment'),
            onPressed: () => Navigator.pop(context, true),
            child: const Text('忘记'),
          ),
        ],
      ),
    );
    if (confirmed == true) await run(() => _backend.forgetMoment(moment.id));
  }

  @override
  void dispose() {
    updates?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final profile = ref.watch(profileProvider(widget.bot)).value;
    final name = profile?.shownName ?? widget.bot.name;
    final now = DateTime.now();
    final feed = items;
    return Material(
      color: Colors.transparent,
      child: RefreshIndicator(
        onRefresh: load,
        child: ListView(
          padding: const EdgeInsets.fromLTRB(20, 18, 20, 24),
          children: [
            Row(
              children: [
                CompanionAvatar(bot: widget.bot, name: name, size: 44),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        '$name的动态',
                        style: const TextStyle(
                          fontSize: 18,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      const Text(
                        '只有你能看到',
                        style: TextStyle(fontSize: 12, color: Warm.meta),
                      ),
                    ],
                  ),
                ),
                if (widget.onClose != null)
                  IconButton(
                    tooltip: '关闭',
                    onPressed: widget.onClose,
                    icon: const Icon(Icons.close_rounded, color: Warm.textSoft),
                  ),
              ],
            ),
            const SizedBox(height: 16),
            if (memories.isNotEmpty) _memories(now),
            if (error != null)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 12),
                child: Text(error!, style: const TextStyle(color: Warm.warning)),
              ),
            if (feed == null && error == null)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 32),
                child: Center(child: CircularProgressIndicator()),
              )
            else if (feed != null && feed.isEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 32),
                child: Text(
                  '$name还没有发过动态。她每天最多发两条，有想说的话时会出现在这里。',
                  style: const TextStyle(color: Warm.muted, fontSize: 13),
                ),
              )
            else if (feed != null)
              for (final moment in feed) _card(moment, name, now),
          ],
        ),
      ),
    );
  }

  Widget _memories(DateTime now) => Container(
    key: const ValueKey('moment_memories'),
    margin: const EdgeInsets.only(bottom: 14),
    padding: const EdgeInsets.fromLTRB(14, 10, 8, 10),
    decoration: BoxDecoration(
      color: Warm.accentSoft,
      borderRadius: BorderRadius.circular(14),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            const Icon(Icons.history_rounded, size: 18, color: Warm.accent),
            const SizedBox(width: 6),
            const Expanded(
              child: Text(
                '那年今日',
                style: TextStyle(fontWeight: FontWeight.w600),
              ),
            ),
            IconButton(
              tooltip: memoriesOpen ? '收起' : '展开',
              onPressed: () => setState(() => memoriesOpen = !memoriesOpen),
              icon: Icon(
                memoriesOpen ? Icons.expand_less_rounded : Icons.expand_more,
              ),
            ),
          ],
        ),
        if (memoriesOpen)
          for (final moment in memories)
            Padding(
              padding: const EdgeInsets.only(top: 6, right: 8),
              child: Text(
                '${moment.yearsAgo ?? 1} 年前：${moment.text}',
                style: const TextStyle(fontSize: 13, color: Warm.textSoft),
              ),
            ),
      ],
    ),
  );

  Widget _card(Moment moment, String name, DateTime now) => Container(
    key: ValueKey('moment_${moment.id}'),
    margin: const EdgeInsets.only(bottom: 12),
    padding: const EdgeInsets.fromLTRB(14, 12, 6, 6),
    decoration: BoxDecoration(
      color: Warm.bubble,
      borderRadius: BorderRadius.circular(16),
      border: Border.all(color: Warm.bubbleBorder),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            CompanionAvatar(bot: widget.bot, name: name, size: 32),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    name,
                    style: const TextStyle(fontWeight: FontWeight.w600),
                  ),
                  Text(
                    momentTime(moment.createdAt, now),
                    style: const TextStyle(fontSize: 11, color: Warm.meta),
                  ),
                ],
              ),
            ),
            PopupMenuButton<String>(
              key: ValueKey('moment_menu_${moment.id}'),
              tooltip: '更多',
              icon: const Icon(Icons.more_horiz_rounded, color: Warm.muted),
              onSelected: (choice) => choice == 'hide'
                  ? run(() => _backend.hideMoment(moment.id))
                  : forget(moment),
              itemBuilder: (_) => const [
                PopupMenuItem(value: 'hide', child: Text('隐藏')),
                PopupMenuItem(value: 'forget', child: Text('忘记这条')),
              ],
            ),
          ],
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(0, 10, 8, 4),
          child: Text(
            moment.text,
            style: const TextStyle(fontSize: 15, height: 1.5),
          ),
        ),
        Row(
          children: [
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
              decoration: BoxDecoration(
                color: Warm.panel,
                borderRadius: BorderRadius.circular(8),
              ),
              child: Text(
                _triggers[moment.trigger] ?? '动态',
                style: const TextStyle(fontSize: 11, color: Warm.muted),
              ),
            ),
            const Spacer(),
            IconButton(
              key: ValueKey('moment_like_${moment.id}'),
              tooltip: moment.liked ? '取消赞' : '赞',
              onPressed: () => toggleLike(moment),
              icon: Icon(
                moment.liked
                    ? Icons.favorite_rounded
                    : Icons.favorite_border_rounded,
                color: moment.liked ? Warm.warning : Warm.muted,
              ),
            ),
          ],
        ),
      ],
    ),
  );
}
