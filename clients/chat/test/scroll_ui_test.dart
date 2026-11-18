import 'dart:async';

import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/chat/session.dart';
import 'package:companion_chat/dev/demo_backend.dart';
import 'package:companion_chat/persistence/repository.dart';
import 'package:companion_chat/persistence/vault.dart';
import 'package:companion_chat/ui/app.dart';
import 'package:companion_chat/ui/providers.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

class EmptyRepository implements ChatRepository {
  @override
  dynamic noSuchMethod(Invocation invocation) => throw UnimplementedError();
  @override
  Future<List<BotProfile>> bots() async => [];
  @override
  Stream<List<BotProfile>> watchBots() => Stream.value([]);
  @override
  Future<TurnWindow> readTurnWindow(
    String botId, {
    TurnWindow? previous,
  }) async => TurnWindow.empty(botId);
  @override
  Future<List<PendingMessage>> queue(String botId) async => [];
  @override
  Future<Object?> meta(String botId, String name) async => null;
  @override
  Future<void> setMeta(String botId, String name, Object? value) async {}
  @override
  Future<Set<String>> viewed(String botId) async => {};
  @override
  Future<void> markViewed(String botId, Iterable<String> ids) async {}
}

ChatTurn turn(int n) => ChatTurn(
  id: 't$n',
  sequence: n,
  text: '主人消息 $n',
  createdAt: DateTime.utc(2026, 11, 16, 0, n),
  state: 'SUCCEEDED',
  replies: [
    ChatReply(
      id: 'm$n',
      text: '她的回复 $n\n${'很长的消息内容。' * (n % 4 + 1)}',
      state: 'SENT',
    ),
  ],
);

void main() {
  testWidgets(
    'opening and reopening a delayed history starts at the newest turn',
    (tester) async {
      tester.view.physicalSize = const Size(420, 850);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final bot = BotProfile(
        id: 'a',
        name: '鲸鲸',
        endpoint: Uri.parse('https://a.test'),
      );
      final states = StreamController<ChatState>.broadcast();
      final container = ProviderContainer(
        overrides: [
          repositoryProvider.overrideWithValue(EmptyRepository()),
          vaultProvider.overrideWithValue(MemoryVault()),
          backendFactoryProvider.overrideWithValue(DemoBackend.new),
          chatStateProvider.overrideWith((ref, _) => states.stream),
        ],
      );
      Widget host(Key key) => UncontrolledProviderScope(
        container: container,
        child: MaterialApp(
          home: Scaffold(
            body: ChatPane(key: key, bot: bot, compact: true),
          ),
        ),
      );
      await tester.pumpWidget(host(const ValueKey('first')));
      await tester.pump();
      final turns = List.generate(40, turn);
      states.add(ChatState(ready: true, turns: turns));
      await tester.pumpAndSettle();
      final list = find.byKey(const ValueKey('messages_a'));
      ScrollController controller() =>
          tester.widget<ListView>(list).controller!;
      expect(controller().offset, 0);
      expect(find.text('主人消息 39'), findsOneWidget);
      expect(find.text('主人消息 0'), findsNothing);
      await tester.drag(list, const Offset(0, 600));
      await tester.pumpAndSettle();
      expect(controller().offset, greaterThan(200));
      states.add(ChatState(ready: true, turns: [...turns, turn(40)]));
      await tester.pumpAndSettle();
      expect(controller().offset, greaterThan(200));
      await tester.pumpWidget(host(const ValueKey('reopened')));
      await tester.pumpAndSettle();
      states.add(ChatState(ready: true, turns: [...turns, turn(40)]));
      await tester.pumpAndSettle();
      expect(controller().offset, 0);
      expect(find.text('主人消息 40'), findsOneWidget);
      await tester.pumpWidget(const SizedBox());
      container.dispose();
      await states.close();
      await tester.pump(const Duration(seconds: 3));
    },
  );

  testWidgets('erased sent replies have a visible deletion marker', (
    tester,
  ) async {
    final value = ChatTurn.fromJson({
      'event_id': 'old',
      'sequence': 1,
      'text': '保留的主人消息',
      'created_at': '2026-11-15T08:00:00Z',
      'state': 'SUCCEEDED',
      'redacted': false,
      'replies': [
        {
          'message_id': 'cleared',
          'state': 'SENT',
          'text': '',
          'content_erased': true,
        },
      ],
    });
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(body: TurnView(turn: value)),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('保留的主人消息'), findsOneWidget);
    expect(find.text('这条回复已清除'), findsOneWidget);
  });
}
