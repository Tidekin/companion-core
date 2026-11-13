import 'dart:async';

import '../persistence/repository.dart';
import 'core_backend.dart';
import 'models.dart';

class ChatState {
  const ChatState({
    this.turns = const [],
    this.pending = const [],
    this.ready = false,
    this.connected = false,
    this.status = '请登录设备',
    this.draft = '',
    this.unread = 0,
    this.review,
  });
  final List<ChatTurn> turns;
  final List<PendingMessage> pending;
  final bool ready;
  final bool connected;
  final String status;
  final String draft;
  final int unread;
  final ConnectionReview? review;
}

class _SessionStopped implements Exception {}

class BotSession {
  BotSession(
    this.bot,
    this.repository,
    this.backend, {
    DateTime Function()? clock,
    bool autoPoll = true,
  }) : clock = clock ?? DateTime.now {
    loaded = _publish('登录后就可以聊天');
    if (autoPoll) {
      _timer = Timer.periodic(const Duration(seconds: 2), (_) {
        final interval = state.pending.isEmpty ? 15 : 2;
        if (_active &&
            this.clock().difference(_lastSync).inSeconds >= interval) {
          unawaited(sync());
        }
      });
    }
  }
  final BotProfile bot;
  final ChatRepository repository;
  final ChatBackend backend;
  final DateTime Function() clock;
  late final Future<void> loaded;
  ChatState state = const ChatState();
  final _updates = StreamController<ChatState>.broadcast();
  Timer? _timer;
  bool _closed = false;
  bool _active = false;
  bool _connected = false;
  Future<void>? _syncFlight;
  int _generation = 0;
  StreamSubscription<Map<String, dynamic>>? _events;
  Timer? _reconnect;
  Future<void> _eventTail = Future.value();
  int _backoff = 1;

  Future<void> _stopEvents() async {
    _reconnect?.cancel();
    _reconnect = null;
    backend.stopWatching();
    final events = _events;
    _events = null;
    if (events != null) await events.cancel();
  }

  void _startEvents(int generation) {
    if (_closed || !_active || generation != _generation || _events != null) {
      return;
    }
    _events = backend.watchTurns().listen(
      (item) {
        _eventTail = _eventTail
            .then((_) async {
              if (_closed || !_active || generation != _generation) return;
              await repository.merge(bot.id, [item]);
              _guard(generation);
              _backoff = 1;
              _connected = true;
              await _publish('已连接');
            })
            .catchError((Object error) {
              if (error is! _SessionStopped) _streamEnded(generation, error);
            });
      },
      onError: (Object error) => _streamEnded(generation, error),
      onDone: () => _streamEnded(generation, null),
    );
  }

  void _streamEnded(int generation, Object? error) {
    if (_closed || !_active || generation != _generation) return;
    unawaited(_stopEvents());
    _connected = false;
    if (error is BackendError && {401, 403}.contains(error.status)) {
      _active = false;
      unawaited(
        repository
            .withdrawPreviews(bot.id)
            .then((_) => _publish('设备授权或登录已失效，请重新登录')),
      );
      return;
    }
    unawaited(_publish('连接中断；正在恢复原任务'));
    _reconnect = Timer(
      Duration(seconds: _backoff),
      () => _startEvents(generation),
    );
    _backoff = (_backoff * 2).clamp(1, 30);
  }

  void _guard(int generation) {
    if (_closed || generation != _generation) throw _SessionStopped();
  }

  DateTime _lastSync = DateTime.fromMillisecondsSinceEpoch(0);
  Future<void> _networkTail = Future.value();
  ConnectionReview? _review;
  String? _draft;
  String get draft => _draft ?? state.draft;

  Stream<ChatState> watch() => _updates.stream;

  Future<void> _publish(String status) async {
    final turns = await repository.turns(bot.id);
    final pending = await repository.queue(bot.id);
    final viewed = await repository.viewed(bot.id);
    _draft ??= await repository.meta(bot.id, 'draft') as String? ?? '';
    if (_closed) return;
    state = ChatState(
      turns: turns,
      pending: pending,
      connected: _connected,
      ready: await backend.canPrepare(),
      status: status,
      draft: _draft!,
      review: _review,
      unread: turns
          .expand((t) => t.replies)
          .where(
            (r) =>
                r.state == 'SENT' &&
                r.text.isNotEmpty &&
                !viewed.contains(r.id),
          )
          .length,
    );
    _updates.add(state);
  }

  Future<void> _network(Future<void> Function() operation) {
    final generation = _generation;
    final next = _networkTail.then((_) async {
      if (_closed || generation != _generation) return;
      try {
        await loaded;
        _guard(generation);
        await operation();
      } on _SessionStopped {
        return;
      } on BackendError catch (error) {
        if (_closed || generation != _generation) return;
        _connected = false;
        if ({401, 403}.contains(error.status)) {
          _active = false;
          await _stopEvents();
          await repository.withdrawPreviews(bot.id);
        }
        await _publish(
          error.code == 'CONNECTION_UNAVAILABLE'
              ? '连接中断；保留原消息，后台会继续尝试同步'
              : '需要处理：${error.code}',
        );
      } catch (_) {
        if (_closed || generation != _generation) return;
        _connected = false;
        await _publish('本地数据或连接暂时不可用，请稍后重试。');
      }
    });
    _networkTail = next;
    return next;
  }

