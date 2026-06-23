import 'package:flutter/services.dart';

import '../models/wiz_bulb.dart';
import 'sync_service.dart';

typedef SyncStatsListener = void Function(SyncRuntimeStats stats);

class ScreenCaptureService {
  ScreenCaptureService() {
    _channel.setMethodCallHandler(_handleMethodCall);
  }

  static const MethodChannel _channel = MethodChannel(
    'com.wizlight.app/screen_capture',
  );

  SyncStatsListener? onStats;

  Future<bool> requestPermission() async {
    return await _channel.invokeMethod<bool>('requestPermission') ?? false;
  }

  Future<bool> startCapture({
    required List<WizBulb> bulbs,
    required SyncSettings settings,
  }) async {
    final args = settings.toMethodArgs(bulbs);
    return await _channel.invokeMethod<bool>('startCapture', args) ?? false;
  }

  Future<void> stopCapture() async {
    await _channel.invokeMethod<bool>('stopCapture');
  }

  /// Opens the system document picker for a video file (USB drives appear here
  /// on Google TV). Returns the picked content URI, or null if cancelled.
  Future<String?> pickMedia() async {
    return await _channel.invokeMethod<String>('pickMedia');
  }

  /// Launches the on-device USB media player and starts driving bulbs from its
  /// decoded frames (no MediaProjection). [mediaUri] is from [pickMedia].
  Future<bool> playMedia({
    required String mediaUri,
    required List<WizBulb> bulbs,
    required SyncSettings settings,
  }) async {
    final args = Map<String, Object>.from(settings.toMethodArgs(bulbs));
    args['mediaUri'] = mediaUri;
    return await _channel.invokeMethod<bool>('playMedia', args) ?? false;
  }

  Future<void> stopMedia() async {
    await _channel.invokeMethod<bool>('stopMedia');
  }

  Future<void> _handleMethodCall(MethodCall call) async {
    if (call.method != 'onSyncUpdate') {
      return;
    }
    final args = call.arguments;
    if (args is Map) {
      onStats?.call(SyncRuntimeStats.fromMap(args));
    }
  }
}
