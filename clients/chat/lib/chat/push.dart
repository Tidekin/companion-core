import 'package:flutter/services.dart';

import '../persistence/repository.dart';
import 'core_backend.dart';
import 'models.dart';

/// Android owns the system permission, FCM token and the pending notification tap.
class AndroidPush {
  static const _channel = MethodChannel('dev.tidekin.companion_chat/push');

  Future<String?> enable() => _channel.invokeMethod<String>('enable');
  Future<String?> getToken() => _channel.invokeMethod<String>('getToken');
  Future<String?> takeRoute() => _channel.invokeMethod<String>('takeRoute');

  void listen({
    required void Function() refresh,
    required void Function() open,
  }) {
    _channel.setMethodCallHandler((call) async {
      if (call.method == 'tokenChanged') refresh();
      if (call.method == 'routeOpened') open();
    });
  }

  void stopListening() => _channel.setMethodCallHandler(null);
}

class BotPush {
  BotPush(this.botId, this.repository, this.backend, this.android) {
    loaded = _load();
  }
  final String botId;
  final ChatRepository repository;
  final ChatBackend backend;
  final AndroidPush android;
  late final Future<void> loaded;
  Future<void> _tail = Future.value();
  String? _route;
  String? _confirmedToken;
  // An empty route is a durable request to unsubscribe after a lost response.
  bool get enabled => _route != null && _route!.isNotEmpty;
  bool get disabling => _route == '';
  bool matches(String route) => enabled && route == _route;

  Future<void> _load() async {
    _route = await repository.meta(botId, 'push_route') as String?;
  }

  Future<void> _save(String? route) async {
    await repository.setMeta(botId, 'push_route', route);
    _route = route;
    _confirmedToken = null;
  }

  Future<void> _inOrder(Future<void> Function() operation) {
    final next = _tail.then((_) async {
      await loaded;
      await operation();
    });
    _tail = next.catchError((Object _) {});
    return next;
  }

  void sessionChanged() => _confirmedToken = null;

  Future<void> forget() => _inOrder(() async {
    if (_route != null) await _save(null);
  });

  Future<void> prepareLogout() => _inOrder(() async {
    if (enabled) await _save('');
  });

  Future<void> setEnabled(bool value, bool Function() authorized) =>
      _inOrder(() async {
        if (!value) {
          await _save('');
          if (!authorized()) throw const BackendError('PUSH_DISABLE_PENDING');
          await _unregister();
          return;
        }
        if (!authorized()) throw const BackendError('PUSH_LOGIN_REQUIRED');
        final token = await android.enable();
        if (token == null) throw const BackendError('PUSH_PERMISSION_DENIED');
        if (!authorized()) throw const BackendError('PUSH_LOGIN_REQUIRED');
        final route = enabled ? _route! : randomId();
        // Persist the intended route before the request: a dropped response can
        // be retried with the same mapping, including after a process restart.
        if (!enabled) await _save(route);
        try {
          if (!authorized()) throw const BackendError('PUSH_LOGIN_REQUIRED');
          await backend.registerPush(token: token, route: route);
          if (!authorized()) throw const BackendError('PUSH_LOGIN_REQUIRED');
          _confirmedToken = token;
        } on BackendError catch (error) {
          if (authorized() &&
              error.status != null &&
              error.status! >= 400 &&
              error.status! < 500 &&
              error.status != 401 &&
              error.status != 408 &&
              error.status != 429) {
            await _save(null);
          }
          rethrow;
        }
      });

  Future<void> _unregister() async {
    try {
      await backend.unregisterPush();
    } on BackendError catch (error) {
      if (error.status != 404) rethrow;
    }
    await _save(null);
  }

  Future<void> refresh(bool Function() authorized, {bool checkToken = false}) =>
      _inOrder(() async {
        if (!authorized() || _route == null) return;
        if (!enabled) {
          await _unregister();
          return;
        }
        if (_confirmedToken != null && !checkToken) return;
        try {
          final token = await android.getToken();
          if (!authorized()) return;
          if (token == null) {
            _confirmedToken = null;
            return;
          }
          if (token != _confirmedToken) {
            await backend.registerPush(token: token, route: _route!);
            if (authorized()) _confirmedToken = token;
          }
        } catch (_) {
          _confirmedToken = null;
          rethrow;
        }
      });
}
