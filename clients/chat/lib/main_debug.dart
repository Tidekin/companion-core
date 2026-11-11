// Development-only entry point. Never use this target for a release build.
import 'package:drift/native.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_driver/driver_extension.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'chat/models.dart';
import 'persistence/database.dart';
import 'persistence/repository.dart';
import 'persistence/vault.dart';
import 'dev/demo_backend.dart';
import 'ui/app.dart';
import 'ui/providers.dart';

Future<void> main() async {
  if (!kDebugMode) {
    throw UnsupportedError('Development entry requires debug mode.');
  }
  enableFlutterDriverExtension();
  WidgetsFlutterBinding.ensureInitialized();
  final vault = MemoryVault();
  final repository = ChatRepository(
    ChatDatabase(NativeDatabase.memory()),
    CacheCipher(vault),
  );
  for (final id in ['alpha', 'beta']) {
    await repository.addBot(
      BotProfile(
        id: id,
        name: id == 'alpha' ? 'Core · Alpha' : 'Core · Beta',
        endpoint: Uri.parse('https://$id.invalid'),
      ),
    );
  }
  runApp(
    ProviderScope(
      overrides: [
        repositoryProvider.overrideWithValue(repository),
        vaultProvider.overrideWithValue(vault),
        backendFactoryProvider.overrideWithValue(DemoBackend.new),
      ],
      child: const ChatApplication(
        developmentNotice: '开发验收 · 合成 Bot · 数据仅在内存中',
      ),
    ),
  );
}
