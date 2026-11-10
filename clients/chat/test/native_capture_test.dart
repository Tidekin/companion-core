import 'dart:convert';
import 'dart:io';

import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_chat/chat/core_backend.dart';
import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/persistence/database.dart';
import 'package:companion_chat/persistence/repository.dart';
import 'package:companion_chat/persistence/vault.dart';

void main() {
  test(
    'Dart Ed25519 captures match original Python protocol bytes and signatures',
    () async {
      final vectors = jsonDecode(
        await File('test/fixtures/native_capture.json').readAsString(),
      ) as List;
      for (final raw in vectors) {
        final value = Map<String, dynamic>.from(raw as Map);
        final db = ChatDatabase(NativeDatabase.memory());
        final vault = MemoryVault();
        final repo = ChatRepository(db, CacheCipher(vault));
        final bot = BotProfile(
          id: 'fixture',
          name: 'Fixture',
          endpoint: Uri.parse('https://core.test'),
        );
        await repo.addBot(bot);
        await vault.write(
          'companion_fixture_device_seed',
          base64Encode((value['seed'] as List).cast<int>()),
        );
        await repo.setMeta(bot.id, 'core_authority', value['authority']);
        final backend = CoreBackend(bot, repo, vault);
        final packet = await backend.prepare(
          value['text'] as String,
          value['request_id'] as String,
          DateTime.parse(value['time'] as String),
        );
        expect(packet.packet, value['packet']);
        backend.close();
        await db.close();
      }
    },
  );

  test('Native API origins reject credentials, paths and cleartext', () {
    for (final value in [
      'http://localhost',
      'https://user:secret@core.test',
      'https://core.test/bot',
      'https://core.test?key=secret',
    ]) {
      expect(() => coreOrigin(value), throwsFormatException);
    }
  });
}
