import 'package:flutter_test/flutter_test.dart';
import 'package:wizlight_android/services/sync_service.dart';

void main() {
  test('fresh frame age and replay count survive diagnostics serialization',
      () {
    final stats = SyncRuntimeStats.fromMap({
      'framesProcessed': 8,
      'replayedFrames': 12,
      'freshFrameAgeMs': 175,
    });
    expect(stats.framesProcessed, 8);
    expect(stats.replayedFrames, 12);
    expect(stats.freshFrameAgeMs, 175);
    expect(stats.toJson()['replayedFrames'], 12);
    expect(stats.toJson()['freshFrameAgeMs'], 175);
    expect(SyncRuntimeStats.fromMap({}).freshFrameAgeMs, isNull);
  });
}