  Future<void> login(String credential) {
    unawaited(_stopEvents());
    final generation = ++_generation;
    _active = _connected = false;
    return _network(() async {
      _review = null;
      final result = await backend.login(credential);
      _guard(generation);
      if (result.ready) {
        _active = _connected = true;
        await _syncNow(full: true, generation: generation);
      } else {
        _review = result;
        await _publish('请确认登记此设备');
      }
    });
  }

  Future<void> approve() {
    final generation = _generation;
    return _network(() async {
      final review = _review;
      if (review == null) throw const BackendError('请先重新登录以预览设备登记。');
      await backend.approve(review);
      _guard(generation);
      _review = null;
      _active = _connected = true;
      await _syncNow(full: true, generation: generation);
    });
  }

  Future<void> send(String text) async {
    await loaded;
    if (text.trim().isEmpty || text.runes.length > 20000) {
      throw const BackendError('请输入 1 至 20000 个字符。');
    }
    final packet = await backend.prepare(text, randomId(), clock());
    await repository.enqueue(bot.id, packet);
    await _publish('消息已在本机入队');
    if (_active) unawaited(sync());
  }

  Future<void> setDraft(String value) {
    _draft = value;
    return repository.setMeta(bot.id, 'draft', value);
  }

  Future<void> markViewed() async {
    await repository.markViewed(
      bot.id,
      state.turns
          .expand((t) => t.replies)
          .where((r) => r.state == 'SENT')
          .map((r) => r.id),
    );
    await _publish(state.status);
  }

  Future<void> discardRejected() async {
    await repository.discardRejected(bot.id);
    await _publish('已移除被拒绝且尚未接受的本地消息');
  }

  Future<void> sync({bool full = false}) {
    if (!_active || _closed) return Future.value();
    if (_syncFlight != null) return _syncFlight!;
    final generation = _generation;
    return _syncFlight = _network(
      () => _syncNow(full: full, generation: generation),
    ).whenComplete(() => _syncFlight = null);
  }

  Future<void> _syncNow({required bool full, required int generation}) async {
    _guard(generation);
    _lastSync = clock();
    await backend.refresh();
    _guard(generation);
    for (final message in await repository.queue(bot.id)) {
      _guard(generation);
      if (message.error != null) continue;
      try {
        var eventId = message.eventId;
        if (!message.admitted) {
          eventId = await backend.submit(message);
          await repository.accepted(bot.id, message.requestId, eventId);
        }
        _guard(generation);
        if (eventId != null) {
          await repository.merge(bot.id, await backend.status(eventId));
        }
      } on BackendError catch (error) {
        if (!error.rejected) rethrow;
        await repository.rejected(bot.id, message.requestId, error.code);
      }
    }
    _guard(generation);
    final seen = <String>{};
    int? before;
    do {
      _guard(generation);
      final page = await backend.history(before: before);
      _guard(generation);
      final items = (page['items'] as List)
          .map((item) => Map<String, dynamic>.from(item as Map))
          .toList();
      await repository.merge(bot.id, items);
      seen.addAll(items.map((item) => item['event_id'] as String));
      before = page['next_before'] as int?;
    } while (full && before != null);
    if (full) await repository.reconcile(bot.id, seen);
    _guard(generation);
    if (!_active) return;
    _connected = true;
    await _publish('已连接');
    _startEvents(generation);
  }

  Future<void> disconnect() async {
    await _stopEvents();
    _generation++;
    _review = null;
    _active = _connected = false;
    await _publish('已断开连接，本地消息保留');
  }

  Future<void> close() async {
    if (_closed) return;
    _closed = true;
    _generation++;
    _timer?.cancel();
    await _stopEvents();
    backend.close();
    // A paused UI subscriber must not block disposal of the other Bot sessions.
    unawaited(_updates.close());
  }
}

class SessionRegistry {
  SessionRegistry(this.repository, this.createBackend) {
    _subscription = repository.watchBots().listen((bots) {
      for (final bot in bots) {
        _sessions.putIfAbsent(
          bot.id,
          () => BotSession(bot, repository, createBackend(bot)),
        );
      }
    });
  }
  final ChatRepository repository;
  final ChatBackend Function(BotProfile) createBackend;
  final Map<String, BotSession> _sessions = {};
  late final StreamSubscription<List<BotProfile>> _subscription;
  Future<void>? _closing;
  BotSession session(BotProfile bot) => _sessions.putIfAbsent(
    bot.id,
    () => BotSession(bot, repository, createBackend(bot)),
  );
  Future<void> close() => _closing ??= Future.wait<void>([
    // Stop every Bot timer immediately, before waiting for stream cancellation.
    for (final session in _sessions.values) session.close(),
    _subscription.cancel(),
  ]).then((_) {});
}
