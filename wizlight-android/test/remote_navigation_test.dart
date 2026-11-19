import 'dart:io';
import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:wizlight_android/main.dart';
import 'package:wizlight_android/widgets/remote_slider.dart';

void main() {
  testWidgets('remote arrows leave slider vertically and adjust horizontally',
      (tester) async {
    final before = FocusNode();
    final after = FocusNode();
    addTearDown(before.dispose);
    addTearDown(after.dispose);
    var value = 5.0;
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(
            body: StatefulBuilder(
      builder: (context, setState) => FocusTraversalGroup(
          child: Column(children: [
        TextButton(
            focusNode: before, onPressed: () {}, child: const Text('Before')),
        RemoteSlider(
            label: 'Level',
            value: value,
            min: 0,
            max: 10,
            divisions: 10,
            onChanged: (next) => setState(() => value = next)),
        TextButton(
            focusNode: after, onPressed: () {}, child: const Text('After')),
      ])),
    ))));
    before.requestFocus();
    await tester.pump();
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.pumpAndSettle();
    expect(FocusManager.instance.primaryFocus?.debugLabel, 'Slider: Level');
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowRight);
    await tester.pump();
    expect(value, 6);
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
    await tester.pumpAndSettle();
    expect(before.hasFocus, isTrue);
    expect(value, 6);
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.pump();
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
    await tester.pumpAndSettle();
    expect(after.hasFocus, isTrue);
    expect(value, 6);
    await tester.pumpWidget(const SizedBox());
  });

  for (final initial in [0.0, 10.0]) {
    testWidgets('slider endpoint $initial does not trap focus or exceed range',
        (tester) async {
      final after = FocusNode();
      addTearDown(after.dispose);
      var value = initial;
      await tester.pumpWidget(MaterialApp(
          home: Scaffold(
              body: StatefulBuilder(
        builder: (context, setState) => Column(children: [
          RemoteSlider(
              label: 'Endpoint',
              value: value,
              min: 0,
              max: 10,
              divisions: 10,
              onChanged: (next) => setState(() => value = next)),
          TextButton(
              focusNode: after, onPressed: () {}, child: const Text('After')),
        ]),
      ))));
      await tester.tap(find.text('Endpoint'));
      await tester.pumpAndSettle();
      final arrow = initial == 0
          ? LogicalKeyboardKey.arrowLeft
          : LogicalKeyboardKey.arrowRight;
      await tester.sendKeyDownEvent(arrow);
      await tester.pump();
      await tester.sendKeyRepeatEvent(arrow);
      await tester.pump();
      await tester.sendKeyUpEvent(arrow);
      expect(value, initial);
      expect(
          FocusManager.instance.primaryFocus?.debugLabel, 'Slider: Endpoint');
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pumpAndSettle();
      expect(after.hasFocus, isTrue);
      await tester.pumpWidget(const SizedBox());
    });
  }

  for (final size in [const Size(1280, 720), const Size(390, 844)]) {
    testWidgets('complete home supports scrolling remote focus at $size',
        (tester) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      SharedPreferences.setMockInitialValues({});
      const channel = MethodChannel('com.wizlight.app/screen_capture');
      tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(channel,
          (call) async {
        if (call.method == 'querySession') {
          return {'sessionId': '', 'backend': 'screen', 'state': 'stopped'};
        }
        throw StateError('Unexpected device call: ${call.method}');
      });
      addTearDown(() => tester.binding.defaultBinaryMessenger
          .setMockMethodCallHandler(channel, null));
      final fontDirectory = Platform.environment['WIZLIGHT_UI_FONT_DIR'];
      if (fontDirectory != null) {
        await tester.runAsync(() async {
          for (final entry in {
            'Roboto': 'roboto-regular.ttf',
            'MaterialIcons': 'materialicons-regular.otf'
          }.entries) {
            final loader = FontLoader(entry.key);
            loader.addFont(File('$fontDirectory/${entry.value}')
                .readAsBytes()
                .then(ByteData.sublistView));
            await loader.load();
          }
        });
      }
      const boundary = ValueKey('preview');
      await tester.pumpWidget(
          const RepaintBoundary(key: boundary, child: WizLightAndroidApp()));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      Future<void> preview(String name) async {
        final directory = Platform.environment['WIZLIGHT_UI_PREVIEW'];
        if (directory == null || size.width < 900) return;
        final render =
            tester.renderObject<RenderRepaintBoundary>(find.byKey(boundary));
        await tester.runAsync(() async {
          final image = await render.toImage();
          final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
          image.dispose();
          await File('$directory/$name.png')
              .writeAsBytes(bytes!.buffer.asUint8List());
        });
      }

      await preview('tv-home');
      final focus = tester
          .widgetList<Focus>(find.descendant(
            of: find.byType(RemoteSlider).first,
            matching: find.byType(Focus),
          ))
          .firstWhere(
              (widget) => widget.focusNode?.debugLabel == 'Slider: Max FPS')
          .focusNode!;
      focus.requestFocus();
      await tester.pumpAndSettle();
      for (final next in ['Smoothing', 'Color Boost']) {
        await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
        await tester.pumpAndSettle();
        expect(FocusManager.instance.primaryFocus?.debugLabel, 'Slider: $next');
        expect(tester.takeException(), isNull);
      }
      await preview('tv-slider-focus');
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pumpAndSettle();
      expect(FocusManager.instance.primaryFocus?.debugLabel,
          isNot('Slider: Color Boost'));
      expect(tester.takeException(), isNull);
      await tester.ensureVisible(find.text('Back to sync'));
      await tester.tap(find.text('Back to sync'));
      await tester.pumpAndSettle();
      expect(FocusManager.instance.primaryFocus?.debugLabel, 'Sync action');
      expect(find.text('Start Screen Sync').hitTestable(), findsOneWidget);
      await tester.pumpWidget(const SizedBox());
    });
  }
}
