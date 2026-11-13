import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/ui/app.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  testWidgets('a message the companion started has no owner bubble', (
    tester,
  ) async {
    final turn = ChatTurn.fromJson({
      'event_id': 'evt_p',
      'sequence': 3,
      'kind': 'proactive',
      'text': '',
      'created_at': '2026-11-13T12:00:00Z',
      'state': 'SUCCEEDED',
      'redacted': false,
      'replies': [
        {'message_id': 'm1', 'text': '今天那只猫又来了吗', 'state': 'SENT'},
      ],
    });
    expect(turn.proactive, isTrue);
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(body: TurnView(turn: turn)),
      ),
    );
    expect(find.text('今天那只猫又来了吗'), findsOneWidget);
    expect(find.byType(Bubble), findsOneWidget);
  });

  test('ordinary turns are not proactive', () {
    final turn = ChatTurn.fromJson({
      'event_id': 'evt_o',
      'sequence': 1,
      'text': '你好',
      'created_at': '2026-11-13T12:00:00Z',
      'state': 'SUCCEEDED',
      'redacted': false,
      'replies': [],
    });
    expect(turn.proactive, isFalse);
  });
}
