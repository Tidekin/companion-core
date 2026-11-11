import 'dart:ui' show ImageFilter;

import 'package:flutter/material.dart';

/// Warm-light visual tokens. Decoration only: no colour or light here signals
/// mood, presence or read state.
abstract final class Warm {
  static const text = Color(0xFF343238);
  static const textSoft = Color(0xFF5F5146);
  static const brand = Color(0xFF685649);
  static const muted = Color(0xFF94877B);
  static const meta = Color(0xFF8C8179);
  static const accent = Color(0xFF477B7E);
  static const accentSoft = Color(0x25477B7E);
  static const line = Color(0x1A806753);
  static const backgroundTop = Color(0xFFF3E9DF);
  static const backgroundBottom = Color(0xFFE6D8CA);
  static const glass = Color(0x8AFFFAF5);
  static const glassBorder = Color(0xCCFFFFFF);
  static const panel = Color(0x55FFFFFF);
  static const panelHover = Color(0x66FFFFFF);
  static const conversationActive = Color(0xADD5E5DF);
  static const bubble = Color(0xE0FFFDFA);
  static const bubbleBorder = Color(0x99FFFFFF);
  static const bubbleOwner = Color(0xD9D7E7E0);
  static const bubbleOwnerBorder = Color(0x80D4E5DF);
  static const composer = Color(0x99FFFFFF);
  static const sheet = Color(0xFFF9F4EE);
  static const dialog = Color(0xFFFAF6F0);
  static const warning = Color(0xFF9A5B3F);
  static const avatars = [
    Color(0xFFBFD6D0),
    Color(0xFFE5CDB5),
    Color(0xFFD9C3C6),
    Color(0xFFC9D3BC),
    Color(0xFFCEC6DA),
  ];
}

/// Motion durations from the warm-light specification.
abstract final class Motion {
  static const enter = Cubic(0.16, 1, 0.3, 1);
  static const message = Duration(milliseconds: 180);
  static const panel = Duration(milliseconds: 240);
  static const panelClose = Duration(milliseconds: 180);
  static const press = Duration(milliseconds: 120);

  /// Follows the platform "reduce motion" setting.
  static bool reduced(BuildContext context) =>
      MediaQuery.maybeDisableAnimationsOf(context) ?? false;
}

ThemeData warmTheme() {
  final scheme = ColorScheme.fromSeed(
    seedColor: Warm.accent,
    primary: Warm.accent,
    surface: Warm.dialog,
  ).copyWith(onSurface: Warm.text);
  OutlineInputBorder border(Color color) => OutlineInputBorder(
    borderRadius: BorderRadius.circular(14),
    borderSide: BorderSide(color: color),
  );
  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: Warm.backgroundTop,
    fontFamilyFallback: const [
      'Microsoft YaHei',
      'Noto Sans CJK SC',
      'PingFang SC',
    ],
    textTheme: Typography.blackMountainView.apply(
      bodyColor: Warm.text,
      displayColor: Warm.text,
    ),
    dividerTheme: const DividerThemeData(color: Warm.line, space: 1),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: const Color(0xB3FFFFFF),
      border: border(const Color(0x33806753)),
      enabledBorder: border(const Color(0x33806753)),
      focusedBorder: border(Warm.accent),
      hintStyle: const TextStyle(color: Color(0xFFA39A91)),
    ),
    dialogTheme: DialogThemeData(
      backgroundColor: Warm.dialog,
      barrierColor: const Color(0x33473A30),
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(22)),
      titleTextStyle: const TextStyle(
        color: Warm.textSoft,
        fontSize: 19,
        fontWeight: FontWeight.w600,
      ),
    ),
    bottomSheetTheme: const BottomSheetThemeData(
      backgroundColor: Warm.sheet,
      surfaceTintColor: Colors.transparent,
      showDragHandle: true,
      dragHandleColor: Color(0x33806753),
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
      ),
    ),
    popupMenuTheme: PopupMenuThemeData(
      color: Warm.dialog,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
    ),
    snackBarTheme: SnackBarThemeData(
      behavior: SnackBarBehavior.floating,
      backgroundColor: Warm.textSoft,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: Warm.accent,
        foregroundColor: Colors.white,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(
        foregroundColor: Warm.accent,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
      ),
    ),
    badgeTheme: const BadgeThemeData(backgroundColor: Warm.accent),
    tooltipTheme: TooltipThemeData(
      decoration: BoxDecoration(
        color: Warm.textSoft,
        borderRadius: BorderRadius.circular(8),
      ),
    ),
  );
}

