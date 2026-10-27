import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:wizlight_android/main.dart';
import 'package:wizlight_android/models/wiz_bulb.dart';
import 'package:wizlight_android/services/settings_service.dart';

void main() {
  testWidgets('adding a third light preserves every saved light', (tester) async {
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
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(channel,
        (call) async => {'sessionId': '', 'backend': 'screen', 'state': 'stopped'});
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
}
