import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/ui/app.dart';
import 'package:companion_chat/ui/typing.dart';

ChatTurn turn(
  String state, {
  bool canReplyNow = false,
  ChatPreview? preview,
  List<ChatReply> replies = const [],
}) => ChatTurn(
  id: 't',
  sequence: 1,
  text: 'question',
  createdAt: DateTime.utc(2026),
  state: state,
  canReplyNow: canReplyNow,
  preview: preview,
  replies: replies,
);

Widget host(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  testWidgets(
    'only a queued turn with an advertised input window offers reply now',
    (tester) async {
      var taps = 0;
      Future<void> show(ChatTurn value) => tester.pumpWidget(
        host(TurnView(turn: value, onReplyNow: () => taps++)),
      );
      await show(turn('QUEUED'));
      expect(find.text('立即回复'), findsNothing);
      await show(turn('QUEUED', canReplyNow: true));
      await tester.tap(find.text('立即回复'));
      expect(taps, 1);
      await show(turn('RUNNING', canReplyNow: true));
      expect(find.text('立即回复'), findsNothing);
    },
  );

  test('streamed text splits into the same bubbles as the final reply', () {
    expect(bubbleLines('第一句。\n\n第二句？\n'), ['第一句。', '第二句？']);
    expect(bubbleLines('  '), isEmpty);
  });

  testWidgets('a running reply without text shows typing dots', (tester) async {
    await tester.pumpWidget(host(TurnView(turn: turn('RUNNING'))));
    expect(find.byType(TypingDots), findsOneWidget);
    expect(find.text('正在回复'), findsNothing);
    await tester.pumpWidget(
      host(
        TurnView(
          turn: turn(
            'SUCCEEDED',
            replies: const [ChatReply(id: 'r', text: '好的', state: 'SENT')],
          ),
        ),
      ),
    );
    expect(find.byType(TypingDots), findsNothing);
  });

  testWidgets('streamed preview is revealed gradually, one bubble per line', (
    tester,
  ) async {
    await tester.pumpWidget(
      host(
        TurnView(
          turn: turn(
            'RUNNING',
            preview: const ChatPreview(1, '第一行。\n第二行正在生成', 'GENERATING'),
          ),
        ),
      ),
    );
    expect(find.text('第一行。'), findsOneWidget);
    // The first grapheme paints immediately while the rest catches up.
    expect(find.text('第'), findsOneWidget);
    expect(find.text('第二行正在生成'), findsNothing);
    await tester.pump(const Duration(seconds: 1));
    expect(find.text('第二行正在生成'), findsOneWidget);
    expect(find.byType(TypingDots), findsNothing);
  });

  testWidgets(
    'first frame renders a whole grapheme and replacement starts fresh',
    (tester) async {
      await tester.pumpWidget(host(const RevealText('👩‍🔬正在输入')));
      expect(find.text('👩‍🔬'), findsOneWidget);
      await tester.pumpWidget(host(const RevealText('👩‍🚀更新')));
      expect(find.text('👩‍🚀'), findsOneWidget);
    },
  );
}
