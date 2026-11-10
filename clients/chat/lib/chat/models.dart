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

class ChatReply {
  const ChatReply({required this.id, required this.text, required this.state});
  final String id;
  final String text;
  final String state;
  factory ChatReply.fromJson(Map<String, dynamic> value) => ChatReply(
    id: value['message_id'] as String,
    text: value['text'] as String,
    state: value['state'] as String,
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
  factory ChatTurn.fromJson(Map<String, dynamic> value) => ChatTurn(
    id: value['event_id'] as String,
    sequence: value['sequence'] as int,
    text: value['text'] as String,
    createdAt: DateTime.parse(value['created_at'] as String),
    state: value['state'] as String,
    redacted: value['redacted'] as bool,
    uncertain: value['uncertain'] as bool? ?? false,
    error: value['error'] as String?,
    replies: (value['replies'] as List)
        .map((r) => ChatReply.fromJson(Map<String, dynamic>.from(r as Map)))
        .toList(),
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
