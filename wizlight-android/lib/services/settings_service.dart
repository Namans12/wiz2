import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

import '../models/wiz_bulb.dart';
import 'sync_service.dart';

class SettingsService {
  static const _bulbsKey = 'wizlight.bulbs';
  static const _syncSettingsKey = 'wizlight.syncSettings';
  String? warning;

  Future<void> _preserveInvalid(
      SharedPreferences prefs, String key, Object? value) async {
    if (value == null) return;
    final data = value is String ? value : jsonEncode(value);
    var backupKey = '$key.corrupt';
    if (prefs.containsKey(backupKey) && prefs.getString(backupKey) != data) {
      backupKey = '$backupKey.${DateTime.now().microsecondsSinceEpoch}';
    }
    if (!await prefs.setString(backupKey, data)) {
      throw StateError('Could not preserve invalid settings');
    }
  }

  Future<List<WizBulb>> loadBulbs() async {
    final prefs = await SharedPreferences.getInstance();
    final original = prefs.get(_bulbsKey);
    try {
      if (original != null && original is! String) {
        throw const FormatException('Invalid bulb storage type');
      }
      final raw = original as String?;
      if (raw != null) {
        final decoded = jsonDecode(raw);
        if (decoded is! List) throw const FormatException('Invalid bulb list');
        return WizBulb.merge(const [], WizBulb.decodeList(raw));
      }
      final legacyValue = prefs.get('wizBulbs');
      if (legacyValue != null && legacyValue is! List<String>) {
        throw const FormatException('Invalid legacy bulb storage type');
      }
      final legacy = legacyValue as List<String>?;
      if (legacy == null) return const [];
      // Validate the complete conversion before writing; leave legacy data intact.
      final values = legacy.map(jsonDecode).toList();
      if (values.any((value) => value is! Map)) {
        throw const FormatException('Invalid legacy bulb');
      }
      final bulbs =
          WizBulb.merge(const [], WizBulb.decodeList(jsonEncode(values)));
      if (bulbs.length != values.length) {
        throw const FormatException('Invalid legacy bulb address');
      }
      await saveBulbs(bulbs);
      return bulbs;
    } on FormatException {
      await _preserveInvalid(prefs, _bulbsKey, original);
      warning = 'Saved bulb data could not be read. Original data was kept.';
      return const [];
    }
  }

  Future<void> saveBulbs(List<WizBulb> bulbs) async {
    final prefs = await SharedPreferences.getInstance();
    if (!await prefs.setString(_bulbsKey, WizBulb.encodeList(bulbs))) {
      throw StateError('Could not save bulbs');
    }
  }

  Future<SyncSettings> loadSyncSettings() async {
    final prefs = await SharedPreferences.getInstance();
    final original = prefs.get(_syncSettingsKey);
    try {
      if (original != null && original is! String) {
        throw const FormatException('Invalid settings storage type');
      }
      final raw = original as String?;
      if (raw != null) {
        final decoded = jsonDecode(raw);
        if (decoded is! Map<String, dynamic>) {
          throw const FormatException('Invalid settings');
        }
        final schema = decoded['schemaVersion'];
        if (schema != null && schema != 1) {
          throw const FormatException('Unknown settings version');
        }
        final values = schema == null ? decoded : decoded['settings'];
        if (values is! Map<String, dynamic>) {
          throw const FormatException('Invalid settings values');
        }
        return SyncSettings.fromJson(values);
      }
      final legacy = <String, dynamic>{};
      for (final key in const SyncSettings().toJson().keys) {
        if (prefs.containsKey(key)) legacy[key] = prefs.get(key);
      }
      final settings = SyncSettings.fromJson(legacy);
      if (legacy.isNotEmpty) await saveSyncSettings(settings);
      return settings;
    } on FormatException {
      await _preserveInvalid(prefs, _syncSettingsKey, original);
      warning = 'Saved settings could not be read. Original data was kept.';
      return const SyncSettings();
    }
  }

  Future<void> saveSyncSettings(SyncSettings settings) async {
    final prefs = await SharedPreferences.getInstance();
    if (!await prefs.setString(
        _syncSettingsKey,
        jsonEncode({
          'schemaVersion': 1,
          'settings': settings.toJson(),
        }))) {
      throw StateError('Could not save settings');
    }
  }
}
