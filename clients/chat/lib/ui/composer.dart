import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../chat/models.dart';
import '../chat/session.dart';
import 'attachments.dart';
import 'stickers.dart';
import 'theme.dart';

class ChatComposer extends StatefulWidget {
  const ChatComposer({
    super.key,
    required this.session,
    required this.ready,
    required this.compact,
    required this.onSent,
  });
  final BotSession session;
  final bool ready;
  final bool compact;
  final VoidCallback onSent;

  @override
  State<ChatComposer> createState() => _ChatComposerState();
}

class _ChatComposerState extends State<ChatComposer> {
  final input = TextEditingController();
  // Images picked for the next message, in order.
  final attachments = <OutgoingImage>[];
  bool stickersOpen = false;
  Timer? draftTimer;
  String lastDraft = '';
  bool sending = false;
  bool initialized = false;

  BotSession get session => widget.session;

  @override
  void initState() {
    super.initState();
    final active = session;
    active.loaded.then((_) {
      if (!mounted) return;
      input.text = lastDraft = active.draft;
      initialized = true;
    });
  }

  Future<void> attach() async {
    try {
      final picked = await pickChatImages(
        remaining: maxImagesPerMessage - attachments.length,
        alreadyBytes: attachments.fold(
          0,
          (sum, image) => sum + image.bytes.length,
        ),
      );
      if (picked.isNotEmpty && mounted) {
        setState(() => attachments.addAll(picked));
      }
    } on AttachPickError catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(error.message)));
      }
    }
  }

  /// A favorite goes out alone, like tapping a sticker in QQ; the draft stays.
  Future<void> sendSticker(OutgoingImage sticker) async {
    if (sending) return;
    setState(() => sending = true);
    try {
      await session.send('', images: [sticker]);
    } finally {
      if (mounted) setState(() => sending = false);
    }
  }

  Future<void> send() async {
    final text = input.text;
    if (sending ||
        text.trim().isEmpty && attachments.isEmpty ||
        input.value.composing.isValid && !input.value.composing.isCollapsed) {
      return;
    }
    setState(() => sending = true);
    final active = session;
    try {
      await active.send(text, images: List.of(attachments));
      if (mounted) setState(attachments.clear);
      if (lastDraft == text) {
        lastDraft = '';
        if (mounted) input.clear();
      }
      await active.setDraft(lastDraft);
      if (mounted) widget.onSent();
    } on BackendError catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(error.code)));
      }
    } finally {
      if (mounted) setState(() => sending = false);
    }
  }

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      if (attachments.isNotEmpty)
        AttachmentStrip(
          key: ValueKey('attachments_${session.bot.id}'),
          images: attachments,
          onRemove: (index) => setState(() => attachments.removeAt(index)),
        ),
      Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          Expanded(
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 14),
              decoration: BoxDecoration(
                color: Warm.composer,
                borderRadius: BorderRadius.circular(16),
                border: Border.all(color: Warm.bubbleBorder),
              ),
              child: CallbackShortcuts(
                bindings: {
                  const SingleActivator(
                    LogicalKeyboardKey.enter,
                    control: true,
                  ): send,
                },
                child: TextField(
                  key: ValueKey('compose_${session.bot.id}'),
                  onTap: () {
                    if (stickersOpen) {
                      setState(() => stickersOpen = false);
                    }
                  },
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
                    contentPadding: EdgeInsets.symmetric(vertical: 12),
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
          ),
          const SizedBox(width: 8),
          _SendButton(
            key: ValueKey('send_${session.bot.id}'),
            onPressed: !widget.ready || sending ? null : send,
          ),
        ],
      ),
      Padding(
        padding: const EdgeInsets.only(top: 4),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.spaceAround,
          children: [
            IconButton(
              key: ValueKey('attach_${session.bot.id}'),
              tooltip: '添加图片',
              color: Warm.textSoft,
              iconSize: 28,
              onPressed:
                  !widget.ready ||
                      sending ||
                      attachments.length >= maxImagesPerMessage
                  ? null
                  : attach,
              icon: const Icon(Icons.image_outlined),
            ),
            IconButton(
              key: ValueKey('stickers_${session.bot.id}'),
              tooltip: stickersOpen ? '收起表情' : '表情',
              color: stickersOpen ? Warm.accent : Warm.textSoft,
              iconSize: 28,
              onPressed: () {
                // Like QQ: the panel takes the keyboard's place.
                if (!stickersOpen) FocusScope.of(context).unfocus();
                setState(() => stickersOpen = !stickersOpen);
              },
              icon: const Icon(Icons.sentiment_satisfied_alt_outlined),
            ),
          ],
        ),
      ),
      if (stickersOpen)
        StickerPanel(
          onEmoji: (emoji) {
            insertAtCaret(input, emoji);
            lastDraft = input.text;
            unawaited(session.setDraft(input.text));
          },
          onSticker: widget.ready && !sending ? sendSticker : null,
        ),
      if (!widget.compact)
        const Padding(
          padding: EdgeInsets.fromLTRB(18, 6, 0, 0),
          child: Text(
            'Ctrl + Enter 发送 · Enter 换行',
            style: TextStyle(fontSize: 10, color: Color(0xFFA39A91)),
          ),
        ),
    ],
  );

  @override
  void dispose() {
    draftTimer?.cancel();
    if (initialized) unawaited(session.setDraft(lastDraft));
    input.dispose();
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
            height: 44,
            padding: const EdgeInsets.symmetric(horizontal: 18),
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: Warm.accent,
              borderRadius: BorderRadius.circular(14),
            ),
            child: const Text(
              '发送',
              style: TextStyle(
                color: Colors.white,
                fontSize: 15,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ),
      ),
    ),
  );
}
