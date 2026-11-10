import 'dart:convert';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../chat/models.dart';

abstract interface class SecretVault {
  Future<String?> read(String key);
  Future<void> write(String key, String value);
  Future<void> delete(String key);
}

class PlatformVault implements SecretVault {
  const PlatformVault([this.storage = const FlutterSecureStorage()]);
  final FlutterSecureStorage storage;
  @override
  Future<String?> read(String key) => storage.read(key: key);
  @override
  Future<void> write(String key, String value) =>
      storage.write(key: key, value: value);
  @override
  Future<void> delete(String key) => storage.delete(key: key);
}

class MemoryVault implements SecretVault {
  final Map<String, String> values = {};
  @override
  Future<String?> read(String key) async => values[key];
  @override
  Future<void> write(String key, String value) async {
    values[key] = value;
  }

  @override
  Future<void> delete(String key) async {
    values.remove(key);
  }
}

class CacheCipher {
  CacheCipher(this.vault);
  final SecretVault vault;
  final AesGcm algorithm = AesGcm.with256bits();
  final Map<String, Future<SecretKey>> _keys = {};

  Future<SecretKey> _key(String botId) => _keys.putIfAbsent(botId, () async {
    final name = 'companion_${botId}_cache_key';
    final saved = await vault.read(name);
    if (saved != null) return SecretKey(base64Decode(saved));
    final key = await algorithm.newSecretKey();
    await vault.write(name, base64Encode(await key.extractBytes()));
    return key;
  });

  Future<Uint8List> seal(String botId, String purpose, Object? value) async {
    final box = await algorithm.encrypt(
      canonicalBytes(value),
      secretKey: await _key(botId),
      aad: utf8.encode('$botId/$purpose'),
    );
    return Uint8List.fromList(box.concatenation());
  }

  Future<Object?> open(String botId, String purpose, List<int> value) async {
    final box = SecretBox.fromConcatenation(
      value,
      nonceLength: 12,
      macLength: 16,
    );
    return jsonDecode(
      utf8.decode(
        await algorithm.decrypt(
          box,
          secretKey: await _key(botId),
          aad: utf8.encode('$botId/$purpose'),
        ),
      ),
    );
  }
}
