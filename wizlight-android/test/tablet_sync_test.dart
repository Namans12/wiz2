import 'dart:ui';

import 'package:flutter_test/flutter_test.dart';
import 'package:wizlight_android/models/wiz_bulb.dart';
import 'package:wizlight_android/services/sync_service.dart';

void main() {
  test('WizBulb JSON round-trip keeps IP, name, mac, and region', () {
    const bulb = WizBulb(
      ip: '192.168.1.4',
      name: 'Living Room',
      mac: 'CC4085E2D228',
      region: 'right',
    );

    final restored = WizBulb.fromJson(bulb.toJson());

    expect(restored.ip, bulb.ip);
    expect(restored.name, bulb.name);
    expect(restored.mac, bulb.mac);
    expect(restored.region, bulb.region);
  });

  test('SyncRuntimeStats primary color averages per-bulb output', () {
    const runtime = SyncRuntimeStats(
      mode: 'zones',
      requestedFps: 24,
      currentFps: 24,
      frameTimeMs: 8.5,
      sendRateHz: 12.0,
      motionScore: 0.02,
      smoothingFactor: 0.2,
      updatesSent: 3,
      outputColors: <String, Color>{
        '192.168.1.4': Color.fromARGB(255, 200, 100, 0),
        '192.168.1.5': Color.fromARGB(255, 0, 100, 200),
      },
    );

    final average = runtime.primaryOutputColor!;

    expect((average.r * 255.0).round(), 100);
    expect((average.g * 255.0).round(), 100);
    expect((average.b * 255.0).round(), 100);
  });

  test('SyncRuntimeStats decodes production diagnostics fields', () {
    final stats = SyncRuntimeStats.fromMap(<String, Object?>{
      'mode': 'single',
      'requestedFps': 28,
      'currentFps': 22,
      'frameTimeMs': 6.25,
      'sendRateHz': 18.0,
      'motionScore': 0.04,
      'smoothingFactor': 0.16,
      'effectiveColorBoost': 1.12,
      'minBrightness': 20,
      'deltaThreshold': 6,
      'captureBackend': 'mediaprojection',
      'sourceColorSpace': 'display-p3',
      'hdrToneMapping': true,
      'updatesSent': 9,
    });

    expect(stats.requestedFps, 28);
    expect(stats.frameTimeMs, 6.25);
    expect(stats.smoothingFactor, 0.16);
    expect(stats.effectiveColorBoost, 1.12);
    expect(stats.minBrightness, 20);
    expect(stats.deltaThreshold, 6);
    expect(stats.captureBackend, 'mediaprojection');
    expect(stats.sourceColorSpace, 'display-p3');
    expect(stats.hdrToneMapping, isTrue);
  });

  test('SyncSettings passes wide gamut settings to native capture', () {
    const settings = SyncSettings(
      sourceColorSpace: 'display-p3',
      hdrToneMapping: true,
    );

    final args = settings.toMethodArgs(const <WizBulb>[]);

    expect(args['sourceColorSpace'], 'display-p3');
    expect(args['hdrToneMapping'], isTrue);
  });
}
