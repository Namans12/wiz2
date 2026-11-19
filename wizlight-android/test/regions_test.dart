import 'package:flutter_test/flutter_test.dart';
import 'package:wizlight_android/models/wiz_bulb.dart';

void main() {
  test('supported light positions survive settings serialization', () {
    for (final region in WizBulb.supportedRegions) {
      final bulb = WizBulb(ip: '192.168.1.2', name: 'Lamp', region: region);
      expect(WizBulb.fromJson(bulb.toJson()).region, region);
    }
    expect(WizBulb.fromJson(const {'ip': '192.168.1.2', 'region': 'invalid'}).region, 'left');
  });
}
