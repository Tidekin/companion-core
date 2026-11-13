import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';

import 'theme.dart';

/// Three softly pulsing dots shown on the companion's side while a reply is
/// being prepared and no preview text has arrived yet.
class TypingDots extends StatefulWidget {
  const TypingDots({super.key});
  @override
  State<TypingDots> createState() => _TypingDotsState();
}

class _TypingDotsState extends State<TypingDots>
    with SingleTickerProviderStateMixin {
  late final AnimationController controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 1200),
  );

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (Motion.reduced(context)) {
      controller.stop();
    } else if (!controller.isAnimating) {
      controller.repeat();
    }
  }

  @override
  void dispose() {
    controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Semantics(
    label: '正在输入',
    child: AnimatedBuilder(
      animation: controller,
      builder: (context, _) => Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          for (var i = 0; i < 3; i++)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 2.5),
              child: Opacity(
                opacity: _opacity(i),
                child: Container(
                  width: 7,
                  height: 7,
                  decoration: const BoxDecoration(
                    color: Warm.textSoft,
                    shape: BoxShape.circle,
                  ),
                ),
              ),
            ),
        ],
      ),
    ),
  );

  double _opacity(int index) {
    if (!controller.isAnimating) return 0.6;
    final phase = (controller.value - index * 0.18) % 1.0;
    // A short rise and fall per dot, staggered left to right.
    return 0.3 + 0.7 * (phase < 0.5 ? phase * 2 : (1 - phase) * 2);
  }
}

/// Reveals [text] at a steady reading pace instead of in network-sized bursts.
/// It catches up faster when far behind, so it never lags far behind the stream.
class RevealText extends StatefulWidget {
  const RevealText(this.text, {super.key, this.style});
  final String text;
  final TextStyle? style;
  @override
  State<RevealText> createState() => _RevealTextState();
}

class _RevealTextState extends State<RevealText>
    with SingleTickerProviderStateMixin {
  late final Ticker ticker = createTicker(_tick);
  double shown = 0;
  Duration last = Duration.zero;

  @override
  void initState() {
    super.initState();
    shown = 0;
    ticker.start();
  }

  @override
  void didUpdateWidget(RevealText old) {
    super.didUpdateWidget(old);
    if (!widget.text.startsWith(old.text.substring(0, shown.floor()))) {
      shown = 0; // A different text: start over rather than show a mismatch.
    }
    if (!ticker.isActive) {
      last = Duration.zero;
      ticker.start();
    }
  }

  void _tick(Duration elapsed) {
    final runes = widget.text.characters.length;
    if (Motion.reduced(context)) {
      setState(() => shown = runes.toDouble());
      ticker.stop();
      return;
    }
    final seconds = (elapsed - last).inMicroseconds / 1e6;
    last = elapsed;
    final behind = runes - shown;
    if (behind <= 0) {
      ticker.stop();
      return;
    }
    // About 30 characters a second, faster while there is a backlog.
    final speed = 30 + behind * 2.5;
    setState(
      () => shown = (shown + speed * seconds).clamp(0, runes).toDouble(),
    );
  }

  @override
  void dispose() {
    ticker.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Text(
    widget.text.characters.take(shown.floor()).toString(),
    style: widget.style,
  );
}

/// Splits streamed or final text into the bubbles the semantic segmenter
/// would produce: one per non-empty line.
List<String> bubbleLines(String text) => [
  for (final line in text.split('\n'))
    if (line.trim().isNotEmpty) line.trim(),
];
