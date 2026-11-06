import 'dart:convert';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:wizlight_android/models/wiz_bulb.dart';
import 'package:wizlight_android/services/settings_service.dart';
import 'package:wizlight_android/services/sync_service.dart';
import 'package:wizlight_android/services/screen_capture_service.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() => SharedPreferences.setMockInitialValues({}));

  test('rediscovery preserves user name and layout across DHCP changes', () {
    final merged = WizBulb.merge(
      const [
        WizBulb(ip: '192.168.1.2', name: 'Desk', mac: 'AA:BB', region: 'right')
      ],
      const [WizBulb(ip: '192.168.1.9', name: 'Bulb 1', mac: 'aa:bb')],
    );
    expect(merged.single.ip, '192.168.1.9');
    expect(merged.single.name, 'Desk');
    expect(merged.single.region, 'right');
  });
  test('invalid regions and numeric settings cannot crash saved-data loading',
      () {
    expect(
        WizBulb.fromJson(const {'ip': '192.168.1.2', 'region': 'invalid'})
            .region,
        'left');
    final settings = SyncSettings.fromJson(
        {'maxFps': 'bad', 'minFps': double.nan, 'smoothing': {}});
    expect(settings.maxFps, 24);
    expect(settings.minFps, 10);
    expect(settings.smoothing, .2);
    expect(WizBulb.isValidIp('300.1.2.3'), false);
  });
  test('legacy settings migrate without removing original keys', () async {
    final legacyBulb =
        jsonEncode({'ip': '192.168.1.2', 'name': 'Desk', 'region': 'right'});
    SharedPreferences.setMockInitialValues({
      'wizBulbs': [legacyBulb],
      'maxFps': 28,
      'brightnessFollow': false
    });
    final service = SettingsService();
    expect((await service.loadBulbs()).single.name, 'Desk');
    expect((await service.loadSyncSettings()).maxFps, 28);
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getStringList('wizBulbs'), [legacyBulb]);
    expect(
        jsonDecode(prefs.getString('wizlight.syncSettings')!)['schemaVersion'],
        1);
  });
  test('corrupt preferences survive a later valid save', () async {
    SharedPreferences.setMockInitialValues(
        {'wizlight.syncSettings': '{broken'});
    final service = SettingsService();
    await service.loadSyncSettings();
    expect(service.warning, isNotNull);
    await service.saveSyncSettings(const SyncSettings());
    expect(
        (await SharedPreferences.getInstance())
            .getString('wizlight.syncSettings.corrupt'),
        '{broken');
  });
  test('channel carries one session id through start and stop', () async {
    const channel = MethodChannel('com.wizlight.app/screen_capture');
    final calls = <MethodCall>[];
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      if (call.method == 'querySession') {
        return {'state': 'stopped', 'sessionId': '', 'backend': 'screen'};
      }
      return true;
    });
    final service = ScreenCaptureService();
    try {
      await service
          .startCapture(bulbs: const [], settings: const SyncSettings());
      await service.stopCapture();
      final start = calls
          .firstWhere((call) => call.method == 'startCapture')
          .arguments as Map;
      final stop = calls
          .firstWhere((call) => call.method == 'stopCapture')
          .arguments as Map;
      expect(start['sessionId'], isNotEmpty);
      expect(stop['sessionId'], start['sessionId']);
    } finally {
      service.dispose();
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
          .setMockMethodCallHandler(channel, null);
    }
  });
}
