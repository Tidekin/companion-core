// Synthetic transport for debug UI and tests. Production main never imports it.
import 'dart:async';

import '../chat/core_backend.dart';
import '../chat/models.dart';

class DemoBackend implements ChatBackend {
  DemoBackend(this.bot);
  final BotProfile bot;
  final List<Map<String, dynamic>> records = [];
  Future<void>? refreshBarrier;
  bool loseNextAcknowledgement = false;
  int submissions = 0;
  int generations = 0;
  bool bound = true;
  final events = StreamController<Map<String, dynamic>>.broadcast();
  final profiles = StreamController<CompanionProfile>.broadcast();
  late CompanionProfile current = CompanionProfile(
    companionId: 'cmp_synthetic_${bot.id}',
    revision: 1,
    displayName: bot.name,
    autoName: true,
    autoAvatar: true,
    settingsRevision: 1,
    selfUpdateEnabled: true,
  );
  late final List<ProfileChange> changes = [
    ProfileChange(
      changeId: 'pchg_initial_${bot.id}',
      newRevision: 1,
      fields: 'initial',
      newName: bot.name,
      actorType: 'system',
      createdAt: DateTime.now().toUtc().subtract(const Duration(days: 1)),
    ),
  ];

  /// Development fixture only: stands in for a confirmed companion tool call.
  void simulateCompanionRename(String name) {
    if (!current.autoName || name == current.displayName) return;
    _commit(name, 'companion');
  }

  void _commit(String name, String actor, {String? restoredFrom}) {
    changes.add(
      ProfileChange(
        changeId: 'pchg_${changes.length + 1}_${bot.id}',
        oldRevision: current.revision,
        newRevision: current.revision + 1,
        fields: 'name',
        oldName: current.displayName,
        newName: name,
        actorType: actor,
        restoredFromChangeId: restoredFrom,
        createdAt: DateTime.now().toUtc(),
      ),
    );
    current = _copy(revision: current.revision + 1, displayName: name);
    profiles.add(current);
  }

  CompanionProfile _copy({
    int? revision,
    String? displayName,
    bool? autoName,
    String? viewerAlias,
    bool clearAlias = false,
    int? settingsRevision,
  }) => CompanionProfile(
    companionId: current.companionId,
    revision: revision ?? current.revision,
    displayName: displayName ?? current.displayName,
    autoName: autoName ?? current.autoName,
    autoAvatar: current.autoAvatar,
    viewerAlias: clearAlias ? null : viewerAlias ?? current.viewerAlias,
    settingsRevision: settingsRevision ?? current.settingsRevision,
    selfUpdateEnabled: true,
  );

  @override
  Stream<CompanionProfile> get profileUpdates => profiles.stream;
  @override
  Future<CompanionProfile> profile() async => current;
  @override
  Future<List<ProfileChange>> profileChanges({int? before}) async =>
      changes.reversed.toList();
  @override
  Future<CompanionProfile> updateProfileSettings({
    required int expectedSettingsRevision,
    bool? autoName,
  }) async {
    if (expectedSettingsRevision != current.settingsRevision) {
      throw const BackendError(
        'PROFILE_SETTINGS_REVISION_CONFLICT',
        status: 409,
      );
    }
    current = _copy(
      autoName: autoName,
      settingsRevision: current.settingsRevision + 1,
    );
    profiles.add(current);
    return current;
  }

  @override
  Future<CompanionProfile> setProfileAlias({
    required int expectedSettingsRevision,
    String? alias,
  }) async {
    if (expectedSettingsRevision != current.settingsRevision) {
      throw const BackendError(
        'PROFILE_SETTINGS_REVISION_CONFLICT',
        status: 409,
      );
    }
    current = _copy(
      viewerAlias: alias,
      clearAlias: alias == null,
      settingsRevision: current.settingsRevision + 1,
    );
    profiles.add(current);
    return current;
  }

  @override
  Future<CompanionProfile> restoreProfile({
    required String changeId,
    required int expectedRevision,
    required bool pauseName,
    required String operationId,
  }) async {
    if (expectedRevision != current.revision) {
      throw const BackendError('PROFILE_REVISION_CONFLICT', status: 409);
    }
    final target = changes.firstWhere((item) => item.changeId == changeId);
    _commit(target.newName, 'owner', restoredFrom: changeId);
    if (pauseName) {
      current = _copy(
        autoName: false,
        settingsRevision: current.settingsRevision + 1,
      );
      profiles.add(current);
    }
    return current;
  }

  @override
  Stream<Map<String, dynamic>> watchTurns() => events.stream;
  @override
  void stopWatching() {}
  @override
  Future<ConnectionReview> login(String credential) async =>
      const ConnectionReview(true, {});
  @override
  Future<void> approve(ConnectionReview review) async {
    bound = true;
  }

  @override
  Future<bool> canPrepare() async => bound;
  @override
  Future<PendingMessage> prepare(
    String text,
    String requestId,
    DateTime occurredAt,
  ) async => PendingMessage(
    requestId: requestId,
    text: text,
    eventId: 'event_$requestId',
    packet: {'request': requestId, 'text': text, 'original_authority': 1},
  );
  @override
  Future<String> submit(PendingMessage message) async {
    submissions++;
    final id = 'event_${message.requestId}';
    final rename = RegExp(r'^改名[:：]\s*(.+)$').firstMatch(message.text.trim());
    if (rename != null) simulateCompanionRename(rename.group(1)!.trim());
    if (!records.any((r) => r['event_id'] == id)) {
      generations++;
      records.add({
        'event_id': id,
        'sequence': records.length + 1,
        'text': message.text,
        'created_at': DateTime.now().toUtc().toIso8601String(),
        'state': 'SUCCEEDED',
        'redacted': false,
        'uncertain': false,
        'error': null,
        'replies': [
          {
            'message_id': 'reply_${records.length}',
            'text': '${bot.name}：收到「${message.text}」。这是开发验收的合成回复。',
            'state': 'SENT',
          },
        ],
      });
    }
    if (loseNextAcknowledgement) {
      loseNextAcknowledgement = false;
      throw const BackendError('CONNECTION_UNAVAILABLE');
    }
    return id;
  }

  @override
  Future<void> refresh() async {
    await refreshBarrier;
  }

  @override
  Future<Map<String, dynamic>> history({int? before}) async => {
    'items': records,
    'next_before': null,
  };
  @override
  Future<List<Map<String, dynamic>>> status(String eventId) async =>
      records.where((r) => r['event_id'] == eventId).toList();
  @override
  Future<String> publicIdentity() async => 'synthetic-${bot.id}';
  @override
  void close() {
    unawaited(events.close());
    unawaited(profiles.close());
  }
}
