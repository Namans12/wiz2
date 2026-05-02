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
    };
  }

  factory SyncSettings.fromJson(Map<String, dynamic> json) {
    return SyncSettings(
      mode: json['mode'] as String? ?? 'single',
      minFps: (json['minFps'] as num?)?.toInt() ?? 10,
      maxFps: (json['maxFps'] as num?)?.toInt() ?? 24,
      smoothing: (json['smoothing'] as num?)?.toDouble() ?? 0.2,
      sampleSize: (json['sampleSize'] as num?)?.toInt() ?? 56,
      colorBoost: (json['colorBoost'] as num?)?.toDouble() ?? 1.18,
      edgeWeight: (json['edgeWeight'] as num?)?.toDouble() ?? 1.5,
      colorAlgorithm: json['colorAlgorithm'] as String? ?? 'auto',
      minBrightness: (json['minBrightness'] as num?)?.toInt() ?? 20,
      minColorDelta: (json['minColorDelta'] as num?)?.toInt() ?? 8,
      adaptiveFps: json['adaptiveFps'] as bool? ?? true,
      predictiveSmoothing: json['predictiveSmoothing'] as bool? ?? true,
      ignoreLetterbox: json['ignoreLetterbox'] as bool? ?? true,
    );
  }
}

class SyncRuntimeStats {
  const SyncRuntimeStats({
    required this.mode,
    required this.currentFps,
    required this.sendRateHz,
    required this.motionScore,
    required this.smoothing,
    required this.updatesSent,
    this.colorAlgorithm = 'auto',
    this.predictiveSmoothing = true,
    this.targetColors = const <String, Color>{},
    this.outputColors = const <String, Color>{},
    this.lastError,
  });

  final String mode;
  final int currentFps;
  final double sendRateHz;
  final double motionScore;
  final double smoothing;
  final int updatesSent;
  final String colorAlgorithm;
  final bool predictiveSmoothing;
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

  factory SyncRuntimeStats.fromMap(Map<dynamic, dynamic> map) {
    return SyncRuntimeStats(
      mode: map['mode'] as String? ?? 'single',
      currentFps: (map['currentFps'] as num?)?.toInt() ?? 0,
      sendRateHz: (map['sendRateHz'] as num?)?.toDouble() ?? 0.0,
      motionScore: (map['motionScore'] as num?)?.toDouble() ?? 0.0,
      smoothing: (map['smoothing'] as num?)?.toDouble() ?? 0.0,
      updatesSent: (map['updatesSent'] as num?)?.toInt() ?? 0,
      colorAlgorithm: map['colorAlgorithm'] as String? ?? 'auto',
      predictiveSmoothing: map['predictiveSmoothing'] as bool? ?? true,
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
}
