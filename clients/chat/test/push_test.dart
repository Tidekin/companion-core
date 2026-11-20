import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/chat/push.dart';
import 'package:companion_chat/chat/session.dart';
import 'package:companion_chat/dev/demo_backend.dart';
import 'package:companion_chat/persistence/vault.dart';
import 'package:companion_chat/ui/app.dart';
import 'package:companion_chat/ui/providers.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'widget_test.dart' show UiRepository;

class FakeAndroid extends AndroidPush {
  String? token = 'installation-1', tap;
  int prompts = 0;
  void Function()? open;
  @override
  Future<String?> enable() async {
    prompts++;
    return token;
  }

  @override
  Future<String?> getToken() async => token;
  @override
  Future<String?> takeRoute() async {
    final route = tap;
    tap = null;
    return route;
  }

  @override
  void listen({
    required void Function() refresh,
    required void Function() open,
  }) {
    this.open = open;
  }

  @override
  void stopListening() {
    open = null;
  }
}

class PushBackend extends DemoBackend {
  PushBackend(super.bot);
  final registered = <(String, String)>[];
  String? failure;
  int deletions = 0;
  bool hasSession = true;
  bool offlineAtStart = false;
  @override
  Future<bool> resume() async {
    if (offlineAtStart) throw const BackendError('CONNECTION_UNAVAILABLE');
    return hasSession;
  }

  @override
  Future<void> registerPush({
    required String token,
    required String route,
  }) async {
    registered.add((token, route));
    if (failure != null) {
      throw BackendError(
        failure!,
        status: failure == 'NATIVE_PUSH_DISABLED' ? 409 : null,
      );
    }
  }

  @override
  Future<void> unregisterPush() async {
    deletions++;
  }
}

class PushUiRepository extends UiRepository {
  @override
  Future<void> merge(String botId, List<Map<String, dynamic>> items) async {}
  @override
  Future<void> reconcile(String botId, Set<String> present) async {}
  @override
  Future<void> withdrawPreviews(String botId) async {}
}

BotProfile bot(String id) =>
    BotProfile(id: id, name: id, endpoint: Uri.parse('https://$id.invalid'));

