// Synthetic transport for debug UI and tests. Production main never imports it.
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
    if (!records.any((r) => r['event_id'] == id)) {
      generations++;
      records.add({
        'event_id': id,
        'sequence': records.length + 1,
        'text': message.text,
        'created_at': '2026-11-10T00:00:00Z',
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
  void close() {}
}
