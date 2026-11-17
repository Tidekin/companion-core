import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../chat/models.dart';
import '../chat/stickers.dart';
import 'attachments.dart';
import 'providers.dart';
import 'theme.dart';

/// Everyday emoji, grouped the way QQ's panel reads: faces first, then gestures,
/// hearts and small things. Unicode only; no bundled third-party artwork.
const emojiGroups = <(String, List<String>)>[
  (
    '表情',
    [
      '😀', '😁', '😂', '🤣', '😊', '😇', '🙂', '😉', '😍', '🥰', //
      '😘', '😋', '😛', '😜', '🤪', '🤗', '🤭', '🤔', '🤨', '😐', //
      '😑', '😶', '🙄', '😏', '😮', '😯', '😲', '😳', '🥺', '😢', //
      '😭', '😤', '😠', '😡', '🤯', '😱', '😨', '😰', '😓', '😪', //
      '😴', '🥱', '😌', '🤤', '😷', '🤒', '🤕', '🥴', '😵', '🤠', //
      '🥳', '😎', '🤓', '🧐', '😕', '🙁', '😟', '😣', '😖', '😫', //
    ],
  ),
  (
    '手势',
    [
      '👍', '👎', '👌', '✌️', '🤞', '🤟', '🤙', '👋', '👏', '🙌', //
      '🙏', '🤝', '💪', '👉', '👈', '👆', '👇', '✊', '👊', '🫶', //
    ],
  ),
  (
    '心情',
    [
      '❤️', '🧡', '💛', '💚', '💙', '💜', '🤍', '🖤', '💕', '💞', //
      '💗', '💖', '💔', '💯', '✨', '🌟', '🔥', '💤', '💢', '💦', //
    ],
  ),
  (
    '小物',
    [
      '🍚', '🍜', '🍰', '🍦', '🍓', '🍉', '☕', '🧋', '🍺', '🎂', //
      '🎁', '🎉', '🌸', '🌈', '☀️', '🌙', '⭐', '🐱', '🐶', '🐳', //
    ],
  ),
];

/// The panel under the composer: the owner's favorites first, then emoji groups.
/// [onEmoji] inserts at the caret; [onSticker] sends a favorite at once.
class StickerPanel extends ConsumerStatefulWidget {
  const StickerPanel({super.key, required this.onEmoji, this.onSticker});
  final ValueChanged<String> onEmoji;
  final ValueChanged<OutgoingImage>? onSticker;

  @override
  ConsumerState<StickerPanel> createState() => _StickerPanelState();
}

class _StickerPanelState extends ConsumerState<StickerPanel> {
  /// -1 is the favorites tab.
  int group = -1;

