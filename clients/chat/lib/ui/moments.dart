import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../chat/core_backend.dart';
import '../chat/models.dart';
import 'attachments.dart';
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

const _triggers = {'OWNER': '我发的', 'LIFE': '她自己想说的', 'MEMORY': '记住了一件重要的事'};

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
  final composer = TextEditingController();
  List<OutgoingImage> draftImages = [];
  bool posting = false;
  final openThreads = <String>{};
  final threads = <String, List<MomentComment>>{};
  final commentFields = <String, TextEditingController>{};

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
      final open = {
        for (final id in openThreads) id: await _backend.momentComments(id),
      };
      if (!mounted) return;
      setState(() {
        items = feed;
        memories = past;
        threads.addAll(open);
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
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(_failure(failure))));
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

  /// Signs and sends the owner's post or comment the same way as a chat message.
  Future<void> send(
    String text,
    Map<String, dynamic> moment, {
    List<OutgoingImage> images = const [],
  }) async {
    final pending = await _backend.prepare(
      text,
      randomId(),
      DateTime.now(),
      images: images,
      moment: moment,
    );
    await _backend.submit(pending);
  }

  Future<void> post() async {
    final text = composer.text.trim();
    if (text.isEmpty && draftImages.isEmpty) return;
    setState(() => posting = true);
    await run(() async {
      await send(text, {'kind': 'post'}, images: draftImages);
      composer.clear();
      draftImages = [];
    });
    if (mounted) setState(() => posting = false);
  }

  Future<void> pickImages() async {
    try {
      final picked = await pickChatImages(
        remaining: maxImagesPerMessage - draftImages.length,
        alreadyBytes: draftImages.fold(
          0,
          (sum, item) => sum + item.bytes.length,
        ),
      );
      if (picked.isNotEmpty && mounted) {
        setState(() => draftImages = [...draftImages, ...picked]);
      }
    } on AttachPickError catch (failure) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(failure.message)));
      }
    }
  }

  Future<void> toggleThread(Moment moment) async {
    if (openThreads.remove(moment.id)) {
      setState(() {});
      return;
    }
    openThreads.add(moment.id);
    await run(() async {
      threads[moment.id] = await _backend.momentComments(moment.id);
    });
  }

  Future<void> comment(Moment moment) async {
    final field = commentFields[moment.id];
    final text = field?.text.trim() ?? '';
    if (text.isEmpty) return;
    await run(() async {
      await send(text, {'kind': 'comment', 'moment_id': moment.id});
      field!.clear();
      openThreads.add(moment.id);
    });
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
    composer.dispose();
    for (final field in commentFields.values) {
      field.dispose();
    }
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
            _composer(),
            if (memories.isNotEmpty) _memories(now),
            if (error != null)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 12),
                child: Text(
                  error!,
                  style: const TextStyle(color: Warm.warning),
                ),
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

  Widget _composer() => Container(
    key: const ValueKey('moment_composer'),
    margin: const EdgeInsets.only(bottom: 14),
    padding: const EdgeInsets.fromLTRB(12, 6, 6, 6),
    decoration: BoxDecoration(
      color: Warm.composer,
      borderRadius: BorderRadius.circular(16),
      border: Border.all(color: Warm.bubbleBorder),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (draftImages.isNotEmpty)
          AttachmentStrip(
            images: draftImages,
            onRemove: (index) => setState(() => draftImages.removeAt(index)),
          ),
        Row(
          children: [
            Expanded(
              child: TextField(
                key: const ValueKey('moment_post_text'),
                controller: composer,
                minLines: 1,
                maxLines: 4,
                maxLength: 500,
                decoration: const InputDecoration(
                  hintText: '发一条动态，她会看到',
                  border: InputBorder.none,
                  counterText: '',
                ),
              ),
            ),
            IconButton(
              tooltip: '添加图片',
              onPressed: posting ? null : pickImages,
              icon: const Icon(Icons.image_outlined, color: Warm.muted),
            ),
            FilledButton(
              key: const ValueKey('moment_post_send'),
              onPressed: posting ? null : post,
              child: const Text('发布'),
            ),
          ],
        ),
      ],
    ),
  );

  Widget _thread(Moment moment, String name) {
    final field = commentFields.putIfAbsent(
      moment.id,
      TextEditingController.new,
    );
    final entries = threads[moment.id] ?? const <MomentComment>[];
    return Padding(
      padding: const EdgeInsets.fromLTRB(0, 4, 8, 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          for (final entry in entries)
            Padding(
              padding: const EdgeInsets.only(bottom: 4),
              child: Text.rich(
                TextSpan(
                  children: [
                    TextSpan(
                      text: entry.author == 'owner' ? '我：' : '$name：',
                      style: const TextStyle(fontWeight: FontWeight.w600),
                    ),
                    TextSpan(text: entry.text),
                  ],
                ),
                style: const TextStyle(fontSize: 13, color: Warm.textSoft),
              ),
            ),
          Row(
            children: [
              Expanded(
                child: TextField(
                  key: ValueKey('moment_comment_text_${moment.id}'),
                  controller: field,
                  maxLength: 300,
                  decoration: const InputDecoration(
                    hintText: '评论…',
                    isDense: true,
                    counterText: '',
                  ),
                  onSubmitted: (_) => comment(moment),
                ),
              ),
              IconButton(
                key: ValueKey('moment_comment_send_${moment.id}'),
                tooltip: '发送评论',
                onPressed: () => comment(moment),
                icon: const Icon(Icons.send_rounded, color: Warm.accent),
              ),
            ],
          ),
        ],
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
            moment.mine
                ? const CircleAvatar(
                    radius: 16,
                    backgroundColor: Warm.accentSoft,
                    child: Icon(Icons.person, size: 18, color: Warm.accent),
                  )
                : CompanionAvatar(bot: widget.bot, name: name, size: 32),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    moment.mine ? '我' : name,
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
              itemBuilder: (_) => [
                if (!moment.mine)
                  const PopupMenuItem(value: 'hide', child: Text('隐藏')),
                PopupMenuItem(
                  value: 'forget',
                  child: Text(moment.mine ? '删除这条' : '忘记这条'),
                ),
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
        if (moment.imageSlots.isNotEmpty)
          MessageImages(
            images: [
              for (final slot in moment.imageSlots)
                _backend.messageImage(moment.id, slot),
            ],
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
            TextButton.icon(
              key: ValueKey('moment_comments_${moment.id}'),
              onPressed: () => toggleThread(moment),
              icon: const Icon(Icons.mode_comment_outlined, size: 18),
              label: Text(
                moment.commentCount > 0 ? '${moment.commentCount}' : '评论',
              ),
            ),
            if (!moment.mine)
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
        if (openThreads.contains(moment.id)) _thread(moment, name),
      ],
    ),
  );
}

