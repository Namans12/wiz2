import 'dart:ui';

import '../models/wiz_bulb.dart';

class SyncSettings {
  const SyncSettings({
    this.mode = 'single',
    this.minFps = 10,
    this.maxFps = 24,
    this.smoothing = 0.2,
    this.sampleSize = 56,
    this.colorBoost = 1.18,
    this.edgeWeight = 1.5,
    this.colorAlgorithm = 'auto',
    this.minBrightness = 20,
    this.minColorDelta = 8,
    this.adaptiveFps = true,
    this.predictiveSmoothing = true,
    this.ignoreLetterbox = true,
    this.sourceColorSpace = 'auto',
    this.hdrToneMapping = false,
    this.hdrCapture = false,
    this.syncProfile = 'auto',
    this.brightnessFollow = true,
  });

  final String mode;
  final int minFps;
  final int maxFps;
  final double smoothing;
  final int sampleSize;
  final double colorBoost;
  final double edgeWeight;
  final String colorAlgorithm;
  final int minBrightness;
  final int minColorDelta;
  final bool adaptiveFps;
  final bool predictiveSmoothing;
  final bool ignoreLetterbox;
  final String sourceColorSpace;
  final bool hdrToneMapping;
  final bool hdrCapture;
  final String syncProfile;
  final bool brightnessFollow;

  SyncSettings copyWith({
    String? mode,
    int? minFps,
    int? maxFps,
    double? smoothing,
    int? sampleSize,
    double? colorBoost,
    double? edgeWeight,
    String? colorAlgorithm,
    int? minBrightness,
    int? minColorDelta,
    bool? adaptiveFps,
    bool? predictiveSmoothing,
    bool? ignoreLetterbox,
    String? sourceColorSpace,
    bool? hdrToneMapping,
    bool? hdrCapture,
    String? syncProfile,
    bool? brightnessFollow,
  }) {
    return SyncSettings(
      mode: mode ?? this.mode,
      minFps: minFps ?? this.minFps,
      maxFps: maxFps ?? this.maxFps,
      smoothing: smoothing ?? this.smoothing,
      sampleSize: sampleSize ?? this.sampleSize,
      colorBoost: colorBoost ?? this.colorBoost,
      edgeWeight: edgeWeight ?? this.edgeWeight,
      colorAlgorithm: colorAlgorithm ?? this.colorAlgorithm,
      minBrightness: minBrightness ?? this.minBrightness,
      minColorDelta: minColorDelta ?? this.minColorDelta,
      adaptiveFps: adaptiveFps ?? this.adaptiveFps,
      predictiveSmoothing: predictiveSmoothing ?? this.predictiveSmoothing,
      ignoreLetterbox: ignoreLetterbox ?? this.ignoreLetterbox,
      sourceColorSpace: sourceColorSpace ?? this.sourceColorSpace,
      hdrToneMapping: hdrToneMapping ?? this.hdrToneMapping,
      hdrCapture: hdrCapture ?? this.hdrCapture,
      syncProfile: syncProfile ?? this.syncProfile,
      brightnessFollow: brightnessFollow ?? this.brightnessFollow,
    );
  }

  Map<String, Object> toMethodArgs(List<WizBulb> bulbs) {
    return <String, Object>{
      'bulbsJson': WizBulb.encodeList(bulbs),
      'mode': mode,
      'minFps': minFps,
      'maxFps': maxFps,
      'smoothing': smoothing,
      'sampleSize': sampleSize,
      'colorBoost': colorBoost,
      'edgeWeight': edgeWeight,
      'colorAlgorithm': colorAlgorithm,
      'minBrightness': minBrightness,
      'minColorDelta': minColorDelta,
      'adaptiveFps': adaptiveFps,
      'predictiveSmoothing': predictiveSmoothing,
      'ignoreLetterbox': ignoreLetterbox,
      'sourceColorSpace': sourceColorSpace,
      'hdrToneMapping': hdrToneMapping,
      'hdrCapture': hdrCapture,
      'syncProfile': syncProfile,
      'brightnessFollow': brightnessFollow,
    };
  }

