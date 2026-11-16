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
      'trigger': 'KEEPSAKE',
      'created_at': '2026-11-16T12:00:00Z',
      'liked': true,
    });
    expect((moment.id, moment.trigger, moment.liked), (
      'moment_1',
      'KEEPSAKE',
      true,
    ));
    expect(moment.withLiked(false).text, moment.text);
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
    addTearDown(container.dispose);
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
  });
}
