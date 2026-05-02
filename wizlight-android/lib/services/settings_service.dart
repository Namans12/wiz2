import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

import '../models/wiz_bulb.dart';
import 'sync_service.dart';

class SettingsService {
  static const _bulbsKey = 'wizlight.bulbs';
  static const _syncSettingsKey = 'wizlight.syncSettings';

  Future<List<WizBulb>> loadBulbs() async {
    final prefs = await SharedPreferences.getInstance();
    final raw = prefs.getString(_bulbsKey);
    if (raw == null) {
      return const <WizBulb>[];
    }
    return WizBulb.decodeList(raw);
  }

  Future<void> saveBulbs(List<WizBulb> bulbs) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_bulbsKey, WizBulb.encodeList(bulbs));
  }

  Future<SyncSettings> loadSyncSettings() async {
    final prefs = await SharedPreferences.getInstance();
    final raw = prefs.getString(_syncSettingsKey);
    if (raw == null) {
      return const SyncSettings();
    }
    return SyncSettings.fromJson(jsonDecode(raw) as Map<String, dynamic>);
  }

  Future<void> saveSyncSettings(SyncSettings settings) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_syncSettingsKey, jsonEncode(settings.toJson()));
  }
}
