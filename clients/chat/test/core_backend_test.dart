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
  String? eventData;
  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(options);
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
