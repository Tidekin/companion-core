import 'dart:io';
import 'dart:typed_data';

import 'package:companion_chat/chat/stickers.dart';
import 'package:flutter_test/flutter_test.dart';

final png = Uint8List.fromList([
  0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 1, 2, 3, //
]);
final gif = Uint8List.fromList([...'GIF89a'.codeUnits, 9, 9]);

void main() {
  test('only image types Core accepts are recognised', () {
    expect(sniffImageMime(png), 'image/png');
    expect(sniffImageMime(gif), 'image/gif');
    expect(
      sniffImageMime(Uint8List.fromList([0xFF, 0xD8, 0xFF, 0])),
      'image/jpeg',
    );
    expect(
      sniffImageMime(
        Uint8List.fromList([
          ...'RIFF'.codeUnits,
          0,
          0,
          0,
          0,
          ...'WEBP'.codeUnits,
        ]),
      ),
      'image/webp',
    );
    expect(sniffImageMime(Uint8List.fromList('hello'.codeUnits)), isNull);
  });

  test(
    'the file store keeps newest first, one copy each, and survives reopening',
    () async {
      final root = await Directory.systemTemp.createTemp('stickers');
      addTearDown(() => root.delete(recursive: true));
      final store = FileStickerStore(() async => root);
      final first = await store.add(png);
      await store.add(gif);
      await store.add(png);
      final reopened = FileStickerStore(() async => root);
      final items = await reopened.list();
      expect(items.map((item) => item.id), [
        first.id,
        (await store.list())[1].id,
      ]);
      expect(items.first.mimeType, 'image/png');
      expect(items.first.bytes, png);
      await reopened.remove(first.id);
      expect((await store.list()).map((item) => item.mimeType), ['image/gif']);
      expect(File('${root.path}/stickers/${first.id}').existsSync(), isFalse);
    },
  );

  test('a non-image is refused', () async {
    expect(
      () => MemoryStickerStore().add(Uint8List.fromList('text'.codeUnits)),
      throwsA(isA<StickerError>()),
    );
  });
}
