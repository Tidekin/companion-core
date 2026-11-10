import 'dart:async';

import 'package:drift/native.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/chat/session.dart';
import 'package:companion_chat/data/database.dart';
import 'package:companion_chat/data/repository.dart';
import 'package:companion_chat/data/vault.dart';
import 'package:companion_chat/dev/demo_backend.dart';

void main() {
  late ChatDatabase db;
  late ChatRepository repo;
  Future<BotSession> create(String id, DemoBackend backend) async {
    await repo.addBot(backend.bot);
    final session = BotSession(backend.bot, repo, backend, autoPoll: false);
    await session.loaded;
    addTearDown(session.close);
    return session;
  }

  BotProfile bot(String id) =>
      BotProfile(id: id, name: id, endpoint: Uri.parse('https://$id.test'));
  setUp(() {
    db = ChatDatabase(NativeDatabase.memory());
    repo = ChatRepository(db, CacheCipher(MemoryVault()));
  });
  tearDown(() => db.close());

  test('slow Bot A cannot block Bot B or either local outbox', () async {
    final aBackend = DemoBackend(bot('a'));
    final bBackend = DemoBackend(bot('b'));
    final a = await create('a', aBackend);
    final b = await create('b', bBackend);
    await a.login('credential-a');
    await b.login('credential-b');
    final barrier = Completer<void>();
    aBackend.refreshBarrier = barrier.future;
    final stuck = a.sync();
    await a.send('only A');
    await b.send('only B');
    await b.sync();
    // A sync can be blocked indefinitely while B completes its own turn.
    expect(b.state.turns.single.text, 'only B');
    expect(a.state.pending.single.text, 'only A');
    expect(b.state.turns.single.replies.single.text, isNot(contains('only A')));
    barrier.complete();
    await stuck;
    expect(a.state.turns.single.text, 'only A');
  });

  test('lost acceptance retries original immutable packet without a second generation', () async {
    final backend = DemoBackend(bot('a'));
    final a = await create('a', backend);
    await a.send('lost acknowledgement');
    final original = (await repo.queue('a')).single.packet;
    backend.loseNextAcknowledgement = true;
    await a.login('credential');
    expect(a.state.pending.single.packet, original);
    expect(a.state.connected, isFalse);
    await a.sync();
    expect(backend.submissions, 2);
    expect(backend.generations, 1);
    expect(a.state.pending, isEmpty);
    expect(a.state.turns, hasLength(1));
    await a.sync();
    expect(backend.submissions, 2);
  });

  test(
    'disconnect during refresh prevents later sends and stale connected state',
    () async {
      final backend = DemoBackend(bot('a'));
      final a = await create('a', backend);
      await a.login('credential');
      final barrier = Completer<void>();
      backend.refreshBarrier = barrier.future;
      final inFlight = a.sync();
      await a.send('keep local');
      await a.disconnect();
      barrier.complete();
      await inFlight;
      expect(backend.submissions, 0);
      expect(a.state.connected, isFalse);
      expect((await repo.queue('a')).single.text, 'keep local');
      backend.refreshBarrier = null;
      await a.login('credential');
      expect(backend.generations, 1);
      expect(a.state.connected, isTrue);
    },
  );
  test(
    'registry disposal completes with the real SQLite watch and stops all Bots',
    () async {
      final profile = bot('registered');
      await repo.addBot(profile);
      final registry = SessionRegistry(repo, DemoBackend.new);
      await registry.session(profile).loaded;
      await registry.close().timeout(const Duration(seconds: 2));
    },
  );
}
