import 'dart:typed_data';

import 'package:file_selector/file_selector.dart';
import 'package:flutter/material.dart';

import '../chat/models.dart';
import 'theme.dart';

class AttachPickError implements Exception {
  const AttachPickError(this.message);
  final String message;
}

const _mimeByExtension = {
  'png': 'image/png',
  'jpg': 'image/jpeg',
  'jpeg': 'image/jpeg',
  'webp': 'image/webp',
  'gif': 'image/gif',
};

/// Opens the system picker for up to [remaining] images, checked against
/// Core's limits before anything is signed or queued.
Future<List<OutgoingImage>> pickChatImages({
  required int remaining,
  required int alreadyBytes,
}) async {
  const images = XTypeGroup(
    label: '图片',
    extensions: ['png', 'jpg', 'jpeg', 'webp', 'gif'],
    mimeTypes: ['image/png', 'image/jpeg', 'image/webp', 'image/gif'],
    uniformTypeIdentifiers: [
      'public.png',
      'public.jpeg',
      'org.webmproject.webp',
      'com.compuserve.gif',
    ],
  );
  final files = await openFiles(acceptedTypeGroups: [images]);
  if (files.isEmpty) return const [];
  if (files.length > remaining) {
    throw const AttachPickError('一条消息最多带 4 张图片。');
  }
  final picked = <OutgoingImage>[];
  var total = alreadyBytes;
  for (final file in files) {
    final extension = file.name.split('.').last.toLowerCase();
    final mime = _mimeByExtension[extension];
    if (mime == null) {
      throw const AttachPickError('只能发送 PNG、JPEG、WebP 或 GIF 图片。');
    }
    final size = await file.length();
    if (size > maxImageBytes) {
      throw const AttachPickError('单张图片请小于 4 MB。');
    }
    total += size;
    if (total > maxImagesTotalBytes) {
      throw const AttachPickError('这几张图片加起来太大了，请少选几张。');
    }
    picked.add((bytes: await file.readAsBytes(), mimeType: mime));
  }
  return picked;
}

/// Picked images waiting in the composer; each can be removed before sending.
class AttachmentStrip extends StatelessWidget {
  const AttachmentStrip({
    super.key,
    required this.images,
    required this.onRemove,
  });
  final List<OutgoingImage> images;
  final void Function(int index) onRemove;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.fromLTRB(4, 0, 4, 8),
    child: Wrap(
      spacing: 8,
      runSpacing: 8,
      children: [
        for (final (index, image) in images.indexed)
          Stack(
            key: ValueKey('attachment_$index'),
            clipBehavior: Clip.none,
            children: [
              ClipRRect(
                borderRadius: BorderRadius.circular(8),
                child: Image.memory(
                  image.bytes,
                  width: 56,
                  height: 56,
                  fit: BoxFit.cover,
                  gaplessPlayback: true,
                  errorBuilder: (_, _, _) => const _Broken(size: 56),
                ),
              ),
              Positioned(
                top: -6,
                right: -6,
                child: Material(
                  color: Warm.textSoft,
                  shape: const CircleBorder(),
                  child: InkWell(
                    key: ValueKey('remove_attachment_$index'),
                    customBorder: const CircleBorder(),
                    onTap: () => onRemove(index),
                    child: const Padding(
                      padding: EdgeInsets.all(3),
                      child: Icon(Icons.close, size: 13, color: Colors.white),
                    ),
                  ),
                ),
              ),
            ],
          ),
      ],
    ),
  );
}

/// Images inside a message bubble; tap one to see it larger.
class MessageImages extends StatelessWidget {
  /// Images fetched from Core, resolved as they arrive.
  const MessageImages({super.key, required List<Future<Uint8List>> images})
    : _remote = images,
      _local = null;

  /// Images still on this device, shown without waiting.
  const MessageImages.local({super.key, required List<Uint8List> images})
    : _local = images,
      _remote = null;

  final List<Future<Uint8List>>? _remote;
  final List<Uint8List>? _local;

  int get _count => _local?.length ?? _remote!.length;

  @override
  Widget build(BuildContext context) {
    final side = _count == 1 ? 220.0 : 120.0;
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Wrap(
        spacing: 6,
        runSpacing: 6,
        children: [
          for (var index = 0; index < _count; index++)
            if (_local case final local?)
              _tile(context, index, local[index], side)
            else
              FutureBuilder<Uint8List>(
                key: ValueKey('message_image_$index'),
                future: _remote![index],
                builder: (context, snapshot) {
                  if (snapshot.hasError) return _Broken(size: side);
                  final bytes = snapshot.data;
                  if (bytes == null) {
                    return SizedBox(
                      width: side,
                      height: side * 0.75,
                      child: const Center(
                        child: SizedBox.square(
                          dimension: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        ),
                      ),
                    );
                  }
                  return _tile(context, index, bytes, side);
                },
              ),
        ],
      ),
    );
  }

  Widget _tile(BuildContext context, int index, Uint8List bytes, double side) =>
      Semantics(
        key: ValueKey('message_image_tile_$index'),
        label: '图片 ${index + 1}',
        button: true,
        child: GestureDetector(
          onTap: () => showDialog<void>(
            context: context,
            builder: (context) => Dialog(
              clipBehavior: Clip.antiAlias,
              child: InteractiveViewer(
                child: Image.memory(bytes, fit: BoxFit.contain),
              ),
            ),
          ),
          child: ClipRRect(
            borderRadius: BorderRadius.circular(10),
            child: ConstrainedBox(
              constraints: BoxConstraints(
                maxWidth: side,
                maxHeight: side * 1.3,
              ),
              child: Image.memory(
                bytes,
                fit: BoxFit.cover,
                gaplessPlayback: true,
                errorBuilder: (_, _, _) => _Broken(size: side),
              ),
            ),
          ),
        ),
      );
}

class _Broken extends StatelessWidget {
  const _Broken({required this.size});
  final double size;

  @override
  Widget build(BuildContext context) => Container(
    width: size,
    height: size * 0.75,
    decoration: BoxDecoration(
      color: Warm.accentSoft,
      borderRadius: BorderRadius.circular(10),
    ),
    alignment: Alignment.center,
    child: const Text(
      '图片不可用',
      style: TextStyle(fontSize: 12, color: Warm.meta),
    ),
  );
}
