import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../chat/core_backend.dart';
import '../chat/models.dart';
import '../chat/session.dart';
import '../persistence/repository.dart';
import '../persistence/vault.dart';

final repositoryProvider = Provider<ChatRepository>(
  (ref) => throw UnimplementedError(),
);
final vaultProvider = Provider<SecretVault>(
  (ref) => throw UnimplementedError(),
);
final backendFactoryProvider = Provider<ChatBackend Function(BotProfile)>(
  (ref) =>
      (bot) => CoreBackend(
        bot,
        ref.read(repositoryProvider),
        ref.read(vaultProvider),
      ),
);
final registryProvider = Provider<SessionRegistry>((ref) {
  final registry = SessionRegistry(
    ref.watch(repositoryProvider),
    ref.watch(backendFactoryProvider),
  );
  ref.onDispose(() {
    registry.close();
  });
  return registry;
});
final botsProvider = StreamProvider<List<BotProfile>>(
  (ref) => ref.watch(repositoryProvider).watchBots(),
);
final chatStateProvider = StreamProvider.family<ChatState, BotProfile>(
  (ref, bot) => ref.watch(registryProvider).session(bot).watch(),
);
