import 'dart:convert';
import 'dart:io';

import 'package:cryptography/cryptography.dart';
import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/persistence/database.dart';
import 'package:companion_chat/persistence/repository.dart';
import 'package:companion_chat/persistence/vault.dart';

Map<String, dynamic> turn(String text) => {
  'event_id': 'same-event',
  'sequence': 1,
  'text': text,
  'created_at': '2026-11-10T00:00:00Z',
  'state': 'SUCCEEDED',
  'redacted': false,
  'uncertain': false,
  'error': null,
  'replies': [
    {'message_id': 'same-reply', 'text': 'reply $text', 'state': 'SENT'},
  ],
};

void main() {
  test('identical backend message IDs, drafts and unread marks remain scoped to each Bot', () async {
    final db = ChatDatabase(NativeDatabase.memory());
    final vault = MemoryVault();
    final repo = ChatRepository(db, CacheCipher(vault));
    for (final id in ['a', 'b']) {
      await repo.addBot(
        BotProfile(id: id, name: id, endpoint: Uri.parse('https://$id.test')),
      );
      await repo.setMeta(id, 'draft', 'draft $id');
      await repo.merge(id, [turn('private $id')]);
    }
    expect((await repo.turns('a')).single.text, 'private a');
    expect((await repo.turns('b')).single.text, 'private b');
    expect(await repo.meta('a', 'draft'), 'draft a');
    expect(await repo.meta('b', 'draft'), 'draft b');
    await repo.markViewed('a', ['same-reply']);
    expect(await repo.viewed('a'), {'same-reply'});
    expect(await repo.viewed('b'), isEmpty);
    await repo.merge('a', [
      {...turn(''), 'redacted': true, 'replies': []},
    ]);
    expect((await repo.turns('a')).single.redacted, isTrue);
    expect((await repo.turns('b')).single.text, 'private b');
    await db.close();
  });

  test('SQLite body and immutable pending packet recover with authenticated encryption', () async {
    final folder = await Directory.systemTemp.createTemp('companion-cache-');
    final file = File('${folder.path}/chat.db');
    final vault = MemoryVault();
    final db = ChatDatabase(NativeDatabase(file));
    final repo = ChatRepository(db, CacheCipher(vault));
    await repo.addBot(
      BotProfile(id: 'a', name: 'A', endpoint: Uri.parse('https://a.test')),
    );
    const secret = 'private payload must stay encrypted';
    const packet = PendingMessage(
      requestId: 'request',
      text: secret,
      eventId: 'event',
      packet: {'original_authority': 3, 'signature': 'original-signature'},
    );
    await repo.enqueue('a', packet);
    await repo.merge('a', [turn(secret)]);
    await db.close();
    expect(
      utf8.decode(await file.readAsBytes(), allowMalformed: true),
      isNot(contains(secret)),
    );
    final restoredDb = ChatDatabase(NativeDatabase(file));
    final restored = ChatRepository(restoredDb, CacheCipher(vault));
    expect((await restored.queue('a')).single.packet, packet.packet);
    expect((await restored.turns('a')).single.text, secret);
    final cipher = CacheCipher(vault);
    final protected = await cipher.seal('a', 'turn/example', {'text': secret});
    await expectLater(
      cipher.open('b', 'turn/example', protected),
      throwsA(isA<SecretBoxAuthenticationError>()),
    );
    await restoredDb.close();
    await folder.delete(recursive: true);
  });
}
