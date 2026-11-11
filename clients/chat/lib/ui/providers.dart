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

/// The committed profile for a connected Bot; null before login or when Core
/// has no profile capability. Only newer revisions replace the current value.
final profileProvider = StreamProvider.family<CompanionProfile?, BotProfile>((
  ref,
  bot,
) async* {
  final connected = ref.watch(
    chatStateProvider(bot).select((value) => value.value?.connected ?? false),
  );
  if (!connected) {
    yield null;
    return;
  }
  final backend = ref.watch(registryProvider).session(bot).backend;
  CompanionProfile? last;
  try {
    last = await backend.profile();
  } on BackendError {
    last = null;
  }
  yield last;
  await for (final update in backend.profileUpdates) {
    if (update.isNewerThan(last)) {
      last = update;
      yield update;
    }
  }
});

/// Change records, refetched whenever the committed profile revision changes.
final profileChangesProvider =
    FutureProvider.family<List<ProfileChange>, BotProfile>((ref, bot) async {
      final revision = ref.watch(
        profileProvider(bot).select((value) => value.value?.revision),
      );
      if (revision == null) return const [];
      try {
        return await ref
            .watch(registryProvider)
            .session(bot)
            .backend
            .profileChanges();
      } on BackendError {
        return const [];
      }
    });
