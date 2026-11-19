import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:wizlight_android/main.dart';
import 'package:wizlight_android/models/wiz_bulb.dart';
import 'package:wizlight_android/services/settings_service.dart';
import 'package:wizlight_android/services/sync_service.dart';
import 'package:wizlight_android/widgets/lamp_area_menu.dart';

const _channel = MethodChannel('com.wizlight.app/screen_capture');

Future<List<MethodCall>> _pumpHome(
  WidgetTester tester, {
  bool withLamp = true,
  bool regions = false,
  bool tv = false,
  Size size = const Size(390, 844),
  String? runningBackend,
  Object? Function(MethodCall)? onUpdateSettings,
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
  }
}
