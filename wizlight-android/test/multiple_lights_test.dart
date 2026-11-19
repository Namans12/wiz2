import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:wizlight_android/main.dart';
import 'package:wizlight_android/models/wiz_bulb.dart';
import 'package:wizlight_android/screens/home_screen.dart';
import 'package:wizlight_android/services/wiz_bulb_service.dart';
import 'package:wizlight_android/services/settings_service.dart';

void main() {
  testWidgets('adding a third light preserves every saved light',
      (tester) async {
    tester.view.physicalSize = const Size(1280, 720);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    SharedPreferences.setMockInitialValues({});
    final settings = SettingsService();
    await settings.saveBulbs(const [
      WizBulb(ip: '192.168.1.2', name: 'One'),
      WizBulb(ip: '192.168.1.3', name: 'Two'),
    ]);
    const channel = MethodChannel('com.wizlight.app/screen_capture');
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
        channel,
        (call) async =>
            {'sessionId': '', 'backend': 'screen', 'state': 'stopped'});
    addTearDown(() => tester.binding.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, null));
    await tester.pumpWidget(const WizLightAndroidApp());
    await tester.pumpAndSettle();
    await tester.tap(find.byIcon(Icons.add));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), '192.168.1.4');
    await tester.tap(find.text('Add').last);
    await tester.pumpAndSettle();
    expect((await settings.loadBulbs()).map((b) => b.ip),
        ['192.168.1.2', '192.168.1.3', '192.168.1.4']);
  });

  testWidgets('screen sync changes selected lights live and stops when empty',
      (tester) async {
    tester.view.physicalSize = const Size(1280, 1100);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    SharedPreferences.setMockInitialValues({});
    final settings = SettingsService();
    await settings.saveBulbs(const [
      WizBulb(ip: '192.168.1.2', name: 'One'),
      WizBulb(ip: '192.168.1.3', name: 'Two', screenSyncEnabled: false),
    ]);
    var state = 'stopped';
    var session = '';
    final calls = <MethodCall>[];
    const channel = MethodChannel('com.wizlight.app/screen_capture');
    final messenger = tester.binding.defaultBinaryMessenger;
    messenger.setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      switch (call.method) {
        case 'querySession':
          return {'sessionId': session, 'backend': 'screen', 'state': state};
        case 'startCapture':
          state = 'running';
          session = (call.arguments as Map)['sessionId'] as String;
        case 'stopCapture':
          state = 'stopped';
      }
      return true;
    });
    addTearDown(() => messenger.setMockMethodCallHandler(channel, null));
    await tester.pumpWidget(
        MaterialApp(home: HomeScreen(bulbService: _RecordingBulbService())));
    await tester.pumpAndSettle();
    final checkbox = find.byType(CheckboxListTile);
    await tester.tap(find.text('Start Screen Sync'));
    await tester.pumpAndSettle();
    List<String> ipsFor(String method) {
      final call = calls.lastWhere((call) => call.method == method);
      return WizBulb.decodeList((call.arguments as Map)['bulbsJson'] as String)
          .map((bulb) => bulb.ip)
          .toList();
    }

    expect(ipsFor('startCapture'), ['192.168.1.2']);
    await tester.ensureVisible(checkbox.last);
    await tester.tap(checkbox.last);
    await tester.pumpAndSettle();
    expect(ipsFor('updateTargets'), ['192.168.1.2', '192.168.1.3']);
    await tester.ensureVisible(checkbox.first);
    await tester.tap(checkbox.first);
    await tester.pumpAndSettle();
    expect(ipsFor('updateTargets'), ['192.168.1.3']);
    await tester.ensureVisible(checkbox.last);
    await tester.tap(checkbox.last);
    await tester.pumpAndSettle();
    expect(state, 'stopped');
    expect(WizBulb.syncTargets(await settings.loadBulbs()), isEmpty);
    final start = find.text('Start Screen Sync');
    await tester.ensureVisible(start);
    await tester.tap(start);
    await tester.pumpAndSettle();
    expect(find.textContaining('Include at least one bulb'), findsOneWidget);
    expect(calls.where((call) => call.method == 'requestPermission'),
        hasLength(1));
  });

  testWidgets('unticking the only selected light stops sync and turns it off once',
      (tester) async {
    tester.view.physicalSize = const Size(1280, 1100);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    SharedPreferences.setMockInitialValues({});
    await SettingsService().saveBulbs(const [
      WizBulb(ip: '192.168.1.2', name: 'One'),
    ]);
    var state = 'stopped';
    var session = '';
    const channel = MethodChannel('com.wizlight.app/screen_capture');
    final messenger = tester.binding.defaultBinaryMessenger;
    messenger.setMockMethodCallHandler(channel, (call) async {
      switch (call.method) {
        case 'querySession':
          return {'sessionId': session, 'backend': 'screen', 'state': state};
        case 'startCapture':
          state = 'running';
          session = (call.arguments as Map)['sessionId'] as String;
        case 'stopCapture':
          state = 'stopped';
      }
      return true;
    });
    addTearDown(() => messenger.setMockMethodCallHandler(channel, null));
    final service = _RecordingBulbService();
    await tester.pumpWidget(MaterialApp(home: HomeScreen(bulbService: service)));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Start Screen Sync'));
    await tester.pumpAndSettle();
    expect(state, 'running');
    final checkbox = find.byType(CheckboxListTile);
    await tester.ensureVisible(checkbox.first);
    await tester.tap(checkbox.first);
    await tester.pumpAndSettle();
    expect(state, 'stopped');
    expect(service.off, ['192.168.1.2']);
  });
}

class _RecordingBulbService extends WizBulbService {
  final off = <String>[];

  @override
  Future<bool> turnOff(String ip) async {
    off.add(ip);
    return true;
  }
}
