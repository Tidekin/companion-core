import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_chat/chat/core_backend.dart';
import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/persistence/database.dart';
import 'package:companion_chat/persistence/repository.dart';
import 'package:companion_chat/persistence/vault.dart';

class Wire implements HttpClientAdapter {
  Wire(this.name);
  final String name;
  final requests = <RequestOptions>[];
  bool redirect = false;
  int sessionStatus = 200;
  bool requireFreshCsrf = false;
  Completer<void>? loginEntered;
  Future<void>? loginBarrier;
  String? eventData;
  Map<String, dynamic>? captureIdentity;
  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(options);
    if (options.path.endsWith('/session') &&
        options.method == 'POST' &&
        loginBarrier != null) {
      loginEntered?.complete();
      await loginBarrier;
    }
    if (options.path.endsWith('/devices/preview') &&
        requireFreshCsrf &&
        options.headers['X-CSRF-Token'] != 'csrf-$name') {
      return ResponseBody.fromString(
        '{"error":"CONSOLE_CSRF_REQUIRED"}',
        403,
        headers: {
          'content-type': ['application/json'],
        },
      );
    }
    if (sessionStatus != 200) {
      return ResponseBody.fromString(
        '{"error":"CONSOLE_LOGIN_REQUIRED"}',
        sessionStatus,
        headers: {
          'content-type': ['application/json'],
        },
      );
    }
    if (options.path.endsWith('/events') && eventData != null) {
      return ResponseBody(
        Stream.fromIterable(
          utf8.encode(eventData!).map((byte) => Uint8List.fromList([byte])),
        ),
        200,
        headers: {
          'content-type': ['text/event-stream; charset=utf-8'],
        },
      );
    }
    final login = options.path.endsWith('/session');
    return ResponseBody.fromString(
      jsonEncode(
        login
            ? {'csrf_token': 'csrf-$name'}
            : {
                'registered': true,
                'authority': {'revision': requests.length, 'for': name},
                'capture_identity': ?captureIdentity,
                'items': [],
                'next_before': null,
              },
      ),
      redirect ? 302 : 200,
      headers: {
        'content-type': ['application/json'],
        if (login)
          'set-cookie': [
            'companion_owner_session=session-$name; Secure; HttpOnly; SameSite=Strict; Path=/',
          ],
        if (redirect) 'location': ['https://unrelated.invalid'],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

void main() {
  test('signed credential generation survives a client restart', () async {
    final db = ChatDatabase(NativeDatabase.memory());
    final vault = MemoryVault();
    final repo = ChatRepository(db, CacheCipher(vault));
    final bot = BotProfile(
      id: 'generation',
      name: 'A',
      endpoint: Uri.parse('https://a.test'),
    );
    await repo.addBot(bot);
    final wire = Wire('generation')
      ..captureIdentity = {
        'identity_sha256': 'd' * 64,
        'protocol_epoch': 'native-v2-registration_generation',
      };
    final first = CoreBackend(
      bot,
      repo,
      vault,
      transport: Dio()..httpClientAdapter = wire,
    );
    await first.login('owner');
    final packet = await first.prepare(
      'before restart',
      'a' * 32,
      DateTime.utc(2026),
    );
    expect(
      (packet.packet['capture'] as Map)['protocol_epoch'],
      wire.captureIdentity!['protocol_epoch'],
    );
    first.close();
    final restored = CoreBackend(
      bot,
      repo,
      vault,
      transport: Dio()..httpClientAdapter = Wire('restored'),
    );
    final next = await restored.prepare(
      'after restart',
      'b' * 32,
      DateTime.utc(2026),
    );
    expect((next.packet['capture'] as Map)['identity_sha256'], 'd' * 64);
    expect(
      (next.packet['capture'] as Map)['protocol_epoch'],
      wire.captureIdentity!['protocol_epoch'],
    );
    restored.close();
    await db.close();
  });
  test(
    'logout during login prevents a late cookie from restoring saved access',
    () async {
      final db = ChatDatabase(NativeDatabase.memory());
      final vault = MemoryVault();
      final barrier = Completer<void>();
      final entered = Completer<void>();
      final wire = Wire('racing')
        ..loginBarrier = barrier.future
        ..loginEntered = entered;
      final bot = BotProfile(
        id: 'racing',
        name: 'racing',
        endpoint: Uri.parse('https://racing.test'),
      );
      final repository = ChatRepository(db, CacheCipher(vault));
      await repository.addBot(bot);
      final backend = CoreBackend(
        bot,
        repository,
        vault,
        transport: Dio()..httpClientAdapter = wire,
      );
      final pending = backend.login('one-time-password');
      final rejected = expectLater(
        pending,
        throwsA(
          isA<BackendError>().having((e) => e.code, 'code', 'SESSION_CHANGED'),
        ),
      );
      await entered.future;
      await backend.logout();
      barrier.complete();
      await rejected;
      expect(await vault.read('companion_racing_session'), isNull);
      await backend.history();
      expect(wire.requests.last.headers['Cookie'], isNull);
      backend.close();
      await db.close();
    },
  );
  test('saved session resumes without a password, stays per-Bot and logout revokes it', () async {
    final db = ChatDatabase(NativeDatabase.memory());
    final vault = MemoryVault();
    final repository = ChatRepository(db, CacheCipher(vault));
    final bot = BotProfile(
      id: 'persist',
      name: 'persist',
      endpoint: Uri.parse('https://persist.test'),
    );
    await repository.addBot(bot);
    final first = CoreBackend(
      bot,
      repository,
      vault,
      transport: Dio()..httpClientAdapter = Wire('persist'),
    );
    await first.login('one-time-owner-password');
    expect(
      vault.values['companion_persist_session'],
      isNot(contains('one-time-owner-password')),
    );
    first.close();
    final wire = Wire('resumed');
    final restored = CoreBackend(
      bot,
      repository,
      vault,
      transport: Dio()..httpClientAdapter = wire,
    );
    expect(await restored.resume(), isTrue);
    expect(wire.requests.single.path, '/v1/console/chat/devices/preview');
    expect(wire.requests.single.method, 'POST');
    expect(
      wire.requests.first.headers['Cookie'],
      'companion_owner_session=session-persist',
    );
    expect(wire.requests.last.headers['X-CSRF-Token'], 'csrf-persist');
    wire.requireFreshCsrf = true;
    wire.requests.clear();
    expect(await restored.resume(), isTrue);
    expect(wire.requests.map((r) => r.path), [
      '/v1/console/chat/devices/preview',
      '/v1/console/session',
      '/v1/console/chat/devices/preview',
    ]);
    expect(wire.requests.last.headers['X-CSRF-Token'], 'csrf-resumed');
    expect(
      jsonDecode(vault.values['companion_persist_session']!)['csrf'],
      'csrf-resumed',
    );
    expect(
      wire.requests.where(
        (r) => r.path.endsWith('/session') && r.method == 'POST',
      ),
      isEmpty,
    );
    final other = CoreBackend(
      BotProfile(id: 'other', name: 'other', endpoint: bot.endpoint),
      repository,
      vault,
      transport: Dio()..httpClientAdapter = Wire('other'),
    );
    expect(await other.resume(), isFalse);
    await restored.logout();
    expect(wire.requests.last.method, 'DELETE');
    expect(await vault.read('companion_persist_session'), isNull);
    await restored.login('one-time-owner-password');
    wire.redirect = true;
    await expectLater(restored.logout(), throwsA(isA<BackendError>()));
    expect(await vault.read('companion_persist_session'), isNull);
    restored.close();
    other.close();
    await db.close();
  });

  test(
    'expired or origin-mismatched saved sessions never silently log in',
    () async {
      final db = ChatDatabase(NativeDatabase.memory());
      final vault = MemoryVault();
      final repository = ChatRepository(db, CacheCipher(vault));
      final bot = BotProfile(
        id: 'expired',
        name: 'expired',
        endpoint: Uri.parse('https://expired.test'),
      );
      await repository.addBot(bot);
      final first = CoreBackend(
        bot,
        repository,
        vault,
        transport: Dio()..httpClientAdapter = Wire('expired'),
      );
      await first.login('one-time-password');
      first.close();
      final stored = vault.values['companion_expired_session']!;
      final wire = Wire('expired')..sessionStatus = 401;
      final expired = CoreBackend(
        bot,
        repository,
        vault,
        transport: Dio()..httpClientAdapter = wire,
      );
      expect(await expired.resume(), isFalse);
      expect(await vault.read('companion_expired_session'), isNull);
      await vault.write('companion_expired_session', stored);
      final otherOrigin = CoreBackend(
        BotProfile(
          id: bot.id,
          name: bot.name,
          endpoint: Uri.parse('https://changed.test'),
        ),
        repository,
        vault,
        transport: Dio()..httpClientAdapter = wire,
      );
      final before = wire.requests.length;
      expect(await otherOrigin.resume(), isFalse);
      expect(wire.requests.length, before);
      expired.close();
      otherOrigin.close();
      await db.close();
    },
  );
  test('SSE handles fragmented UTF8, comments and multiline frames; session revocation stops it', () async {
    final db = ChatDatabase(NativeDatabase.memory());
    final vault = MemoryVault();
    final wire = Wire('stream')
      ..eventData =
          ': heartbeat\r\n\r\nevent: turn\r\n'
          'data: {"event_id":"one",\r\ndata: "text":"你好🐟"}\r\n\r\n'
          'event: session\ndata: {"error":"NATIVE_DEVICE_REVOKED"}\n\n';
    final backend = CoreBackend(
      BotProfile(
        id: 'stream',
        name: 'stream',
        endpoint: Uri.parse('https://stream.test'),
      ),
      ChatRepository(db, CacheCipher(vault)),
      vault,
      transport: Dio()..httpClientAdapter = wire,
    );
    final seen = <Map<String, dynamic>>[];
    await expectLater(
      backend.watchTurns().forEach(seen.add),
      throwsA(
        isA<BackendError>().having((error) => error.status, 'status', 403),
      ),
    );
    expect(seen.single['text'], '你好🐟');
    backend.close();
    await db.close();
  });
  test(
    'per-Bot device seed, cookie, CSRF, authority and HTTPS redirect boundary',
    () async {
      final db = ChatDatabase(NativeDatabase.memory());
      final vault = MemoryVault();
      final repository = ChatRepository(db, CacheCipher(vault));
      final backends = <CoreBackend>[];
      final wires = <Wire>[];
      for (final id in ['a', 'b']) {
        final bot = BotProfile(
          id: id,
          name: id,
          endpoint: Uri.parse('https://$id.test'),
        );
        await repository.addBot(bot);
        final wire = Wire(id);
        wires.add(wire);
        final backend = CoreBackend(
          bot,
          repository,
          vault,
          transport: Dio()..httpClientAdapter = wire,
        );
        backends.add(backend);
        await backend.login('secret-$id');
        await backend.history();
        expect(wire.requests.first.headers['Cookie'], isNull);
        expect(
          wire.requests.last.headers['Cookie'],
          'companion_owner_session=session-$id',
        );
        expect(wire.requests.last.headers['X-CSRF-Token'], 'csrf-$id');
        expect(wire.requests.last.headers['Origin'], 'https://$id.test');
        expect(wire.requests.last.followRedirects, isFalse);
        expect(
          jsonEncode((await repository.meta(id, 'core_authority'))),
          contains(id),
        );
      }
      expect(
        await backends[0].publicIdentity(),
        isNot(await backends[1].publicIdentity()),
      );
      final prepared = await backends[0].prepare(
        'queued',
        'a' * 32,
        DateTime.utc(2026, 11, 10),
      );
      final original = jsonEncode(prepared.packet);
      await backends[0].refresh();
      expect(
        jsonEncode(prepared.packet),
        original,
      ); // no silent re-sign under a new grant revision
      wires[0].redirect = true;
      await expectLater(backends[0].history(), throwsA(isA<BackendError>()));
      for (final backend in backends) {
        backend.close();
      }
      await db.close();
    },
  );
}
