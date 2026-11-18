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
    this.screenSyncEnabled = true,
  });

  final String ip;
  final String name;
  final String? mac;
  final String region;
  final bool screenSyncEnabled;

  WizBulb copyWith({
    String? ip,
    String? name,
    String? mac,
    String? region,
    bool? screenSyncEnabled,
  }) {
    return WizBulb(
      ip: ip ?? this.ip,
      name: name ?? this.name,
      mac: mac ?? this.mac,
      region: region ?? this.region,
      screenSyncEnabled: screenSyncEnabled ?? this.screenSyncEnabled,
    );
  }

  Map<String, dynamic> toJson() {
    return <String, dynamic>{
      'ip': ip,
      'name': name,
      'mac': mac,
      'region': region,
      'screenSyncEnabled': screenSyncEnabled,
    };
  }

  factory WizBulb.fromJson(Map<String, dynamic> json) {
    return WizBulb(
      ip: json['ip'] is String ? (json['ip'] as String).trim() : '',
      name: json['name'] is String && (json['name'] as String).trim().isNotEmpty
          ? (json['name'] as String).trim()
          : 'WiZ Bulb',
      mac: json['mac'] is String ? json['mac'] as String : null,
      region: supportedRegions.contains(json['region'])
          ? json['region'] as String
          : 'left',
      screenSyncEnabled: json['screenSyncEnabled'] != false,
    );
  }

  static String encodeList(List<WizBulb> bulbs) {
    return jsonEncode(bulbs.map((bulb) => bulb.toJson()).toList());
  }

  static List<WizBulb> syncTargets(List<WizBulb> bulbs) =>
      bulbs.where((bulb) => bulb.screenSyncEnabled).toList(growable: false);

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

  static String? _identity(String? mac) {
    final normalized = mac?.toLowerCase().replaceAll(RegExp('[:-]'), '');
    return normalized == null || normalized.isEmpty ? null : normalized;
  }

  /// Preserve user settings across DHCP changes and partial discovery.
  static List<WizBulb> merge(List<WizBulb> saved, List<WizBulb> discovered) {
    final result = saved.where((bulb) => isValidIp(bulb.ip)).toList();
    final byIdentity = {
      for (final bulb in result)
        if (_identity(bulb.mac) != null) _identity(bulb.mac): bulb,
    };
    final byIp = {for (final bulb in result) bulb.ip: bulb};
    final updates = <String, WizBulb>{};
    final replaced = <WizBulb>{};
    for (final incoming in discovered) {
      if (!isValidIp(incoming.ip)) continue;
      final identity = _identity(incoming.mac);
      final atAddress = byIp[incoming.ip];
      final known = byIdentity[identity] ??
          (identity == null || _identity(atAddress?.mac) == null
              ? atAddress
              : null);
      if (known != null) replaced.add(known);
      updates[incoming.ip] = known == null
          ? incoming
          : incoming.copyWith(
              name: known.name,
              mac: known.mac,
              region: known.region,
              screenSyncEnabled: known.screenSyncEnabled,
            );
    }
    return [
      ...result.where(
          (bulb) => !replaced.contains(bulb) && !updates.containsKey(bulb.ip)),
      ...updates.values,
    ];
  }
}
