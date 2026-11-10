import 'package:drift/drift.dart';

import '../chat/models.dart';
import 'database.dart';
import 'vault.dart';

class ChatRepository {
  ChatRepository(this.db, this.cipher);
  final ChatDatabase db;
  final CacheCipher cipher;

  Future<List<BotProfile>> bots() async => (await db.select(db.botRows).get())
      .map(
        (row) => BotProfile(
          id: row.botId,
          name: row.displayName,
          backend: row.backend,
          endpoint: Uri.parse(row.endpoint),
        ),
      )
      .toList();

  Stream<List<BotProfile>> watchBots() => db
      .select(db.botRows)
      .watch()
      .map(
        (rows) => rows
            .map(
              (row) => BotProfile(
                id: row.botId,
                name: row.displayName,
                backend: row.backend,
                endpoint: Uri.parse(row.endpoint),
              ),
            )
            .toList(),
      );

  Future<void> addBot(BotProfile bot) async {
    await db
        .into(db.botRows)
        .insert(
          BotRowsCompanion.insert(
            botId: bot.id,
            displayName: bot.name.trim(),
            backend: bot.backend,
            endpoint: bot.endpoint.origin,
          ),
        );
  }

  Future<Object?> meta(String botId, String name) async {
    final row =
        await (db.select(db.metaRows)
              ..where((r) => r.botId.equals(botId) & r.name.equals(name)))
            .getSingleOrNull();
    return row == null ? null : cipher.open(botId, 'meta/$name', row.payload);
  }

  Future<void> setMeta(String botId, String name, Object? value) async {
    final bytes = await cipher.seal(botId, 'meta/$name', value);
    await db
        .into(db.metaRows)
        .insertOnConflictUpdate(
          MetaRowsCompanion.insert(botId: botId, name: name, payload: bytes),
        );
  }

  Future<List<ChatTurn>> turns(String botId) async {
    final rows =
        await (db.select(db.turnRows)
              ..where((r) => r.botId.equals(botId))
              ..orderBy([(r) => OrderingTerm.desc(r.sequence)])
              ..limit(200))
            .get();
    final result = <ChatTurn>[];
    for (final row in rows.reversed) {
      final value = await cipher.open(
        botId,
        'turn/${row.eventId}',
        row.payload,
      );
      result.add(ChatTurn.fromJson(Map<String, dynamic>.from(value! as Map)));
    }
    return result;
  }

  Future<void> merge(String botId, List<Map<String, dynamic>> items) async {
    final sealed = <(Map<String, dynamic>, Uint8List)>[];
    for (final item in items) {
      sealed.add((
        item,
        await cipher.seal(botId, 'turn/${item['event_id']}', item),
      ));
    }
    await db.transaction(() async {
      for (final (item, bytes) in sealed) {
        final eventId = item['event_id'] as String;
        await db
            .into(db.turnRows)
            .insertOnConflictUpdate(
              TurnRowsCompanion.insert(
                botId: botId,
                eventId: eventId,
                sequence: item['sequence'] as int,
                payload: bytes,
              ),
            );
        if (item['redacted'] == true ||
            {'SUCCEEDED', 'FAILED', 'CANCELLED'}.contains(item['state'])) {
          await (db.delete(db.queueRows)..where(
                (r) => r.botId.equals(botId) & r.eventId.equals(eventId),
              ))
              .go();
        }
      }
    });
  }

  Future<void> reconcile(String botId, Set<String> present) async {
    await (db.delete(
      db.turnRows,
    )..where((r) => r.botId.equals(botId) & r.eventId.isNotIn(present))).go();
  }

  Future<void> enqueue(String botId, PendingMessage message) async {
    final pending = await queue(botId);
    if (pending.length >= 100) throw const BackendError('待发送消息已满，请先同步。');
    final bytes = await cipher.seal(botId, 'queue/${message.requestId}', {
      'text': message.text,
      'packet': message.packet,
    });
    await db
        .into(db.queueRows)
        .insert(
          QueueRowsCompanion.insert(
            botId: botId,
            requestId: message.requestId,
            eventId: Value(message.eventId),
            payload: bytes,
            ordinal: DateTime.now().microsecondsSinceEpoch,
          ),
        );
  }

  Future<List<PendingMessage>> queue(String botId) async {
    final rows =
        await (db.select(db.queueRows)
              ..where((r) => r.botId.equals(botId))
              ..orderBy([(r) => OrderingTerm.asc(r.ordinal)]))
            .get();
    final result = <PendingMessage>[];
    for (final row in rows) {
      final value = Map<String, dynamic>.from(
        (await cipher.open(botId, 'queue/${row.requestId}', row.payload))!
            as Map,
      );
      result.add(
        PendingMessage(
          requestId: row.requestId,
          text: value['text'] as String,
          packet: Map<String, dynamic>.from(value['packet'] as Map),
          eventId: row.eventId,
          admitted: row.admitted,
          error: row.error,
        ),
      );
    }
    return result;
  }

  Future<void> accepted(String botId, String requestId, String eventId) async {
    await (db.update(db.queueRows)
          ..where((r) => r.botId.equals(botId) & r.requestId.equals(requestId)))
        .write(
          QueueRowsCompanion(
            admitted: const Value(true),
            eventId: Value(eventId),
            error: const Value(null),
          ),
        );
  }

  Future<void> rejected(String botId, String requestId, String code) async {
    await (db.update(db.queueRows)
          ..where((r) => r.botId.equals(botId) & r.requestId.equals(requestId)))
        .write(QueueRowsCompanion(error: Value(code)));
  }

  Future<void> discardRejected(String botId) async {
    await (db.delete(db.queueRows)..where(
          (r) =>
              r.botId.equals(botId) &
              r.admitted.equals(false) &
              r.error.isNotNull(),
        ))
        .go();
  }

  Future<Set<String>> viewed(String botId) async =>
      (await (db.select(
            db.viewedReplies,
          )..where((r) => r.botId.equals(botId))).get())
          .map((r) => r.messageId)
          .toSet();

  Future<void> markViewed(String botId, Iterable<String> messageIds) async {
    await db.batch((batch) {
      batch.insertAllOnConflictUpdate(
        db.viewedReplies,
        messageIds.map(
          (id) => ViewedRepliesCompanion.insert(botId: botId, messageId: id),
        ),
      );
    });
  }
}
