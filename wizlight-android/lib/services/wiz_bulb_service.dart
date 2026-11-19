import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import '../models/wiz_bulb.dart';

bool _validDiscoveryAddress(String address) {
  final parts = address.split('.');
  if (parts.length != 4) return false;
  final numbers = parts.map(int.tryParse).toList();
  if (numbers.any((value) => value == null || value < 0 || value > 255)) {
    return false;
  }
  return numbers[0] != 0 &&
      numbers[0] != 127 &&
      numbers[0]! < 224 &&
      numbers[3] != 255;
}

@visibleForTesting
void drainDiscoveryPackets(
    Datagram? Function() receive, Map<String, WizBulb> bulbs) {
  Datagram? packet;
  while ((packet = receive()) != null) {
    final current = packet!;
    if (current.port != WizBulbService.wizPort) continue;
    try {
      final message = jsonDecode(utf8.decode(current.data));
      final result = message is Map ? message['result'] : null;
      if (result is! Map ||
          result['mac'] is! String ||
          (result['mac'] as String).isEmpty) {
        continue;
      }
      final bulb = WizBulb.fromJson({
        'ip': current.address.address,
        'mac': result['mac'],
        'name': result['moduleName'] ?? 'WiZ ${current.address.address}',
      });
      if (_validDiscoveryAddress(bulb.ip)) bulbs[bulb.ip] = bulb;
    } catch (_) {/* Ignore unrelated LAN datagrams and keep draining. */}
  }
}

class WizBulbService {
  static const int wizPort = 38899;

  Future<List<WizBulb>> discover(
      {Duration timeout = const Duration(seconds: 3)}) async {
    final socket = await RawDatagramSocket.bind(InternetAddress.anyIPv4, 0);
    StreamSubscription<RawSocketEvent>? subscription;
    Timer? retryTimer;
    final bulbs = <String, WizBulb>{};
    try {
      socket.broadcastEnabled = true;
      subscription = socket.listen((event) {
        if (event != RawSocketEvent.read) return;
        drainDiscoveryPackets(socket.receive, bulbs);
      });
      final payload = utf8.encode('{"method":"getSystemConfig","params":{}}');
      void sendProbe() {
        socket.send(payload, InternetAddress('255.255.255.255'), wizPort);
      }

      sendProbe();
      retryTimer = Timer.periodic(const Duration(milliseconds: 600), (_) {
        try {
          sendProbe();
        } on SocketException {
          // The network can disappear during the discovery window.
        }
      });
      await Future<void>.delayed(timeout);
      return bulbs.values.toList(growable: false);
    } finally {
      retryTimer?.cancel();
      await subscription?.cancel();
      socket.close();
    }
  }

  Future<bool> probe(String ip) =>
      _request(ip, 'getPilot', const {}, (result) => true);

  Future<bool> turnOff(String ip) => _request(ip, 'setPilot',
      const {'state': false}, (result) => result['success'] == true);

  Future<bool> _request(String ip, String method, Map<String, Object> params,
      bool Function(Map result) succeeded) async {
    if (!WizBulb.isValidIp(ip)) return false;
    final socket = await RawDatagramSocket.bind(InternetAddress.anyIPv4, 0);
    StreamSubscription<RawSocketEvent>? subscription;
    final reply = Completer<bool>();
    try {
      subscription = socket.listen((event) {
        if (event != RawSocketEvent.read || reply.isCompleted) return;
        final packet = socket.receive();
        if (packet == null ||
            packet.address.address != ip ||
            packet.port != wizPort) {
          return;
        }
        try {
          final message = jsonDecode(utf8.decode(packet.data));
          if (message is Map && message['result'] is Map) {
            reply.complete(succeeded(message['result'] as Map));
          }
        } catch (_) {/* Keep waiting for a valid response. */}
      });
      socket.send(utf8.encode(jsonEncode({'method': method, 'params': params})),
          InternetAddress(ip), wizPort);
      return await reply.future
          .timeout(const Duration(seconds: 2), onTimeout: () => false);
    } finally {
      await subscription?.cancel();
      socket.close();
    }
  }

  WizBulb fromManualIp(String ip, {int index = 1}) {
    if (!WizBulb.isValidIp(ip)) {
      throw const FormatException('Enter a valid local IPv4 address');
    }
    return WizBulb(ip: ip.trim(), name: 'Bulb $index');
  }
}
