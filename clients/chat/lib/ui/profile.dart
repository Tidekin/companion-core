import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../chat/core_backend.dart';
import '../chat/models.dart';
import 'providers.dart';
import 'theme.dart';

String _time(DateTime value) {
  final local = value.toLocal();
  String two(int part) => part.toString().padLeft(2, '0');
  return '${local.month}月${local.day}日 ${two(local.hour)}:${two(local.minute)}';
}

String _message(Object error) => error is BackendError
    ? switch (error.code) {
        'PROFILE_SETTINGS_REVISION_CONFLICT' ||
        'PROFILE_REVISION_CONFLICT' => '资料刚刚在别处更新过，已刷新，请再试一次。',
        'PROFILE_ALIAS_INVALID' => '备注不能为空，也不能包含不可见字符。',
        'PROFILE_NO_CHANGE' => '当前已经是这个名字。',
        _ => '保存失败（${error.code}），已保留原来的设置。',
      }
    : '保存失败，已保留原来的设置。';

/// "形象与称呼": current committed profile, alias, self-update switch, history.
class ProfilePanel extends ConsumerStatefulWidget {
  const ProfilePanel({super.key, required this.bot, this.onClose});
  final BotProfile bot;
  final VoidCallback? onClose;
  @override
  ConsumerState<ProfilePanel> createState() => _ProfilePanelState();
}

class _ProfilePanelState extends ConsumerState<ProfilePanel> {
  bool saving = false;

  ChatBackend get _backend =>
      ref.read(registryProvider).session(widget.bot).backend;

