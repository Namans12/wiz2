import 'package:flutter_test/flutter_test.dart';
import 'package:wizlight_android/models/wiz_bulb.dart';
import 'package:wizlight_android/services/sync_service.dart';

void main() {
  final bulbs = <WizBulb>[
    const WizBulb(ip: '10.0.0.1', name: 'A', region: 'left'),
    const WizBulb(ip: '10.0.0.2', name: 'B', region: 'right'),
  ];

  // Same keys the Kotlin side reads (ScreenCaptureService.EXTRA_*).
  const nativeKeys = <String>{
    'mode', 'minFps', 'maxFps', 'smoothing', 'sampleSize', 'colorBoost',
    'edgeWeight', 'colorAlgorithm', 'minBrightness', 'minColorDelta',
    'adaptiveFps', 'predictiveSmoothing', 'ignoreLetterbox',
    'sourceColorSpace', 'hdrToneMapping', 'hdrCapture', 'syncProfile',
    'brightnessFollow',
  };

  test('every setting is sent to the native side under the key it reads', () {
    final args = const SyncSettings().toMethodArgs(bulbs);
    expect(args.keys.toSet(), nativeKeys.union({'bulbsJson'}));
  });

  test('changing each setting changes the native arguments', () {
    final base = const SyncSettings().toMethodArgs(bulbs);
    final variants = <String, SyncSettings>{
      'mode': const SyncSettings().copyWith(mode: 'zones'),
      'minFps': const SyncSettings().copyWith(minFps: 6),
      'maxFps': const SyncSettings().copyWith(maxFps: 30),
      'smoothing': const SyncSettings().copyWith(smoothing: 0.6),
      'sampleSize': const SyncSettings().copyWith(sampleSize: 80),
      'colorBoost': const SyncSettings().copyWith(colorBoost: 1.6),
      'edgeWeight': const SyncSettings().copyWith(edgeWeight: 2.2),
      'colorAlgorithm': const SyncSettings().copyWith(colorAlgorithm: 'kmeans'),
      'minBrightness': const SyncSettings().copyWith(minBrightness: 40),
      'minColorDelta': const SyncSettings().copyWith(minColorDelta: 30),
      'adaptiveFps': const SyncSettings().copyWith(adaptiveFps: false),
      'predictiveSmoothing':
          const SyncSettings().copyWith(predictiveSmoothing: false),
      'ignoreLetterbox': const SyncSettings().copyWith(ignoreLetterbox: false),
      'sourceColorSpace':
          const SyncSettings().copyWith(sourceColorSpace: 'rec2020'),
      'hdrToneMapping': const SyncSettings().copyWith(hdrToneMapping: true),
      'hdrCapture': const SyncSettings().copyWith(hdrCapture: true),
      'syncProfile': const SyncSettings().copyWith(syncProfile: 'action'),
      'brightnessFollow':
          const SyncSettings().copyWith(brightnessFollow: false),
    };
    expect(variants.keys.toSet(), nativeKeys);
    for (final entry in variants.entries) {
      final args = entry.value.toMethodArgs(bulbs);
      expect(args[entry.key], isNot(base[entry.key]),
          reason: '${entry.key} must reach the native arguments');
      for (final other in nativeKeys.where((k) => k != entry.key)) {
        expect(args[other], base[other],
            reason: 'changing ${entry.key} must not disturb $other');
      }
    }
  });

  test('settings survive a save / load round trip', () {
    const tuned = SyncSettings(
      mode: 'zones',
      minFps: 6,
      maxFps: 28,
      smoothing: 0.6,
      sampleSize: 80,
      colorBoost: 1.6,
      edgeWeight: 2.2,
      colorAlgorithm: 'histogram',
      minBrightness: 40,
      minColorDelta: 30,
      adaptiveFps: false,
      predictiveSmoothing: false,
      ignoreLetterbox: false,
      sourceColorSpace: 'display-p3',
      hdrToneMapping: true,
      hdrCapture: true,
      syncProfile: 'cinema',
      brightnessFollow: false,
    );
    final restored = SyncSettings.fromJson(tuned.toJson());
    expect(restored.toMethodArgs(bulbs), tuned.toMethodArgs(bulbs));
  });

  test('lamp areas are sent per lamp whatever the mode', () {
    for (final mode in ['single', 'zones']) {
      final args = SyncSettings(mode: mode).toMethodArgs(bulbs);
      final json = args['bulbsJson'] as String;
      expect(json, contains('"region":"left"'));
      expect(json, contains('"region":"right"'));
    }
  });
}