void main() {
  test(
    'per-Bot opt-in, token rotation, recovery and logout are independent',
    () async {
      final repo = UiRepository(), android = FakeAndroid();
      final first = PushBackend(bot('first')),
          second = PushBackend(bot('second'));
      final a = BotPush('first', repo, first, android),
          b = BotPush('second', repo, second, android);
      bool yes() => true;
      await a.refresh(yes);
      expect(first.registered, isEmpty);
      await a.setEnabled(true, yes);
      await b.setEnabled(true, yes);
      final firstRoute = first.registered.single.$2,
          secondRoute = second.registered.single.$2;
      expect(firstRoute, matches(RegExp(r'^[a-f0-9]{32}$')));
      expect(firstRoute, isNot(secondRoute));
      expect(first.registered.single.$1, second.registered.single.$1);
      await a.refresh(yes);
      await a.refresh(yes, checkToken: true);
      expect(first.registered, hasLength(1));
      android.token = 'rotated';
      first.failure = 'CONNECTION_UNAVAILABLE';
      await expectLater(
        a.refresh(yes, checkToken: true),
        throwsA(isA<BackendError>()),
      );
      first.failure = null;
      await a.refresh(yes);
      expect(first.registered.last, ('rotated', firstRoute));
      expect(second.registered, hasLength(1));
      final restarted = BotPush('first', repo, first, android);
      await restarted.refresh(yes);
      expect(first.registered.last, ('rotated', firstRoute));
      expect(android.prompts, 2);
      await expectLater(
        restarted.setEnabled(false, () => false),
        throwsA(isA<BackendError>()),
      );
      expect(restarted.matches(firstRoute), false);
      final offlineRestart = BotPush('first', repo, first, android);
      await offlineRestart.refresh(yes);
      expect(first.deletions, 1);
      await offlineRestart.refresh(yes);
      expect(first.deletions, 1);
      expect(b.matches(secondRoute), true);
      await b.forget();
      expect(b.matches(secondRoute), false);
    },
  );

  test(
    'denial, disabled server and lost opt-in reply preserve correct intent',
    () async {
      final repo = UiRepository(), android = FakeAndroid()..token = null;
      final backend = PushBackend(bot('one'));
      final subscription = BotPush('one', repo, backend, android);
      bool yes() => true;
      await expectLater(
        subscription.setEnabled(true, yes),
        throwsA(isA<BackendError>()),
      );
      expect(subscription.enabled, false);
      expect(backend.registered, isEmpty);
      android.token = 'ok';
      backend.failure = 'NATIVE_PUSH_DISABLED';
      await expectLater(
        subscription.setEnabled(true, yes),
        throwsA(isA<BackendError>()),
      );
      expect(subscription.enabled, false);
      backend.failure = 'CONNECTION_UNAVAILABLE';
      await expectLater(
        subscription.setEnabled(true, yes),
        throwsA(isA<BackendError>()),
      );
      final route = backend.registered.last.$2;
      backend.failure = null;
      await subscription.refresh(yes);
      expect(backend.registered.last, ('ok', route));
      expect(subscription.matches(route), true);
    },
  );

  test('expired login waits for authentication; logout and revocation reject old taps', () async {
    final repo = PushUiRepository(), android = FakeAndroid();
    final profile = bot('private');
    await repo.addBot(profile);
    final route = 'c' * 32;
    await repo.setMeta(profile.id, 'push_route', route);
    final backend = PushBackend(profile)..hasSession = false;
    final registry = SessionRegistry(
      repo,
      (_) => backend,
      androidPush: android,
    );
    addTearDown(registry.close);
    final session = registry.session(profile);
    expect(await registry.notificationTarget(route), isNull);
    await session.login('owner');
    await session.push!.refresh(() => session.state.connected);
    expect((await registry.notificationTarget(route))?.bot, profile);
    expect(backend.registered.last, ('installation-1', route));

    final revoked = session.watch().firstWhere((state) => !state.connected);
    backend.events.addError(const BackendError('DEVICE_REVOKED', status: 403));
    await revoked;
    expect(await registry.notificationTarget(route), isNull);
    await session.login('owner');
    await session.setPushEnabled(true);
    final newRoute = backend.registered.last.$2;
    expect(newRoute, isNot(route));
    await session.disconnect();
    expect(await registry.notificationTarget(newRoute), isNull);
    expect(await repo.meta(profile.id, 'push_route'), isNull);
  });

  test(
    'a tap during a transient offline resume still selects its original Bot',
    () async {
      final repo = PushUiRepository(), profile = bot('offline');
      await repo.addBot(profile);
      final route = 'd' * 32;
      await repo.setMeta(profile.id, 'push_route', route);
      final backend = PushBackend(profile)..offlineAtStart = true;
      final registry = SessionRegistry(
        repo,
        (_) => backend,
        androidPush: FakeAndroid(),
      );
      addTearDown(registry.close);
      final target = await registry.notificationTarget(route);
      expect(target?.bot, profile);
      expect(target?.state.connected, false);
      expect(backend.registered, isEmpty);
      await target!.sync();
      expect(target.state.connected, true);
    },
  );

  testWidgets(
    'cold and warm notification taps select their enrolled Bot only',
    (tester) async {
      final repo = PushUiRepository();
      final android = FakeAndroid()..tap = 'a' * 32;
      final a = bot('first'), b = bot('second');
      await repo.addBot(a);
      await repo.addBot(b);
      await repo.setMeta(a.id, 'push_route', 'a' * 32);
      await repo.setMeta(b.id, 'push_route', 'b' * 32);
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            repositoryProvider.overrideWithValue(repo),
            vaultProvider.overrideWithValue(MemoryVault()),
            backendFactoryProvider.overrideWithValue(
              (profile) => PushBackend(profile),
            ),
            androidPushProvider.overrideWithValue(android),
          ],
          child: const ChatApplication(),
        ),
      );
      await tester.pumpAndSettle();
      expect(tester.widget<ChatPane>(find.byType(ChatPane)).bot.id, 'first');
      android.tap = 'https://untrusted.invalid';
      android.open!();
      await tester.pumpAndSettle();
      expect(tester.widget<ChatPane>(find.byType(ChatPane)).bot.id, 'first');
      android.tap = 'b' * 32;
      android.open!();
      await tester.pumpAndSettle();
      expect(tester.widget<ChatPane>(find.byType(ChatPane)).bot.id, 'second');
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump(const Duration(seconds: 3));
    },
  );
}
