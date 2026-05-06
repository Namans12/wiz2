import 'dart:async';
import 'dart:convert';
import 'dart:io';

import '../models/wiz_bulb.dart';

class WizBulbService {
  static const int wizPort = 38899;

  Future<List<WizBulb>> discover({
    Duration timeout = const Duration(seconds: 3),
  }) async {
    final socket = await RawDatagramSocket.bind(InternetAddress.anyIPv4, 0);
    final bulbs = <String, WizBulb>{};
    final done = Completer<List<WizBulb>>();
    Timer? timer;

    socket.broadcastEnabled = true;
    socket.listen((event) {
      if (event != RawSocketEvent.read) {
        return;
      }
      final packet = socket.receive();
      if (packet == null) {
        return;
      }
      try {
        final message = jsonDecode(utf8.decode(packet.data));
        if (message is! Map) {
          return;
        }
        final result = message['result'];
        final mac = result is Map ? result['mac'] as String? : null;
        final moduleName =
            result is Map ? result['moduleName'] as String? : null;
        final ip = packet.address.address;
        bulbs[ip] = WizBulb(
          ip: ip,
          name: moduleName?.isNotEmpty == true ? moduleName! : 'WiZ $ip',
          mac: mac,
        );
      } catch (_) {
        // Ignore non-WiZ UDP replies.
      }
    });

    final payload = utf8.encode('{"method":"getSystemConfig","params":{}}');
    socket.send(payload, InternetAddress('255.255.255.255'), wizPort);
    timer = Timer(timeout, () {
      socket.close();
      if (!done.isCompleted) {
        done.complete(bulbs.values.toList(growable: false));
      }
    });

    final result = await done.future;
    timer.cancel();
    return result;
  }

  WizBulb fromManualIp(String ip, {int index = 1}) {
    return WizBulb(ip: ip.trim(), name: 'Bulb $index');
  }
}