  Map<String, dynamic> toJson() {
    return <String, dynamic>{
      'mode': mode,
      'minFps': minFps,
      'maxFps': maxFps,
      'smoothing': smoothing,
      'sampleSize': sampleSize,
      'colorBoost': colorBoost,
      'edgeWeight': edgeWeight,
      'colorAlgorithm': colorAlgorithm,
      'minBrightness': minBrightness,
      'minColorDelta': minColorDelta,
      'adaptiveFps': adaptiveFps,
      'predictiveSmoothing': predictiveSmoothing,
      'ignoreLetterbox': ignoreLetterbox,
      'sourceColorSpace': sourceColorSpace,
      'hdrToneMapping': hdrToneMapping,
      'hdrCapture': hdrCapture,
      'syncProfile': syncProfile,
      'brightnessFollow': brightnessFollow,
    };
  }

  factory SyncSettings.fromJson(Map<String, dynamic> json) {
    num number(String key, num fallback) =>
        json[key] is num && (json[key] as num).isFinite
            ? json[key] as num
            : fallback;
    bool flag(String key, bool fallback) =>
        json[key] is bool ? json[key] as bool : fallback;
    String text(String key, String fallback) =>
        json[key] is String ? json[key] as String : fallback;
    final minFps = number('minFps', 10).toInt().clamp(4, 30);
    return SyncSettings(
      mode: text('mode', 'single') == 'zones' ? 'zones' : 'single',
      minFps: minFps,
      maxFps: number('maxFps', 24).toInt().clamp(minFps, 30),
      smoothing: number('smoothing', 0.2).toDouble().clamp(0.05, 0.85),
      sampleSize: number('sampleSize', 56).toInt().clamp(24, 96),
      colorBoost: number('colorBoost', 1.18).toDouble().clamp(1.0, 1.8),
      edgeWeight: number('edgeWeight', 1.5).toDouble().clamp(1.0, 2.5),
      colorAlgorithm: const {'auto', 'weighted', 'kmeans', 'histogram'}
              .contains(json['colorAlgorithm'])
          ? json['colorAlgorithm'] as String
          : 'auto',
      minBrightness: number('minBrightness', 20).toInt().clamp(0, 80),
      minColorDelta: number('minColorDelta', 8).toInt().clamp(2, 48),
      adaptiveFps: flag('adaptiveFps', true),
      predictiveSmoothing: flag('predictiveSmoothing', true),
      ignoreLetterbox: flag('ignoreLetterbox', true),
      sourceColorSpace:
          _normalizeSourceColorSpace(text('sourceColorSpace', 'auto')),
      hdrToneMapping: flag('hdrToneMapping', false),
      hdrCapture: flag('hdrCapture', false),
      syncProfile: _normalizeSyncProfile(text('syncProfile', 'auto')),
      brightnessFollow: flag('brightnessFollow', true),
    );
  }

  static String _normalizeSyncProfile(String value) {
    final normalized = value.toLowerCase();
    if (normalized == 'cinema' || normalized == 'action') {
      return normalized;
    }
    return 'auto';
  }

  static String _normalizeSourceColorSpace(String value) {
    final normalized = value.toLowerCase().replaceAll('_', '-');
    if (normalized == 'p3' || normalized == 'display-p3') {
      return 'display-p3';
    }
    if (normalized == 'bt2020' ||
        normalized == 'bt.2020' ||
        normalized == 'rec-2020' ||
        normalized == 'rec2020') {
      return 'rec2020';
    }
    if (normalized == 'srgb') {
      return 'srgb';
    }
    return 'auto';
  }
}

class SyncRuntimeStats {
  const SyncRuntimeStats({
    required this.mode,
    required this.requestedFps,
    required this.currentFps,
    required this.frameTimeMs,
    required this.sendRateHz,
    required this.motionScore,
    required this.smoothingFactor,
    required this.updatesSent,
    this.effectiveColorBoost = 1.0,
    this.minBrightness = 0,
    this.deltaThreshold = 0,
    this.captureBackend = 'unknown',
    this.colorAlgorithm = 'auto',
    this.sourceColorSpace = 'srgb',
    this.hdrToneMapping = false,
    this.predictiveSmoothing = true,
    this.detectedDisplayGamut = 'srgb',
    this.detectedHdrSupport = false,
    this.deviceProfile = 'generic',
    this.supportedHdrTypes = const <String>[],
    this.primaryHdrType = 'none',
    this.hdrCaptureActive = false,
    this.captureBufferFormat = 'rgba8888',
    this.sceneCut = false,
    this.cutsDetected = 0,
    this.sceneNeutral = 0.0,
    this.sceneLuma = 0.0,
    this.syncProfile = 'auto',
    this.brightnessFollow = true,
    this.captureLooksBlack = false,
    this.framesProcessed = 0,
    this.replayedFrames = 0,
    this.freshFrameAgeMs,
    this.targetColors = const <String, Color>{},
    this.outputColors = const <String, Color>{},
    this.lastError,
  });

