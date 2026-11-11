import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/dev/demo_backend.dart';
import 'package:companion_chat/ui/avatar.dart';
import 'package:companion_chat/ui/profile.dart';
import 'package:companion_chat/ui/providers.dart';
import 'package:companion_chat/ui/theme.dart';

final bot = BotProfile(
  id: 'a',
  name: 'Core A',
  endpoint: Uri.parse('https://a.test'),
);

const asset = 'avt_0123456789abcdef0123456789abcdef';

CompanionProfile profile({String? avatar}) => CompanionProfile(
  companionId: 'cmp_a',
  revision: 3,
  displayName: '小满',
  avatarAssetId: avatar,
  autoName: true,
  autoAvatar: true,
  settingsRevision: 1,
  selfUpdateEnabled: true,
);

Future<Uint8List> png(int width, int height) async {
  final recorder = ui.PictureRecorder();
  ui.Canvas(recorder).drawRect(
    Rect.fromLTWH(0, 0, width.toDouble(), height.toDouble()),
    Paint()..color = const Color(0xFFC87850),
  );
  final image = await recorder.endRecording().toImage(width, height);
  final data = await image.toByteData(format: ui.ImageByteFormat.png);
  return data!.buffer.asUint8List();
}

Widget host(Widget child, CompanionProfile value, Uint8List? bytes) =>
    ProviderScope(
      overrides: [
        profileProvider.overrideWith((ref, _) => Stream.value(value)),
        profileChangesProvider.overrideWith((ref, _) async => const []),
        avatarImageProvider.overrideWith((ref, _) async => bytes),
      ],
      child: MaterialApp(
        theme: warmTheme(),
        home: Scaffold(body: child),
      ),
    );

void main() {
  testWidgets('a picked image is centre-cropped to one square PNG', (
    tester,
  ) async {
    await tester.runAsync(() async {
      final picked = await squareAvatar(await png(900, 300));
      expect(picked.mimeType, 'image/png');
      final codec = await ui.instantiateImageCodec(picked.bytes);
      final frame = (await codec.getNextFrame()).image;
      expect((frame.width, frame.height), (avatarUploadSide, avatarUploadSide));
      await expectLater(
        squareAvatar(Uint8List.fromList([1, 2, 3])),
        throwsA(isA<AvatarPickError>()),
      );
    });
  });

  testWidgets('the committed avatar replaces the initial; a missing asset '
      'falls back to it', (tester) async {
    final bytes = (await tester.runAsync(() => png(8, 8)))!;
    await tester.pumpWidget(
      host(CompanionAvatar(bot: bot), profile(avatar: asset), bytes),
    );
    await tester.pumpAndSettle();
    expect(find.byType(Image), findsOneWidget);
    expect(find.text('小'), findsNothing);
  });

  testWidgets('a missing asset falls back to the initial', (tester) async {
    await tester.pumpWidget(
      host(CompanionAvatar(bot: bot), profile(avatar: asset), null),
    );
    await tester.pumpAndSettle();
    expect(find.byType(Image), findsNothing);
    expect(find.text('小'), findsOneWidget);
  });

  testWidgets('the panel offers upload and the avatar switch', (tester) async {
    tester.view.physicalSize = const Size(800, 1400);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(host(ProfilePanel(bot: bot), profile(), null));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('profile_change_avatar')), findsOneWidget);
    expect(find.byKey(const ValueKey('profile_auto_avatar')), findsOneWidget);
  });

  testWidgets('timeline names an avatar change by its real actor', (
    tester,
  ) async {
    final change = ProfileChange(
      changeId: 'pchg_3',
      oldRevision: 2,
      newRevision: 3,
      fields: 'avatar',
      oldName: '小满',
      newName: '小满',
      newAvatarAssetId: asset,
      actorType: 'companion',
      createdAt: DateTime.utc(2026, 11, 12, 1),
    );
    await tester.pumpWidget(
      host(ProfileEventRow(bot: bot, change: change), profile(), null),
    );
    await tester.pumpAndSettle();
    expect(find.text('小满 换了新头像'), findsOneWidget);
  });

  test(
    'demo upload commits one avatar revision and restore brings it back',
    () async {
      final backend = DemoBackend(bot);
      addTearDown(backend.close);
      final bytes = Uint8List.fromList([1, 2, 3]);
      final updated = await backend.uploadAvatar(
        bytes: bytes,
        mimeType: 'image/png',
        expectedRevision: 1,
        operationId: 'avatar-1',
      );
      expect(updated.revision, 2);
      expect(await backend.avatarImage(updated.avatarAssetId!), bytes);
      expect(backend.changes.last.fields, 'avatar');
      await expectLater(
        backend.uploadAvatar(
          bytes: bytes,
          mimeType: 'image/png',
          expectedRevision: 1,
          operationId: 'avatar-2',
        ),
        throwsA(isA<Object>()),
      );
    },
  );
}
