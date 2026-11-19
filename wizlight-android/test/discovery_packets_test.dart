import 'dart:collection';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:wizlight_android/models/wiz_bulb.dart';
import 'package:wizlight_android/services/wiz_bulb_service.dart';

Datagram packet(String ip, String body, {int port = 38899}) => Datagram(
    Uint8List.fromList(utf8.encode(body)), InternetAddress(ip), port);

void main() {
  test('drains bursts and ignores invalid packets without losing valid replies', () {
    final queue = Queue<Datagram>.of([
      packet('192.168.1.9', 'not json'),
      packet('192.168.1.10', '{"result":{"mac":"one"}}'),
      packet('192.168.1.11', '{"result":{"mac":"two"}}'),
      packet('192.168.1.12', '{"result":{"mac":"wrong-port"}}', port: 1234),
    ]);
    final bulbs = <String, WizBulb>{};
    drainDiscoveryPackets(() => queue.isEmpty ? null : queue.removeFirst(), bulbs);
    expect(bulbs.keys.toSet(), {'192.168.1.10', '192.168.1.11'});
    expect(queue, isEmpty);
  });
}
