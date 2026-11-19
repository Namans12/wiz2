import 'package:flutter/material.dart';

import 'screens/home_screen.dart';
import 'services/screen_capture_service.dart';

void main() {
  runApp(const WizLightAndroidApp());
}

/// Builds the app theme. Focus must be obvious from across a room, so every
/// button family draws a thick outline when focused.
ThemeData buildWizTheme({bool tv = false}) {
  final scheme = ColorScheme.fromSeed(
    seedColor: const Color(0xFF80B7FF),
    brightness: Brightness.dark,
    surface: const Color(0xFF172234),
  );
  const ringWidth = 3.0;
  WidgetStateProperty<BorderSide?> ring(Color color) =>
      WidgetStateProperty.resolveWith((states) =>
          states.contains(WidgetState.focused)
              ? BorderSide(color: color, width: ringWidth)
              : null);
  WidgetStateProperty<Color?> overlay() => WidgetStateProperty.resolveWith(
      (states) => states.contains(WidgetState.focused)
          ? scheme.primary.withValues(alpha: 0.28)
          : null);
  final density = tv ? const VisualDensity(horizontal: 1, vertical: 1) : null;
  return ThemeData(
    brightness: Brightness.dark,
    scaffoldBackgroundColor: const Color(0xFF0D1422),
    colorScheme: scheme,
    focusColor: scheme.primary.withValues(alpha: 0.28),
    appBarTheme: const AppBarTheme(backgroundColor: Color(0xFF0D1422)),
    filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
      minimumSize: const Size(64, 56),
      textStyle: const TextStyle(
          fontSize: 16, fontWeight: FontWeight.w600, fontFamily: 'Roboto'),
    ).copyWith(
      // The fill is already the primary colour, so the ring is white there.
      side: ring(Colors.white),
    )),
    outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
      minimumSize: const Size(64, 52),
    ).copyWith(side: ring(scheme.primary), overlayColor: overlay())),
    textButtonTheme: TextButtonThemeData(
        style:
            ButtonStyle(side: ring(scheme.primary), overlayColor: overlay())),
    iconButtonTheme: IconButtonThemeData(
        style:
            ButtonStyle(side: ring(scheme.primary), overlayColor: overlay())),
    segmentedButtonTheme: SegmentedButtonThemeData(
        style: ButtonStyle(
            side: WidgetStateProperty.resolveWith((states) {
              if (states.contains(WidgetState.focused)) {
                return BorderSide(color: scheme.primary, width: ringWidth);
              }
              return BorderSide(color: scheme.outline);
            }),
            overlayColor: overlay())),
    visualDensity: density ?? VisualDensity.standard,
    useMaterial3: true,
  );
}

class WizLightAndroidApp extends StatefulWidget {
  const WizLightAndroidApp({super.key, this.isTelevision});

  /// Tests can force the TV decision; null asks the native side.
  final bool? isTelevision;

  @override
  State<WizLightAndroidApp> createState() => _WizLightAndroidAppState();
}

class _WizLightAndroidAppState extends State<WizLightAndroidApp> {
  late bool _tv = widget.isTelevision ?? false;

  @override
  void initState() {
    super.initState();
    if (widget.isTelevision == null) {
      ScreenCaptureService.isTelevision().then((tv) {
        if (mounted && tv != _tv) setState(() => _tv = tv);
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'WizLight Sync',
      debugShowCheckedModeBanner: false,
      theme: buildWizTheme(tv: _tv),
      builder: (context, child) {
        if (!_tv) return child!;
        final media = MediaQuery.of(context);
        return MediaQuery(
          data: media.copyWith(
              textScaler: TextScaler.linear(media.textScaler.scale(1) * 1.1)),
          child: child!,
        );
      },
      home: const HomeScreen(),
    );
  }
}
