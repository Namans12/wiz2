import 'dart:convert';

import 'package:flutter/foundation.dart';

@immutable
class WizBulb {
  static const supportedRegions = {'left', 'right', 'top', 'bottom', 'center'};
  const WizBulb({
    required this.ip,
    required this.name,
    this.mac,
    this.region = 'left',
  });

  final String ip;
  final String name;
  final String? mac;
  final String region;

  WizBulb copyWith({String? ip, String? name, String? mac, String? region}) {
    return WizBulb(
      ip: ip ?? this.ip,
      name: name ?? this.name,
      mac: mac ?? this.mac,
      region: region ?? this.region,
    );
  }

  Map<String, dynamic> toJson() {
    return <String, dynamic>{
      'ip': ip,
      'name': name,
      'mac': mac,
      'region': region,
    };
  }

  factory WizBulb.fromJson(Map<String, dynamic> json) {
    return WizBulb(
      ip: json['ip'] is String ? (json['ip'] as String).trim() : '',
      name: json['name'] is String && (json['name'] as String).trim().isNotEmpty
          ? (json['name'] as String).trim()
          : 'WiZ Bulb',
      mac: json['mac'] is String ? json['mac'] as String : null,
      region: supportedRegions.contains(json['region']) ? json['region'] as String : 'left',
    );
  }

  static String encodeList(List<WizBulb> bulbs) {
    return jsonEncode(bulbs.map((bulb) => bulb.toJson()).toList());
  }

  static List<WizBulb> decodeList(String rawJson) {
    final data = jsonDecode(rawJson);
    if (data is! List) {
      return const <WizBulb>[];
    }
    return data
        .whereType<Map>()
        .map((item) => WizBulb.fromJson(Map<String, dynamic>.from(item)))
        .where((bulb) => isValidIp(bulb.ip))
        .toList(growable: false);
  }

  static bool isValidIp(String value) {
    final parts = value.trim().split('.');
    if (parts.length != 4) return false;
    final numbers = parts.map(int.tryParse).toList();
    if (numbers.any((part) => part == null || part < 0 || part > 255)) {
      return false;
    }
    return numbers[0] != 0 &&
        numbers[0] != 127 &&
        numbers[0]! < 224 &&
        numbers[3] != 255;
  }

  /// Preserve user labels/layout across DHCP changes and partial discovery.
  static List<WizBulb> merge(List<WizBulb> saved, List<WizBulb> discovered) {
    final result = saved.where((bulb) => isValidIp(bulb.ip)).toList();
    for (final incoming in discovered) {
      if (!isValidIp(incoming.ip)) continue;
      final index = result.indexWhere((old) =>
          (incoming.mac?.isNotEmpty == true &&
              old.mac?.toLowerCase() == incoming.mac?.toLowerCase()) ||
          old.ip == incoming.ip);
      if (index < 0) {
        result.add(incoming);
      } else {
        final old = result[index];
        result[index] = incoming.copyWith(name: old.name, region: old.region);
      }
    }
    final seen = <String>{};
    return result.where((bulb) => seen.add(bulb.ip)).toList();
  }
}
