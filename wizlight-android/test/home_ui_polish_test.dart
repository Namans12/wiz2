import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:wizlight_android/main.dart';
import 'package:wizlight_android/models/wiz_bulb.dart';
import 'package:wizlight_android/services/settings_service.dart';
import 'package:wizlight_android/services/sync_service.dart';
import 'package:wizlight_android/widgets/lamp_area_menu.dart';
import 'package:wizlight_android/widgets/remote_slider.dart';

const _channel = MethodChannel('com.wizlight.app/screen_capture');

Future<List<MethodCall>> _pumpHome(
  WidgetTester tester, {
  bool withLamp = true,
  bool regions = false,
  bool tv = false,
  Size size = const Size(390, 844),
  String? runningBackend,
  FutureOr<Object?> Function(MethodCall)? onUpdateSettings,
  FutureOr<Object?> Function(MethodCall)? nativeHandler,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  SharedPreferences.setMockInitialValues({});
  final settings = SettingsService();
  if (withLamp) {
    await settings
        .saveBulbs(const [WizBulb(ip: '192.168.1.2', name: 'Left lamp')]);
  }
  if (regions) {
    await settings.saveSyncSettings(const SyncSettings(mode: 'zones'));
  }
  final calls = <MethodCall>[];
  tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(_channel,
      (call) async {
    calls.add(call);
    if (nativeHandler != null) return nativeHandler(call);
    if (call.method == 'querySession') {
      return {
        'sessionId': runningBackend == null ? '' : 'sync-session',
        'backend': runningBackend ?? 'screen',
        'state': runningBackend == null ? 'stopped' : 'running',
      };
    }
    if (call.method == 'updateSettings') {
      return onUpdateSettings?.call(call);
    }
    return null;
  });
  addTearDown(() => tester.binding.defaultBinaryMessenger
      .setMockMethodCallHandler(_channel, null));
  await tester.pumpWidget(WizLightAndroidApp(isTelevision: tv));
  await tester.pumpAndSettle();
  return calls;
}

Finder _slider(String label) => find.descendant(
      of: find.widgetWithText(RemoteSlider, label),
      matching: find.byType(Slider),
    );

void main() {
  testWidgets('home paints a full-screen gradient and drops the old hints',
      (tester) async {
    await _pumpHome(tester);
    final backdrop = find.byKey(const ValueKey('wizlight-background'));
    final decoration =
        tester.widget<DecoratedBox>(backdrop).decoration as BoxDecoration;
    expect(decoration.gradient, isA<LinearGradient>());
    expect(tester.getSize(backdrop), const Size(390, 844));
    expect(find.text('WizLight'), findsOneWidget);
    expect(find.textContaining('Your atmosphere'), findsNothing);
    expect(find.textContaining('Remote:'), findsNothing);
    expect(find.textContaining('Up / Down'), findsNothing);
  });

  testWidgets('mode buttons keep their geometry and offer an explanation',
      (tester) async {
    await _pumpHome(tester, size: const Size(320, 740));
    expect(find.text('Used in Regions mode'), findsOneWidget);
    final area = find.descendant(
        of: find.byType(LampAreaMenu), matching: find.byType(OutlinedButton));
    expect(tester.widget<OutlinedButton>(area).onPressed, isNull);
    final single = find.text('Single');
    final regions = find.text('Regions');
    await tester.ensureVisible(regions);
    await tester.pumpAndSettle();
    final mode = find.ancestor(
        of: regions, matching: find.byType(SegmentedButton<String>));
    final before = [
      for (final part in [mode, single, regions]) tester.getRect(part)
    ];
    await tester.tap(regions);
    await tester.pumpAndSettle();
    expect([
      for (final part in [mode, single, regions]) tester.getRect(part)
    ], before);
    expect(tester.widget<OutlinedButton>(area).onPressed, isNotNull);
    expect(find.text('Used in Regions mode'), findsNothing);

    await tester.tap(find.byTooltip('About Single and Regions'));
    await tester.pumpAndSettle();
    expect(find.text('Single and Regions'), findsOneWidget);
    expect(find.textContaining('Lamp areas are ignored'), findsOneWidget);
    expect(
        find.textContaining('different sides of the screen'), findsOneWidget);
  });

  for (final usb in [false, true]) {
    testWidgets(
        '${usb ? 'USB playback' : 'screen sync'} guides users with no lamps',
        (tester) async {
      final calls = await _pumpHome(tester, withLamp: false);
      expect(find.text('No lamps added'), findsOneWidget);
      if (usb) {
        await tester.tap(find.text('USB video'));
        await tester.pumpAndSettle();
      }
      await tester
          .tap(find.text(usb ? 'Pick & Play USB Video' : 'Start Screen Sync'));
      await tester.pumpAndSettle();
      final notice = find.byType(AlertDialog);
      expect(find.text('Add your lamps first'), findsOneWidget);
      expect(find.textContaining('Wi-Fi network'), findsOneWidget);
      expect(find.descendant(of: notice, matching: find.text('Discover')),
          findsOneWidget);
      expect(calls.map((call) => call.method),
          isNot(contains(usb ? 'pickMedia' : 'requestPermission')));
      await tester.tap(find.text('Add by IP'));
      await tester.pumpAndSettle();
      expect(find.byType(TextField), findsOneWidget);
    });
  }

  testWidgets('area menu opens below its field and supports remote selection',
      (tester) async {
    await _pumpHome(tester,
        regions: true, tv: true, size: const Size(400, 520));
    final field = find.descendant(
        of: find.byType(LampAreaMenu), matching: find.byType(OutlinedButton));
    await tester.ensureVisible(field);
    await tester.pumpAndSettle();
    final focus = tester.widget<OutlinedButton>(field).focusNode!;
    focus.requestFocus();
    await tester.pumpAndSettle();
    await tester.sendKeyEvent(LogicalKeyboardKey.enter);
    await tester.pumpAndSettle();
    final items = find.descendant(
      of: find.byKey(const ValueKey('lamp-area-options')),
      matching: find.byType(TextButton),
    );
    expect(items, findsNWidgets(5));
    expect(tester.getTopLeft(items.first).dy,
        greaterThan(tester.getBottomLeft(field).dy));
    expect(tester.getSize(items.first).height, greaterThanOrEqualTo(48));
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
    await tester.sendKeyEvent(LogicalKeyboardKey.select);
    await tester.pumpAndSettle();
    expect(items, findsNothing);
    expect((await SettingsService().loadBulbs()).single.region, 'right');
    expect(focus.hasFocus, isTrue);

    await tester.sendKeyEvent(LogicalKeyboardKey.select);
    await tester.pumpAndSettle();
    expect(items, findsNWidgets(5));
    await tester.binding.handlePopRoute();
    await tester.pumpAndSettle();
    expect(items, findsNothing);
    expect(focus.hasFocus, isTrue);
  });

  testWidgets('rotating the viewport dismisses the lamp area menu',
      (tester) async {
    await _pumpHome(tester, regions: true, size: const Size(600, 900));
    final field = find.descendant(
        of: find.byType(LampAreaMenu), matching: find.byType(OutlinedButton));
    final menu = find.byKey(const ValueKey('lamp-area-options'));
    await tester.ensureVisible(field);
    await tester.tap(field);
    await tester.pumpAndSettle();
    expect(menu, findsOneWidget);

    tester.view.physicalSize = const Size(900, 600);
    await tester.pumpAndSettle();
    expect(menu, findsNothing);

    await tester.ensureVisible(field);
    await tester.tap(field);
    await tester.pumpAndSettle();
    expect(menu, findsOneWidget);
    expect(tester.getTopLeft(menu).dy,
        greaterThan(tester.getBottomLeft(field).dy));
  });

  testWidgets('opening diagnostics brings the runtime into view',
      (tester) async {
    await _pumpHome(tester);
    final tile = find.text('Live diagnostics');
    await tester.ensureVisible(tile);
    await tester.pumpAndSettle();
    final position = Scrollable.of(tester.element(tile)).position;
    final closedOffset = position.pixels;
    await tester.tap(tile);
    await tester.pumpAndSettle();
    expect(position.pixels, greaterThan(closedOffset));
    expect(find.text('Runtime').hitTestable(), findsOneWidget);
    expect(find.textContaining('HDR fallback'), findsNothing);

    await tester.binding.defaultBinaryMessenger.handlePlatformMessage(
      _channel.name,
      const StandardMethodCodec().encodeMethodCall(const MethodCall(
        'onSyncUpdate',
        {'hdrFallbackReason': 'No usable HDR frames'},
      )),
      (_) {},
    );
    await tester.pump();
    expect(find.text('HDR fallback: No usable HDR frames'), findsOneWidget);
  });

  for (final backend in ['screen', 'usb']) {
    testWidgets(
        '$backend applies changes live and prompts only for a restart key',
        (tester) async {
      final calls = await _pumpHome(tester, runningBackend: backend,
          onUpdateSettings: (call) {
        return {
          'accepted': true,
          'restartRequired': [
            if (call.arguments['hdrCapture'] == true) 'hdrCapture',
          ],
        };
      });

      final regions = find.text('Regions');
      await tester.ensureVisible(regions);
      await tester.tap(regions);
      await tester.pumpAndSettle();
      final updates = calls.where((call) => call.method == 'updateSettings');
      expect(updates.single.arguments, containsPair('mode', 'zones'));
      expect(
          updates.single.arguments, containsPair('sessionId', 'sync-session'));
      expect(find.text('Restart needed'), findsNothing);
      expect((await SettingsService().loadSyncSettings()).mode, 'zones');

      final hdrCapture = find.text('HDR-aware capture (Android 14+)');
      await tester.ensureVisible(hdrCapture);
      await tester.tap(hdrCapture);
      await tester.pumpAndSettle();
      expect(updates.last.arguments, containsPair('hdrCapture', true));
      expect(find.text('Restart needed'), findsOneWidget);

      await tester.ensureVisible(hdrCapture);
      await tester.tap(hdrCapture);
      await tester.pumpAndSettle();
      expect(updates.last.arguments, containsPair('hdrCapture', false));
      expect(updates, hasLength(3));
      expect(find.text('Restart needed'), findsNothing);
      expect(calls.map((call) => call.method),
          isNot(anyOf(contains('stopCapture'), contains('stopMedia'))));
    });

    testWidgets('$backend applies the newest edits made while starting',
        (tester) async {
      var session = <String, Object?>{
        'sessionId': '',
        'backend': backend,
        'state': 'stopped',
      };
      final startMethod = backend == 'screen' ? 'startCapture' : 'playMedia';
      final calls = await _pumpHome(tester, nativeHandler: (call) {
        if (call.method == startMethod) {
          session = {
            ...session,
            'sessionId': call.arguments['sessionId'],
            'state': 'starting',
          };
          return true;
        }
        return switch (call.method) {
          'querySession' => session,
          'requestPermission' => true,
          'pickMedia' => 'content://test/video.mp4',
          'updateSettings' => {
              'accepted': true,
              'restartRequired': ['hdrCapture'],
            },
          _ => null,
        };
      });

      if (backend == 'usb') {
        await tester.tap(find.text('USB video'));
        await tester.pumpAndSettle();
      }
      await tester.tap(find.text(
          backend == 'screen' ? 'Start Screen Sync' : 'Pick & Play USB Video'));
      await tester.pumpAndSettle();
      final start = calls.singleWhere((call) => call.method == startMethod);
      expect(start.arguments, containsPair('mode', 'single'));
      expect(start.arguments, containsPair('hdrCapture', false));
      expect(find.text('Sync starting'), findsOneWidget);

      for (final label in ['Regions', 'HDR-aware capture (Android 14+)']) {
        final control = find.text(label);
        await tester.ensureVisible(control);
        await tester.tap(control);
        await tester.pumpAndSettle();
      }
      final updates = calls.where((call) => call.method == 'updateSettings');
      expect(updates, isEmpty);

      session = {...session, 'state': 'running'};
      for (var i = 0; i < 2; i++) {
        await tester.binding.defaultBinaryMessenger.handlePlatformMessage(
          _channel.name,
          const StandardMethodCodec().encodeMethodCall(
            MethodCall('onSessionState', session),
          ),
          (_) {},
        );
        await tester.pumpAndSettle();
      }
      expect(updates.single.arguments, containsPair('mode', 'zones'));
      expect(updates.single.arguments, containsPair('hdrCapture', true));
      expect(updates.single.arguments,
          containsPair('sessionId', start.arguments['sessionId']));
      expect(find.text('Restart needed'), findsOneWidget);
    });
  }

  testWidgets(
      'slider drag keeps only the newest pending live value and saves on release',
      (tester) async {
    final firstReply = Completer<void>();
    var inFlight = 0;
    final values = <int>[];
    await _pumpHome(tester, runningBackend: 'screen',
        onUpdateSettings: (call) async {
      expect(++inFlight, 1);
      values.add(call.arguments['maxFps'] as int);
      if (values.length == 1) await firstReply.future;
      inFlight--;
      return {'accepted': true};
    });

    final slider = _slider('Max FPS');
    await tester.ensureVisible(slider);
    await tester.pumpAndSettle();
    final bounds = tester.getRect(slider);
    Offset onTrack(double fraction) =>
        Offset(bounds.left + bounds.width * fraction, bounds.center.dy);
    final drag = await tester.startGesture(onTrack(0.2));
    await tester.pump();
    for (final fraction in [0.4, 0.55, 0.7, 0.9]) {
      await drag.moveTo(onTrack(fraction));
      await tester.pump(const Duration(milliseconds: 20));
    }
    final latest = tester.widget<Slider>(slider).value.round();
    expect(values, hasLength(1));
    expect(latest, greaterThan(values.single));
    expect((await SettingsService().loadSyncSettings()).maxFps,
        const SyncSettings().maxFps);

    await drag.up();
    await tester.pump();
    expect((await SettingsService().loadSyncSettings()).maxFps, latest);
    expect(values, hasLength(1));

    firstReply.complete();
    await tester.pumpAndSettle();
    expect(values, hasLength(2));
    expect(values.last, latest);
    expect(inFlight, 0);
  });

  testWidgets(
      'slider prefs debounce edits and flush pending settings on dispose',
      (tester) async {
    await _pumpHome(tester);
    final slider = _slider('Max FPS');
    final settings = SettingsService();
    tester.widget<Slider>(slider).onChanged!(25);
    await tester.pump(const Duration(milliseconds: 200));
    tester.widget<Slider>(slider).onChanged!(26);
    await tester.pump(const Duration(milliseconds: 299));
    expect((await settings.loadSyncSettings()).maxFps,
        const SyncSettings().maxFps);
    await tester.pump(const Duration(milliseconds: 1));
    expect((await settings.loadSyncSettings()).maxFps, 26);

    tester.widget<Slider>(slider).onChanged!(27);
    await tester.pumpWidget(const SizedBox());
    expect((await settings.loadSyncSettings()).maxFps, 27);
  });
}