  final String mode;
  final int requestedFps;
  final int currentFps;
  final double frameTimeMs;
  final double sendRateHz;
  final double motionScore;
  final double smoothingFactor;
  final double effectiveColorBoost;
  final int minBrightness;
  final int deltaThreshold;
  final String captureBackend;
  final int updatesSent;
  final String colorAlgorithm;
  final String sourceColorSpace;
  final bool hdrToneMapping;
  final bool predictiveSmoothing;
  final String detectedDisplayGamut;
  final bool detectedHdrSupport;
  final String deviceProfile;
  final List<String> supportedHdrTypes;
  final String primaryHdrType;
  final bool hdrCaptureActive;
  final String captureBufferFormat;
  final bool sceneCut;
  final int cutsDetected;
  final double sceneNeutral;
  final double sceneLuma;
  final String syncProfile;
  final bool brightnessFollow;
  final bool captureLooksBlack;
  final int framesProcessed;
  final int replayedFrames;
  final int? freshFrameAgeMs;
  final Map<String, Color> targetColors;
  final Map<String, Color> outputColors;
  final String? lastError;

  Color? get primaryOutputColor {
    if (outputColors.isEmpty) {
      return null;
    }
    var red = 0;
    var green = 0;
    var blue = 0;
    for (final color in outputColors.values) {
      red += (color.r * 255).round();
      green += (color.g * 255).round();
      blue += (color.b * 255).round();
    }
    final count = outputColors.length;
    return Color.fromARGB(255, red ~/ count, green ~/ count, blue ~/ count);
  }

  Map<String, dynamic> toJson() {
    return <String, dynamic>{
      'mode': mode,
      'requestedFps': requestedFps,
      'currentFps': currentFps,
      'frameTimeMs': frameTimeMs,
      'sendRateHz': sendRateHz,
      'motionScore': motionScore,
      'smoothingFactor': smoothingFactor,
      'effectiveColorBoost': effectiveColorBoost,
      'minBrightness': minBrightness,
      'deltaThreshold': deltaThreshold,
      'captureBackend': captureBackend,
      'colorAlgorithm': colorAlgorithm,
      'sourceColorSpace': sourceColorSpace,
      'hdrToneMapping': hdrToneMapping,
      'predictiveSmoothing': predictiveSmoothing,
      'detectedDisplayGamut': detectedDisplayGamut,
      'detectedHdrSupport': detectedHdrSupport,
      'deviceProfile': deviceProfile,
      'supportedHdrTypes': supportedHdrTypes,
      'primaryHdrType': primaryHdrType,
      'hdrCaptureActive': hdrCaptureActive,
      'captureBufferFormat': captureBufferFormat,
      'sceneCut': sceneCut,
      'cutsDetected': cutsDetected,
      'sceneNeutral': sceneNeutral,
      'sceneLuma': sceneLuma,
      'syncProfile': syncProfile,
      'brightnessFollow': brightnessFollow,
      'captureLooksBlack': captureLooksBlack,
      'framesProcessed': framesProcessed,
      'replayedFrames': replayedFrames,
      'freshFrameAgeMs': freshFrameAgeMs,
      'updatesSent': updatesSent,
      'targetColors': _encodeColorMap(targetColors),
      'outputColors': _encodeColorMap(outputColors),
      'lastError': lastError,
    };
  }

