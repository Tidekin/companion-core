import 'dart:convert';
import 'dart:typed_data';

import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/ui/app.dart';
import 'package:companion_chat/ui/attachments.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

final pixel = base64Decode(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA'
  '60e6kgAAAABJRU5ErkJggg==',
);

ChatTurn turn() => ChatTurn.fromJson({
  'event_id': 'evt_s',
  'sequence': 1,
  'text': '抱抱我',
  'created_at': '2026-11-14T12:00:00Z',
  'state': 'SUCCEEDED',
  'redacted': false,
  'replies': [
    {'message_id': 'msg_a', 'text': '来啦。', 'state': 'SENT'},
    {'message_id': 'msg_b', 'text': '', 'state': 'SENT', 'sticker': true},
  ],
});

void main() {
  testWidgets('a meme part is its own bubble with the fetched image', (
    tester,
  ) async {
    final asked = <String>[];
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: TurnView(
            turn: turn(),
            loadSticker: (id) {
              asked.add(id);
              return Future.value(Uint8List.fromList(pixel));
            },
          ),
        ),
      ),
    );
    await tester.pump();
    expect(asked, ['msg_b']);
    expect(find.text('来啦。'), findsOneWidget);
    expect(find.byType(MessageImages), findsOneWidget);
  });

  testWidgets('without a sticker loader the empty part is not shown', (
    tester,
  ) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(body: TurnView(turn: turn())),
      ),
    );
    expect(find.byType(MessageImages), findsNothing);
    expect(turn().replies.last.sticker, isTrue);
  });
}
