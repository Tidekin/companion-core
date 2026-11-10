import 'dart:convert';
import 'dart:io';

import 'package:cryptography/cryptography.dart';
import 'package:dio/dio.dart';

import '../persistence/repository.dart';
import '../persistence/vault.dart';
import 'models.dart';

class ConnectionReview {
  const ConnectionReview(this.ready, this.data);
  final bool ready;
  final Map<String, dynamic> data;
}

abstract interface class ChatBackend {
  Future<ConnectionReview> login(String credential);
  Future<void> approve(ConnectionReview review);
  Future<bool> canPrepare();
  Future<PendingMessage> prepare(
    String text,
    String requestId,
    DateTime occurredAt,
  );
  Future<String> submit(PendingMessage message);
  Future<void> refresh();
  Future<Map<String, dynamic>> history({int? before});
  Future<List<Map<String, dynamic>>> status(String eventId);
  Stream<Map<String, dynamic>> watchTurns();
  void stopWatching();
  Future<String> publicIdentity();
  void close();
}

class CoreBackend implements ChatBackend {
  CoreBackend(this.bot, this.repository, this.vault, {Dio? transport})
    : http = transport ?? Dio() {
    http.options = BaseOptions(
      baseUrl: bot.endpoint.origin,
      followRedirects: false,
      connectTimeout: const Duration(seconds: 15),
      receiveTimeout: const Duration(seconds: 20),
      sendTimeout: const Duration(seconds: 20),
      validateStatus: (_) => true,
    );
  }
  final BotProfile bot;
  final ChatRepository repository;
  final SecretVault vault;
  final Dio http;
  Future<SimpleKeyPair>? _key;
  Map<String, dynamic>? _authority;
  String? _csrf;
  String? _cookie;
  CancelToken? _events;

  Map<String, dynamic> get _headers => {
    'Origin': bot.endpoint.origin,
    if (_csrf != null) 'X-CSRF-Token': _csrf,
    if (_cookie != null) HttpHeaders.cookieHeader: _cookie,
  };

  Future<SimpleKeyPair> _deviceKey() => _key ??= () async {
    final name = 'companion_${bot.id}_device_seed';
    final saved = await vault.read(name);
    final algorithm = Ed25519();
    if (saved != null) return algorithm.newKeyPairFromSeed(base64Decode(saved));
    final key = await algorithm.newKeyPair();
    await vault.write(name, base64Encode((await key.extract()).bytes));
    return key;
  }();

  @override
  Future<String> publicIdentity() async =>
      hexBytes((await (await _deviceKey()).extractPublicKey()).bytes);

  Future<Map<String, dynamic>> _request(
    String method,
    String path, {
    Object? body,
    Map<String, dynamic>? query,
  }) async {
    if (!(path.startsWith('/v1/console/chat/') ||
        path == '/v1/console/session')) {
      throw const BackendError('CLIENT_CHAT_SCOPE_REQUIRED');
    }
    try {
      final response = await http.request<Object?>(
        path,
        data: body,
        queryParameters: query,
        options: Options(method: method, headers: _headers),
      );
      final status = response.statusCode ?? 503;
      final value = response.data is Map
          ? Map<String, dynamic>.from(response.data! as Map)
          : <String, dynamic>{};
      if (status < 200 || status >= 300) {
        throw BackendError(
          value['error'] as String? ?? 'HTTP_$status',
          status: status,
        );
      }
      if (path == '/v1/console/session' && method == 'POST') {
        final cookies = response.headers[HttpHeaders.setCookieHeader] ?? [];
        final cookie = cookies
            .map(Cookie.fromSetCookieValue)
            .where((item) => item.name == 'companion_owner_session')
            .firstOrNull;
        if (cookie == null || !cookie.secure || !cookie.httpOnly) {
          throw const BackendError('NATIVE_SESSION_COOKIE_REQUIRED');
        }
        _cookie = '${cookie.name}=${cookie.value}';
      }
      return value;
    } on DioException {
      throw const BackendError('CONNECTION_UNAVAILABLE');
    }
  }

  @override
  Future<ConnectionReview> login(String credential) async {
    _cookie = _csrf = null;
    final response = await _request(
      'POST',
      '/v1/console/session',
      body: {
        'password': credential,
        'device_public_key': await publicIdentity(),
      },
    );
    _csrf = response['csrf_token'] as String;
    final preview = await _request(
      'POST',
      '/v1/console/chat/devices/preview',
      body: {'public_key': await publicIdentity()},
    );
    if (preview['registered'] == true) await _bind(preview);
    return ConnectionReview(preview['registered'] == true, preview);
  }

  Future<void> _bind(Map<String, dynamic> value) async {
    _authority = Map<String, dynamic>.from(value['authority'] as Map);
    await repository.setMeta(bot.id, 'core_authority', _authority);
  }

  @override
  Future<void> approve(ConnectionReview review) async {
    final response = await _request(
      'POST',
      '/v1/console/chat/devices/authorize',
      body: {
        'public_key': await publicIdentity(),
        'expected_revision': review.data['expected_revision'],
        'preview_sha256': review.data['preview_sha256'],
      },
    );
    if (response['registered'] != true) {
      throw const BackendError('NATIVE_DEVICE_APPROVAL_REQUIRED');
    }
    await _bind(response);
  }

