import 'dart:typed_data';
import 'dart:ui' as ui;

import 'package:file_selector/file_selector.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../chat/models.dart';
import 'providers.dart';
import 'theme.dart';

/// Upload side of the square; Core makes the final 512x512 PNG and checks it again.
const avatarUploadSide = 512;

/// Larger originals are refused before decoding; Core's own limit is 5 MiB.
const avatarPickLimit = 20 * 1024 * 1024;

/// One picked image ready for upload.
typedef PickedAvatar = ({Uint8List bytes, String mimeType});

/// Opens the system picker for one PNG/JPEG/WebP; null when cancelled.
typedef AvatarPicker = Future<PickedAvatar?> Function();

Future<PickedAvatar?> pickAvatarFile() async {
  const images = XTypeGroup(
    label: '图片',
    extensions: ['png', 'jpg', 'jpeg', 'webp'],
    mimeTypes: ['image/png', 'image/jpeg', 'image/webp'],
    uniformTypeIdentifiers: [
      'public.png',
      'public.jpeg',
      'org.webmproject.webp',
    ],
  );
  final file = await openFile(acceptedTypeGroups: [images]);
  if (file == null) return null;
  if (await file.length() > avatarPickLimit) {
    throw const AvatarPickError('图片太大，请选一张 20 MB 以内的图片。');
  }
  return squareAvatar(await file.readAsBytes());
}

class AvatarPickError implements Exception {
  const AvatarPickError(this.message);
  final String message;
}

/// Centre-crops and scales locally so the upload stays small; the original's
/// metadata never leaves the device because only decoded pixels are re-encoded.
Future<PickedAvatar> squareAvatar(Uint8List original) async {
  ui.Image? decoded;
  try {
    final buffer = await ui.ImmutableBuffer.fromUint8List(original);
    final codec = await ui.instantiateImageCodecWithSize(
      buffer,
      getTargetSize: (width, height) {
        final short = width < height ? width : height;
        if (short <= avatarUploadSide) {
          return ui.TargetImageSize(width: width, height: height);
        }
        final scale = avatarUploadSide / short;
        return ui.TargetImageSize(
          width: (width * scale).ceil(),
          height: (height * scale).ceil(),
        );
      },
    );
    decoded = (await codec.getNextFrame()).image;
    codec.dispose();
  } catch (_) {
    throw const AvatarPickError('无法读取这张图片，请换一张 PNG、JPEG 或 WebP。');
  }
  final side = decoded.width < decoded.height ? decoded.width : decoded.height;
  final recorder = ui.PictureRecorder();
  ui.Canvas(recorder).drawImageRect(
    decoded,
    Rect.fromLTWH(
      (decoded.width - side) / 2,
      (decoded.height - side) / 2,
      side.toDouble(),
      side.toDouble(),
    ),
    Rect.fromLTWH(
      0,
      0,
      avatarUploadSide.toDouble(),
      avatarUploadSide.toDouble(),
    ),
    Paint()..filterQuality = FilterQuality.high,
  );
  decoded.dispose();
  final picture = recorder.endRecording();
  final square = await picture.toImage(avatarUploadSide, avatarUploadSide);
  picture.dispose();
  final png = await square.toByteData(format: ui.ImageByteFormat.png);
  square.dispose();
  if (png == null) throw const AvatarPickError('图片处理失败，请换一张再试。');
  return (bytes: png.buffer.asUint8List(), mimeType: 'image/png');
}

/// The companion's committed avatar, falling back to its initial while the
/// image loads, when it has none, or when the asset is no longer available.
class CompanionAvatar extends ConsumerWidget {
  const CompanionAvatar({
    super.key,
    required this.bot,
    this.name,
    this.size = 46,
  });
  final BotProfile bot;
  final String? name;
  final double size;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final profile = ref.watch(profileProvider(bot)).value;
    final shown = name ?? profile?.displayName ?? bot.name;
    final fallback = InitialAvatar(id: bot.id, name: shown, size: size);
    final asset = profile?.avatarAssetId;
    final image = asset == null
        ? null
        : ref.watch(avatarImageProvider((bot, asset))).value;
    final child = image == null
        ? fallback
        : ExcludeSemantics(
            child: Container(
              width: size,
              height: size,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                border: Border.all(color: const Color(0xB3FFFFFF), width: 1.5),
              ),
              child: ClipOval(
                child: Image.memory(
                  image,
                  width: size,
                  height: size,
                  fit: BoxFit.cover,
                  gaplessPlayback: true,
                  errorBuilder: (_, _, _) => fallback,
                ),
              ),
            ),
          );
    return AnimatedSwitcher(
      duration: Motion.reduced(context)
          ? Duration.zero
          : const Duration(milliseconds: 160),
      child: KeyedSubtree(
        key: ValueKey('avatar_${image == null ? 'initial' : asset}'),
        child: child,
      ),
    );
  }
}
