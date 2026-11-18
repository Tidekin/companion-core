// Synthetic transport for debug UI and tests. Production main never imports it.
import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

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

  final avatars = <String, Uint8List>{};

  void _commit(
    String name,
    String actor, {
    String? restoredFrom,
    String? avatar,
  }) {
    final newAvatar = avatar ?? current.avatarAssetId;
    changes.add(
      ProfileChange(
        changeId: 'pchg_${changes.length + 1}_${bot.id}',
        oldRevision: current.revision,
        newRevision: current.revision + 1,
        fields: newAvatar == current.avatarAssetId
            ? 'name'
            : name == current.displayName
            ? 'avatar'
            : 'both',
        oldName: current.displayName,
        newName: name,
        oldAvatarAssetId: current.avatarAssetId,
        newAvatarAssetId: newAvatar,
        actorType: actor,
        restoredFromChangeId: restoredFrom,
        createdAt: DateTime.now().toUtc(),
      ),
    );
    current = _copy(
      revision: current.revision + 1,
      displayName: name,
      avatarAssetId: newAvatar,
    );
    profiles.add(current);
  }

  CompanionProfile _copy({
    int? revision,
    String? displayName,
    String? avatarAssetId,
    bool? autoName,
    bool? autoAvatar,
    String? viewerAlias,
    bool clearAlias = false,
    int? settingsRevision,
  }) => CompanionProfile(
    companionId: current.companionId,
    revision: revision ?? current.revision,
    displayName: displayName ?? current.displayName,
    avatarAssetId: avatarAssetId ?? current.avatarAssetId,
    autoName: autoName ?? current.autoName,
    autoAvatar: autoAvatar ?? current.autoAvatar,
    viewerAlias: clearAlias ? null : viewerAlias ?? current.viewerAlias,
    settingsRevision: settingsRevision ?? current.settingsRevision,
    selfUpdateEnabled: true,
  );

  @override
  Stream<CompanionProfile> get profileUpdates => profiles.stream;

  final momentChanges = StreamController<void>.broadcast();
  late final List<Moment> feed = [
    Moment(
      id: 'moment_synthetic_${bot.id}',
      text: '今天读到恒星的颜色和温度有关，想再弄懂一点光谱。',
      trigger: 'LIFE',
      createdAt: DateTime.now().toUtc().subtract(const Duration(hours: 2)),
      liked: false,
    ),
  ];

  @override
  Stream<void> get momentUpdates => momentChanges.stream;
  @override
  Future<List<Moment>> moments({DateTime? before}) async => [
    for (final item in feed)
      if (before == null || item.createdAt.isBefore(before)) item,
  ];
  @override
  Future<List<Moment>> momentMemories() async => const [];
  final threads = <String, List<MomentComment>>{};
  @override
  Future<List<MomentComment>> momentComments(String momentId) async =>
      threads[momentId] ?? const [];
  @override
  Future<Moment?> latestMoment(DateTime? after) async => feed
      .where(
        (item) =>
            !item.mine && (after == null || item.createdAt.isAfter(after)),
      )
      .firstOrNull;
  @override
  Future<void> likeMoment(String momentId, bool liked) async {
    final index = feed.indexWhere((item) => item.id == momentId);
    if (index < 0) throw const BackendError('MOMENT_NOT_FOUND', status: 404);
    feed[index] = feed[index].withLiked(liked);
    momentChanges.add(null);
  }

  @override
  Future<void> hideMoment(String momentId) async {
    feed.removeWhere((item) => item.id == momentId);
    momentChanges.add(null);
  }

  @override
  Future<void> forgetMoment(String momentId) => hideMoment(momentId);
  @override
  Future<CompanionProfile> profile() async => current;
  @override
  Future<List<ProfileChange>> profileChanges({int? before}) async =>
      changes.reversed.toList();
  @override
  Future<CompanionProfile> updateProfileSettings({
    required int expectedSettingsRevision,
    bool? autoName,
    bool? autoAvatar,
  }) async {
    if (expectedSettingsRevision != current.settingsRevision) {
      throw const BackendError(
        'PROFILE_SETTINGS_REVISION_CONFLICT',
        status: 409,
      );
    }
    current = _copy(
      autoName: autoName,
      autoAvatar: autoAvatar,
      settingsRevision: current.settingsRevision + 1,
    );
    profiles.add(current);
    return current;
  }

  @override
  Future<CompanionProfile> uploadAvatar({
    required Uint8List bytes,
    required String mimeType,
    required int expectedRevision,
    required String operationId,
  }) async {
    if (expectedRevision != current.revision) {
      throw const BackendError('PROFILE_REVISION_CONFLICT', status: 409);
    }
    final id = 'avt_${(avatars.length + 1).toRadixString(16).padLeft(32, '0')}';
    avatars[id] = bytes;
    _commit(current.displayName, 'owner', avatar: id);
    return current;
  }

  @override
  Future<Uint8List> avatarImage(String assetId) async =>
      avatars[assetId] ??
      (throw const BackendError('PROFILE_ASSET_UNAVAILABLE', status: 410));

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
    _commit(
      target.newName,
      'owner',
      restoredFrom: changeId,
      avatar: target.newAvatarAssetId,
    );
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
  Future<bool> resume() async => false;
  @override
  Future<void> logout() async {}
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
    DateTime occurredAt, {
    List<OutgoingImage> images = const [],
    Map<String, dynamic>? moment,
  }) async => PendingMessage(
    requestId: requestId,
    text: text,
    eventId: 'event_$requestId',
    packet: {
      'request': requestId,
      'text': text,
      'original_authority': 1,
      'moment': ?moment,
      if (images.isNotEmpty)
        'images': [
          for (final image in images)
            {
              'mime_type': image.mimeType,
              'data_base64': base64Encode(image.bytes),
            },
        ],
    },
  );

  @override
  Future<Uint8List> replySticker(String messageId) async =>
      demoStickers[messageId] ??
      (throw const BackendError('NATIVE_IMAGE_NOT_FOUND'));

  /// Meme bytes by reply id, standing in for Core's catalog.
  final demoStickers = <String, Uint8List>{};

  /// Attached image bytes by "event/slot", standing in for Core's previews.
  final demoImages = <String, Uint8List>{};

  @override
  Future<Uint8List> momentImage(String momentId, int slot) async =>
      demoImages['$momentId/$slot'] ??
      (throw const BackendError('MOMENT_IMAGE_NOT_FOUND'));

  @override
  Future<Uint8List> messageImage(String eventId, int slot) async =>
      demoImages['$eventId/$slot'] ??
      (throw const BackendError('NATIVE_IMAGE_NOT_FOUND'));
  @override
  Future<String> submit(PendingMessage message) async {
    submissions++;
    final id = 'event_${message.requestId}';
    final moment = message.packet['moment'] as Map<String, dynamic>?;
    if (moment != null) {
      // Moment posts and comments stay under their moment, never in the chat.
      if (moment['kind'] == 'post') {
        feed.insert(
          0,
          Moment(
            id: id,
            text: message.text,
            trigger: 'OWNER',
            createdAt: DateTime.now().toUtc(),
            liked: false,
            author: 'owner',
          ),
        );
      } else {
        threads
            .putIfAbsent(moment['moment_id'] as String, () => [])
            .add(MomentComment(author: 'owner', text: message.text, id: id));
      }
      momentChanges.add(null);
      return id;
    }
    final rename = RegExp(r'^改名[:：]\s*(.+)$').firstMatch(message.text.trim());
    if (rename != null) simulateCompanionRename(rename.group(1)!.trim());
    if (!records.any((r) => r['event_id'] == id)) {
      generations++;
      final images = message.images;
      for (final (slot, bytes) in images.indexed) {
        demoImages['$id/$slot'] = bytes;
      }
      records.add({
        'event_id': id,
        'sequence': records.length + 1,
        'text': message.text,
        'images': [
          for (var slot = 0; slot < images.length; slot++) {'slot': slot},
        ],
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
  Future<Map<String, dynamic>> replyNow(String eventId) async =>
      records.firstWhere((r) => r['event_id'] == eventId)
        ..remove('can_reply_now');
  @override
  Future<String> publicIdentity() async => 'synthetic-${bot.id}';
  @override
  void close() {
    unawaited(events.close());
    unawaited(profiles.close());
  }
}
