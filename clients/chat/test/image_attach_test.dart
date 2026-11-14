import 'dart:convert';
import 'dart:typed_data';

import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/ui/app.dart';
import 'package:companion_chat/ui/attachments.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

// 1x1 transparent PNG.
final pixel = base64Decode(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA'
  '60e6kgAAAABJRU5ErkJggg==',
);

void main() {
  test('the signed image projection matches Core byte for byte', () async {
    const hash =
        '3608bca1e44ea6c4d268eb6db02260269892c0b42b86bbf1e77a6fa16c3c9282';
    expect(
      await digest({
        'text': '看这个',
        'rich': imageRich('看这个', [hash]),
      }),
      'e575b29b610d00aeb48bcf6df9a4015930a17513eb6d213d811ffc33024d2fe6',
    );
    expect(
      await digest({
        'text': '',
        'rich': imageRich('', [hash, hash]),
      }),
      '1f1e11b4841da9cf1015c119b58b751492a1a9e7d08df616edf4aed8e5c21011',
    );
    expect(imageRich('只有字', const []), isNull);
  });

  test('a turn lists its image slots and a queued message keeps its bytes', () {
    final turn = ChatTurn.fromJson({
      'event_id': 'evt_i',
      'sequence': 1,
      'text': '',
      'images': [
        {'slot': 0},
        {'slot': 1},
      ],
      'created_at': '2026-11-14T12:00:00Z',
      'state': 'SUCCEEDED',
      'redacted': false,
      'replies': [],
    });
    expect(turn.images, [0, 1]);
    final pending = PendingMessage(
      requestId: 'r',
      text: '',
      packet: {
        'images': [
          {'mime_type': 'image/png', 'data_base64': base64Encode(pixel)},
        ],
      },
    );
    expect(pending.images.single, pixel);
  });

  testWidgets('an image-only message shows its picture without an empty line', (
    tester,
  ) async {
    final turn = ChatTurn.fromJson({
      'event_id': 'evt_i',
      'sequence': 1,
      'text': '',
      'images': [
        {'slot': 0},
      ],
      'created_at': '2026-11-14T12:00:00Z',
      'state': 'SUCCEEDED',
      'redacted': false,
      'replies': [],
    });
    final asked = <int>[];
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: TurnView(
            turn: turn,
            loadImage: (slot) {
              asked.add(slot);
              return Future.value(Uint8List.fromList(pixel));
            },
          ),
        ),
      ),
    );
    await tester.pump();
    expect(asked, [0]);
    expect(find.byType(MessageImages), findsOneWidget);
    expect(find.byKey(const ValueKey('message_image_tile_0')), findsOneWidget);
    expect(find.text(''), findsNothing);
  });

  testWidgets('picked images can be removed before sending', (tester) async {
    final images = <OutgoingImage>[
      (bytes: Uint8List.fromList(pixel), mimeType: 'image/png'),
      (bytes: Uint8List.fromList(pixel), mimeType: 'image/png'),
    ];
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: StatefulBuilder(
            builder: (context, setState) => AttachmentStrip(
              images: images,
              onRemove: (index) => setState(() => images.removeAt(index)),
            ),
          ),
        ),
      ),
    );
    expect(find.byKey(const ValueKey('attachment_1')), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('remove_attachment_0')));
    await tester.pump();
    expect(images, hasLength(1));
    expect(find.byKey(const ValueKey('attachment_1')), findsNothing);
  });
}
