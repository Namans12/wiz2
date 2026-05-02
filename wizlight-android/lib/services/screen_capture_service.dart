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
