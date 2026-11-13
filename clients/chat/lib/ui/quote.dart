import 'package:flutter/material.dart';

import '../chat/models.dart';
import 'theme.dart';

/// What a quote points at, as this device last synchronized it.
class QuotedMessage {
  const QuotedMessage({
    required this.turnId,
    required this.owner,
    required this.text,
  });

  /// The turn to scroll to.
  final String turnId;

  /// Whether the owner wrote it (otherwise the companion did).
  final bool owner;

  /// Empty when the message was deleted or is not available here.
  final String text;
}

/// Resolves quote ids against the turns already on this device.
class QuoteLookup {
  QuoteLookup(Iterable<ChatTurn> turns) {
    for (final turn in turns) {
      _owner[turn.id] = QuotedMessage(
        turnId: turn.id,
        owner: true,
        text: turn.redacted ? '' : turn.text.trim(),
      );
      for (final reply in turn.replies) {
        _replies[reply.id] = QuotedMessage(
          turnId: turn.id,
          owner: false,
          text: turn.redacted ? '' : reply.text.trim(),
        );
      }
    }
  }

  final _owner = <String, QuotedMessage>{};
  final _replies = <String, QuotedMessage>{};

  QuotedMessage? find(ChatQuote quote) =>
      quote.eventId != null ? _owner[quote.eventId] : _replies[quote.messageId];
}

/// A small quoted line above a reply; tapping it jumps to the original.
class QuoteBox extends StatelessWidget {
  const QuoteBox({
    super.key,
    required this.message,
    required this.botName,
    this.onTap,
  });

  /// Null when the original is not on this device.
  final QuotedMessage? message;
  final String botName;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final found = message;
    final available = found != null && found.text.isNotEmpty;
    final who = found == null ? '' : (found.owner ? '你' : botName);
    final label = available
        ? '$who：${found.text.replaceAll(RegExp(r'\s+'), ' ')}'
        : '原消息已不可用';
    return Semantics(
      button: available && onTap != null,
      label: '引用 $label',
      excludeSemantics: true,
      child: MouseRegion(
        cursor: available && onTap != null
            ? SystemMouseCursors.click
            : MouseCursor.defer,
        child: GestureDetector(
          onTap: available ? onTap : null,
          child: Container(
            margin: const EdgeInsets.only(bottom: 6),
            padding: const EdgeInsets.fromLTRB(9, 4, 10, 4),
            decoration: BoxDecoration(
              color: Warm.accentSoft.withValues(alpha: 0.10),
              borderRadius: BorderRadius.circular(6),
              border: const Border(
                left: BorderSide(color: Warm.accent, width: 2.5),
              ),
            ),
            child: Text(
              label,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(
                fontSize: 12.5,
                height: 1.45,
                color: Warm.meta,
              ),
            ),
          ),
        ),
      ),
    );
  }
}
