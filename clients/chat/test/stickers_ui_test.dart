import 'dart:typed_data';

import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/chat/stickers.dart';
import 'package:companion_chat/ui/attachments.dart';
import 'package:companion_chat/ui/providers.dart';
import 'package:companion_chat/ui/stickers.dart';
import 'package:companion_chat/ui/theme.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

final png = Uint8List.fromList([
  0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 1, 2, 3, //
]);

Widget host(StickerStore store, Widget child) => ProviderScope(
  overrides: [stickerStoreProvider.overrideWithValue(store)],
  child: MaterialApp(
    theme: warmTheme(),
    home: Scaffold(body: child),
  ),
);

void main() {
  test('an emoji goes in at the caret and replaces a selection', () {
    final controller = TextEditingController(text: '早上好');
    controller.selection = const TextSelection.collapsed(offset: 2);
    insertAtCaret(controller, '☀️');
    expect(controller.text, '早上☀️好');
    expect(controller.selection.baseOffset, 2 + '☀️'.length);
    controller.selection = const TextSelection(baseOffset: 0, extentOffset: 2);
    insertAtCaret(controller, '😊');
    expect(controller.text, '😊☀️好');
    controller.dispose();
  });

  test('a controller without a caret appends', () {
    final controller = TextEditingController(text: '晚安');
    insertAtCaret(controller, '🌙');
    expect(controller.text, '晚安🌙');
    controller.dispose();
  });

  testWidgets('the panel switches groups and reports the tapped emoji', (
    tester,
  ) async {
    final picked = <String>[];
    await tester.pumpWidget(
      host(MemoryStickerStore(), StickerPanel(onEmoji: picked.add)),
    );
    await tester.tap(find.byKey(const ValueKey('emoji_group_0')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('emoji_😂')));
    await tester.tap(find.byKey(const ValueKey('emoji_group_3')));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('emoji_😂')), findsNothing);
    await tester.tap(find.byKey(const ValueKey('emoji_🍚')));
    expect(picked, ['😂', '🍚']);
  });

  testWidgets('a favorite sends with one tap and a long press deletes it', (
    tester,
  ) async {
    final store = MemoryStickerStore();
    final sticker = await tester.runAsync(() => store.add(png));
    final sent = <OutgoingImage>[];
    await tester.pumpWidget(
      host(store, StickerPanel(onEmoji: (_) {}, onSticker: sent.add)),
    );
    await tester.pumpAndSettle();
    final tile = find.byKey(ValueKey('sticker_${sticker!.id}'));
    await tester.tap(tile);
    expect(sent.single.mimeType, 'image/png');
    await tester.longPress(tile);
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('sticker_remove_confirm')));
    await tester.pumpAndSettle();
    expect(await tester.runAsync(store.list), isEmpty);
    expect(tile, findsNothing);
  });

  testWidgets('long-pressing a chat image keeps it as a favorite', (
    tester,
  ) async {
    final store = MemoryStickerStore();
    await tester.pumpWidget(host(store, MessageImages.local(images: [png])));
    await tester.longPress(find.byKey(const ValueKey('message_image_tile_0')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('favorite_image')));
    await tester.pumpAndSettle();
    final items = await tester.runAsync(store.list);
    expect(items!.single.bytes, png);
  });
}
