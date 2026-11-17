import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';

import 'models.dart';

/// One favorite image the owner keeps for quick sending, like QQ's 收藏表情.
class Sticker {
  const Sticker({
    required this.id,
    required this.mimeType,
    required this.bytes,
  });

  /// The SHA-256 of the bytes, so saving the same image twice keeps one copy.
  final String id;
  final String mimeType;
  final Uint8List bytes;

  OutgoingImage get image => (bytes: bytes, mimeType: mimeType);
}

class StickerError implements Exception {
  const StickerError(this.message);
  final String message;
}

const maxStickers = 300;

/// The image type from its leading bytes, or null for anything Core would refuse.
String? sniffImageMime(Uint8List bytes) {
  bool starts(List<int> magic, [int offset = 0]) =>
      bytes.length >= offset + magic.length &&
      Iterable<int>.generate(magic.length)
          .every((i) => bytes[offset + i] == magic[i]);
  if (starts([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])) {
    return 'image/png';
  }
  if (starts([0xFF, 0xD8, 0xFF])) return 'image/jpeg';
  if (starts(ascii.encode('GIF87a')) || starts(ascii.encode('GIF89a'))) {
    return 'image/gif';
  }
  if (starts(ascii.encode('RIFF')) && starts(ascii.encode('WEBP'), 8)) {
    return 'image/webp';
  }
  return null;
}

Future<String> _digest(Uint8List bytes) async {
  final hash = await Sha256().hash(bytes);
  return hash.bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
}

/// Favorites live only on this device; nothing about them is sent until one is used.
abstract class StickerStore {
  /// Newest first.
  Future<List<Sticker>> list();
  Future<Sticker> add(Uint8List bytes);
  Future<void> remove(String id);
}

Future<Sticker> _checked(Uint8List bytes) async {
  final mime = sniffImageMime(bytes);
  if (mime == null) {
    throw const StickerError('只能收藏 PNG、JPEG、WebP 或 GIF 图片。');
  }
  if (bytes.length > maxImageBytes) {
    throw const StickerError('这张图片超过 4 MB，不能收藏。');
  }
  return Sticker(id: await _digest(bytes), mimeType: mime, bytes: bytes);
}

class MemoryStickerStore implements StickerStore {
  final _items = <Sticker>[];

  @override
  Future<List<Sticker>> list() async => List.unmodifiable(_items);

  @override
  Future<Sticker> add(Uint8List bytes) async {
    final sticker = await _checked(bytes);
    _items.removeWhere((item) => item.id == sticker.id);
    if (_items.length >= maxStickers) {
      throw const StickerError('收藏已满 300 个，先删掉一些吧。');
    }
    _items.insert(0, sticker);
    return sticker;
  }

  @override
  Future<void> remove(String id) async =>
      _items.removeWhere((item) => item.id == id);
}

/// `<app support>/stickers/<sha256>` plus `index.json` holding the order and types.
class FileStickerStore implements StickerStore {
  FileStickerStore([Future<Directory> Function()? root])
    : _root = root ?? getApplicationSupportDirectory;
  final Future<Directory> Function() _root;
  Future<Directory>? _directory;

  Future<Directory> get _folder => _directory ??= () async {
    final folder = Directory(p.join((await _root()).path, 'stickers'));
    await folder.create(recursive: true);
    return folder;
  }();

  Future<List<Map<String, dynamic>>> _index() async {
    final file = File(p.join((await _folder).path, 'index.json'));
    if (!await file.exists()) return [];
    try {
      return [
        for (final item in jsonDecode(await file.readAsString()) as List)
          Map<String, dynamic>.from(item as Map),
      ];
    } on FormatException {
      return [];
    }
  }

  Future<void> _save(List<Map<String, dynamic>> index) async {
    final folder = await _folder;
    final pending = File(p.join(folder.path, 'index.json.pending'));
    await pending.writeAsString(jsonEncode(index), flush: true);
    await pending.rename(p.join(folder.path, 'index.json'));
  }

  bool _valid(String id) => RegExp(r'^[a-f0-9]{64}$').hasMatch(id);

  @override
  Future<List<Sticker>> list() async {
    final folder = await _folder;
    final result = <Sticker>[];
    for (final item in await _index()) {
      final id = item['id'] as String? ?? '';
      final file = File(p.join(folder.path, id));
      if (!_valid(id) || !await file.exists()) continue;
      result.add(
        Sticker(
          id: id,
          mimeType: item['mime'] as String? ?? 'image/png',
          bytes: await file.readAsBytes(),
        ),
      );
    }
    return result;
  }

  @override
  Future<Sticker> add(Uint8List bytes) async {
    final sticker = await _checked(bytes);
    final index = (await _index())
      ..removeWhere((item) => item['id'] == sticker.id);
    if (index.length >= maxStickers) {
      throw const StickerError('收藏已满 300 个，先删掉一些吧。');
    }
    await File(p.join((await _folder).path, sticker.id))
        .writeAsBytes(bytes, flush: true);
    await _save([
      {'id': sticker.id, 'mime': sticker.mimeType},
      ...index,
    ]);
    return sticker;
  }

  @override
  Future<void> remove(String id) async {
    if (!_valid(id)) return;
    await _save((await _index())..removeWhere((item) => item['id'] == id));
    final file = File(p.join((await _folder).path, id));
    if (await file.exists()) await file.delete();
  }
}
