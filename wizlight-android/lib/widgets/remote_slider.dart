import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'focus_ring.dart' show isKeyboardFocusMode;

/// One focus stop: Left/Right adjust the value. Up/Down are left to the
/// surrounding traversal so every control navigates the same way.
/// The child Slider handles touch, mouse, and accessibility actions directly;
/// touching it never moves focus, so the first drag changes the value.
class RemoteSlider extends StatefulWidget {
  const RemoteSlider({
    super.key,
    required this.label,
    required this.value,
    required this.min,
    required this.max,
    required this.onChanged,
    this.divisions,
  });

  final String label;
  final double value;
  final double min;
  final double max;
  final int? divisions;
  final ValueChanged<double>? onChanged;

  @override
  State<RemoteSlider> createState() => _RemoteSliderState();
}

class _RemoteSliderState extends State<RemoteSlider> {
  late final _focus = FocusNode(debugLabel: 'Slider: ${widget.label}');
  bool _focused = false;
  bool _keyboardMode = isKeyboardFocusMode();

  @override
  void initState() {
    super.initState();
    FocusManager.instance.addHighlightModeListener(_onHighlightMode);
  }

  void _onHighlightMode(FocusHighlightMode mode) {
    if (!mounted) return;
    final keyboard = mode == FocusHighlightMode.traditional;
    if (keyboard != _keyboardMode) setState(() => _keyboardMode = keyboard);
  }

  @override
  void dispose() {
    FocusManager.instance.removeHighlightModeListener(_onHighlightMode);
    _focus.dispose();
    super.dispose();
  }

  KeyEventResult _handleKey(FocusNode node, KeyEvent event) {
    if (event is! KeyDownEvent && event is! KeyRepeatEvent) {
      return KeyEventResult.ignored;
    }
    final key = event.logicalKey;
    if (key == LogicalKeyboardKey.arrowLeft ||
        key == LogicalKeyboardKey.arrowRight) {
      final step = (widget.max - widget.min) / (widget.divisions ?? 20);
      final direction = key == LogicalKeyboardKey.arrowRight ? 1 : -1;
      widget.onChanged?.call(
        (widget.value + direction * step).clamp(widget.min, widget.max),
      );
      // Also consume arrows at min/max; focus must not jump sideways.
      return KeyEventResult.handled;
    }
    return KeyEventResult.ignored;
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final ring = _focused && _keyboardMode;
    final displayed = widget.value.toStringAsFixed(widget.value < 2 ? 2 : 0);
    return Focus(
      focusNode: _focus,
      canRequestFocus: widget.onChanged != null,
      onKeyEvent: _handleKey,
      onFocusChange: (focused) {
        setState(() => _focused = focused);
        // Only keyboard / D-pad focus scrolls; never move content under a
        // finger.
        if (focused && isKeyboardFocusMode()) {
          WidgetsBinding.instance.addPostFrameCallback((_) {
            if (mounted && _focus.hasFocus) {
              Scrollable.ensureVisible(context,
                  alignment: 0.5, duration: const Duration(milliseconds: 120));
            }
          });
        }
      },
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 6),
        padding: const EdgeInsets.fromLTRB(12, 10, 12, 0),
        decoration: BoxDecoration(
          color: ring ? scheme.primaryContainer : Colors.transparent,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(
              color: ring ? scheme.primary : scheme.outlineVariant,
              width: ring ? 3 : 2),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              Expanded(child: Text(widget.label)),
              Text(displayed,
                  style: const TextStyle(fontWeight: FontWeight.w700)),
            ]),
            ExcludeFocus(
              child: Slider(
                value: widget.value.clamp(widget.min, widget.max),
                min: widget.min,
                max: widget.max,
                divisions: widget.divisions,
                semanticFormatterCallback: (value) =>
                    '${widget.label}: ${value.toStringAsFixed(value < 2 ? 2 : 0)}',
                onChanged: widget.onChanged,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
