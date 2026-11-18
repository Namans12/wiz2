import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';

import '../models/wiz_bulb.dart';
import '../widgets/remote_slider.dart';
import '../services/screen_capture_service.dart';
import '../services/settings_service.dart';
import '../services/sync_service.dart';
import '../services/wiz_bulb_service.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> with WidgetsBindingObserver {
  final SettingsService _settingsService = SettingsService();
  final WizBulbService _bulbService = WizBulbService();
  late final ScreenCaptureService _captureService;

  List<WizBulb> _bulbs = const <WizBulb>[];
  SyncSettings _settings = const SyncSettings();
  SyncRuntimeStats? _runtimeStats;
  final _syncActionFocus = FocusNode(debugLabel: 'Sync action');
  bool _loading = true;
  bool _syncRunning = false;
  bool _playerRunning = false;
  String _syncBackend = 'screen'; // 'screen' | 'usb'
  String? _status;
  bool _busy = false;
  bool _settingsPending = false;
  String _sessionState = 'stopped';
  String? _lastMediaUri;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _captureService = ScreenCaptureService()
      ..onStats = (stats) {
        if (mounted) {
          setState(() => _runtimeStats = stats);
        }
      };
    _captureService.onSession = _onSession;
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
      _status = _settingsService.warning;
    });
    await _refreshSession();
  }

  Future<bool> _saveBulbs(List<WizBulb> bulbs, {String? turnOffIp}) async {
    if (_transitioning) return false;
    setState(() => _busy = true);
    try {
      final targets = WizBulb.syncTargets(bulbs);
      final previousTargets = WizBulb.syncTargets(_bulbs);
      if ((_syncRunning || _playerRunning) &&
          WizBulb.encodeList(targets) != WizBulb.encodeList(previousTargets)) {
        if (targets.isEmpty) {
          if (_playerRunning) {
            await _captureService.stopMedia();
          } else {
            await _captureService.stopCapture();
          }
          await _refreshSession();
        } else {
          await _captureService.updateTargets(targets);
        }
      }
      await _settingsService.saveBulbs(bulbs);
      if (!mounted) return false;
      setState(() => _bulbs = bulbs.toList(growable: false));
      if (turnOffIp != null) {
        final confirmed = await _bulbService.turnOff(turnOffIp);
        if (mounted) {
          setState(() {
            _status = confirmed
                ? 'Bulb turned off'
                : 'Bulb did not confirm power off';
          });
        }
      }
      return true;
    } catch (error) {
      if (mounted) setState(() => _status = 'Bulb settings: $error');
      return false;
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _saveSettings(SyncSettings settings) async {
    setState(() {
      _settings = settings;
      _settingsPending = _syncRunning || _playerRunning;
    });
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
      if (!await _saveBulbs(WizBulb.merge(_bulbs, discovered))) return;
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
    if (!WizBulb.isValidIp(ip)) {
      setState(() => _status = 'Enter a valid IPv4 bulb address.');
      return;
    }
    final nextBulbs = <WizBulb>[
      _bulbService.fromManualIp(ip, index: _bulbs.length + 1),
    ];
    await _saveBulbs(WizBulb.merge(_bulbs, nextBulbs));
  }

  void _onSession(Map<dynamic, dynamic> session) {
    if (!mounted) return;
    setState(() {
      _sessionState = session['state'] as String? ?? 'stopped';
      final running = _sessionState == 'running';
      _syncRunning = running && session['backend'] == 'screen';
      _playerRunning = running && session['backend'] == 'usb';
      if (running) _syncBackend = session['backend'] as String? ?? 'screen';
      _status = session['error'] as String? ?? 'Sync $_sessionState';
    });
  }

  Future<void> _refreshSession() async {
    try {
      await _captureService.refreshSession();
    } catch (error) {
      if (mounted) {
        setState(() => _status = 'Native status unavailable: $error');
      }
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) _refreshSession();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _captureService.dispose();
    _syncActionFocus.dispose();
    super.dispose();
  }

  bool get _transitioning =>
      _busy || _sessionState == 'starting' || _sessionState == 'stopping';

  List<WizBulb> _requireSyncTargets() {
    if (_bulbs.isEmpty) throw StateError('Add at least one bulb first');
    final targets = WizBulb.syncTargets(_bulbs);
    if (targets.isEmpty) {
      throw StateError('Include at least one bulb in screen sync');
    }
    return targets;
  }

  Future<void> _toggleSync() async {
    if (_transitioning) return;
    setState(() => _busy = true);
    try {
      if (_syncRunning) {
        await _captureService.stopCapture();
        await _refreshSession();
        return;
      }
      if (_playerRunning) {
        throw StateError('Stop the player before starting screen capture');
      }
      final targets = _requireSyncTargets();
      final granted = await _captureService.requestPermission();
      if (!mounted) return;
      if (!granted) throw StateError('Screen capture permission denied');
      setState(() {
        _settingsPending = false;
        _status = 'Starting screen capture...';
      });
      final accepted = await _captureService.startCapture(
          bulbs: targets, settings: _settings);
      if (!accepted) throw StateError('Capture start was rejected');
      await _refreshSession();
    } catch (error) {
      if (mounted) setState(() => _status = 'Screen sync: $error');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _pickAndPlayUsb({String? replayUri}) async {
    if (_transitioning) return;
    setState(() => _busy = true);
    try {
      if (_playerRunning) {
        await _captureService.stopMedia();
        await _refreshSession();
        return;
      }
      if (_syncRunning) {
        throw StateError('Stop screen capture before starting the player');
      }
      final targets = _requireSyncTargets();
      final uri = replayUri ?? await _captureService.pickMedia();
      if (!mounted || uri == null || uri.isEmpty) return;
      _lastMediaUri = uri;
      setState(() {
        _settingsPending = false;
        _status = 'Starting local player...';
      });
      final accepted = await _captureService.playMedia(
          mediaUri: uri, bulbs: targets, settings: _settings);
      if (!accepted) throw StateError('Player start was rejected');
      await _refreshSession();
    } catch (error) {
      if (mounted) setState(() => _status = 'Local player: $error');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _applySettings() async {
    if (_transitioning) return;
    final player = _playerRunning;
    if (player) {
      await _pickAndPlayUsb();
    } else if (_syncRunning) {
      await _toggleSync();
    }
    if (!mounted || _syncRunning || _playerRunning) return;
    if (player) {
      await _pickAndPlayUsb(replayUri: _lastMediaUri);
    } else {
      await _toggleSync();
    }
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

  Widget _pendingSettingsNotice() => Column(children: [
        if (_settingsPending)
          ListTile(
            title: const Text(
                'Settings saved; running sync still uses previous settings.'),
            trailing: FilledButton(
                onPressed: _transitioning ? null : _applySettings,
                child: const Text('Apply / restart')),
          ),
        if (_syncBackend == 'usb')
          const Padding(
            padding: EdgeInsets.all(8),
            child: Text(
                'Local player uses single full-frame sampling. Zone, letterbox and FP16 capture controls apply only to screen capture.'),
          ),
      ]);

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }

    // Build the small settings form eagerly so off-screen controls remain in
    // focus traversal. A lazy ListView can strand a remote at its last child.
    return FocusTraversalGroup(
      policy: ReadingOrderTraversalPolicy(),
      child: Scaffold(
        appBar: AppBar(
          title: const Text('WizLight'),
          actions: <Widget>[
            IconButton(
                onPressed: _discover,
                icon: const Icon(Icons.search),
                tooltip: 'Discover bulbs'),
            IconButton(
                onPressed: _addIp,
                icon: const Icon(Icons.add),
                tooltip: 'Add bulb by IP'),
            IconButton(
                onPressed: _showSyncGuide,
                icon: const Icon(Icons.help_outline),
                tooltip: 'Sync guide'),
          ],
        ),
        body: SafeArea(
          child: LayoutBuilder(builder: (context, constraints) {
            final wide = constraints.maxWidth >= 900;
            return SingleChildScrollView(
              padding: EdgeInsets.all(wide ? 32 : 16),
              child: Center(
                child: ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 960),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Text('Your screen. Your atmosphere.',
                          style: Theme.of(context).textTheme.headlineSmall),
                      const SizedBox(height: 6),
                      const Text(
                          'Remote: Up / Down moves between controls. Left / Right adjusts a slider.'),
                      const SizedBox(height: 20),
                      _pendingSettingsNotice(),
                      _buildSyncHero(),
                      const SizedBox(height: 16),
                      Text('Your lights',
                          style: Theme.of(context).textTheme.titleLarge),
                      const SizedBox(height: 8),
                      ..._bulbs.map(_buildBulbTile),
                      if (_bulbs.isEmpty)
                        const Card(
                            child: ListTile(
                          leading: Icon(Icons.lightbulb_outline),
                          title: Text('No bulbs configured'),
                          subtitle: Text(
                              'Use Discover or + above to add your lights.'),
                        )),
                      const SizedBox(height: 16),
                      _buildSyncControls(),
                      const SizedBox(height: 16),
                      Card(
                          child: ExpansionTile(
                        title: const Text('Live diagnostics'),
                        subtitle: const Text(
                            'Performance, capture details and support report'),
                        children: [
                          _buildRuntimeCard(),
                          _buildDiagnosticsCard()
                        ],
                      )),
                      const SizedBox(height: 16),
                      Align(
                          alignment: Alignment.centerLeft,
                          child: TextButton.icon(
                            onPressed: () {
                              _syncActionFocus.requestFocus();
                              final actionContext = _syncActionFocus.context;
                              if (actionContext != null) {
                                Scrollable.ensureVisible(actionContext,
                                    alignment: 0.1);
                              }
                            },
                            icon: const Icon(Icons.arrow_upward),
                            label: const Text('Back to sync'),
                          )),
                    ],
                  ),
                ),
              ),
            );
          }),
        ),
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
                            : _playerRunning
                                ? 'USB player running'
                                : 'Ready to sync',
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
            if (stats?.captureLooksBlack == true) ...<Widget>[
              const SizedBox(height: 10),
              Container(
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  color: Colors.orange.withValues(alpha: 0.15),
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(color: Colors.orange),
                ),
                child: const Row(
                  children: <Widget>[
                    Icon(Icons.visibility_off, color: Colors.orange),
                    SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        'Capture looks black. The scene may be dark, or this '
                        'app or input may not allow screen capture. For local '
                        'files, you can try USB Media mode.',
                        style: TextStyle(fontSize: 12),
                      ),
                    ),
                  ],
                ),
              ),
            ],
            const SizedBox(height: 14),
            SegmentedButton<String>(
              segments: <ButtonSegment<String>>[
                ButtonSegment(
                  value: 'screen',
                  label: Text(MediaQuery.sizeOf(context).width < 500
                      ? 'Screen'
                      : 'Screen capture'),
                  icon: const Icon(Icons.screenshot_monitor),
                ),
                const ButtonSegment(
                  value: 'usb',
                  label: Text('USB video'),
                  icon: Icon(Icons.usb),
                ),
              ],
              selected: <String>{_syncBackend},
              onSelectionChanged:
                  (_syncRunning || _playerRunning || _transitioning)
                      ? null
                      : (selection) =>
                          setState(() => _syncBackend = selection.first),
            ),
            const SizedBox(height: 6),
            Text(
              _syncBackend == 'usb'
                  ? 'Plays a local USB video and sends one shared colour to the lights. Supports non-DRM files.'
                  : 'Mirrors accessible screen content. Some protected apps and HDMI inputs may not allow capture.',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: 10),
            SizedBox(
              width: double.infinity,
              child: _syncBackend == 'usb'
                  ? FilledButton.icon(
                      autofocus: true,
                      focusNode: _syncActionFocus,
                      onPressed:
                          _transitioning ? null : () => _pickAndPlayUsb(),
                      icon: Icon(
                          _playerRunning ? Icons.stop : Icons.video_library),
                      label: Text(_playerRunning
                          ? 'Stop USB Player'
                          : 'Pick & Play USB Video'),
                    )
                  : FilledButton.icon(
                      autofocus: true,
                      focusNode: _syncActionFocus,
                      onPressed: _transitioning ? null : _toggleSync,
                      icon: Icon(_syncRunning ? Icons.stop : Icons.play_arrow),
                      label: Text(_syncRunning
                          ? 'Stop Screen Sync'
                          : 'Start Screen Sync'),
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
      child: Column(children: [
        ListTile(
          leading: const Icon(Icons.lightbulb),
          title: Text(bulb.name),
          subtitle: Text(bulb.ip),
          trailing: DropdownButton<String>(
            value: bulb.region,
            items: const <DropdownMenuItem<String>>[
              DropdownMenuItem(value: 'left', child: Text('Left')),
              DropdownMenuItem(value: 'right', child: Text('Right')),
              DropdownMenuItem(value: 'top', child: Text('Top')),
              DropdownMenuItem(value: 'bottom', child: Text('Bottom')),
              DropdownMenuItem(value: 'center', child: Text('Center')),
            ],
            onChanged: _transitioning
                ? null
                : (value) {
                    if (value == null) {
                      return;
                    }
                    final next = _bulbs
                        .map(
                          (item) => item.ip == bulb.ip
                              ? item.copyWith(region: value)
                              : item,
                        )
                        .toList(growable: false);
                    _saveBulbs(next);
                  },
          ),
        ),
        Row(
          children: [
            Expanded(
              child: CheckboxListTile(
                title: const Text('Include in screen sync'),
                controlAffinity: ListTileControlAffinity.leading,
                value: bulb.screenSyncEnabled,
                onChanged: _transitioning
                    ? null
                    : (value) {
                        if (value == null) return;
                        _saveBulbs([
                          for (final item in _bulbs)
                            item.ip == bulb.ip
                                ? item.copyWith(screenSyncEnabled: value)
                                : item,
                        ]);
                      },
              ),
            ),
            IconButton(
              tooltip: 'Turn off ${bulb.name}',
              icon: const Icon(Icons.power_settings_new),
              onPressed: _transitioning
                  ? null
                  : () {
                      final syncing = _syncRunning || _playerRunning;
                      _saveBulbs([
                        for (final item in _bulbs)
                          syncing && item.ip == bulb.ip
                              ? item.copyWith(screenSyncEnabled: false)
                              : item,
                      ], turnOffIp: bulb.ip);
                    },
            ),
          ],
        ),
      ]),
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
            Text('Picture & response',
                style: Theme.of(context).textTheme.titleLarge),
            const SizedBox(height: 12),
            SegmentedButton<String>(
              segments: const <ButtonSegment<String>>[
                ButtonSegment(value: 'single', label: Text('Single')),
                ButtonSegment(value: 'zones', label: Text('Regions')),
              ],
              selected: <String>{_settings.mode},
              onSelectionChanged: (selection) {
                _saveSettings(_settings.copyWith(mode: selection.first));
              },
            ),
            const SizedBox(height: 12),
            Text('Sync feel', style: Theme.of(context).textTheme.labelLarge),
            const SizedBox(height: 6),
            SegmentedButton<String>(
              segments: <ButtonSegment<String>>[
                ButtonSegment(
                  value: 'auto',
                  label: const Text('Auto'),
                  icon: MediaQuery.sizeOf(context).width < 500
                      ? null
                      : const Icon(Icons.auto_awesome),
                ),
                ButtonSegment(
                  value: 'cinema',
                  label: const Text('Cinema'),
                  icon: MediaQuery.sizeOf(context).width < 500
                      ? null
                      : const Icon(Icons.theaters),
                ),
                ButtonSegment(
                  value: 'action',
                  label: const Text('Action'),
                  icon: MediaQuery.sizeOf(context).width < 500
                      ? null
                      : const Icon(Icons.sports_esports),
                ),
              ],
              selected: <String>{_settings.syncProfile},
              onSelectionChanged: (selection) => _saveSettings(
                _settings.copyWith(syncProfile: selection.first),
              ),
            ),
            const SizedBox(height: 4),
            Text(
              _settings.syncProfile == 'cinema'
                  ? 'Long, calm transitions for slow movies and dramas.'
                  : _settings.syncProfile == 'action'
                      ? 'Near-instant response for games and fast content.'
                      : 'Adapts between calm and fast based on on-screen motion.',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            SwitchListTile(
              value: _settings.brightnessFollow,
              onChanged: (value) =>
                  _saveSettings(_settings.copyWith(brightnessFollow: value)),
              title: const Text('Follow scene brightness'),
              subtitle: const Text(
                  'Dim scenes dim the bulb; black turns it off; B&W maps to true white.'),
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<String>(
              initialValue: _settings.sourceColorSpace,
              isExpanded: true,
              decoration: const InputDecoration(labelText: 'Color Source'),
              items: const <DropdownMenuItem<String>>[
                DropdownMenuItem(
                  value: 'auto',
                  child: Text('Auto (detect display)'),
                ),
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
            SwitchListTile(
              value: _settings.hdrCapture,
              onChanged: (value) =>
                  _saveSettings(_settings.copyWith(hdrCapture: value)),
              title: const Text('HDR-aware capture (Android 14+)'),
              subtitle: const Text(
                  'FP16 + BT.2020 PQ. Falls back automatically if unsupported.'),
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
    return RemoteSlider(
      label: label,
      value: value,
      min: min,
      max: max,
      divisions: divisions,
      onChanged: onChanged,
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
            Text('Device profile: ${stats?.deviceProfile ?? '-'}'),
            Text(
                'Detected gamut: ${stats?.detectedDisplayGamut ?? '-'}${(stats?.detectedHdrSupport ?? false) ? ' (HDR)' : ''}'),
            Text(
                'HDR types: ${(stats?.supportedHdrTypes.isEmpty ?? true) ? '-' : stats!.supportedHdrTypes.join(', ')}'),
            Text('Primary HDR: ${stats?.primaryHdrType ?? '-'}'),
            Text(
                'Capture buffer: ${stats?.captureBufferFormat ?? '-'}${(stats?.hdrCaptureActive ?? false) ? ' (HDR active)' : ''}'),
            Text('Updates queued: ${stats?.updatesSent ?? 0}'),
            Text('Scene cuts: ${stats?.cutsDetected ?? 0}'),
            Text('Profile: ${stats?.syncProfile ?? _settings.syncProfile}'),
            Text(
                'Scene: ${((stats?.sceneNeutral ?? 0) * 100).toStringAsFixed(0)}% neutral, luma ${(stats?.sceneLuma ?? 0).toStringAsFixed(2)}'),
            Text('Frames: ${stats?.framesProcessed ?? 0}'),
            Text('Replayed frames: ${stats?.replayedFrames ?? 0}'),
            Text(
                'Since last new frame: ${stats?.freshFrameAgeMs == null ? '-' : '${stats!.freshFrameAgeMs} ms'}'),
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
