import 'dart:io';

import 'package:drift/native.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path/path.dart' as path;
import 'package:path_provider/path_provider.dart';

import 'data/database.dart';
import 'data/repository.dart';
import 'data/vault.dart';
import 'ui/app.dart';
import 'ui/providers.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  const override = String.fromEnvironment('COMPANION_DATA_DIR');
  final directory = override.isEmpty
      ? await getApplicationSupportDirectory()
      : Directory(override);
  await directory.create(recursive: true);
  final db = ChatDatabase(
    NativeDatabase.createInBackground(
      File(path.join(directory.path, 'chat.db')),
    ),
  );
  const vault = PlatformVault();
  final repository = ChatRepository(db, CacheCipher(vault));
  runApp(
    ProviderScope(
      overrides: [
        repositoryProvider.overrideWithValue(repository),
        vaultProvider.overrideWithValue(vault),
      ],
      child: const ChatApplication(),
    ),
  );
}
