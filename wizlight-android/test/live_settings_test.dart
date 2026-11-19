import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:wizlight_android/services/screen_capture_service.dart';
import 'package:wizlight_android/services/sync_service.dart';

void main() {
  hdrReasonTests();
  TestWidgetsFlutterBinding.ensureInitialized();
  const channel = MethodChannel('com.wizlight.app/screen_capture');
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;

  tearDown(() => messenger.setMockMethodCallHandler(channel, null));

  test('toLiveArgs carries every setting and no lamp list', () {
    final args = const SyncSettings(mode: 'zones').toLiveArgs();
    expect(args.containsKey('bulbsJson'), isFalse);
    expect(args.keys.toSet(), const SyncSettings().toMethodArgs(const []).keys.toSet()..remove('bulbsJson'));
    expect(args['mode'], 'zones');
  });

  test('updateSettings sends the settings and returns the restart-only keys',
      () async {
    MethodCall? seen;
    messenger.setMockMethodCallHandler(channel, (call) async {
      seen = call;
      return <String, Object?>{
        'accepted': true,
        'applied': <String>['smoothing'],
        'restartRequired': <String>['hdrCapture'],
      };
    });
    final service = ScreenCaptureService();
    addTearDown(service.dispose);
    final restart = await service.updateSettings(
        const SyncSettings(smoothing: 0.6, hdrCapture: true));
    expect(seen!.method, 'updateSettings');
    final sent = seen!.arguments as Map;
    expect(sent['smoothing'], 0.6);
    expect(sent['hdrCapture'], true);
    expect(sent.containsKey('sessionId'), isTrue);
    expect(sent.containsKey('bulbsJson'), isFalse);
    expect(restart, {'hdrCapture'});
  });

  test('everything applied live returns an empty set', () async {
    messenger.setMockMethodCallHandler(
        channel,
        (call) async => <String, Object?>{
              'accepted': true,
              'applied': <String>['mode'],
              'restartRequired': <String>[],
            });
    final service = ScreenCaptureService();
    addTearDown(service.dispose);
    expect(await service.updateSettings(const SyncSettings(mode: 'zones')),
        isEmpty);
  });

  test('no running session returns null', () async {
    messenger.setMockMethodCallHandler(
        channel, (call) async => <String, Object?>{'accepted': false});
    final service = ScreenCaptureService();
    addTearDown(service.dispose);
    expect(await service.updateSettings(const SyncSettings()), isNull);
    expect(ScreenCaptureService.parseSettingsReply(null), isNull);
  });
}

void hdrReasonTests() {
  test('hdrFallbackReason decodes, and absent or empty means null', () {
    expect(SyncRuntimeStats.fromMap({}).hdrFallbackReason, isNull);
    expect(SyncRuntimeStats.fromMap({'hdrFallbackReason': ''}).hdrFallbackReason,
        isNull);
    expect(SyncRuntimeStats.fromMap({'hdrFallbackReason': 7}).hdrFallbackReason,
        isNull);
    final stats = SyncRuntimeStats.fromMap({
      'hdrFallback': true,
      'hdrFallbackReason': 'needs Android 14',
    });
    expect(stats.hdrFallback, isTrue);
    expect(stats.hdrFallbackReason, 'needs Android 14');
  });
}
