import 'dart:convert';
import 'dart:math';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';

String randomId() {
  final random = Random.secure();
  return hexBytes(List<int>.generate(16, (_) => random.nextInt(256)));
}

String hexBytes(List<int> bytes) =>
    bytes.map((byte) => byte.toRadixString(16).padLeft(2, '0')).join();

Object? sortedJson(Object? value) {
  if (value is Map) {
    final keys = value.keys.cast<String>().toList()..sort();
    return {for (final key in keys) key: sortedJson(value[key])};
  }
  if (value is List) return value.map(sortedJson).toList();
  return value;
}

Uint8List canonicalBytes(Object? value) =>
    Uint8List.fromList(utf8.encode(jsonEncode(sortedJson(value))));

Future<String> digest(Object? value) async =>
    hexBytes((await Sha256().hash(canonicalBytes(value))).bytes);

String captureTime(DateTime value) {
  final utc = value.toUtc();
  String two(int part) => part.toString().padLeft(2, '0');
  final micros = utc.millisecond * 1000 + utc.microsecond;
  final fraction = micros == 0 ? '' : '.${micros.toString().padLeft(6, '0')}';
  return '${utc.year.toString().padLeft(4, '0')}-${two(utc.month)}-${two(utc.day)}'
      'T${two(utc.hour)}:${two(utc.minute)}:${two(utc.second)}$fraction+00:00';
}

Uri coreOrigin(String value) {
  final uri = Uri.parse(value.trim());
  if (uri.scheme != 'https' ||
      uri.host.isEmpty ||
      uri.userInfo.isNotEmpty ||
      uri.hasQuery ||
      uri.hasFragment ||
      (uri.path.isNotEmpty && uri.path != '/') ||
      uri.port < 1 ||
      uri.port > 65535) {
    throw const FormatException('请填写 HTTPS 根地址，不含路径、账号或参数。');
  }
  return Uri.parse(uri.origin);
}

class BotProfile {
  const BotProfile({
    required this.id,
    required this.name,
    required this.endpoint,
    this.backend = 'core',
  });
  final String id;
  final String name;
  final Uri endpoint;
  final String backend;
  @override
  bool operator ==(Object other) =>
      other is BotProfile &&
      other.id == id &&
      other.name == name &&
      other.endpoint == endpoint &&
      other.backend == backend;
  @override
  int get hashCode => Object.hash(id, name, endpoint, backend);
}

/// An earlier message a reply answers; Core sends only its id.
class ChatQuote {
  const ChatQuote({this.eventId, this.messageId});

  /// The owner's message (a turn id).
  final String? eventId;

  /// One of the companion's earlier replies.
  final String? messageId;
  static ChatQuote? fromJson(Object? value) {
    if (value is! Map) return null;
    final eventId = value['event_id'];
    final messageId = value['message_id'];
    if (eventId is String) return ChatQuote(eventId: eventId);
    if (messageId is String) return ChatQuote(messageId: messageId);
    return null;
  }
}

class ChatReply {
  const ChatReply({
    required this.id,
    required this.text,
    required this.state,
    this.quote,
  });
  final String id;
  final String text;
  final String state;
  final ChatQuote? quote;
  factory ChatReply.fromJson(Map<String, dynamic> value) => ChatReply(
    id: value['message_id'] as String,
    text: value['text'] as String,
    state: value['state'] as String,
    quote: ChatQuote.fromJson(value['quote']),
  );
}

class ChatTurn {
  const ChatTurn({
    required this.id,
    required this.sequence,
    required this.text,
    required this.createdAt,
    required this.state,
    required this.replies,
    this.redacted = false,
    this.uncertain = false,
    this.error,
    this.preview,
  });
  final String id;
  final int sequence;
  final String text;
  final DateTime createdAt;
  final String state;
  final bool redacted;
  final bool uncertain;
  final String? error;
  final List<ChatReply> replies;
  final ChatPreview? preview;
  factory ChatTurn.fromJson(Map<String, dynamic> value) => ChatTurn(
    id: value['event_id'] as String,
    sequence: value['sequence'] as int,
    text: value['text'] as String,
    createdAt: DateTime.parse(value['created_at'] as String),
    state: value['state'] as String,
    redacted: value['redacted'] as bool,
    uncertain: value['uncertain'] as bool? ?? false,
    error: value['error'] as String?,
    preview: value['preview'] is Map
        ? ChatPreview.fromJson(
            Map<String, dynamic>.from(value['preview'] as Map),
          )
        : null,
    replies: (value['replies'] as List)
        .map((r) => ChatReply.fromJson(Map<String, dynamic>.from(r as Map)))
        .toList(),
  );
}