  @override
  Future<bool> canPrepare() async {
    _authority ??= switch (await repository.meta(bot.id, 'core_authority')) {
      final Map value => Map<String, dynamic>.from(value),
      _ => null,
    };
    return _authority != null;
  }

  @override
  Future<PendingMessage> prepare(
    String text,
    String requestId,
    DateTime occurredAt,
  ) async {
    if (!await canPrepare()) throw const BackendError('请先登录并登记此设备。');
    final key = await _deviceKey();
    final publicKey = await publicIdentity();
    final deviceId = hexBytes(
      (await Sha256().hash((await key.extractPublicKey()).bytes)).bytes,
    ).substring(0, 32);
    final source = 'native:$deviceId:$requestId';
    final eventId =
        'evt_${hexBytes((await Sha256().hash(utf8.encode(source))).bytes)}';
    final timestamp = captureTime(occurredAt);
    final capture = <String, dynamic>{
      'authority': _authority,
      'kind': 'authenticated_native_device',
      'event_id': eventId,
      'source_event_id': source,
      'input_sha256': await digest(text),
      'frame_sha256': await digest([requestId, text, timestamp]),
      'evidence_sha256': await digest(['native.chat', deviceId, requestId]),
      'identity_sha256': await digest(['native.chat', deviceId, publicKey]),
      'protocol_epoch': 'native-v1',
      'received_at': timestamp,
      'occurred_at': timestamp,
    };
    final signature = await Ed25519().sign([
      ...utf8.encode('companion-core/owner-capture/v2\u0000'),
      ...canonicalBytes(capture),
    ], keyPair: key);
    return PendingMessage(
      requestId: requestId,
      text: text,
      eventId: eventId,
      packet: {
        'capture': {...capture, 'signature': hexBytes(signature.bytes)},
        'text': text,
      },
    );
  }

  @override
  Future<String> submit(PendingMessage message) async =>
      (await _request(
            'POST',
            '/v1/console/chat/messages',
            body: message.packet,
          ))['event_id']
          as String;

  @override
  Future<void> refresh() async {
    final response = await _request(
      'POST',
      '/v1/console/chat/devices/preview',
      body: {'public_key': await publicIdentity()},
    );
    if (response['registered'] != true) {
      throw const BackendError('NATIVE_DEVICE_APPROVAL_REQUIRED', status: 403);
    }
    await _bind(response);
  }

  @override
  Future<Map<String, dynamic>> history({int? before}) =>
      _request('GET', '/v1/console/chat/messages', query: {'before': ?before});

  @override
  Future<List<Map<String, dynamic>>> status(String eventId) async =>
      ((await _request('GET', '/v1/console/chat/messages/$eventId'))['items']
              as List)
          .map((item) => Map<String, dynamic>.from(item as Map))
          .toList();

  @override
  Stream<Map<String, dynamic>> watchTurns() async* {
    stopWatching();
    final token = _events = CancelToken();
    try {
      final response = await http.get<ResponseBody>(
        '/v1/console/chat/events',
        cancelToken: token,
        options: Options(
          responseType: ResponseType.stream,
          headers: _headers,
          receiveTimeout: const Duration(seconds: 30),
        ),
      );
      if (response.statusCode != 200) {
        throw BackendError(
          'STREAM_SESSION_REQUIRED',
          status: response.statusCode,
        );
      }
      if (!(response.headers.value('content-type') ?? '').startsWith(
        'text/event-stream',
      )) {
        throw const BackendError('INVALID_EVENT_STREAM');
      }
      var event = '';
      final data = <String>[];
      var size = 0;
      await for (final line
          in response.data!.stream
              .cast<List<int>>()
              .transform(utf8.decoder)
              .transform(const LineSplitter())) {
        if (line.isEmpty) {
          if (data.isNotEmpty) {
            final value = Map<String, dynamic>.from(
              jsonDecode(data.join('\n')) as Map,
            );
            if (event == 'session') {
              throw BackendError(value['error'] as String, status: 403);
            }
            if (event == 'turn') yield value;
          }
          event = '';
          data.clear();
          size = 0;
        } else if (line.startsWith('event:')) {
          event = line.substring(6).trimLeft();
        } else if (line.startsWith('data:')) {
          final value = line.substring(5).replaceFirst(RegExp(r'^ '), '');
          size += value.length;
          if (size > 262144) throw const BackendError('INVALID_EVENT_STREAM');
          data.add(value);
        }
      }
    } on DioException {
      if (!token.isCancelled) {
        throw const BackendError('CONNECTION_UNAVAILABLE');
      }
    } finally {
      token.cancel();
      if (identical(_events, token)) _events = null;
    }
  }

  @override
  void stopWatching() {
    _events?.cancel();
    _events = null;
  }

  @override
  void close() {
    stopWatching();
    _cookie = _csrf = null;
    http.close(force: true);
  }
}
