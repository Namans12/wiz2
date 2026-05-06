import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';

import '../models/wiz_bulb.dart';
import '../services/screen_capture_service.dart';
import '../services/settings_service.dart';
import '../services/sync_service.dart';
import '../services/wiz_bulb_service.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  final SettingsService _settingsService = SettingsService();
  final WizBulbService _bulbService = WizBulbService();
  late final ScreenCaptureService _captureService;

  List<WizBulb> _bulbs = const <WizBulb>[];
  SyncSettings _settings = const SyncSettings();
  SyncRuntimeStats? _runtimeStats;
  bool _loading = true;
  bool _syncRunning = false;
  String? _status;

  @override
  void initState() {
    super.initState();
    _captureService = ScreenCaptureService()
      ..onStats = (stats) {
        if (mounted) {
          setState(() => _runtimeStats = stats);
        }
      };
    _load();
  }

  Future<void> _load() async {
    final bulbs = await _settingsService.loadBulbs();
    final settings = await _settingsService.loadSyncSettings();
    if (!mounted) {
      return;
    }
    setState(() {
      _bulbs = bulbs;
      _settings = settings;
      _loading = false;
    });
  }

  Future<void> _saveBulbs(List<WizBulb> bulbs) async {
    setState(() => _bulbs = bulbs.take(2).toList(growable: false));
    await _settingsService.saveBulbs(_bulbs);
  }

  Future<void> _saveSettings(SyncSettings settings) async {
    setState(() => _settings = settings);
    await _settingsService.saveSyncSettings(settings);
  }

  Future<void> _discover() async {
    setState(() => _status = 'Discovering bulbs...');
    try {
      final discovered = await _bulbService.discover();
      if (!mounted) {
        return;
      }
      if (discovered.isEmpty) {
        setState(() => _status = 'No bulbs found. Add by IP instead.');
        return;
      }
      await _saveBulbs(discovered);
      if (!mounted) {
        return;
      }
      setState(() => _status = 'Found ${discovered.length} bulb(s)');
    } catch (error) {
      if (!mounted) {
        return;
      }
      setState(() => _status = 'Discovery failed: $error');
    }
  }

  Future<void> _addIp() async {
    final controller = TextEditingController();
    final ip = await showDialog<String>(
      context: context,
      builder: (context) {
        return AlertDialog(
          title: const Text('Add WiZ Bulb IP'),
          content: TextField(
            controller: controller,
            decoration: const InputDecoration(hintText: '192.168.1.10'),
            keyboardType: TextInputType.number,
            autofocus: true,
          ),
          actions: <Widget>[
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, controller.text.trim()),
              child: const Text('Add'),
            ),
          ],
        );
      },
    );
    if (ip == null || ip.isEmpty) {
      return;
    }
    if (!mounted) {
      return;
    }
    final nextBulbs = <WizBulb>[
      ..._bulbs,
      _bulbService.fromManualIp(ip, index: _bulbs.length + 1),
    ];
    await _saveBulbs(nextBulbs);
  }

  Future<void> _toggleSync() async {
    if (_syncRunning) {
      await _captureService.stopCapture();
      if (!mounted) {
        return;
      }
      setState(() {
        _syncRunning = false;
        _status = 'Screen sync stopped';
      });
      return;
    }

    if (_bulbs.isEmpty) {
      setState(() => _status = 'Add at least one bulb first.');
      return;
    }

    final granted = await _captureService.requestPermission();
    if (!mounted) {
      return;
    }
    if (!granted) {
      setState(() => _status = 'Screen capture permission denied.');
      return;
    }

    final started = await _captureService.startCapture(
      bulbs: _bulbs,
      settings: _settings,
    );
    if (!mounted) {
      return;
    }
    setState(() {
      _syncRunning = started;
      _status =
          started ? 'Screen sync running' : 'Could not start screen sync.';
    });
  }

  Future<void> _exportDiagnostics() async {
    final report = <String, Object?>{
      'schemaVersion': 1,
      'generatedAt': DateTime.now().toUtc().toIso8601String(),
      'platform': 'android',
      'appVersion': '1.0.0+1',
      'bulbs': _bulbs.map((bulb) => bulb.toJson()).toList(growable: false),
      'screenSyncSettings': _settings.toJson(),
      'runtimeStats': _runtimeStats?.toJson() ?? <String, Object?>{},
      'notes': <String>[
        'WiZ local communication must be enabled on the trusted LAN.',
        'Hardware color accuracy still requires the camera validation flow.',
      ],
    };
    final file = File(
      '${Directory.systemTemp.path}/wizlight-diagnostics-${DateTime.now().millisecondsSinceEpoch}.json',
    );
    await file
        .writeAsString(const JsonEncoder.withIndent('  ').convert(report));
    if (!mounted) {
      return;
    }
    setState(() => _status = 'Diagnostics exported: ${file.path}');
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }

    return Scaffold(
      appBar: AppBar(
        title: const Text('WizLight Sync'),
        actions: <Widget>[
          IconButton(
            onPressed: _discover,
            icon: const Icon(Icons.search),
            tooltip: 'Discover',
          ),
          IconButton(
            onPressed: _addIp,
            icon: const Icon(Icons.add),
            tooltip: 'Add IP',
          ),
          IconButton(
            onPressed: _showSyncGuide,
            icon: const Icon(Icons.info_outline),
            tooltip: 'Sync guide',
          ),
        ],
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: <Widget>[
          _buildSyncHero(),
          const SizedBox(height: 16),
          ..._bulbs.map(_buildBulbTile),
          if (_bulbs.isEmpty)
            const ListTile(
              leading: Icon(Icons.lightbulb_outline),
              title: Text('No bulbs configured'),
              subtitle: Text('Discover bulbs or add an IP manually.'),
            ),
          const SizedBox(height: 16),
          _buildSyncControls(),
          const SizedBox(height: 16),
          _buildRuntimeCard(),
          const SizedBox(height: 16),
          _buildDiagnosticsCard(),
        ],
      ),
    );
  }

  Widget _buildSyncHero() {
    final stats = _runtimeStats;
    final color = stats?.primaryOutputColor;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Row(
              children: <Widget>[
                Icon(
                  _syncRunning ? Icons.sync : Icons.sync_disabled,
                  color: _syncRunning ? Colors.green : null,
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: <Widget>[
                      Text(
                        _syncRunning
                            ? 'Screen sync running'
                            : 'Screen sync idle',
                        style: Theme.of(context).textTheme.titleMedium,
                      ),
                      Text(
                        _status ?? '${_bulbs.length} bulb(s) configured',
                        style: Theme.of(context).textTheme.bodySmall,
                      ),
                    ],
                  ),
                ),
                if (color != null)
                  Container(
                    width: 44,
                    height: 32,
                    decoration: BoxDecoration(
                      color: color,
                      borderRadius: BorderRadius.circular(6),
                      border: Border.all(color: Theme.of(context).dividerColor),
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 14),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: <Widget>[
                _buildStatChip('FPS', '${stats?.currentFps ?? '-'}'),
                _buildStatChip(
                  'Send',
                  '${stats?.sendRateHz.toStringAsFixed(1) ?? '-'} Hz',
                ),
                _buildStatChip(
                  'Motion',
                  stats?.motionScore.toStringAsFixed(3) ?? '-',
                ),
                _buildStatChip('Mode', stats?.mode ?? _settings.mode),
                _buildStatChip(
                  'Color',
                  stats?.sourceColorSpace ?? _settings.sourceColorSpace,
                ),
              ],
            ),
            const SizedBox(height: 14),
            SizedBox(
              width: double.infinity,
              child: FilledButton.icon(
                onPressed: _toggleSync,
                icon: Icon(_syncRunning ? Icons.stop : Icons.play_arrow),
                label: Text(
                    _syncRunning ? 'Stop Screen Sync' : 'Start Screen Sync'),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildStatChip(String label, String value) {
    return Chip(
      label: Text('$label: $value'),
      visualDensity: VisualDensity.compact,
    );
  }

  Widget _buildBulbTile(WizBulb bulb) {
    return Card(
      child: ListTile(
        leading: const Icon(Icons.lightbulb),
        title: Text(bulb.name),
        subtitle: Text(bulb.ip),
        trailing: DropdownButton<String>(
          value: bulb.region,
          items: const <DropdownMenuItem<String>>[
            DropdownMenuItem(value: 'left', child: Text('Left')),
            DropdownMenuItem(value: 'right', child: Text('Right')),
          ],
          onChanged: (value) {
            if (value == null) {
              return;
            }
            final next = _bulbs
                .map(
                  (item) =>
                      item.ip == bulb.ip ? item.copyWith(region: value) : item,
                )
                .toList(growable: false);
            _saveBulbs(next);
          },
        ),
      ),
    );
  }

  Future<void> _showSyncGuide() async {
    await showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (context) {
        return ListView(
          padding: const EdgeInsets.fromLTRB(20, 0, 20, 24),
          children: const <Widget>[
            ListTile(
              leading: Icon(Icons.speed),
              title: Text('Max FPS'),
              subtitle: Text(
                'Higher values react faster. WiZ bulbs are capped around 28 updates per second.',
              ),
            ),
            ListTile(
              leading: Icon(Icons.blur_on),
              title: Text('Smoothing'),
              subtitle: Text(
                'Lower is sharper. Higher is calmer and less flickery.',
              ),
            ),
            ListTile(
              leading: Icon(Icons.palette),
              title: Text('Color Boost'),
              subtitle: Text(
                'Raises saturation for stronger ambience. Too high can make scenes look artificial.',
              ),
            ),
            ListTile(
              leading: Icon(Icons.color_lens_outlined),
              title: Text('Color Source'),
              subtitle: Text(
                'Use sRGB for normal apps, Display P3 for OLED wide-gamut content, and Rec.2020 HDR for HDR tests.',
              ),
            ),
            ListTile(
              leading: Icon(Icons.crop_16_9),
              title: Text('Ignore letterbox bars'),
              subtitle: Text(
                'Skips black movie bars so the bulbs follow actual video content.',
              ),
            ),
          ],
        );
      },
    );
  }

  Widget _buildSyncControls() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text('Screen Sync', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 12),
            SegmentedButton<String>(
              segments: const <ButtonSegment<String>>[
                ButtonSegment(value: 'single', label: Text('Single')),
                ButtonSegment(value: 'zones', label: Text('Left + Right')),
              ],
              selected: <String>{_settings.mode},
              onSelectionChanged: (selection) {
                _saveSettings(_settings.copyWith(mode: selection.first));
              },
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<String>(
              initialValue: _settings.sourceColorSpace,
              decoration: const InputDecoration(labelText: 'Color Source'),
              items: const <DropdownMenuItem<String>>[
                DropdownMenuItem(value: 'srgb', child: Text('Standard / sRGB')),
                DropdownMenuItem(
                  value: 'display-p3',
                  child: Text('Display P3 OLED'),
                ),
                DropdownMenuItem(value: 'rec2020', child: Text('Rec.2020 HDR')),
              ],
              onChanged: (value) {
                if (value != null) {
                  _saveSettings(_settings.copyWith(sourceColorSpace: value));
                }
              },
            ),
            SwitchListTile(
              value: _settings.hdrToneMapping,
              onChanged: (value) =>
                  _saveSettings(_settings.copyWith(hdrToneMapping: value)),
              title: const Text('HDR tone mapping'),
            ),
            _buildSlider(
              label: 'Max FPS',
              value: _settings.maxFps.toDouble(),
              min: 8,
              max: 30,
              divisions: 22,
              onChanged: (value) =>
                  _saveSettings(_settings.copyWith(maxFps: value.round())),
            ),
            _buildSlider(
              label: 'Smoothing',
              value: _settings.smoothing,
              min: 0.05,
              max: 0.85,
              divisions: 16,
              onChanged: (value) =>
                  _saveSettings(_settings.copyWith(smoothing: value)),
            ),
            _buildSlider(
              label: 'Color Boost',
              value: _settings.colorBoost,
              min: 1.0,
              max: 1.8,
              divisions: 16,
              onChanged: (value) =>
                  _saveSettings(_settings.copyWith(colorBoost: value)),
            ),
            SwitchListTile(
              value: _settings.ignoreLetterbox,
              onChanged: (value) =>
                  _saveSettings(_settings.copyWith(ignoreLetterbox: value)),
              title: const Text('Ignore letterbox bars'),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildSlider({
    required String label,
    required double value,
    required double min,
    required double max,
    required ValueChanged<double> onChanged,
    int? divisions,
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        const SizedBox(height: 12),
        Text('$label: ${value.toStringAsFixed(value < 2 ? 2 : 0)}'),
        Slider(
          value: value.clamp(min, max),
          min: min,
          max: max,
          divisions: divisions,
          onChanged: onChanged,
        ),
      ],
    );
  }

  Widget _buildRuntimeCard() {
    final stats = _runtimeStats;
    final color = stats?.primaryOutputColor;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text('Runtime', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            Text('Mode: ${stats?.mode ?? '-'}'),
            Text(
              'FPS: ${stats?.currentFps ?? '-'} / requested ${stats?.requestedFps ?? '-'}',
            ),
            Text(
              'Frame: ${stats?.frameTimeMs.toStringAsFixed(1) ?? '-'} ms',
            ),
            Text(
              'Send rate: ${stats?.sendRateHz.toStringAsFixed(1) ?? '-'} Hz',
            ),
            Text('Motion: ${stats?.motionScore.toStringAsFixed(3) ?? '-'}'),
            Text(
              'Smoothing: ${stats?.smoothingFactor.toStringAsFixed(2) ?? '-'}',
            ),
            Text(
              'Color boost: ${stats?.effectiveColorBoost.toStringAsFixed(2) ?? '-'}',
            ),
            Text('Floor: ${stats?.minBrightness ?? '-'}'),
            Text('Delta: ${stats?.deltaThreshold ?? '-'}'),
            Text('Capture: ${stats?.captureBackend ?? '-'}'),
            Text('Extractor: ${stats?.colorAlgorithm ?? '-'}'),
            Text(
                'Color source: ${stats?.sourceColorSpace ?? _settings.sourceColorSpace}'),
            Text(
                'HDR tone map: ${stats?.hdrToneMapping ?? _settings.hdrToneMapping}'),
            Text('Updates: ${stats?.updatesSent ?? 0}'),
            if (color != null) ...<Widget>[
              const SizedBox(height: 12),
              Row(
                children: <Widget>[
                  _buildColorSwatch('Output', color),
                  const SizedBox(width: 12),
                  if (stats?.targetColors.isNotEmpty == true)
                    _buildColorSwatch(
                      'Target',
                      stats!.targetColors.values.first,
                    ),
                ],
              ),
            ],
            if (stats?.lastError != null) Text('Error: ${stats!.lastError}'),
          ],
        ),
      ),
    );
  }

  Widget _buildColorSwatch(String label, Color color) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: <Widget>[
        Text(label, style: Theme.of(context).textTheme.labelSmall),
        const SizedBox(height: 4),
        Container(
          width: 52,
          height: 30,
          decoration: BoxDecoration(
            color: color,
            borderRadius: BorderRadius.circular(6),
            border: Border.all(color: Theme.of(context).dividerColor),
          ),
        ),
      ],
    );
  }

  Widget _buildDiagnosticsCard() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            Text('Diagnostics', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            const Text(
              'Exports a compact JSON report for support and production validation.',
            ),
            const SizedBox(height: 12),
            SizedBox(
              width: double.infinity,
              child: OutlinedButton.icon(
                onPressed: _exportDiagnostics,
                icon: const Icon(Icons.description_outlined),
                label: const Text('Export Diagnostics JSON'),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