  void notice(String text) =>
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));

  Future<void> addFromGallery() async {
    try {
      final picked = await pickChatImages(remaining: 1, alreadyBytes: 0);
      if (picked.isEmpty) return;
      await ref.read(stickerStoreProvider).add(picked.single.bytes);
      ref.invalidate(stickersProvider);
    } on AttachPickError catch (error) {
      if (mounted) notice(error.message);
    } on StickerError catch (error) {
      if (mounted) notice(error.message);
    }
  }

  Future<void> remove(Sticker sticker) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('删除这个收藏？'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('取消'),
          ),
          FilledButton(
            key: const ValueKey('sticker_remove_confirm'),
            onPressed: () => Navigator.pop(context, true),
            child: const Text('删除'),
          ),
        ],
      ),
    );
    if (confirmed != true) return;
    await ref.read(stickerStoreProvider).remove(sticker.id);
    ref.invalidate(stickersProvider);
  }

  Widget favorites() {
    final stickers = ref.watch(stickersProvider).value ?? const <Sticker>[];
    return GridView.builder(
      padding: const EdgeInsets.all(8),
      gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
        maxCrossAxisExtent: 76,
        mainAxisSpacing: 6,
        crossAxisSpacing: 6,
      ),
      itemCount: stickers.length + 1,
      itemBuilder: (context, index) {
        if (index == 0) {
          return InkWell(
            key: const ValueKey('sticker_add'),
            borderRadius: BorderRadius.circular(12),
            onTap: addFromGallery,
            child: Ink(
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: Warm.line),
              ),
              child: const Icon(Icons.add_rounded, color: Warm.muted),
            ),
          );
        }
        final sticker = stickers[index - 1];
        return InkWell(
          key: ValueKey('sticker_${sticker.id}'),
          borderRadius: BorderRadius.circular(12),
          onTap: widget.onSticker == null
              ? null
              : () => widget.onSticker!(sticker.image),
          onLongPress: () => remove(sticker),
          child: ClipRRect(
            borderRadius: BorderRadius.circular(12),
            child: Image.memory(
              sticker.bytes,
              fit: BoxFit.cover,
              gaplessPlayback: true,
              errorBuilder: (_, _, _) =>
                  const Icon(Icons.broken_image_outlined, color: Warm.muted),
            ),
          ),
        );
      },
    );
  }

  Widget emoji(List<String> items) => GridView.builder(
    padding: const EdgeInsets.all(8),
    gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
      maxCrossAxisExtent: 44,
      mainAxisSpacing: 2,
      crossAxisSpacing: 2,
    ),
    itemCount: items.length,
    itemBuilder: (context, index) => InkWell(
      key: ValueKey('emoji_${items[index]}'),
      borderRadius: BorderRadius.circular(10),
      onTap: () => widget.onEmoji(items[index]),
      child: Center(
        child: Text(items[index], style: const TextStyle(fontSize: 24)),
      ),
    ),
  );

  Widget chip(int index, String label) => Padding(
    padding: const EdgeInsets.symmetric(horizontal: 2),
    child: ChoiceChip(
      key: ValueKey('emoji_group_$index'),
      label: Text(label),
      selected: index == group,
      showCheckmark: false,
      onSelected: (_) => setState(() => group = index),
    ),
  );

  @override
  Widget build(BuildContext context) {
    return Container(
      key: const ValueKey('sticker_panel'),
      height: 240,
      margin: const EdgeInsets.only(top: 8),
      decoration: BoxDecoration(
        color: Warm.composer,
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: Warm.bubbleBorder),
      ),
      child: Column(
        children: [
          Expanded(
            child: group < 0 ? favorites() : emoji(emojiGroups[group].$2),
          ),
          const Divider(height: 1, color: Warm.line),
          SizedBox(
            height: 40,
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 6),
              children: [
                chip(-1, '收藏'),
                for (var i = 0; i < emojiGroups.length; i++)
                  chip(i, emojiGroups[i].$1),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// Long-press menu for an image in the chat: keep it as a favorite.
Future<void> offerFavorite(BuildContext context, Uint8List bytes) async {
  final container = ProviderScope.containerOf(context, listen: false);
  final messenger = ScaffoldMessenger.maybeOf(context);
  final chosen = await showModalBottomSheet<bool>(
    context: context,
    builder: (context) => SafeArea(
      child: ListTile(
        key: const ValueKey('favorite_image'),
        leading: const Icon(Icons.favorite_border_rounded),
        title: const Text('收藏为表情'),
        onTap: () => Navigator.pop(context, true),
      ),
    ),
  );
  if (chosen != true) return;
  try {
    await container.read(stickerStoreProvider).add(bytes);
    container.invalidate(stickersProvider);
    messenger?.showSnackBar(const SnackBar(content: Text('已收藏')));
  } on StickerError catch (error) {
    messenger?.showSnackBar(SnackBar(content: Text(error.message)));
  }
}

/// Insert [text] at the controller's caret (or replace its selection).
void insertAtCaret(TextEditingController controller, String text) {
  final value = controller.value;
  final selection = value.selection;
  final start = selection.isValid ? selection.start : value.text.length;
  final end = selection.isValid ? selection.end : value.text.length;
  final updated = value.text.replaceRange(start, end, text);
  controller.value = value.copyWith(
    text: updated,
    selection: TextSelection.collapsed(offset: start + text.length),
    composing: TextRange.empty,
  );
}