  Future<void> run(Future<void> Function() action) async {
    setState(() => saving = true);
    try {
      await action();
    } catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(_message(error))));
      }
      ref.invalidate(profileProvider(widget.bot));
    } finally {
      if (mounted) setState(() => saving = false);
    }
  }

  Future<void> editAlias(CompanionProfile profile) async {
    final controller = TextEditingController(text: profile.viewerAlias ?? '');
    final result = await showDialog<(bool, String?)>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('你的备注'),
        content: SizedBox(
          width: 360,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                '只在你这边显示，不会改变「${profile.displayName}」自己的名字。',
                style: const TextStyle(fontSize: 13, color: Warm.muted),
              ),
              const SizedBox(height: 14),
              TextField(
                key: const ValueKey('profile_alias'),
                controller: controller,
                autofocus: true,
                maxLength: 32,
                decoration: const InputDecoration(labelText: '备注名'),
              ),
            ],
          ),
        ),
        actions: [
          if (profile.viewerAlias != null)
            TextButton(
              onPressed: () => Navigator.pop(context, (true, null)),
              child: const Text('清除备注'),
            ),
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('取消'),
          ),
          FilledButton(
            key: const ValueKey('save_alias'),
            onPressed: () => Navigator.pop(context, (
              true,
              controller.text.trim().isEmpty ? null : controller.text.trim(),
            )),
            child: const Text('保存'),
          ),
        ],
      ),
    );
    controller.dispose();
    if (result == null || !result.$1) return;
    await run(
      () => _backend.setProfileAlias(
        expectedSettingsRevision: profile.settingsRevision,
        alias: result.$2,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final profile = ref.watch(profileProvider(widget.bot)).value;
    return Material(
      color: Colors.transparent,
      child: ListView(
        padding: const EdgeInsets.fromLTRB(20, 18, 20, 24),
        children: [
          Row(
            children: [
              const Expanded(
                child: Text(
                  '形象与称呼',
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
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
          if (profile == null)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 24),
              child: Text(
                '登录后可查看伙伴的名字和变化记录。',
                style: TextStyle(color: Color(0xFF9B8877), fontSize: 13),
              ),
            )
          else ...[
            Center(
              child: AnimatedSwitcher(
                duration: Motion.reduced(context)
                    ? Duration.zero
                    : const Duration(milliseconds: 160),
                child: InitialAvatar(
                  key: ValueKey('avatar_${profile.revision}'),
                  id: widget.bot.id,
                  name: profile.displayName,
                  size: 88,
                ),
              ),
            ),
            const SizedBox(height: 14),
            Center(
              child: AnimatedSwitcher(
                duration: Motion.reduced(context)
                    ? Duration.zero
                    : const Duration(milliseconds: 120),
                child: Text(
                  profile.displayName,
                  key: ValueKey('name_${profile.revision}'),
                  textAlign: TextAlign.center,
                  style: const TextStyle(
                    fontSize: 22,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ),
            const Center(
              child: Text(
                'AI 伙伴',
                style: TextStyle(fontSize: 11, color: Color(0xFF6D7C76)),
              ),
            ),
            const SizedBox(height: 18),
            _Row(
              key: const ValueKey('profile_alias_row'),
              label: '你的备注',
              value: profile.viewerAlias ?? '未设置',
              onTap: saving ? null : () => editAlias(profile),
            ),
            _Row(
              label: '自行更新名字',
              trailing: saving
                  ? const SizedBox(
                      width: 20,
                      height: 20,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : Switch(
                      key: const ValueKey('profile_auto_name'),
                      value: profile.autoName,
                      onChanged: profile.selfUpdateEnabled
                          ? (value) => run(
                              () => _backend.updateProfileSettings(
                                expectedSettingsRevision:
                                    profile.settingsRevision,
                                autoName: value,
                              ),
                            )
                          : null,
                    ),
            ),
            if (!profile.selfUpdateEnabled)
              const Padding(
                padding: EdgeInsets.fromLTRB(4, 0, 4, 8),
                child: Text(
                  'Core 尚未开启伙伴自行改名。',
                  style: TextStyle(fontSize: 11, color: Warm.meta),
                ),
              ),
            _Row(
              key: const ValueKey('profile_history_row'),
              label: '变化记录',
              icon: Icons.history_rounded,
              onTap: () => showProfileChanges(context, ref, widget.bot),
            ),
            const SizedBox(height: 14),
            const Text(
              '更新后在聊天内轻量告知，不打断对话。名字变了，还是同一位伙伴，聊天记录和记忆不变。',
              style: TextStyle(fontSize: 11, height: 1.7, color: Warm.meta),
            ),
          ],
        ],
      ),
    );
  }
}

class _Row extends StatelessWidget {
  const _Row({
    super.key,
    required this.label,
    this.value,
    this.icon,
    this.trailing,
    this.onTap,
  });
  final String label;
  final String? value;
  final IconData? icon;
  final Widget? trailing;
  final VoidCallback? onTap;
  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(bottom: 8),
    child: Material(
      color: const Color(0x88FFFFFF),
      borderRadius: BorderRadius.circular(14),
      child: InkWell(
        borderRadius: BorderRadius.circular(14),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          child: Row(
            children: [
              if (icon != null) ...[
                Icon(icon, size: 18, color: Warm.accent),
                const SizedBox(width: 10),
              ],
              Expanded(
                child: Text(label, style: const TextStyle(fontSize: 14)),
              ),
              if (value != null)
                Flexible(
                  child: Text(
                    value!,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontSize: 12, color: Warm.meta),
                  ),
                ),
              ?trailing,
              if (trailing == null && onTap != null)
                const Icon(
                  Icons.chevron_right_rounded,
                  size: 18,
                  color: Warm.meta,
                ),
            ],
          ),
        ),
      ),
    ),
  );
}

/// The real change history; restore writes a new revision and never erases records.
Future<void> showProfileChanges(
  BuildContext context,
  WidgetRef ref,
  BotProfile bot, {
  ProfileChange? focus,
}) => showWarmSheet<void>(
  context,
  (context) => _ChangesSheet(bot: bot, focus: focus),
);

class _ChangesSheet extends ConsumerWidget {
  const _ChangesSheet({required this.bot, this.focus});
  final BotProfile bot;
  final ProfileChange? focus;

  Future<void> restore(
    BuildContext context,
    WidgetRef ref,
    CompanionProfile profile,
    ProfileChange change,
  ) async {
    var pause = false;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setState) => AlertDialog(
          title: const Text('恢复这个名字？'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('会把名字改回「${change.newName}」，作为一条新的记录保存，原有记录都会保留。'),
              const SizedBox(height: 12),
              CheckboxListTile(
                key: const ValueKey('restore_pause_name'),
                contentPadding: EdgeInsets.zero,
                value: pause,
                onChanged: (value) => setState(() => pause = value ?? false),
                title: const Text('同时暂停伙伴自行更新名字'),
              ),
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('取消'),
            ),
            FilledButton(
              key: const ValueKey('confirm_restore'),
              onPressed: () => Navigator.pop(context, true),
              child: const Text('恢复'),
            ),
          ],
        ),
      ),
    );
    if (confirmed != true) return;
    try {
      await ref
          .read(registryProvider)
          .session(bot)
          .backend
          .restoreProfile(
            changeId: change.changeId,
            expectedRevision: profile.revision,
            pauseName: pause,
            operationId: 'restore-${randomId()}',
          );
    } catch (error) {
      if (context.mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(_message(error))));
      }
    }
    ref.invalidate(profileProvider(bot));
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final profile = ref.watch(profileProvider(bot)).value;
    final changes = ref.watch(profileChangesProvider(bot)).value ?? const [];
    return ConstrainedBox(
      constraints: BoxConstraints(
        maxHeight: MediaQuery.sizeOf(context).height * 0.75,
      ),
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.fromLTRB(20, 0, 20, 24),
        children: [
          const Text(
            '变化记录',
            style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 4),
          const Text(
            '每次名字变化都会保留，恢复也会记成一条新记录。',
            style: TextStyle(fontSize: 12, color: Warm.meta),
          ),
          const SizedBox(height: 14),
          if (changes.isEmpty)
            const Text('还没有记录。', style: TextStyle(color: Warm.meta)),
          for (final change in changes)
            Container(
              key: ValueKey('change_${change.changeId}'),
              margin: const EdgeInsets.only(bottom: 10),
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(
                color: change.changeId == focus?.changeId
                    ? Warm.conversationActive
                    : const Color(0x99FFFFFF),
                borderRadius: BorderRadius.circular(16),
                border: Border.all(color: Warm.bubbleBorder),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Text(
                        change.initial ? '初始名字' : change.actorLabel,
                        style: const TextStyle(
                          fontSize: 11,
                          color: Color(0xFF6D7C76),
                        ),
                      ),
                      const Spacer(),
                      Text(
                        '${_time(change.createdAt)} · 版本 ${change.newRevision}',
                        style: const TextStyle(fontSize: 11, color: Warm.meta),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Text(
                    change.oldName == null
                        ? change.newName
                        : '${change.oldName}  →  ${change.newName}',
                    style: const TextStyle(fontSize: 15),
                  ),
                  if (change.note case final note?) ...[
                    const SizedBox(height: 4),
                    Text(
                      note,
                      style: const TextStyle(fontSize: 12, color: Warm.meta),
                    ),
                  ],
                  if (profile != null && change.newName != profile.displayName)
                    Align(
                      alignment: Alignment.centerRight,
                      child: TextButton(
                        key: ValueKey('restore_${change.changeId}'),
                        onPressed: () => restore(context, ref, profile, change),
                        child: const Text('恢复这个名字'),
                      ),
                    ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

/// A real system event in the timeline: only committed changes, never a demo
/// or pending update.
class ProfileEventRow extends ConsumerWidget {
  const ProfileEventRow({super.key, required this.bot, required this.change});
  final BotProfile bot;
  final ProfileChange change;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final text = change.actorType == 'companion'
        ? '${change.oldName ?? '伙伴'} 把名字改成了「${change.newName}」'
        : change.actorType == 'owner'
        ? '你把伙伴的名字恢复为「${change.newName}」'
        : '名字调整为「${change.newName}」';
    return Center(
      child: Container(
        margin: const EdgeInsets.fromLTRB(0, 6, 0, 18),
        padding: const EdgeInsets.fromLTRB(12, 6, 6, 6),
        decoration: BoxDecoration(
          color: Warm.panel,
          borderRadius: BorderRadius.circular(16),
          border: Border.all(color: const Color(0x77FFFFFF)),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.badge_outlined, size: 15, color: Warm.accent),
            const SizedBox(width: 6),
            Flexible(
              child: Text(
                text,
                style: const TextStyle(fontSize: 12, color: Color(0xFF7C6C5E)),
              ),
            ),
            TextButton(
              key: ValueKey('view_change_${change.changeId}'),
              onPressed: () =>
                  showProfileChanges(context, ref, bot, focus: change),
              child: const Text('查看变化', style: TextStyle(fontSize: 12)),
            ),
          ],
        ),
      ),
    );
  }
}