  factory SyncRuntimeStats.fromMap(Map<dynamic, dynamic> map) {
    return SyncRuntimeStats(
      mode: map['mode'] as String? ?? 'single',
      requestedFps: (map['requestedFps'] as num?)?.toInt() ?? 0,
      currentFps: (map['currentFps'] as num?)?.toInt() ?? 0,
      frameTimeMs: (map['frameTimeMs'] as num?)?.toDouble() ?? 0.0,
      sendRateHz: (map['sendRateHz'] as num?)?.toDouble() ?? 0.0,
      motionScore: (map['motionScore'] as num?)?.toDouble() ?? 0.0,
      smoothingFactor: (map['smoothingFactor'] as num?)?.toDouble() ??
          (map['smoothing'] as num?)?.toDouble() ??
          0.0,
      effectiveColorBoost:
          (map['effectiveColorBoost'] as num?)?.toDouble() ?? 1.0,
      minBrightness: (map['minBrightness'] as num?)?.toInt() ?? 0,
      deltaThreshold: (map['deltaThreshold'] as num?)?.toInt() ?? 0,
      captureBackend: map['captureBackend'] as String? ?? 'unknown',
      updatesSent: (map['updatesSent'] as num?)?.toInt() ?? 0,
      colorAlgorithm: map['colorAlgorithm'] as String? ?? 'auto',
      sourceColorSpace: map['sourceColorSpace'] as String? ?? 'srgb',
      hdrToneMapping: map['hdrToneMapping'] as bool? ?? false,
      predictiveSmoothing: map['predictiveSmoothing'] as bool? ?? true,
      detectedDisplayGamut: map['detectedDisplayGamut'] as String? ?? 'srgb',
      detectedHdrSupport: map['detectedHdrSupport'] as bool? ?? false,
      deviceProfile: map['deviceProfile'] as String? ?? 'generic',
      supportedHdrTypes:
          ((map['supportedHdrTypes'] as List<dynamic>?) ?? const <dynamic>[])
              .map((e) => e.toString())
              .toList(),
      primaryHdrType: map['primaryHdrType'] as String? ?? 'none',
      hdrCaptureActive: map['hdrCaptureActive'] as bool? ?? false,
      captureBufferFormat: map['captureBufferFormat'] as String? ?? 'rgba8888',
      sceneCut: map['sceneCut'] as bool? ?? false,
      cutsDetected: (map['cutsDetected'] as num?)?.toInt() ?? 0,
      sceneNeutral: (map['sceneNeutral'] as num?)?.toDouble() ?? 0.0,
      sceneLuma: (map['sceneLuma'] as num?)?.toDouble() ?? 0.0,
      syncProfile: map['syncProfile'] as String? ?? 'auto',
      brightnessFollow: map['brightnessFollow'] as bool? ?? true,
      captureLooksBlack: map['captureLooksBlack'] as bool? ?? false,
      framesProcessed: (map['framesProcessed'] as num?)?.toInt() ?? 0,
      replayedFrames: (map['replayedFrames'] as num?)?.toInt() ?? 0,
      freshFrameAgeMs: (map['freshFrameAgeMs'] as num?)?.toInt(),
      targetColors: _decodeColorMap(map['targetColors']),
      outputColors: _decodeColorMap(map['outputColors']),
      lastError: map['lastError'] as String?,
    );
  }

  static Map<String, Color> _decodeColorMap(Object? raw) {
    if (raw is! Map) {
      return const <String, Color>{};
    }
    return raw.map((key, value) {
      final channels = value is List ? value : const <Object>[];
      final red = channels.isNotEmpty ? (channels[0] as num).toInt() : 0;
      final green = channels.length > 1 ? (channels[1] as num).toInt() : 0;
      final blue = channels.length > 2 ? (channels[2] as num).toInt() : 0;
      return MapEntry(
        key.toString(),
        Color.fromARGB(
          255,
          red.clamp(0, 255).toInt(),
          green.clamp(0, 255).toInt(),
          blue.clamp(0, 255).toInt(),
        ),
      );
    });
  }

  static Map<String, List<int>> _encodeColorMap(Map<String, Color> colors) {
    return colors.map(
      (key, color) => MapEntry(
        key,
        <int>[
          (color.r * 255.0).round(),
          (color.g * 255.0).round(),
          (color.b * 255.0).round(),
        ],
      ),
    );
  }
}
