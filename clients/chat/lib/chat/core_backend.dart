import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

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
  Future<bool> resume();
  Future<ConnectionReview> login(String credential);
  Future<void> logout();
  Future<void> approve(ConnectionReview review);
  Future<bool> canPrepare();
  Future<PendingMessage> prepare(
    String text,
    String requestId,
    DateTime occurredAt, {
    List<OutgoingImage> images = const [],
    Map<String, dynamic>? moment,
  });
  Future<String> submit(PendingMessage message);

  /// The history preview of one image attached to the owner's message.
  Future<Uint8List> messageImage(String eventId, int slot);

  /// A picture generated for one of her moments.
  Future<Uint8List> momentImage(String momentId, int slot);

  /// The meme a delivered reply carries; refused once it is erased or excluded.
  Future<Uint8List> replySticker(String messageId);
  Future<void> refresh();
  Future<Map<String, dynamic>> history({int? before});
  Future<List<Map<String, dynamic>>> status(String eventId);
  Future<Map<String, dynamic>> replyNow(String eventId);
  Stream<Map<String, dynamic>> watchTurns();
  void stopWatching();
  Future<String> publicIdentity();
  void close();

  /// Committed profile only; a failed or pending update never appears here.
  Future<CompanionProfile> profile();
  Future<List<ProfileChange>> profileChanges({int? before});
  Future<CompanionProfile> updateProfileSettings({
    required int expectedSettingsRevision,
    bool? autoName,
    bool? autoAvatar,
  });

  /// Uploads owner-chosen avatar material and, in the same Core transaction,
  /// makes it the current avatar. Core re-checks and re-encodes the bytes.
  Future<CompanionProfile> uploadAvatar({
    required Uint8List bytes,
    required String mimeType,
    required int expectedRevision,
    required String operationId,
  });

  /// The checked 512x512 PNG for one asset; immutable per asset id.
  Future<Uint8List> avatarImage(String assetId);
  Future<CompanionProfile> setProfileAlias({
    required int expectedSettingsRevision,
    String? alias,
  });
  Future<CompanionProfile> restoreProfile({
    required String changeId,
    required int expectedRevision,
    required bool pauseName,
    required String operationId,
  });

  /// Profile snapshots pushed by the event stream while it is connected.
  Stream<CompanionProfile> get profileUpdates;

  /// Owner-only moments, newest first; `before` pages by creation time.
  Future<List<Moment>> moments({DateTime? before});

  /// Moments from this calendar day in earlier years.
  Future<List<Moment>> momentMemories();
  Future<void> likeMoment(String momentId, bool liked);
  Future<void> hideMoment(String momentId);
  Future<void> forgetMoment(String momentId);

  /// A marker whenever the feed changes; the page re-reads what it shows.
  Stream<void> get momentUpdates;

  /// The thread under one moment, oldest first.
  Future<List<MomentComment>> momentComments(String momentId);

  /// Her newest moment after [after], for the chat page's preview card.
  Future<Moment?> latestMoment(DateTime? after);
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
  Map<String, dynamic>? _captureIdentity;
  String? _csrf;
  String? _cookie;
  String get _sessionKey => 'companion_${bot.id}_session';
  int _sessionGeneration = 0;
  Future<void> _sessionWrites = Future.value();

  void _checkSession(int generation) {
    if (generation != _sessionGeneration) {
      throw const BackendError('SESSION_CHANGED');
    }
  }

  Future<void> _saveSession(String? value, int generation) {
    final next = _sessionWrites.then((_) async {
      if (generation != _sessionGeneration) return;
      if (value == null) {
        await vault.delete(_sessionKey);
      } else {
        await vault.write(_sessionKey, value);
      }
    });
    _sessionWrites = next.catchError((Object _) {});
    return next;
  }

  CancelToken? _events;
  final _profiles = StreamController<CompanionProfile>.broadcast();
  final _moments = StreamController<void>.broadcast();

  @override
  Stream<CompanionProfile> get profileUpdates => _profiles.stream;

  @override
  Stream<void> get momentUpdates => _moments.stream;

  List<Moment> _momentList(Map<String, dynamic> value) => [
    for (final item in value['items'] as List)
      Moment.fromJson(Map<String, dynamic>.from(item as Map)),
  ];

  @override
  Future<List<Moment>> moments({DateTime? before}) async => _momentList(
    await _request(
      'GET',
      '/v1/console/chat/moments',
      query: {if (before != null) 'before': before.toUtc().toIso8601String()},
    ),
  );

  @override
  Future<List<MomentComment>> momentComments(String momentId) async => [
    for (final item
        in (await _request(
              'GET',
              '/v1/console/chat/moments/$momentId/comments',
            ))['items']
            as List)
      MomentComment.fromJson(Map<String, dynamic>.from(item as Map)),
  ];

  @override
  Future<Moment?> latestMoment(DateTime? after) async {
    final value = (await _request(
      'GET',
      '/v1/console/chat/moments/latest',
      query: {if (after != null) 'after': after.toUtc().toIso8601String()},
    ))['item'];
    if (value == null) return null;
    final item = Map<String, dynamic>.from(value as Map);
    return Moment.fromJson({...item, 'trigger': 'LIFE', 'liked': false});
  }

  @override
  Future<List<Moment>> momentMemories() async =>
      _momentList(await _request('GET', '/v1/console/chat/moments/memories'));

  @override
  Future<void> likeMoment(String momentId, bool liked) => _request(
    'POST',
    '/v1/console/chat/moments/$momentId/like',
    body: {'liked': liked},
  );

  @override
  Future<void> hideMoment(String momentId) => _request(
    'POST',
    '/v1/console/chat/moments/$momentId/visibility',
    body: {'hidden': true},
  );

  @override
  Future<void> forgetMoment(String momentId) =>
      _request('POST', '/v1/console/chat/moments/$momentId/forget', body: {});

  @override
  Future<CompanionProfile> profile() async => CompanionProfile.fromJson(
    await _request('GET', '/v1/console/chat/profile'),
  );

  @override
  Future<List<ProfileChange>> profileChanges({int? before}) async =>
      ((await _request(
                'GET',
                '/v1/console/chat/profile/changes',
                query: {'before': ?before},
              ))['changes']
              as List)
          .map(
            (item) =>
                ProfileChange.fromJson(Map<String, dynamic>.from(item as Map)),
          )
          .toList();

  @override
  Future<CompanionProfile> updateProfileSettings({
    required int expectedSettingsRevision,
    bool? autoName,
    bool? autoAvatar,
  }) async => CompanionProfile.fromJson(
    await _request(
      'PUT',
      '/v1/console/chat/profile/settings',
      body: {
        'expected_settings_revision': expectedSettingsRevision,
        'auto_name': ?autoName,
        'auto_avatar': ?autoAvatar,
      },
    ),
  );

  @override
  Future<CompanionProfile> uploadAvatar({
    required Uint8List bytes,
    required String mimeType,
    required int expectedRevision,
    required String operationId,
  }) async => CompanionProfile.fromJson(
    Map<String, dynamic>.from(
      (await _request(
            'POST',
            '/v1/console/chat/profile/avatars',
            body: {
              'mime_type': mimeType,
              'data_base64': base64Encode(bytes),
              'operation_id': operationId,
              'expected_revision': expectedRevision,
            },
          ))['profile']
          as Map,
    ),
  );

  final _avatarImages = <String, Future<Uint8List>>{};

  @override
  Future<Uint8List> avatarImage(String assetId) {
    if (!RegExp(r'^avt_[a-f0-9]{32}$').hasMatch(assetId)) {
      return Future.error(const BackendError('PROFILE_ASSET_UNAVAILABLE'));
    }
    return _avatarImages[assetId] ??= _fetchAvatar(assetId)
        .catchError((Object error) {
          // A failure is not cached; the next build may try again.
          _avatarImages.remove(assetId);
          throw error;
        });
  }

  final _messageImages = <String, Future<Uint8List>>{};

  @override
  Future<Uint8List> momentImage(String momentId, int slot) {
    if (!RegExp(r'^moment_[a-f0-9]{32}$').hasMatch(momentId) || slot < 0) {
      return Future.error(const BackendError('MOMENT_IMAGE_NOT_FOUND'));
    }
    final key = '$momentId/$slot';
    return _messageImages[key] ??=
        _fetchBytes('/v1/console/chat/moments/$momentId/images/$slot')
            .catchError((Object error) {
              _messageImages.remove(key);
              throw error;
            });
  }

  @override
  Future<Uint8List> messageImage(String eventId, int slot) {
    if (!RegExp(r'^evt_[a-f0-9]{64}$').hasMatch(eventId) || slot < 0) {
      return Future.error(const BackendError('NATIVE_IMAGE_NOT_FOUND'));
    }
    final key = '$eventId/$slot';
    return _messageImages[key] ??=
        _fetchBytes('/v1/console/chat/messages/$eventId/images/$slot')
            .catchError((Object error) {
              _messageImages.remove(key);
              throw error;
            });
  }

  final _stickers = <String, Future<Uint8List>>{};

  @override
  Future<Uint8List> replySticker(String messageId) {
    if (!RegExp(r'^msg_[A-Za-z0-9_]{1,64}$').hasMatch(messageId)) {
      return Future.error(const BackendError('NATIVE_IMAGE_NOT_FOUND'));
    }
    return _stickers[messageId] ??=
        _fetchBytes('/v1/console/chat/replies/$messageId/sticker')
            .catchError((Object error) {
              _stickers.remove(messageId);
              throw error;
            });
  }

  Future<Uint8List> _fetchAvatar(String assetId) =>
      _fetchBytes('/v1/console/chat/profile/assets/$assetId');

  Future<Uint8List> _fetchBytes(String path) async {
    try {
      final response = await http.get<List<int>>(
        path,
        options: Options(headers: _headers, responseType: ResponseType.bytes),
      );
      final status = response.statusCode ?? 503;
      if (status != 200 || response.data == null) {
        throw BackendError('HTTP_$status', status: status);
      }
      return Uint8List.fromList(response.data!);
    } on DioException {
      throw const BackendError('CONNECTION_UNAVAILABLE');
    }
  }

  @override
  Future<CompanionProfile> setProfileAlias({
    required int expectedSettingsRevision,
    String? alias,
  }) async => CompanionProfile.fromJson(
    await _request(
      'PUT',
      '/v1/console/chat/profile/alias',
      body: {
        'expected_settings_revision': expectedSettingsRevision,
        'alias': alias,
      },
    ),
  );

  @override
  Future<CompanionProfile> restoreProfile({
    required String changeId,
    required int expectedRevision,
    required bool pauseName,
    required String operationId,
  }) async => CompanionProfile.fromJson(
    await _request(
      'POST',
      '/v1/console/chat/profile/restore',
      body: {
        'change_id': changeId,
        'expected_revision': expectedRevision,
        'pause_name': pauseName,
        'operation_id': operationId,
      },
    ),
  );

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
    Map<String, dynamic>? headers,
  }) async {
    final generation = _sessionGeneration;
    if (!(path.startsWith('/v1/console/chat/') ||
        path == '/v1/console/session')) {
      throw const BackendError('CLIENT_CHAT_SCOPE_REQUIRED');
    }
    try {
      final response = await http.request<Object?>(
        path,
        data: body,
        queryParameters: query,
        options: Options(method: method, headers: headers ?? _headers),
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
        _checkSession(generation);
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
    final generation = ++_sessionGeneration;
    _cookie = _csrf = null;
    await _saveSession(null, generation);
    _checkSession(generation);
    final response = await _request(
      'POST',
      '/v1/console/session',
      body: {
        'password': credential,
        'device_public_key': await publicIdentity(),
      },
    );
    _checkSession(generation);
    _csrf = response['csrf_token'] as String;
    await _saveSession(
      jsonEncode({
        'origin': bot.endpoint.origin,
        'cookie': _cookie,
        'csrf': _csrf,
      }),
      generation,
    );
    _checkSession(generation);
    final preview = await _request(
      'POST',
      '/v1/console/chat/devices/preview',
      body: {'public_key': await publicIdentity()},
    );
    _checkSession(generation);
    if (preview['registered'] == true) await _bind(preview);
    return ConnectionReview(preview['registered'] == true, preview);
  }

  @override
  Future<bool> resume() async {
    final generation = _sessionGeneration;
    final saved = await vault.read(_sessionKey);
    _checkSession(generation);
    if (saved == null) return false;
    try {
      final value = jsonDecode(saved);
      if (value is! Map ||
          value['origin'] != bot.endpoint.origin ||
          value['cookie'] is! String ||
          value['csrf'] is! String ||
          !RegExp(r'^companion_owner_session=[A-Za-z0-9_-]+$')
              .hasMatch(value['cookie'] as String)) {
        throw const FormatException('Invalid saved session');
      }
      _cookie = value['cookie'] as String;
      _csrf = value['csrf'] as String;
    } on FormatException {
      _cookie = _csrf = null;
      await _saveSession(null, generation);
      return false;
    }
    try {
      final session = await _request('GET', '/v1/console/session');
      _checkSession(generation);
      _csrf = session['csrf_token'] as String;
      return true;
    } on BackendError catch (error) {
      if (error.status != 401 && error.status != 403) rethrow;
      _checkSession(generation);
      _cookie = _csrf = null;
      await _saveSession(null, generation);
      return false;
    }
  }

  @override
  Future<void> logout() async {
    final headers = _headers;
    final generation = ++_sessionGeneration;
    _cookie = _csrf = null;
    await _saveSession(null, generation);
    try {
      if (headers.containsKey(HttpHeaders.cookieHeader)) {
        await _request('DELETE', '/v1/console/session', headers: headers);
      }
    } on BackendError catch (error) {
      if (error.status != 401) rethrow;
    }
  }

  Future<void> _bind(Map<String, dynamic> value) async {
    _authority = Map<String, dynamic>.from(value['authority'] as Map);
    await repository.setMeta(bot.id, 'core_authority', _authority);
    _captureIdentity = switch (value['capture_identity']) {
      final Map identity => Map<String, dynamic>.from(identity),
      _ => null,
    };
    await repository.setMeta(bot.id, 'core_capture_identity', _captureIdentity);
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
    _captureIdentity ??= switch (await repository.meta(
      bot.id,
      'core_capture_identity',
    )) {
      final Map identity => Map<String, dynamic>.from(identity),
      _ => null,
    };
    return _authority != null;
  }

  @override
  Future<PendingMessage> prepare(
    String text,
    String requestId,
    DateTime occurredAt, {
    List<OutgoingImage> images = const [],
    Map<String, dynamic>? moment,
  }) async {
    if (!await canPrepare()) throw const BackendError('请先登录并登记此设备。');
    final hashes = [
      for (final image in images)
        hexBytes((await Sha256().hash(image.bytes)).bytes),
    ];
    final rich = imageRich(text, hashes);
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
      // Each image is signed by its hash inside the message projection.
      // A moment reference is signed with the text, exactly as Core checks it.
      'input_sha256': await digest(
        moment != null
            ? {'text': text, 'rich': ?rich, 'moment': moment}
            : rich == null
            ? text
            : {'text': text, 'rich': rich},
      ),
      'frame_sha256': await digest([requestId, text, timestamp]),
      'evidence_sha256': await digest(['native.chat', deviceId, requestId]),
      'identity_sha256':
          _captureIdentity?['identity_sha256'] ??
          await digest(['native.chat', deviceId, publicKey]),
      'protocol_epoch': _captureIdentity?['protocol_epoch'] ?? 'native-v1',
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
  Future<Map<String, dynamic>> replyNow(String eventId) =>
      _request('POST', '/v1/console/chat/messages/$eventId/reply-now');

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
            if (event == 'profile') {
              _profiles.add(CompanionProfile.fromJson(value));
            }
            if (event == 'moment') _moments.add(null);
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
    _sessionGeneration++;
    stopWatching();
    unawaited(_profiles.close());
    unawaited(_moments.close());
    _cookie = _csrf = null;
    http.close(force: true);
  }
}
