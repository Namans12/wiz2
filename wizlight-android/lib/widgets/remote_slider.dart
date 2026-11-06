import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

/// One focus stop: horizontal arrows adjust, vertical arrows leave the slider.
/// The child Slider still handles touch, mouse, and accessibility actions.
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

  @override
  void dispose() {
    _focus.dispose();
    super.dispose();
  }

  KeyEventResult _handleKey(FocusNode node, KeyEvent event) {
    if (event is! KeyDownEvent && event is! KeyRepeatEvent) {
      return KeyEventResult.ignored;
    }
    final key = event.logicalKey;
    if (key == LogicalKeyboardKey.arrowUp ||
        key == LogicalKeyboardKey.arrowDown) {
      // Linear traversal is deliberate: a spatial search can choose this
      // slider again, especially at either end of a scrolled TV screen.
      if (key == LogicalKeyboardKey.arrowUp) {
        node.previousFocus();
      } else {
        node.nextFocus();
      }
      return KeyEventResult.handled;
    }
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
    final displayed = widget.value.toStringAsFixed(widget.value < 2 ? 2 : 0);
    return Focus(
      focusNode: _focus,
      canRequestFocus: widget.onChanged != null,
      onKeyEvent: _handleKey,
      onFocusChange: (focused) {
        setState(() => _focused = focused);
        if (focused) {
          WidgetsBinding.instance.addPostFrameCallback((_) {
            if (mounted && _focus.hasFocus) {
              Scrollable.ensureVisible(context,
                  alignment: 0.5, duration: const Duration(milliseconds: 120));
            }
          });
        }
      },
      child: Listener(
        onPointerDown: (_) => _focus.requestFocus(),
        child: Container(
          margin: const EdgeInsets.symmetric(vertical: 6),
          padding: const EdgeInsets.fromLTRB(12, 10, 12, 0),
          decoration: BoxDecoration(
            color: _focused ? scheme.primaryContainer : Colors.transparent,
            borderRadius: BorderRadius.circular(12),
            border: Border.all(
                color: _focused ? scheme.primary : scheme.outlineVariant,
                width: 2),
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
      ),
    );
  }
}
