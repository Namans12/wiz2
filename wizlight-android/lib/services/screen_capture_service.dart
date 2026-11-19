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
  void Function(Map<dynamic, dynamic>)? onSession;

  /// Native asks Dart to collect a video opened from outside the app.
  void Function()? onPendingMedia;
  String _sessionId = '';

  /// True on Android TV / Google TV. Any failure means "not a TV".
  static Future<bool> isTelevision() async {
    try {
      return await _channel.invokeMethod<bool>('isTelevision') ?? false;
    } catch (_) {
      return false;
    }
  }

  /// A media URI handed to the app by "open with", or null. One-shot.
  Future<String?> takePendingMediaUri() async {
    try {
      final uri = await _channel.invokeMethod<String>('takePendingMediaUri');
      return (uri == null || uri.isEmpty) ? null : uri;
    } catch (_) {
      return null;
    }
  }

  Future<void> refreshSession() async {
    final state =
        await _channel.invokeMapMethod<dynamic, dynamic>('querySession');
    if (state != null) {
      _sessionId = state['sessionId'] as String? ?? '';
      onSession?.call(state);
    }
  }

  void dispose() {
    onStats = null;
    onSession = null;
    onPendingMedia = null;
    _channel.setMethodCallHandler(null);
  }

  Map<String, Object> _startArguments(
      List<WizBulb> bulbs, SyncSettings settings) {
    _sessionId = DateTime.now().microsecondsSinceEpoch.toString();
    return {...settings.toMethodArgs(bulbs), 'sessionId': _sessionId};
  }

  Future<void> waitUntilStopped() async {
    for (var attempt = 0; attempt < 60; attempt++) {
      final state =
          await _channel.invokeMapMethod<dynamic, dynamic>('querySession');
      if (state == null ||
          state['state'] == 'stopped' ||
          state['state'] == 'error') {
        return;
      }
      await Future<void>.delayed(const Duration(milliseconds: 50));
    }
    throw StateError('Previous sync session has not stopped yet');
  }

  Future<bool> requestPermission() async {
    return await _channel.invokeMethod<bool>('requestPermission') ?? false;
  }

  Future<bool> startCapture({
    required List<WizBulb> bulbs,
    required SyncSettings settings,
  }) async {
    final args = _startArguments(bulbs, settings);
    return await _channel.invokeMethod<bool>('startCapture', args) ?? false;
  }

  Future<void> stopCapture() async {
    await _channel.invokeMethod<bool>('stopCapture', {'sessionId': _sessionId});
    await waitUntilStopped();
  }

  /// Applies [settings] to the running session without restarting capture.
  ///
  /// Call it whenever a setting changes while sync is running. Returns the keys that cannot be
  /// applied live and need a restart (today only `hdrCapture`), an empty set when everything took
  /// effect on the next frame, or null when there is no running session to update.
  Future<Set<String>?> updateSettings(SyncSettings settings) async {
    final reply = await _channel.invokeMapMethod<dynamic, dynamic>(
      'updateSettings',
      {...settings.toLiveArgs(), 'sessionId': _sessionId},
    );
    return parseSettingsReply(reply);
  }

  /// Parses the native `updateSettings` reply (see [updateSettings]).
  static Set<String>? parseSettingsReply(Map<dynamic, dynamic>? reply) {
    if (reply == null || reply['accepted'] != true) return null;
    final restart = reply['restartRequired'];
    return restart is List ? restart.whereType<String>().toSet() : <String>{};
  }

  Future<void> updateTargets(List<WizBulb> bulbs) async {
    final accepted = await _channel.invokeMethod<bool>('updateTargets', {
      'sessionId': _sessionId,
      'bulbsJson': WizBulb.encodeList(WizBulb.syncTargets(bulbs)),
    });
    if (accepted != true) throw StateError('Could not update sync lights');
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
    final args = _startArguments(bulbs, settings);
    args['mediaUri'] = mediaUri;
    return await _channel.invokeMethod<bool>('playMedia', args) ?? false;
  }

  Future<void> stopMedia() async {
    await _channel.invokeMethod<bool>('stopMedia', {'sessionId': _sessionId});
    await waitUntilStopped();
  }

  Future<void> _handleMethodCall(MethodCall call) async {
    if (call.method == 'onSessionState' && call.arguments is Map) {
      final state = call.arguments as Map;
      if (state['sessionId'] == _sessionId) onSession?.call(state);
      return;
    }
    if (call.method == 'onPendingMediaUri') {
      onPendingMedia?.call();
      return;
    }
    if (call.method != 'onSyncUpdate') {
      return;
    }
    final args = call.arguments;
    if (args is Map) {
      onStats?.call(SyncRuntimeStats.fromMap(args));
    }
  }
}
