import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/ui/app.dart';
import 'package:companion_chat/ui/theme.dart';

ChatTurn turn(String state, {bool uncertain = false}) => ChatTurn(
  id: 't_$state',
  sequence: 1,
  text: 'question',
  createdAt: DateTime.now(),
  state: state,
  uncertain: uncertain,
  replies: state == 'SUCCEEDED'
      ? [const ChatReply(id: 'r', text: 'answer', state: 'SENT')]
      : [],
);

Widget host(Widget child, {bool reduceMotion = false}) => MaterialApp(
  theme: warmTheme(),
  home: MediaQuery(
    data: MediaQueryData(disableAnimations: reduceMotion),
    child: Scaffold(body: child),
  ),
);

void main() {
  test('sent time is short today and dated otherwise', () {
    final now = DateTime(2026, 11, 11, 15, 30);
    expect(sentAt(DateTime(2026, 11, 11, 9, 5), now: now), '09:05');
    expect(sentAt(DateTime(2026, 3, 2, 21, 0), now: now), '3月2日 21:00');
    expect(sentAt(DateTime(2025, 12, 31, 8, 0), now: now), '2025年12月31日 08:00');
    expect(sentAt(DateTime(2026, 3, 2, 21, 0), now: now, short: true), '3/2');
    expect(
      sentAt(DateTime(2026, 11, 11, 9, 5), now: now, short: true),
      '09:05',
    );
  });

  testWidgets('completed turns show time; other states stay explicit', (
    tester,
  ) async {
    await tester.pumpWidget(host(TurnView(turn: turn('SUCCEEDED'))));
    expect(find.text('已完成'), findsNothing);
    expect(find.text(sentAt(turn('SUCCEEDED').createdAt)), findsOneWidget);

    await tester.pumpWidget(host(TurnView(turn: turn('FAILED'))));
    expect(find.text('回复失败了，可以再发一次'), findsOneWidget);
    // Failure is not shown by colour alone.
    expect(find.byIcon(Icons.error_outline_rounded), findsOneWidget);

    await tester.pumpWidget(
      host(TurnView(turn: turn('SUCCEEDED', uncertain: true))),
    );
    expect(find.text('这条回复没有确认送达'), findsOneWidget);
  });

  testWidgets('entrance plays once and is skipped under reduced motion', (
    tester,
  ) async {
    Opacity opacity() => tester.widget<Opacity>(
      find
          .descendant(of: find.byType(Appear), matching: find.byType(Opacity))
          .first,
    );
    await tester.pumpWidget(host(const Appear(child: Text('new message'))));
    expect(opacity().opacity, lessThan(1));
    await tester.pumpAndSettle();
    expect(opacity().opacity, 1);

    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pumpWidget(
      host(const Appear(child: Text('new message')), reduceMotion: true),
    );
    expect(opacity().opacity, 1);

    await tester.pumpWidget(const SizedBox.shrink());
    await tester.pumpWidget(
      host(const Appear(animate: false, child: Text('history'))),
    );
    expect(opacity().opacity, 1);
  });
}
