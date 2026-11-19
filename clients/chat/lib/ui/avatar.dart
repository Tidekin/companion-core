import 'dart:math' as math;
import 'dart:ui' as ui;

import 'package:file_selector/file_selector.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:image/image.dart' as img;
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../chat/models.dart';
import '../chat/stickers.dart' show sniffImageMime;
import 'providers.dart';
import 'theme.dart';

/// Static avatars retain the original 512-pixel upload contract.
const avatarUploadSide = 512;

/// Larger originals are refused before decoding; Core's own limit is 5 MiB.
const avatarPickLimit = 20 * 1024 * 1024;

/// One picked image ready for upload.
typedef PickedAvatar = ({Uint8List bytes, String mimeType});

/// Opens the system picker for one PNG/JPEG/GIF/WebP; null when cancelled.
typedef AvatarPicker = Future<PickedAvatar?> Function();

Future<PickedAvatar?> pickAvatarFile() async {
  const images = XTypeGroup(
    label: '图片',
    extensions: ['png', 'jpg', 'jpeg', 'gif', 'webp'],
    mimeTypes: ['image/png', 'image/jpeg', 'image/gif', 'image/webp'],
    uniformTypeIdentifiers: [
      'public.png',
      'public.jpeg',
      'com.compuserve.gif',
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
  final mime = sniffImageMime(original);
  if (mime == null) throw const AvatarPickError('请选择 PNG、JPEG、GIF 或 WebP。');
  ui.Codec? codec;
  try {
    int sourcePixels = 0;
    int sourceSide = 0;
    final buffer = await ui.ImmutableBuffer.fromUint8List(original);
    codec = await ui.instantiateImageCodecWithSize(
      buffer,
      getTargetSize: (width, height) {
        sourcePixels = width * height;
        sourceSide = math.max(width, height);
        final scale = math.min(1.0, avatarUploadSide / math.min(width, height));
        return ui.TargetImageSize(
          width: (width * scale).ceil(),
          height: (height * scale).ceil(),
        );
      },
    );
    final animated =
        codec.frameCount > 1 && (mime == 'image/gif' || mime == 'image/webp');
    if (animated &&
        (sourceSide > 4096 ||
            sourcePixels > 16000000 ||
            codec.frameCount > 240 ||
            sourcePixels * codec.frameCount > 64000000)) {
      throw const AvatarPickError('动图过长或尺寸过大，请缩短或缩小后再试。');
    }
    final frames = <(Uint8List, int)>[];
    var side = avatarUploadSide;
    for (var i = 0; i < (animated ? codec.frameCount : 1); i++) {
      final frame = await codec.getNextFrame();
      try {
        if (animated) {
          side = math.min(256, math.min(frame.image.width, frame.image.height));
        }
        final bytes = await _squarePixels(
          frame.image,
          side,
          animated
              ? ui.ImageByteFormat.rawStraightRgba
              : ui.ImageByteFormat.png,
        );
        if (!animated) return (bytes: bytes, mimeType: 'image/png');
        frames.add((bytes, frame.duration.inMilliseconds));
      } finally {
        frame.image.dispose();
      }
    }
    final bytes = await compute(_encodeAnimation, (
      frames,
      side,
      codec.repetitionCount + 1,
    ));
    if (bytes.length > 5 * 1024 * 1024) {
      throw const AvatarPickError('动图处理后超过 5 MiB，请缩短后再试。');
    }
    return (bytes: bytes, mimeType: 'image/webp');
  } on AvatarPickError {
    rethrow;
  } catch (_) {
    throw const AvatarPickError('无法读取这张图片，请换一张 PNG、JPEG、GIF 或 WebP。');
  } finally {
    codec?.dispose();
  }
}

Future<Uint8List> _squarePixels(
  ui.Image source,
  int size,
  ui.ImageByteFormat format,
) async {
  final side = math.min(source.width, source.height);
  final recorder = ui.PictureRecorder();
  ui.Canvas(recorder).drawImageRect(
    source,
    Rect.fromLTWH(
      (source.width - side) / 2,
      (source.height - side) / 2,
      side.toDouble(),
      side.toDouble(),
    ),
    Rect.fromLTWH(0, 0, size.toDouble(), size.toDouble()),
    Paint()..filterQuality = FilterQuality.high,
  );
  final picture = recorder.endRecording();
  try {
    final square = await picture.toImage(size, size);
    try {
      final data = await square.toByteData(format: format);
      if (data == null) throw const AvatarPickError('图片处理失败，请换一张再试。');
      return data.buffer.asUint8List(data.offsetInBytes, data.lengthInBytes);
    } finally {
      square.dispose();
    }
  } finally {
    picture.dispose();
  }
}

Uint8List _encodeAnimation((List<(Uint8List, int)>, int, int) input) {
  final (frames, size, loops) = input;
  img.Image? animation;
  for (final (rgba, duration) in frames) {
    final frame = img.Image.fromBytes(
      width: size,
      height: size,
      bytes: rgba.buffer,
      bytesOffset: rgba.offsetInBytes,
      numChannels: 4,
      frameDuration: duration,
      loopCount: loops,
    );
    if (animation == null) {
      animation = frame;
    } else {
      animation.addFrame(frame);
    }
  }
  return img.encodeWebP(animation!, exact: false);
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
