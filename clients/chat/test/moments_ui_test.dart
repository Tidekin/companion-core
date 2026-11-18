import 'dart:typed_data';

import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/dev/demo_backend.dart';
import 'package:companion_chat/persistence/repository.dart';
import 'package:companion_chat/persistence/vault.dart';
import 'package:companion_chat/ui/moments.dart';
import 'package:companion_chat/ui/providers.dart';
import 'package:companion_chat/ui/theme.dart';
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

class RecordingBackend extends DemoBackend {
  RecordingBackend(super.bot);
  final pictures = <String>[];

  @override
  Future<Uint8List> momentImage(String momentId, int slot) {
    pictures.add('$momentId/$slot');
    return super.momentImage(momentId, slot);
  }
}

final bot = BotProfile(
  id: 'a',
  name: '鲸鲸',
  endpoint: Uri.parse('https://a.test'),
);

void main() {
  test('relative times are short and local', () {
    final now = DateTime(2026, 11, 16, 20);
    expect(momentTime(now.subtract(const Duration(seconds: 20)), now), '刚刚');
    expect(momentTime(now.subtract(const Duration(minutes: 5)), now), '5 分钟前');
    expect(momentTime(DateTime(2026, 11, 16, 9, 7), now), '今天 09:07');
    expect(momentTime(DateTime(2026, 11, 2, 9, 7), now), '11月2日 09:07');
  });

  test('a moment reads Core fields and keeps the original wording', () {
    final moment = Moment.fromJson({
      'id': 'moment_1',
      'text': '今天主人告诉了我他的生日，我要牢牢记下来。',
      'trigger': 'MEMORY',
      'created_at': '2026-11-16T12:00:00Z',
      'liked': true,
    });
    expect(
      (moment.id, moment.trigger, moment.liked),
      ('moment_1', 'MEMORY', true),
    );
    expect(moment.withLiked(false).text, moment.text);
  });

  (ProviderContainer, DemoBackend) setUp() {
    final backend = DemoBackend(bot);
    final container = ProviderContainer(
      overrides: [
        repositoryProvider.overrideWithValue(EmptyRepository()),
        vaultProvider.overrideWithValue(MemoryVault()),
        backendFactoryProvider.overrideWithValue((_) => backend),
        profileProvider.overrideWith((ref, _) => Stream.value(null)),
      ],
    );
    return (container, backend);
  }

  Future<void> close(WidgetTester tester, ProviderContainer container) async {
    await tester.pumpWidget(const SizedBox());
    container.dispose();
    await tester.pump(const Duration(seconds: 3));
  }

  Widget host(ProviderContainer container, Widget child) =>
      UncontrolledProviderScope(
        container: container,
        child: MaterialApp(
          theme: warmTheme(),
          home: Scaffold(body: child),
        ),
      );

  testWidgets('the owner posts and comments; both stay under the moments', (
    tester,
  ) async {
    final (container, backend) = setUp();
    await tester.pumpWidget(host(container, MomentsPanel(bot: bot)));
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const ValueKey('moment_post_text')),
      '今天去爬山了',
    );
    await tester.tap(find.byKey(const ValueKey('moment_post_send')));
    await tester.pumpAndSettle();
    expect(backend.feed.first.mine, isTrue);
    expect(find.text('今天去爬山了'), findsOneWidget);
    expect(backend.records, isEmpty);
    final hers = backend.feed.last;
    await tester.tap(find.byKey(ValueKey('moment_comments_${hers.id}')));
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(ValueKey('moment_comment_text_${hers.id}')),
      '我也想看看',
    );
    await tester.tap(find.byKey(ValueKey('moment_comment_send_${hers.id}')));
    await tester.pumpAndSettle();
    expect(backend.threads[hers.id]!.single.text, '我也想看看');
    expect(find.textContaining('我也想看看'), findsOneWidget);
    await close(tester, container);
  });

  testWidgets('her picture loads from the moment route; drawing shows a note', (
    tester,
  ) async {
    final backend = RecordingBackend(bot);
    final container = ProviderContainer(
      overrides: [
        repositoryProvider.overrideWithValue(EmptyRepository()),
        vaultProvider.overrideWithValue(MemoryVault()),
        backendFactoryProvider.overrideWithValue((_) => backend),
        profileProvider.overrideWith((ref, _) => Stream.value(null)),
      ],
    );
    final hers = backend.feed.single;
    backend.feed
      ..clear()
      ..add(
        Moment(
          id: hers.id,
          text: hers.text,
          trigger: hers.trigger,
          createdAt: hers.createdAt,
          liked: false,
          imagePending: true,
        ),
      );
    await tester.pumpWidget(host(container, MomentsPanel(bot: bot)));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('moment_image_pending')), findsOneWidget);
    backend.feed[0] = Moment(
      id: hers.id,
      text: hers.text,
      trigger: hers.trigger,
      createdAt: hers.createdAt,
      liked: false,
      imageSlots: const [0],
    );
    backend.momentChanges.add(null);
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('moment_image_pending')), findsNothing);
    expect(backend.pictures, ['${hers.id}/0']);
    await close(tester, container);
  });

  testWidgets('her new post shows a chat card until it is dismissed', (
    tester,
  ) async {
    final (container, backend) = setUp();
    await tester.pumpWidget(host(container, MomentPreviewCard(bot: bot)));
    await tester.pumpAndSettle();
    expect(find.textContaining(backend.feed.single.text), findsOneWidget);
    await tester.tap(find.byTooltip('知道了'));
    await tester.pumpAndSettle();
    expect(find.textContaining(backend.feed.single.text), findsNothing);
    await close(tester, container);
  });

  testWidgets('the feed shows her posts and the like reaches Core', (
    tester,
  ) async {
    final backend = DemoBackend(bot);
    final container = ProviderContainer(
      overrides: [
        repositoryProvider.overrideWithValue(EmptyRepository()),
        vaultProvider.overrideWithValue(MemoryVault()),
        backendFactoryProvider.overrideWithValue((_) => backend),
        profileProvider.overrideWith((ref, _) => Stream.value(null)),
      ],
    );
    await tester.pumpWidget(
      UncontrolledProviderScope(
        container: container,
        child: MaterialApp(
          theme: warmTheme(),
          home: Scaffold(body: MomentsPanel(bot: bot)),
        ),
      ),
    );
    await tester.pumpAndSettle();
    final moment = backend.feed.single;
    expect(find.text(moment.text), findsOneWidget);
    expect(find.text('只有你能看到'), findsOneWidget);
    await tester.tap(find.byKey(ValueKey('moment_like_${moment.id}')));
    await tester.pumpAndSettle();
    expect(backend.feed.single.liked, isTrue);
    expect(find.byIcon(Icons.favorite_rounded), findsOneWidget);
    await tester.tap(find.byKey(ValueKey('moment_menu_${moment.id}')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('隐藏'));
    await tester.pumpAndSettle();
    expect(backend.feed, isEmpty);
    expect(find.textContaining('还没有发过动态'), findsOneWidget);
    await tester.pumpWidget(const SizedBox());
    container.dispose();
    await tester.pump(const Duration(seconds: 3));
  });
}