/// The chat page's card for her newest post since the owner last looked (QQ-style):
/// a short preview near the composer that opens the moments panel.
class MomentPreviewCard extends ConsumerStatefulWidget {
  const MomentPreviewCard({super.key, required this.bot});
  final BotProfile bot;
  @override
  ConsumerState<MomentPreviewCard> createState() => _MomentPreviewCardState();
}

class _MomentPreviewCardState extends ConsumerState<MomentPreviewCard> {
  static const _seen = 'moments_seen_at';
  Moment? latest;
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
      final raw =
          await ref.read(repositoryProvider).meta(widget.bot.id, _seen)
              as String?;
      final item = await _backend.latestMoment(
        raw == null ? null : DateTime.tryParse(raw),
      );
      if (mounted) setState(() => latest = item);
    } catch (_) {
      // A card is a convenience; the moments panel stays reachable from the header.
    }
  }

  Future<void> markSeen() async {
    final item = latest;
    setState(() => latest = null);
    if (item == null) return;
    await ref
        .read(repositoryProvider)
        .setMeta(
          widget.bot.id,
          _seen,
          item.createdAt.toUtc().toIso8601String(),
        );
  }

  @override
  void dispose() {
    updates?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final item = latest;
    if (item == null) return const SizedBox.shrink();
    final profile = ref.watch(profileProvider(widget.bot)).value;
    final name = profile?.shownName ?? widget.bot.name;
    return Center(
      child: Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: Material(
          color: Warm.bubble,
          borderRadius: BorderRadius.circular(14),
          child: InkWell(
            key: ValueKey('moment_preview_${widget.bot.id}'),
            borderRadius: BorderRadius.circular(14),
            onTap: () async {
              await markSeen();
              if (context.mounted) {
                await showWarmSheet<void>(
                  context,
                  (context) => MomentsPanel(bot: widget.bot),
                );
              }
            },
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 360),
              child: Padding(
                padding: const EdgeInsets.fromLTRB(12, 8, 4, 8),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Icon(
                      Icons.auto_awesome_outlined,
                      size: 18,
                      color: Warm.accent,
                    ),
                    const SizedBox(width: 8),
                    Flexible(
                      child: Text(
                        '$name发了动态：${item.text}',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          fontSize: 13,
                          color: Warm.textSoft,
                        ),
                      ),
                    ),
                    IconButton(
                      tooltip: '知道了',
                      visualDensity: VisualDensity.compact,
                      onPressed: markSeen,
                      icon: const Icon(
                        Icons.close_rounded,
                        size: 16,
                        color: Warm.muted,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
