// Invoked only by the Python TLS + independent-worker integration harness.
import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:dio/io.dart';
import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_chat/chat/core_backend.dart';
import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/chat/session.dart';
import 'package:companion_chat/persistence/database.dart';
import 'package:companion_chat/persistence/repository.dart';
import 'package:companion_chat/persistence/vault.dart';

void main() {
  test('shipped Dart Core adapter real TLS, persistence and worker interoperability', () async {
    final config = jsonDecode(
      await File(Platform.environment['COMPANION_WIRE_CONFIG']!).readAsString(),
    ) as Map;
    final directory = config['directory'] as String;
    final secrets = File('$directory/test-only-vault.json');
    final vault = MemoryVault();
    if (await secrets.exists()) {
      final values = jsonDecode(await secrets.readAsString()) as Map;
      for (final entry in values.entries) {
        await vault.write(entry.key as String, entry.value as String);
      }
    }
    final database = ChatDatabase(NativeDatabase(File('$directory/chat.db')));
    final repo = ChatRepository(database, CacheCipher(vault));
    final bot = BotProfile(
      id: 'tls',
      name: 'TLS fixture',
      endpoint: coreOrigin(config['origin'] as String),
    );
    if ((await repo.bots()).isEmpty) await repo.addBot(bot);
    final context = SecurityContext()
      ..setTrustedCertificates(config['certificate'] as String);
    final transport = Dio()
      ..httpClientAdapter = IOHttpClientAdapter(
        createHttpClient: () => HttpClient(context: context),
      );
    final backend = CoreBackend(bot, repo, vault, transport: transport);
    final session = BotSession(
      bot,
      repo,
      backend,
      autoPoll: false,
      clock: () => DateTime.parse(config['now'] as String),
    );
    try {
      await session.loaded;
      await session.login(config['password'] as String);
      if (session.state.review != null) await session.approve();
      expect(session.state.connected, isTrue, reason: session.state.status);
      if (config['phase'] == 'initial') {
        await session.send('真实 TLS Flutter 客户端验收。');
        await session.sync();
        expect(session.state.pending.single.admitted, isTrue);
        await File('$directory/receipt.json').writeAsString(
          jsonEncode({
            'event_id': session.state.pending.single.eventId,
            'public_key': await backend.publicIdentity(),
          }),
        );
        final values = <String, String>{};
        for (final key in [
          'companion_tls_device_seed',
          'companion_tls_cache_key',
        ]) {
          values[key] = (await vault.read(key))!;
        }
        await secrets.writeAsString(jsonEncode(values));
      } else if (config['phase'] == 'stream') {
        await File('$directory/subscription-ready').writeAsString('ready');
        final partial = await session
            .watch()
            .firstWhere(
              (state) => state.turns.any(
                (turn) => turn.preview?.text.isNotEmpty ?? false,
              ),
            )
            .timeout(const Duration(seconds: 60));
        expect(partial.turns.single.preview!.text, '真正流式的首段');
        expect(partial.turns.single.preview!.state, 'GENERATING');
        expect(partial.turns.single.state, 'RUNNING');
        expect(partial.turns.single.replies, isEmpty);
        expect(partial.unread, 0);
        await session.disconnect();
        expect(session.state.connected, isFalse);
        await session.login(config['password'] as String);
        expect(session.state.turns.single.id, partial.turns.single.id);
        expect(session.state.turns.single.preview!.text, '真正流式的首段');
        await File('$directory/client-saw-preview')
            .writeAsString('before provider end');
        final complete = await session
            .watch()
            .firstWhere((state) => state.turns.single.state == 'SUCCEEDED')
            .timeout(const Duration(seconds: 30));
        expect(complete.turns.single.id, partial.turns.single.id);
        expect(complete.turns.single.replies.single.text, '真正流式的首段，模型结束后补全。');
        expect(complete.turns.single.preview!.text, isEmpty);
        expect(complete.turns.single.preview!.state, 'COMPLETE');
        expect(complete.pending, isEmpty);
        expect(complete.unread, 1);
      } else {
        final receipt = jsonDecode(
          await File('$directory/receipt.json').readAsString(),
        ) as Map;
        expect(await backend.publicIdentity(), receipt['public_key']);
        expect(session.state.turns.single.id, receipt['event_id']);
        expect(
          session.state.turns.single.replies.single.text,
          '真正流式的首段，模型结束后补全。',
        );
        expect(session.state.pending, isEmpty);
      }
    } finally {
      await session.close();
      await database.close();
    }
  });
}
