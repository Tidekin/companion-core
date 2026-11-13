import 'dart:async';

import 'package:drift/native.dart';
import 'package:companion_chat/persistence/database.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/persistence/repository.dart';
import 'package:companion_chat/persistence/vault.dart';
import 'package:companion_chat/dev/demo_backend.dart';
import 'package:companion_chat/ui/app.dart';
import 'package:companion_chat/ui/providers.dart';

// UI tests exercise navigation with a deterministic repository; SQLite/encryption
// and the production repository are exercised by repository_test and the TLS gate.
class UiRepository implements ChatRepository {
  @override
  dynamic noSuchMethod(Invocation invocation) =>
      throw UnimplementedError(invocation.memberName.toString());
  final profiles = <BotProfile>[];
  final drafts = <String, Object?>{};
  final pending = <String, List<PendingMessage>>{};
  @override
  Future<List<BotProfile>> bots() async => profiles;
  @override
  Stream<List<BotProfile>> watchBots() => Stream.value(List.of(profiles));
  @override
  Future<void> addBot(BotProfile bot) async {
    profiles.add(bot);
  }

  @override
  Future<Object?> meta(String botId, String name) async =>
      drafts['$botId/$name'];
  @override
  Future<void> setMeta(String botId, String name, Object? value) async {
    drafts['$botId/$name'] = value;
  }

  @override
  Future<List<ChatTurn>> turns(String botId) async => [];
  @override
  Future<List<PendingMessage>> queue(String botId) async =>
      pending[botId] ?? [];
  @override
  Future<void> enqueue(String botId, PendingMessage message) async {
    (pending[botId] ??= []).add(message);
  }

  @override
  Future<Set<String>> viewed(String botId) async => {};
}

void main() {
  testWidgets(
    'preview updates in place and final reply replaces its generating bubble',
    (tester) async {
      ChatTurn turn({bool done = false}) => ChatTurn(
        id: 'stream',
        sequence: 1,
        text: 'question',
        createdAt: DateTime.utc(2026),
        state: done ? 'SUCCEEDED' : 'RUNNING',
        preview: ChatPreview(
          done ? 2 : 1,
          done ? '' : '首段正文',
          done ? 'COMPLETE' : 'GENERATING',
        ),
        replies: done
            ? [const ChatReply(id: 'final', text: '首段正文，完成。', state: 'SENT')]
            : [],
      );
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: TurnView(turn: turn())),
        ),
      );
      // The streamed text is revealed at a reading pace in its own bubble.
      await tester.pump(const Duration(seconds: 1));
      expect(find.text('首段正文'), findsOneWidget);
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(body: TurnView(turn: turn(done: true))),
        ),
      );
      expect(find.byKey(const ValueKey('preview_t_0')), findsNothing);
      expect(find.text('首段正文，完成。'), findsOneWidget);
    },
  );
  testWidgets(
    'after login the header shows the connection, not a login button',
    (tester) async {
      tester.view.physicalSize = const Size(1200, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      // The full sync after login needs the real repository.
      final database = ChatDatabase(NativeDatabase.memory());
      final vault = MemoryVault();
      final repo = ChatRepository(database, CacheCipher(vault));
      await repo.addBot(
        BotProfile(id: 'a', name: 'a', endpoint: Uri.parse('https://a.test')),
      );
      final container = ProviderContainer(
        overrides: [
          repositoryProvider.overrideWithValue(repo),
          vaultProvider.overrideWithValue(vault),
          backendFactoryProvider.overrideWithValue(DemoBackend.new),
        ],
      );
      await tester.pumpWidget(
        UncontrolledProviderScope(
          container: container,
          child: const ChatApplication(),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('bot_a')));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('connect_a')), findsOneWidget);
      await tester.tap(find.byKey(const ValueKey('connect_a')));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const ValueKey('owner_credential')),
        'secret',
      );
      await tester.tap(find.byKey(const ValueKey('login_device')));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('connected_a')), findsOneWidget);
      expect(find.byKey(const ValueKey('connect_a')), findsNothing);
      await tester.pumpWidget(const SizedBox());
      container.dispose();
      await tester.pump(const Duration(seconds: 5));
      await tester.runAsync(database.close);
    },
  );
  testWidgets(
    'two private Bot chats keep drafts and replies separate across responsive navigation',
    (tester) async {
      tester.view.physicalSize = const Size(1200, 900);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final vault = MemoryVault();
      final repo = UiRepository();
      for (final id in ['a', 'b']) {
        await repo.addBot(
          BotProfile(id: id, name: id, endpoint: Uri.parse('https://$id.test')),
        );
      }
      final container = ProviderContainer(
        overrides: [
          repositoryProvider.overrideWithValue(repo),
          vaultProvider.overrideWithValue(vault),
          backendFactoryProvider.overrideWithValue(DemoBackend.new),
        ],
      );
      await tester.pumpWidget(
        UncontrolledProviderScope(
          container: container,
          child: const ChatApplication(),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const ValueKey('bot_a')));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const ValueKey('compose_a')),
        'draft A',
      );
      await tester.tap(find.byKey(const ValueKey('bot_b')));
      await tester.pumpAndSettle();
      expect(find.text('draft A'), findsNothing);
      await tester.enterText(
        find.byKey(const ValueKey('compose_b')),
        'private B',
      );
      await tester.tap(find.byKey(const ValueKey('send_b')));
      await tester.pumpAndSettle();
      expect((await repo.queue('b')).single.text, 'private B');
      await tester.tap(find.byKey(const ValueKey('bot_a')));
      await tester.pumpAndSettle();
      expect(find.text('draft A'), findsOneWidget);
      expect(find.text('private B'), findsNothing);
      // Narrow layout uses the same conversation route and an explicit back action.
      tester.view.physicalSize = const Size(412, 915);
      await tester.pumpAndSettle();
      expect(find.byTooltip('返回聊天列表'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.tap(find.byTooltip('返回聊天列表'));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('bot_b')), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
      final registry = container.read(registryProvider);
      unawaited(registry.close());
      container.dispose();
      await tester.pumpAndSettle();
    },
  );
}