class ChatPreview {
  const ChatPreview(this.version, this.text, this.state);
  final int version;
  final String text;
  final String state;
  factory ChatPreview.fromJson(Map<String, dynamic> value) => ChatPreview(
    value['version'] as int,
    value['text'] as String,
    value['state'] as String,
  );
}

class PendingMessage {
  const PendingMessage({
    required this.requestId,
    required this.text,
    required this.packet,
    this.eventId,
    this.admitted = false,
    this.error,
  });
  final String requestId;
  final String text;
  final Map<String, dynamic> packet;
  final String? eventId;
  final bool admitted;
  final String? error;
}

class BackendError implements Exception {
  const BackendError(this.code, {this.status});
  final String code;
  final int? status;
  bool get rejected => {400, 403, 409, 410, 422}.contains(status);
  @override
  String toString() => code;
}

/// The companion's own display profile as committed by Core. `revision` orders
/// updates across devices; `displayName` is never an identity key.
class CompanionProfile {
  const CompanionProfile({
    required this.companionId,
    required this.revision,
    required this.displayName,
    required this.autoName,
    required this.autoAvatar,
    required this.settingsRevision,
    required this.selfUpdateEnabled,
    this.avatarAssetId,
    this.viewerAlias,
  });
  final String? companionId;
  final int revision;
  final String displayName;
  final String? avatarAssetId;
  final bool autoName;
  final bool autoAvatar;
  final String? viewerAlias;
  final int settingsRevision;
  final bool selfUpdateEnabled;

  /// What the owner sees in lists and headers: their own alias first.
  String get shownName => viewerAlias ?? displayName;

  bool isNewerThan(CompanionProfile? other) =>
      other == null ||
      revision > other.revision ||
      revision == other.revision && settingsRevision > other.settingsRevision;

  factory CompanionProfile.fromJson(Map<String, dynamic> value) =>
      CompanionProfile(
        companionId: value['companion_id'] as String?,
        revision: value['revision'] as int,
        displayName: value['display_name'] as String,
        avatarAssetId: value['avatar_asset_id'] as String?,
        autoName: value['auto_name'] as bool? ?? true,
        autoAvatar: value['auto_avatar'] as bool? ?? true,
        viewerAlias: value['viewer_alias'] as String?,
        settingsRevision: value['settings_revision'] as int? ?? 0,
        selfUpdateEnabled: value['self_update_enabled'] as bool? ?? false,
      );
}

class ProfileChange {
  const ProfileChange({
    required this.changeId,
    required this.newRevision,
    required this.fields,
    required this.newName,
    required this.actorType,
    required this.createdAt,
    this.oldRevision,
    this.oldName,
    this.note,
    this.restoredFromChangeId,
    this.oldAvatarAssetId,
    this.newAvatarAssetId,
  });
  final String changeId;
  final int? oldRevision;
  final int newRevision;
  final String fields;
  final String? oldName;
  final String newName;
  final String actorType;
  final String? note;
  final String? restoredFromChangeId;
  final String? oldAvatarAssetId;
  final String? newAvatarAssetId;
  final DateTime createdAt;

  bool get initial => fields == 'initial';
  bool get nameChanged => fields == 'name' || fields == 'both';
  bool get avatarChanged => fields == 'avatar' || fields == 'both';

  /// Real source labels; demonstrations never pose as the companion.
  String get actorLabel => switch (actorType) {
    'companion' => '伙伴更新',
    'owner' => '你更新',
    _ => '系统调整',
  };

  factory ProfileChange.fromJson(Map<String, dynamic> value) => ProfileChange(
    changeId: value['change_id'] as String,
    oldRevision: value['old_revision'] as int?,
    newRevision: value['new_revision'] as int,
    fields: value['fields'] as String,
    oldName: value['old_name'] as String?,
    newName: value['new_name'] as String,
    actorType: value['actor_type'] as String,
    note: value['note'] as String?,
    restoredFromChangeId: value['restored_from_change_id'] as String?,
    oldAvatarAssetId: value['old_avatar_asset_id'] as String?,
    newAvatarAssetId: value['new_avatar_asset_id'] as String?,
    createdAt: DateTime.parse(value['created_at'] as String),
  );
}
