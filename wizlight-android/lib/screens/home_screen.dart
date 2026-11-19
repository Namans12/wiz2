import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../models/wiz_bulb.dart';
import '../services/screen_capture_service.dart';
import '../services/settings_service.dart';
import '../services/sync_service.dart';
import '../services/wiz_bulb_service.dart';
import '../widgets/focus_ring.dart';
import '../widgets/lamp_area_menu.dart';
import '../widgets/remote_slider.dart';
import '../widgets/wizlight_backdrop.dart';

enum _LampSetupAction { discover, addIp }

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key, this.bulbService});

  final WizBulbService? bulbService;

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> with WidgetsBindingObserver {
  final SettingsService _settingsService = SettingsService();
  late final WizBulbService _bulbService =
      widget.bulbService ?? WizBulbService();
  late final ScreenCaptureService _captureService;

  List<WizBulb> _bulbs = const <WizBulb>[];
  SyncSettings _settings = const SyncSettings();
  SyncRuntimeStats? _runtimeStats;
  final _syncActionFocus = FocusNode(debugLabel: 'Sync action');
  final _bodyFocus = FocusNode(
      debugLabel: 'Body', canRequestFocus: false, skipTraversal: true);
  final _headerFocus = <FocusNode>[
    FocusNode(debugLabel: 'Header: Discover'),
    FocusNode(debugLabel: 'Header: Add'),
    FocusNode(debugLabel: 'Header: Help'),
  ];
  static const _diagnosticsDuration = Duration(milliseconds: 200);
  final _diagnosticsBody = GlobalKey();
  Timer? _diagnosticsScroll;
  Timer? _lampHighlightTimer;
  bool _highlightLampActions = false;
  bool _hdrFallbackShown = false;
  bool _playingPending = false;
  bool _pendingAutoRetry = false;
  String? _pendingMediaUri;
  bool _loading = true;
  bool _syncRunning = false;
  bool _playerRunning = false;
  String _syncBackend = 'screen'; // 'screen' | 'usb'
  String? _status;
  bool _busy = false;
  bool _restartNeeded = false;
  String? _runningSessionId;
  int _settingsRevision = 0;
  int? _settingsRevisionAtStart;
  int _sentSettingsRevision = 0;
  int _savedSettingsRevision = 0;
  bool _applyingLiveSettings = false;
  bool _savingSettings = false;
  Timer? _settingsSaveTimer;
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
          if (stats.hdrFallback) _noteHdrFallback();
        }
      };
    _captureService.onSession = _onSession;
    _captureService.onPendingMedia = _checkPendingMedia;
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
    await _checkPendingMedia();
  }

  Future<bool> _saveBulbs(List<WizBulb> bulbs, {String? turnOffIp}) async {
    if (_transitioning) return false;
    setState(() => _busy = true);
    try {
      final targets = WizBulb.syncTargets(bulbs);
      final previousTargets = WizBulb.syncTargets(_bulbs);
      var stoppedIps = const <String>[];
      if ((_syncRunning || _playerRunning) &&
          WizBulb.encodeList(targets) != WizBulb.encodeList(previousTargets)) {
        if (targets.isEmpty) {
          if (_playerRunning) {
            await _captureService.stopMedia();
          } else {
            await _captureService.stopCapture();
          }
          await _refreshSession();
          stoppedIps = previousTargets.map((bulb) => bulb.ip).toList();
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
      // The last selected light left the session: switch it off, not freeze it.
      for (final ip in stoppedIps) {
        if (ip == turnOffIp) continue;
        final confirmed = await _bulbService.turnOff(ip);
        if (mounted) {
          setState(() {
            _status = confirmed
                ? 'Bulb turned off'
                : 'Bulb did not confirm power off';
          });
        }
      }
      if (mounted) {
        setState(() => _busy = false);
        _retryPendingSoon();
      }
      return true;
    } catch (error) {
      if (mounted) setState(() => _status = 'Bulb settings: $error');
      return false;
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _changeSettings(SyncSettings settings, {bool debounceSave = false}) {
    setState(() {
      _settings = settings;
      _settingsRevision++;
    });
    unawaited(_applyLiveSettings());
    if (debounceSave) {
      _settingsSaveTimer?.cancel();
      _settingsSaveTimer = Timer(
        const Duration(milliseconds: 300),
        _flushSettingsSave,
      );
    } else {
      unawaited(_flushSettingsSave());
    }
  }

  Future<void> _flushSettingsSave() async {
    _settingsSaveTimer?.cancel();
    _settingsSaveTimer = null;
    if (_savingSettings) return;
    _savingSettings = true;
    try {
      // A newer drag can restart the debounce while a write is in flight.
      while (_settingsSaveTimer == null &&
          _savedSettingsRevision != _settingsRevision) {
        final revision = _settingsRevision;
        try {
          await _settingsService.saveSyncSettings(_settings);
          _savedSettingsRevision = revision;
        } catch (error) {
          if (mounted) {
            setState(() => _status = 'Could not save settings: $error');
          }
          if (revision == _settingsRevision) break;
        }
      }
    } finally {
      _savingSettings = false;
    }
  }

  Future<void> _applyLiveSettings() async {
    if (_applyingLiveSettings) return;
    _applyingLiveSettings = true;
    try {
      var sessionId = _runningSessionId;
      while (sessionId != null && _sentSettingsRevision != _settingsRevision) {
        final revision = _settingsRevision;
        _sentSettingsRevision = revision;
        // Read the latest snapshot only when the preceding update is done.
        try {
          final restart = await _captureService.updateSettings(_settings);
          if (mounted &&
              sessionId == _runningSessionId &&
              revision == _settingsRevision) {
            final needed = restart?.isNotEmpty ?? false;
            if (needed != _restartNeeded) {
              setState(() => _restartNeeded = needed);
            }
          }
        } catch (error) {
          if (mounted &&
              sessionId == _runningSessionId &&
              revision == _settingsRevision) {
            setState(() => _status = 'Could not apply live settings: $error');
          }
        }
        sessionId = _runningSessionId;
      }
    } finally {
      _applyingLiveSettings = false;
    }
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

  Future<void> _showLampSetup() async {
    _lampHighlightTimer?.cancel();
    setState(() => _highlightLampActions = true);
    _lampHighlightTimer = Timer(const Duration(seconds: 5), () {
      setState(() => _highlightLampActions = false);
    });
    final action = await showDialog<_LampSetupAction>(
      context: context,
      barrierColor: const Color(0x66000000),
      builder: (context) => AlertDialog(
        icon: const Icon(Icons.lightbulb_outline),
        title: const Text('Add your lamps first'),
        content: const Text(
          'Discover finds lamps on your Wi-Fi network. Use + to add a lamp by IP address.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('Close'),
          ),
          OutlinedButton.icon(
            onPressed: () => Navigator.pop(context, _LampSetupAction.addIp),
            icon: const Icon(Icons.add),
            label: const Text('Add by IP'),
          ),
          FilledButton.icon(
            autofocus: true,
            onPressed: () => Navigator.pop(context, _LampSetupAction.discover),
            icon: const Icon(Icons.search),
            label: const Text('Discover'),
          ),
        ],
      ),
    );
    if (!mounted) return;
    switch (action) {
      case _LampSetupAction.discover:
        await _discover();
      case _LampSetupAction.addIp:
        await _addIp();
      case null:
        break;
    }
  }

  void _onSession(Map<dynamic, dynamic> session) {
    if (!mounted) return;
    setState(() {
      _sessionState = session['state'] as String? ?? 'stopped';
      final running = _sessionState == 'running';
      final sessionId =
          running ? (session['sessionId'] as String? ?? '') : null;
      if (_runningSessionId != sessionId) {
        _restartNeeded = false;
        if (sessionId != null) {
          _sentSettingsRevision = _settingsRevisionAtStart ?? 0;
          _settingsRevisionAtStart = null;
        }
      }
      _runningSessionId = sessionId;
      _syncRunning = running && session['backend'] == 'screen';
      _playerRunning = running && session['backend'] == 'usb';
      if (running) _syncBackend = session['backend'] as String? ?? 'screen';
      _status = session['error'] as String? ?? 'Sync $_sessionState';
      if (_sessionState == 'stopped' || _sessionState == 'error') {
        _settingsRevisionAtStart = null;
        // Stale numbers from the previous run must not look live.
        _runtimeStats = null;
      }
    });
    unawaited(_applyLiveSettings());
    if (session['hdrFallback'] == true) _noteHdrFallback();
    _retryPendingSoon();
  }

  void _noteHdrFallback() {
    if (_hdrFallbackShown || !mounted) return;
    _hdrFallbackShown = true;
    ScaffoldMessenger.maybeOf(context)?.showSnackBar(const SnackBar(
        content: Text(
            "HDR capture isn't supported on this device - using standard capture.")));
  }

  /// Collects a video opened from outside the app ("Open with"). The URI is
  /// kept in [_pendingMediaUri] until the player actually accepts it.
  Future<void> _checkPendingMedia() async {
    if (!mounted) return;
    final uri = await _captureService.takePendingMediaUri();
    if (uri != null && mounted) {
      setState(() {
        _pendingMediaUri = uri;
        _syncBackend = 'usb';
      });
    }
    await _playPending();
  }

  String get _pendingMediaName {
    var uri = _pendingMediaUri ?? '';
    try {
      uri = Uri.decodeFull(uri);
    } catch (_) {}
    final tail =
        uri.split(RegExp(r'[/:]')).where((e) => e.isNotEmpty).lastOrNull;
    return tail ?? 'video';
  }

  void _retryPendingSoon() {
    if (_pendingMediaUri == null || !_pendingAutoRetry || !mounted) return;
    if (_transitioning || _playingPending) return;
    _pendingAutoRetry = false;
    Future<void>.microtask(_playPending);
  }

  Future<void> _playPending() async {
    final uri = _pendingMediaUri;
    if (uri == null || _playingPending || !mounted) return;
    _playingPending = true;
    try {
      for (var i = 0; i < 20 && _transitioning && mounted; i++) {
        await Future<void>.delayed(const Duration(milliseconds: 250));
      }
      if (!mounted) return;
      if (_transitioning) {
        _pendingAutoRetry = true; // retried when the transition finishes
        return;
      }
      if (WizBulb.syncTargets(_bulbs).isEmpty) {
        final name = _pendingMediaName;
        _pendingAutoRetry = true; // retried once bulbs are added
        setState(() => _status = _bulbs.isEmpty
            ? 'Add at least one bulb first, then play $name.'
            : 'Include at least one bulb in screen sync, then play $name.');
        ScaffoldMessenger.maybeOf(context)
          ?..hideCurrentSnackBar()
          ..showSnackBar(SnackBar(
              content: Text('Add a bulb to play $name.'),
              action: SnackBarAction(
                  label: 'Play $name', onPressed: _playPending)));
        return;
      }
      setState(() => _syncBackend = 'usb');
      try {
        if (_playerRunning) {
          await _captureService.stopMedia();
          await _refreshSession();
        } else if (_syncRunning) {
          await _captureService.stopCapture();
          await _refreshSession();
        }
      } catch (error) {
        if (mounted) setState(() => _status = 'Local player: $error');
        return;
      }
      if (!mounted) return;
      final played = await _pickAndPlayUsb(replayUri: uri);
      if (!mounted) return;
      if (played && _pendingMediaUri == uri) {
        setState(() => _pendingMediaUri = null);
        ScaffoldMessenger.maybeOf(context)?.hideCurrentSnackBar();
      }
    } finally {
      _playingPending = false;
    }
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
    if (state == AppLifecycleState.resumed) {
      _refreshSession().then((_) => _checkPendingMedia());
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _runningSessionId = null;
    unawaited(_flushSettingsSave());
    _captureService.dispose();
    _syncActionFocus.dispose();
    _bodyFocus.dispose();
    _diagnosticsScroll?.cancel();
    _lampHighlightTimer?.cancel();
    for (final node in _headerFocus) {
      node.dispose();
    }
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
    if (!_syncRunning && !_playerRunning && _bulbs.isEmpty) {
      await _showLampSetup();
      return;
    }
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
        _restartNeeded = false;
        _status = 'Starting screen capture...';
      });
      _settingsRevisionAtStart = _settingsRevision;
      final accepted = await _captureService.startCapture(
          bulbs: targets, settings: _settings);
      if (!accepted) throw StateError('Capture start was rejected');
      await _refreshSession();
    } catch (error) {
      _settingsRevisionAtStart = null;
      if (mounted) setState(() => _status = 'Screen sync: $error');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<bool> _pickAndPlayUsb({String? replayUri}) async {
    if (_transitioning) return false;
    if (!_playerRunning && !_syncRunning && _bulbs.isEmpty) {
      await _showLampSetup();
      return false;
    }
    setState(() => _busy = true);
    try {
      if (_playerRunning) {
        await _captureService.stopMedia();
        await _refreshSession();
        return false;
      }
      if (_syncRunning) {
        throw StateError('Stop screen capture before starting the player');
      }
      final targets = _requireSyncTargets();
      final uri = replayUri ?? await _captureService.pickMedia();
      if (!mounted || uri == null || uri.isEmpty) return false;
      _lastMediaUri = uri;
      setState(() {
        _restartNeeded = false;
        _status = 'Starting local player...';
      });
      _settingsRevisionAtStart = _settingsRevision;
      final accepted = await _captureService.playMedia(
          mediaUri: uri, bulbs: targets, settings: _settings);
      if (!accepted) throw StateError('Player start was rejected');
      await _refreshSession();
      return true;
    } on PlatformException catch (error) {
      _settingsRevisionAtStart = null;
      if (mounted) {
        setState(() => _status = error.code == 'NO_PICKER'
            ? "No file picker on this device. Open the video from your TV's file browser and choose WizLight."
            : 'Local player: ${error.message ?? error.code}');
      }
    } catch (error) {
      _settingsRevisionAtStart = null;
      if (mounted) setState(() => _status = 'Local player: $error');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
    return false;
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
        if (_restartNeeded)
          ListTile(
            title: const Text('Restart needed'),
            subtitle: const Text('Restart to apply HDR-aware capture changes.'),
            trailing: FilledButton(
                onPressed: _transitioning ? null : _applySettings,
                child: const Text('Restart sync')),
          ),
        if (_syncBackend == 'usb')
          const Padding(
            padding: EdgeInsets.all(8),
            child: Text(
                'USB video uses Single mode. Regions, letterbox and HDR-aware capture apply to screen capture.'),
          ),
      ]);

  @override
  Widget build(BuildContext context) {
    return Stack(
      fit: StackFit.expand,
      children: [const WizLightBackdrop(), _buildPage()],
    );
  }

  Widget _buildPage() {
    if (_loading) {
      return const Scaffold(
        backgroundColor: Colors.transparent,
        body: Center(child: CircularProgressIndicator()),
      );
    }

    // Build the small settings form eagerly so off-screen controls remain in
    // focus traversal. A lazy ListView can strand a remote at its last child.
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (didPop) return;
        if (!_syncActionFocus.hasFocus && _syncActionFocus.context != null) {
          _focusSyncAction();
        } else {
          SystemNavigator.pop();
        }
      },
      child: Scaffold(
        backgroundColor: Colors.transparent,
        appBar: AppBar(
          backgroundColor: Colors.transparent,
          title: Row(children: [
            Image.asset(
              'assets/branding/wizlight_mark.png',
              width: 32,
              height: 32,
              excludeFromSemantics: true,
              errorBuilder: (_, __, ___) =>
                  const Icon(Icons.lightbulb_outline, size: 32),
            ),
            const SizedBox(width: 10),
            const Flexible(child: Text('WizLight')),
          ]),
          actions: <Widget>[_buildHeaderActions()],
        ),
        body: SafeArea(
          child: LayoutBuilder(builder: (context, constraints) {
            final wide = constraints.maxWidth >= 900;
            return FocusTraversalGroup(
                policy: ReadingOrderTraversalPolicy(),
                child: Focus(
                    focusNode: _bodyFocus,
                    child: Shortcuts(
                        shortcuts: const <ShortcutActivator, Intent>{
                          SingleActivator(LogicalKeyboardKey.arrowUp):
                              PreviousFocusIntent(),
                          SingleActivator(LogicalKeyboardKey.arrowDown):
                              NextFocusIntent(),
                        },
                        child: Actions(
                            actions: <Type, Action<Intent>>{
                              PreviousFocusIntent:
                                  CallbackAction<PreviousFocusIntent>(
                                      onInvoke: (_) => _moveVertical(-1)),
                              NextFocusIntent: CallbackAction<NextFocusIntent>(
                                  onInvoke: (_) => _moveVertical(1)),
                              DirectionalFocusIntent:
                                  CallbackAction<DirectionalFocusIntent>(
                                      onInvoke: (intent) =>
                                          _moveHorizontal(intent.direction)),
                            },
                            child: SingleChildScrollView(
                              padding: EdgeInsets.all(wide ? 32 : 16),
                              child: Center(
                                child: ConstrainedBox(
                                  constraints:
                                      const BoxConstraints(maxWidth: 960),
                                  child: Column(
                                    crossAxisAlignment:
                                        CrossAxisAlignment.stretch,
                                    children: [
                                      _pendingSettingsNotice(),
                                      _buildSyncHero(),
                                      const SizedBox(height: 16),
                                      _buildLights(),
                                      const SizedBox(height: 16),
                                      _buildSyncControls(),
                                      const SizedBox(height: 16),
                                      _buildLiveDiagnostics(),
                                      const SizedBox(height: 16),
                                      Align(
                                          alignment: Alignment.centerLeft,
                                          child: TextButton.icon(
                                            onPressed: _focusSyncAction,
                                            icon:
                                                const Icon(Icons.arrow_upward),
                                            label: const Text('Back to sync'),
                                          )),
                                    ],
                                  ),
                                ),
                              ),
                            )))));
          }),
        ),
      ),
    );
  }

  Widget _sectionTitle(IconData icon, String title) {
    return Row(children: [
      Icon(icon, size: 20, color: Theme.of(context).colorScheme.primary),
      const SizedBox(width: 10),
      Expanded(
          child: Text(title, style: Theme.of(context).textTheme.titleLarge)),
    ]);
  }

  Widget _buildLights() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            _sectionTitle(Icons.lightbulb_outline, 'Lights'),
            const SizedBox(height: 12),
            if (_bulbs.isEmpty)
              const Card(
                child: ListTile(
                  title: Text('No lamps added'),
                  subtitle:
                      Text('Tap Discover to find lamps or + to add one by IP.'),
                ),
              ),
            for (final bulb in _bulbs) ...[
              if (bulb != _bulbs.first) const SizedBox(height: 12),
              _buildBulbTile(bulb),
            ],
          ],
        ),
      ),
    );
  }

  Widget _buildLiveDiagnostics() {
    return Card(
      child: FocusRing(
        radius: 22,
        child: ExpansionTile(
          leading: Icon(Icons.monitor_heart_outlined,
              color: Theme.of(context).colorScheme.primary, size: 20),
          title: const Text('Live diagnostics'),
          tilePadding: const EdgeInsets.symmetric(horizontal: 16),
          childrenPadding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
          shape: const Border(),
          collapsedShape: const Border(),
          expansionAnimationStyle:
              const AnimationStyle(duration: _diagnosticsDuration),
          onExpansionChanged: _revealDiagnostics,
          children: [
            Column(
              key: _diagnosticsBody,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                _buildRuntimeCard(),
                const SizedBox(height: 12),
                _buildDiagnosticsCard(),
              ],
            ),
          ],
        ),
      ),
    );
  }

  void _revealDiagnostics(bool expanded) {
    _diagnosticsScroll?.cancel();
    if (!expanded) return;
    _diagnosticsScroll = Timer(_diagnosticsDuration, () {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        final body = _diagnosticsBody.currentContext;
        if (body == null) return;
        Scrollable.ensureVisible(body,
            duration: const Duration(milliseconds: 220), curve: Curves.easeOut);
      });
      WidgetsBinding.instance.ensureVisualUpdate();
    });
  }

  void _focusSyncAction() {
    _syncActionFocus.requestFocus();
    final ctx = _syncActionFocus.context;
    if (ctx != null) Scrollable.ensureVisible(ctx, alignment: 0.1);
  }

  /// Header icons: their own group. Left/Right move between them only, Down
  /// returns to the Start/Stop button, Up does nothing (already at the top).
  Widget _buildHeaderActions() {
    KeyEventResult onKey(FocusNode _, KeyEvent event) {
      if (event is! KeyDownEvent && event is! KeyRepeatEvent) {
        return KeyEventResult.ignored;
      }
      final key = event.logicalKey;
      final index = _headerFocus.indexWhere((node) => node.hasFocus);
      if (key == LogicalKeyboardKey.arrowDown) {
        _focusSyncAction();
        return KeyEventResult.handled;
      }
      if (key == LogicalKeyboardKey.arrowUp) return KeyEventResult.handled;
      if (index >= 0 &&
          (key == LogicalKeyboardKey.arrowLeft ||
              key == LogicalKeyboardKey.arrowRight)) {
        final next = index + (key == LogicalKeyboardKey.arrowRight ? 1 : -1);
        if (next >= 0 && next < _headerFocus.length) {
          _headerFocus[next].requestFocus();
        }
        return KeyEventResult.handled;
      }
      return KeyEventResult.ignored;
    }

    return FocusTraversalGroup(
      policy: ReadingOrderTraversalPolicy(),
      child: Focus(
        canRequestFocus: false,
        skipTraversal: true,
        onKeyEvent: onKey,
        child: Row(mainAxisSize: MainAxisSize.min, children: <Widget>[
          _lampHeaderAction(IconButton(
              focusNode: _headerFocus[0],
              onPressed: _discover,
              icon: const Icon(Icons.search),
              tooltip: 'Discover bulbs')),
          _lampHeaderAction(IconButton(
              focusNode: _headerFocus[1],
              onPressed: _addIp,
              icon: const Icon(Icons.add),
              tooltip: 'Add bulb by IP')),
          IconButton(
              focusNode: _headerFocus[2],
              onPressed: _showSyncGuide,
              icon: const Icon(Icons.help_outline),
              tooltip: 'Sync guide'),
        ]),
      ),
    );
  }

  Widget _lampHeaderAction(IconButton button) {
    final accent = Theme.of(context).colorScheme.primary;
    return AnimatedContainer(
      duration: const Duration(milliseconds: 220),
      foregroundDecoration: BoxDecoration(
        shape: BoxShape.circle,
        border: Border.all(
          width: 2,
          color: _highlightLampActions ? accent : Colors.transparent,
        ),
      ),
      child: button,
    );
  }

  bool _isOffstage(BuildContext context) {
    var hidden = false;
    context.visitAncestorElements((element) {
      final widget = element.widget;
      if (widget is Offstage && widget.offstage) {
        hidden = true;
        return false;
      }
      return true;
    });
    return hidden;
  }

  List<FocusNode> _bodyStops() => [for (final row in _bodyRows()) ...row];

  /// Body stops grouped into visual rows (top to bottom, left to right).
  List<List<FocusNode>> _bodyRows() {
    final nodes = _bodyFocus.traversalDescendants
        .where((node) =>
            node.context != null &&
            node.context!.mounted &&
            node.rect.height > 0 &&
            !_isOffstage(node.context!))
        .toList()
      ..sort((a, b) => a.rect.top.compareTo(b.rect.top));
    final rows = <List<FocusNode>>[];
    for (final node in nodes) {
      final r = node.rect;
      if (rows.isNotEmpty &&
          r.center.dy >= rows.last.first.rect.top &&
          r.center.dy <= rows.last.first.rect.bottom) {
        rows.last.add(node);
      } else {
        rows.add([node]);
      }
    }
    for (final row in rows) {
      row.sort((a, b) => a.rect.left.compareTo(b.rect.left));
    }
    return rows;
  }

  void _focusNode(FocusNode node) {
    node.requestFocus();
    final ctx = node.context;
    if (ctx != null) {
      Scrollable.ensureVisible(ctx,
          alignment: 0.5, duration: const Duration(milliseconds: 120));
    }
  }

  /// Up/Down: previous/next visual row in reading order (column kept as close
  /// as possible). Down stops at the last row; Up from the first row
  /// deliberately enters the header.
  Object? _moveVertical(int delta) {
    final rows = _bodyRows();
    final current = FocusManager.instance.primaryFocus;
    final rowIndex =
        current == null ? -1 : rows.indexWhere((row) => row.contains(current));
    if (rowIndex < 0) {
      if (rows.isNotEmpty) {
        _focusNode(delta > 0 ? rows.first.first : rows.last.first);
      }
      return null;
    }
    final next = rowIndex + delta;
    if (next < 0) {
      _headerFocus.first.requestFocus();
    } else if (next < rows.length) {
      // Pick the stop in the next row closest to the current column.
      final x = current!.rect.center.dx;
      final target = rows[next].reduce((a, b) =>
          (a.rect.center.dx - x).abs() <= (b.rect.center.dx - x).abs() ? a : b);
      _focusNode(target);
    }
    return null;
  }

  /// Left/Right: nearest stop in the same visual row, otherwise stay put.
  Object? _moveHorizontal(TraversalDirection direction) {
    final current = FocusManager.instance.primaryFocus;
    if (current == null) return null;
    final here = current.rect;
    final sign = direction == TraversalDirection.right ? 1 : -1;
    FocusNode? best;
    var bestDistance = double.infinity;
    for (final node in _bodyStops()) {
      if (node == current) continue;
      final r = node.rect;
      if (r.center.dy < here.top || r.center.dy > here.bottom) continue;
      final distance = (r.center.dx - here.center.dx) * sign;
      if (distance > 0 && distance < bestDistance) {
        best = node;
        bestDistance = distance;
      }
    }
    if (best != null) _focusNode(best);
    return null;
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
                  _playerRunning ? Icons.usb : Icons.sync,
                  size: 20,
                  color: Theme.of(context).colorScheme.primary,
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
                        style: Theme.of(context).textTheme.titleLarge,
                      ),
                      Text(
                        _status ?? '${_bulbs.length} lamps added',
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
            if (stats != null) ...[
              const SizedBox(height: 14),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  _buildStatChip('FPS', '${stats.currentFps}'),
                  _buildStatChip(
                      'Send', '${stats.sendRateHz.toStringAsFixed(1)} Hz'),
                  _buildStatChip(
                      'Motion', stats.motionScore.toStringAsFixed(3)),
                  _buildStatChip(
                      'Mode', stats.mode == 'zones' ? 'Regions' : 'Single'),
                  _buildStatChip('Color', stats.sourceColorSpace),
                ],
              ),
            ],
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
                        'Screen looks black. Netflix, Hotstar, Sony LIV and '
                        'some TV players block screen capture (protected '
                        'video). For your own videos use Play from USB.',
                        style: TextStyle(fontSize: 12),
                      ),
                    ),
                  ],
                ),
              ),
            ],
            if (_sendFailure != null) ...<Widget>[
              const SizedBox(height: 10),
              Row(children: <Widget>[
                const Icon(Icons.wifi_off, color: Colors.orange),
                const SizedBox(width: 10),
                Expanded(
                    child: Text("Can't reach the lamp: $_sendFailure",
                        style: const TextStyle(fontSize: 12))),
              ]),
            ],
            if (_pendingMediaUri != null) ...<Widget>[
              const SizedBox(height: 10),
              SizedBox(
                width: double.infinity,
                child: FilledButton.tonalIcon(
                  onPressed: _transitioning ? null : _playPending,
                  icon: const Icon(Icons.play_circle_outline),
                  label: Text('Play $_pendingMediaName',
                      overflow: TextOverflow.ellipsis),
                ),
              ),
            ],
            const SizedBox(height: 14),
            SegmentedButton<String>(
              showSelectedIcon: false,
              expandedInsets: EdgeInsets.zero,
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
                  ? 'Play an unprotected video from USB.'
                  : 'Sync lamps to your screen. Some apps and HDMI inputs block capture.',
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

  String? get _sendFailure {
    final error = _runtimeStats?.sendError;
    return (error == null || error.isEmpty) ? null : error;
  }

  Widget _buildStatChip(String label, String value) {
    return Chip(
      label: Text('$label: $value'),
      visualDensity: VisualDensity.compact,
    );
  }

  Widget _buildBulbTile(WizBulb bulb) {
    final inRegions = _settings.mode == 'zones';
    return Card(
      key: ValueKey(bulb.ip),
      child: Column(children: [
        ListTile(
          leading: const Icon(Icons.lightbulb),
          title: Text(bulb.name),
          subtitle: Text(bulb.ip),
        ),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  inRegions ? 'Screen area' : 'Used in Regions mode',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ),
              const SizedBox(width: 12),
              Flexible(
                child: LampAreaMenu(
                  value: bulb.region,
                  busy: _transitioning,
                  onChanged: !inRegions
                      ? null
                      : (value) => _saveBulbs([
                            for (final item in _bulbs)
                              item.ip == bulb.ip
                                  ? item.copyWith(region: value)
                                  : item,
                          ]),
                ),
              ),
            ],
          ),
        ),
        Row(
          children: [
            Expanded(
              child: FocusRing(
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
              )),
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

  Widget _ringedSwitch({
    required bool value,
    required ValueChanged<bool>? onChanged,
    required Widget title,
    Widget? subtitle,
  }) {
    return FocusRing(
        child: SwitchListTile(
            value: value,
            onChanged: onChanged,
            title: title,
            subtitle: subtitle));
  }

  Future<void> _showModeInfo() => showDialog<void>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('Single and Regions'),
          content: const Text(
            'Single uses one colour from the whole screen for every lamp. '
            'Lamp areas are ignored.\n\n'
            'Regions gives each lamp the colour of its assigned screen area '
            '(left, right, top, bottom or centre). Use Regions when your lamps '
            'sit on different sides of the screen.',
          ),
          scrollable: true,
          actions: [
            TextButton(
              autofocus: true,
              onPressed: () => Navigator.pop(context),
              child: const Text('Close'),
            ),
          ],
        ),
      );

  Widget _buildSyncControls() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            _sectionTitle(Icons.tune, 'Picture & response'),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: SegmentedButton<String>(
                    showSelectedIcon: false,
                    expandedInsets: EdgeInsets.zero,
                    style: const ButtonStyle(
                        padding: WidgetStatePropertyAll(
                      EdgeInsets.symmetric(horizontal: 6),
                    )),
                    segments: const <ButtonSegment<String>>[
                      ButtonSegment(
                        value: 'single',
                        label: Text('Single'),
                        icon: Icon(Icons.lightbulb_outline),
                      ),
                      ButtonSegment(
                        value: 'zones',
                        label: Text('Regions'),
                        icon: Icon(Icons.view_column),
                      ),
                    ],
                    selected: <String>{_settings.mode},
                    onSelectionChanged: (selection) {
                      _changeSettings(
                          _settings.copyWith(mode: selection.first));
                    },
                  ),
                ),
                const SizedBox(width: 4),
                IconButton(
                  onPressed: _showModeInfo,
                  icon: const Icon(Icons.info_outline),
                  tooltip: 'About Single and Regions',
                ),
              ],
            ),
            const SizedBox(height: 12),
            Text('Sync feel', style: Theme.of(context).textTheme.labelLarge),
            const SizedBox(height: 6),
            SegmentedButton<String>(
              showSelectedIcon: false,
              expandedInsets: EdgeInsets.zero,
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
              onSelectionChanged: (selection) => _changeSettings(
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
            _ringedSwitch(
              value: _settings.brightnessFollow,
              onChanged: (value) =>
                  _changeSettings(_settings.copyWith(brightnessFollow: value)),
              title: const Text('Follow scene brightness'),
              subtitle: const Text(
                  'Dim scenes dim the bulb; black turns it off; B&W maps to true white.'),
            ),
            const SizedBox(height: 12),
            FocusRing(
                child: DropdownButtonFormField<String>(
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
                  _changeSettings(_settings.copyWith(sourceColorSpace: value));
                }
              },
            )),
            _ringedSwitch(
              value: _settings.hdrToneMapping,
              onChanged: _settings.sourceColorSpace == 'auto'
                  ? null
                  : (value) => _changeSettings(
                      _settings.copyWith(hdrToneMapping: value)),
              title: const Text('HDR tone mapping'),
              subtitle: const Text(
                'Not used when Source colour space is Auto.',
              ),
            ),
            _ringedSwitch(
              value: _settings.hdrCapture,
              onChanged: (value) =>
                  _changeSettings(_settings.copyWith(hdrCapture: value)),
              title: const Text('HDR-aware capture (Android 14+)'),
              subtitle: const Text(
                  'FP16 + BT.2020 PQ. Falls back automatically if unsupported.'),
            ),
            _buildSlider(
              label: 'Max FPS',
              value: _settings.maxFps.toDouble(),
              min: _settings.minFps.toDouble(),
              max: 30,
              divisions:
                  30 - _settings.minFps > 0 ? 30 - _settings.minFps : null,
              settingsForValue: (value) =>
                  _settings.copyWith(maxFps: value.round()),
            ),
            _buildSlider(
              label: 'Smoothing',
              value: _settings.smoothing,
              min: 0.05,
              max: 0.85,
              divisions: 16,
              settingsForValue: (value) => _settings.copyWith(smoothing: value),
            ),
            _buildSlider(
              label: 'Color Boost',
              value: _settings.colorBoost,
              min: 1.0,
              max: 1.8,
              divisions: 16,
              settingsForValue: (value) =>
                  _settings.copyWith(colorBoost: value),
            ),
            _buildSlider(
              label: 'Minimum brightness',
              value: _settings.minBrightness.clamp(10, 60).toDouble(),
              min: 10,
              max: 60,
              divisions: 10,
              settingsForValue: (value) =>
                  _settings.copyWith(minBrightness: value.round()),
            ),
            _ringedSwitch(
              value: _settings.ignoreLetterbox,
              onChanged: (value) =>
                  _changeSettings(_settings.copyWith(ignoreLetterbox: value)),
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
    required SyncSettings Function(double) settingsForValue,
    int? divisions,
  }) {
    return RemoteSlider(
      label: label,
      value: value,
      min: min,
      max: max,
      divisions: divisions,
      onChanged: (value) =>
          _changeSettings(settingsForValue(value), debounceSave: true),
      onChangeEnd: (_) {
        unawaited(_applyLiveSettings());
        unawaited(_flushSettingsSave());
      },
    );
  }

  Widget _buildRuntimeCard() {
    final stats = _runtimeStats;
    final color = stats?.primaryOutputColor;
    final fallbackReason = stats?.hdrFallbackReason;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: <Widget>[
            _sectionTitle(Icons.speed, 'Runtime'),
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
            if (fallbackReason != null) Text('HDR fallback: $fallbackReason'),
            Text('Updates queued: ${stats?.updatesSent ?? 0}'),
            Text('Scene cuts: ${stats?.cutsDetected ?? 0}'),
            Text('Profile: ${stats?.syncProfile ?? _settings.syncProfile}'),
            Text(
                'Scene: ${((stats?.sceneNeutral ?? 0) * 100).toStringAsFixed(0)}% neutral, luma ${(stats?.sceneLuma ?? 0).toStringAsFixed(2)}'),
            Text('Datagrams sent: ${stats?.datagramsSent ?? 0}'),
            Text('Send errors: ${stats?.sendErrors ?? 0}'),
            if (_sendFailure != null)
              Text("Can't reach the lamp: $_sendFailure",
                  style: const TextStyle(color: Colors.orange)),
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
