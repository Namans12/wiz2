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
      ip: json['ip'] as String? ?? '',
      name: json['name'] as String? ?? 'WiZ Bulb',
      mac: json['mac'] as String?,
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
        .where((bulb) => bulb.ip.isNotEmpty)
        .toList(growable: false);
  }
}