/// Static warm background. It never animates or reflects state.
class WarmBackground extends StatelessWidget {
  const WarmBackground({super.key, required this.child});
  final Widget child;
  @override
  Widget build(BuildContext context) => DecoratedBox(
    decoration: const BoxDecoration(
      color: Warm.backgroundTop,
      image: DecorationImage(
        image: AssetImage('assets/images/warm-room.jpg'),
        fit: BoxFit.cover,
      ),
    ),
    child: DecoratedBox(
      // A soft cream veil keeps text contrast on any part of the photo.
      decoration: const BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [Color(0x40FBF3EA), Color(0x66F3E7DB)],
        ),
      ),
      child: child,
    ),
  );
}

/// Translucent cream layer with a soft blur.
class Glass extends StatelessWidget {
  const Glass({
    super.key,
    required this.child,
    this.radius = 24,
    this.color = Warm.glass,
  });
  final Widget child;
  final double radius;
  final Color color;
  @override
  Widget build(BuildContext context) {
    final shape = BorderRadius.circular(radius);
    return DecoratedBox(
      decoration: BoxDecoration(
        borderRadius: shape,
        boxShadow: const [
          BoxShadow(
            color: Color(0x1B66503A),
            blurRadius: 60,
            offset: Offset(0, 18),
          ),
        ],
      ),
      child: ClipRRect(
        borderRadius: shape,
        child: BackdropFilter(
          filter: ImageFilter.blur(sigmaX: 18, sigmaY: 18),
          child: DecoratedBox(
            decoration: BoxDecoration(
              color: color,
              borderRadius: shape,
              border: Border.all(color: Warm.glassBorder),
            ),
            child: child,
          ),
        ),
      ),
    );
  }
}

/// A once-only entrance: fade plus a short upward move. Disabled entirely when
/// [animate] is false or the platform asks for reduced motion.
class Appear extends StatefulWidget {
  const Appear({
    super.key,
    required this.child,
    this.animate = true,
    this.duration = Motion.message,
    this.offset = 8,
  });
  final Widget child;
  final bool animate;
  final Duration duration;
  final double offset;
  @override
  State<Appear> createState() => _AppearState();
}

class _AppearState extends State<Appear> with SingleTickerProviderStateMixin {
  late final AnimationController controller = AnimationController(
    vsync: this,
    duration: widget.duration,
    value: widget.animate ? 0 : 1,
  );
  late final Animation<double> curve = CurvedAnimation(
    parent: controller,
    curve: Motion.enter,
  );

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (controller.value == 0 && !controller.isAnimating) {
      if (Motion.reduced(context)) {
        controller.value = 1;
      } else {
        controller.forward();
      }
    }
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
    animation: curve,
    child: widget.child,
    builder: (context, child) => Opacity(
      opacity: curve.value,
      child: Transform.translate(
        offset: Offset(0, widget.offset * (1 - curve.value)),
        child: child,
      ),
    ),
  );

  @override
  void dispose() {
    controller.dispose();
    super.dispose();
  }
}

/// Initial-letter avatar with a stable soft tint per Bot id. Not a presence
/// indicator; no ring or glow.
class InitialAvatar extends StatelessWidget {
  const InitialAvatar({
    super.key,
    required this.id,
    required this.name,
    this.size = 46,
  });
  final String id;
  final String name;
  final double size;
  @override
  Widget build(BuildContext context) {
    final tint =
        Warm.avatars[id.codeUnits.fold<int>(
              0,
              (a, b) => (a * 31 + b) & 0xffff,
            ) %
            Warm.avatars.length];
    final initial = name.trim().isEmpty ? '?' : name.trim().characters.first;
    return ExcludeSemantics(
      child: Container(
        width: size,
        height: size,
        alignment: Alignment.center,
        decoration: BoxDecoration(
          color: tint,
          shape: BoxShape.circle,
          border: Border.all(color: const Color(0xB3FFFFFF), width: 1.5),
        ),
        child: Text(
          initial,
          style: TextStyle(
            color: Warm.textSoft,
            fontSize: size * 0.4,
            fontWeight: FontWeight.w600,
          ),
        ),
      ),
    );
  }
}

/// Opens a bottom sheet with the warm-light timing (240 ms in, 180 ms out).
Future<T?> showWarmSheet<T>(BuildContext context, WidgetBuilder builder) =>
    showModalBottomSheet<T>(
      context: context,
      useSafeArea: true,
      barrierColor: const Color(0x33473A30),
      sheetAnimationStyle: Motion.reduced(context)
          ? AnimationStyle.noAnimation
          : const AnimationStyle(
              duration: Motion.panel,
              reverseDuration: Motion.panelClose,
              curve: Motion.enter,
            ),
      builder: builder,
    );
