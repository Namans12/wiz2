import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:wizlight_android/main.dart';
import 'package:wizlight_android/models/wiz_bulb.dart';
import 'package:wizlight_android/services/settings_service.dart';

const _channel = MethodChannel('com.wizlight.app/screen_capture');

Future<List<MethodCall>> _pumpHome(
  WidgetTester tester, {
  Map<String, Object?> session = const {
    'sessionId': '',
    'backend': 'screen',
    'state': 'stopped'
  },
  String? pendingUri,
  Object? pickError,
  bool withBulb = true,
}) async {
  tester.view.physicalSize = const Size(1280, 1000);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  SharedPreferences.setMockInitialValues({});
  if (withBulb) {
    await SettingsService()
        .saveBulbs(const [WizBulb(ip: '192.168.1.2', name: 'One')]);
  }
  final calls = <MethodCall>[];
  var pending = pendingUri;
  tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(_channel,
      (call) async {
    calls.add(call);
    switch (call.method) {
      case 'querySession':
        return session;
      case 'takePendingMediaUri':
        final uri = pending;
        pending = null;
        return uri;
      case 'pickMedia':
        if (pickError != null) throw pickError;
        return null;
      case 'isTelevision':
        return false;
      default:
        return true;
    }
  });
  addTearDown(() => tester.binding.defaultBinaryMessenger
      .setMockMethodCallHandler(_channel, null));
  await tester.pumpWidget(const WizLightAndroidApp(isTelevision: false));
  await tester.pumpAndSettle();
  return calls;
}

String? _focusLabel() => FocusManager.instance.primaryFocus?.debugLabel;

void main() {
  testWidgets(
      'focus starts on Start/Stop; Up needs two presses to reach header',
      (tester) async {
    await _pumpHome(tester);
    expect(_focusLabel(), 'Sync action');

    await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
    await tester.pumpAndSettle();
    expect(_focusLabel(), isNot(startsWith('Header')));
    expect(_focusLabel(), isNot('Sync action'));

    await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
    await tester.pumpAndSettle();
    expect(_focusLabel(), 'Header: Discover');

    // Up at the very top stays in the header; Left/Right stay in the header.
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
    await tester.pumpAndSettle();
    expect(_focusLabel(), 'Header: Discover');
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowRight);
    await tester.pumpAndSettle();
    expect(_focusLabel(), 'Header: Add');
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowLeft);
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowLeft);
    await tester.pumpAndSettle();
    expect(_focusLabel(), 'Header: Discover');

    await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
    await tester.pumpAndSettle();
    expect(_focusLabel(), 'Sync action');
  });

  testWidgets('Down walks the body linearly and stops at the end',
      (tester) async {
    await _pumpHome(tester);
    final seen = <String?>[];
    for (var i = 0; i < 40; i++) {
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pumpAndSettle();
      seen.add(_focusLabel());
      expect(_focusLabel(), isNot(startsWith('Header')));
    }
    expect(seen.where((l) => l == 'Slider: Smoothing'), isNotEmpty);
    // Reached the last stop and stayed there rather than wrapping.
    expect(seen.last, seen[seen.length - 2]);
  });

  testWidgets('Back returns focus to Start/Stop before exiting',
      (tester) async {
    await _pumpHome(tester);
    for (var i = 0; i < 6; i++) {
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pumpAndSettle();
    }
    expect(_focusLabel(), isNot('Sync action'));
    await tester.binding.handlePopRoute();
    await tester.pumpAndSettle();
    expect(_focusLabel(), 'Sync action');
  });

  testWidgets('hdrFallback shows a one-time snackbar', (tester) async {
    await _pumpHome(tester, session: {
      'sessionId': '',
      'backend': 'screen',
      'state': 'running',
      'hdrFallback': true,
    });
    expect(
        find.text(
            "HDR capture isn't supported on this device - using standard capture."),
        findsOneWidget);
  });

  testWidgets('NO_PICKER shows the file browser guidance', (tester) async {
    await _pumpHome(tester,
        pickError: PlatformException(code: 'NO_PICKER', message: 'none'));
    await tester.tap(find.text('USB video'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Pick & Play USB Video'));
    await tester.pumpAndSettle();
    expect(find.textContaining("Open the video from your TV's file browser"),
        findsWidgets);
  });

  testWidgets('pending media URI starts playback on launch', (tester) async {
    final calls = await _pumpHome(tester, pendingUri: 'content://video/1');
    final play = calls.where((c) => c.method == 'playMedia').toList();
    expect(play, hasLength(1));
    expect((play.single.arguments as Map)['mediaUri'], 'content://video/1');
  });

  testWidgets('video opened with no bulbs is kept and plays after adding one',
      (tester) async {
    final calls = await _pumpHome(tester,
        pendingUri: 'content://media/clips%2Fmovie.mp4', withBulb: false);
    expect(calls.where((c) => c.method == 'playMedia'), isEmpty);
    expect(find.textContaining('Add at least one bulb first'), findsWidgets);
    expect(find.text('Play movie.mp4'), findsWidgets);

    await tester.tap(find.byIcon(Icons.add));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), '192.168.1.9');
    await tester.tap(find.text('Add').last);
    await tester.pumpAndSettle();

    final play = calls.where((c) => c.method == 'playMedia').toList();
    expect(play, hasLength(1));
    expect((play.single.arguments as Map)['mediaUri'],
        'content://media/clips%2Fmovie.mp4');
    await tester.pumpAndSettle();
    expect(find.text('Play movie.mp4'), findsNothing);
  });
}
