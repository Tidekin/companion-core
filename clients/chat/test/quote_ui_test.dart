import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/ui/app.dart';
import 'package:companion_chat/ui/quote.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

ChatTurn turn(String id, String text, List<Map<String, dynamic>> replies) =>
    ChatTurn.fromJson({
      'event_id': id,
      'sequence': 1,
      'text': text,
      'created_at': '2026-11-13T12:00:00Z',
      'state': 'SUCCEEDED',
      'redacted': false,
      'replies': replies,
    });

Map<String, dynamic> reply(String id, String text, [Object? quote]) => {
  'message_id': id,
  'text': text,
  'state': 'SENT',
  'quote': ?quote,
};

void main() {
  test('a quote names an owner turn or an earlier reply', () {
    expect(ChatQuote.fromJson({'event_id': 'e'})?.eventId, 'e');
    expect(ChatQuote.fromJson({'message_id': 'm'})?.messageId, 'm');
    expect(ChatQuote.fromJson(null), isNull);
    expect(ChatQuote.fromJson({'other': 1}), isNull);
  });

  testWidgets('a reply shows what it quotes and jumps to it', (tester) async {
    final first = turn('t1', '猫把纸箱挤塌了', [reply('m1', '它是不是很得意')]);
    final second = turn('t2', '换个话题', [
      reply('m2', '刚才那只猫后来呢', {'event_id': 't1'}),
      reply('m3', '还有', {'message_id': 'm1'}),
    ]);
    final third = turn('t3', '嗯', [
      reply('m4', '对了', {'event_id': 'gone'}),
    ]);
    final lookup = QuoteLookup([first, second, third]);
    final jumps = <String>[];
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: ListView(
            children: [
              for (final t in [second, third])
                TurnView(turn: t, quotes: lookup, onQuoteTap: jumps.add),
            ],
          ),
        ),
      ),
    );
    expect(find.text('你：猫把纸箱挤塌了'), findsOneWidget);
    expect(find.text('TA：它是不是很得意'), findsOneWidget);
    expect(find.text('原消息已不可用'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('quote_m2')));
    await tester.tap(find.byKey(const ValueKey('quote_m4')));
    expect(jumps, ['t1']);
  });
}
