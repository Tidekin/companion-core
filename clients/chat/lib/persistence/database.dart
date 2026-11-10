import 'package:drift/drift.dart';

part 'database.g.dart';

class BotRows extends Table {
  TextColumn get botId => text()();
  TextColumn get displayName => text().withLength(min: 1, max: 80)();
  TextColumn get backend => text()();
  TextColumn get endpoint => text()();
  @override
  Set<Column<Object>> get primaryKey => {botId};
  @override
  List<Set<Column<Object>>> get uniqueKeys => [
    {backend, endpoint},
  ];
}

class TurnRows extends Table {
  TextColumn get botId =>
      text().references(BotRows, #botId, onDelete: KeyAction.cascade)();
  TextColumn get eventId => text()();
  IntColumn get sequence => integer()();
  BlobColumn get payload => blob()();
  @override
  Set<Column<Object>> get primaryKey => {botId, eventId};
}

class QueueRows extends Table {
  TextColumn get botId =>
      text().references(BotRows, #botId, onDelete: KeyAction.cascade)();
  TextColumn get requestId => text()();
  TextColumn get eventId => text().nullable()();
  BlobColumn get payload => blob()();
  BoolColumn get admitted => boolean().withDefault(const Constant(false))();
  TextColumn get error => text().nullable()();
  IntColumn get ordinal => integer()();
  @override
  Set<Column<Object>> get primaryKey => {botId, requestId};
}

class MetaRows extends Table {
  TextColumn get botId =>
      text().references(BotRows, #botId, onDelete: KeyAction.cascade)();
  TextColumn get name => text()();
  BlobColumn get payload => blob()();
  @override
  Set<Column<Object>> get primaryKey => {botId, name};
}

class ViewedReplies extends Table {
  TextColumn get botId =>
      text().references(BotRows, #botId, onDelete: KeyAction.cascade)();
  TextColumn get messageId => text()();
  @override
  Set<Column<Object>> get primaryKey => {botId, messageId};
}

@DriftDatabase(tables: [BotRows, TurnRows, QueueRows, MetaRows, ViewedReplies])
class ChatDatabase extends _$ChatDatabase {
  ChatDatabase(super.executor);
  @override
  int get schemaVersion => 1;
  @override
  MigrationStrategy get migration => MigrationStrategy(
    beforeOpen: (_) async {
      await customStatement('PRAGMA foreign_keys = ON');
    },
  );
}
