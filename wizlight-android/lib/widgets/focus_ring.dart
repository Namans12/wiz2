import 'package:flutter/material.dart';

/// Draws a 3px primary outline while any descendant holds focus, and scrolls
/// the focused control into view. Used around tiles/dropdowns whose own
/// widgets only show a faint highlight.
class FocusRing extends StatefulWidget {
  const FocusRing({super.key, required this.child, this.radius = 12});

  final Widget child;
  final double radius;

  @override
  State<FocusRing> createState() => _FocusRingState();
}

class _FocusRingState extends State<FocusRing> {
  bool _focused = false;
  bool _keyboardMode = _isKeyboardMode();

  @override
  void initState() {
    super.initState();
    FocusManager.instance.addHighlightModeListener(_onHighlightMode);
  }

  @override
  void dispose() {
    FocusManager.instance.removeHighlightModeListener(_onHighlightMode);
    super.dispose();
  }

  void _onHighlightMode(FocusHighlightMode mode) {
    if (!mounted) return;
    final keyboard = mode == FocusHighlightMode.traditional;
    if (keyboard != _keyboardMode) setState(() => _keyboardMode = keyboard);
  }

  @override
  Widget build(BuildContext context) {
    final color = Theme.of(context).colorScheme.primary;
    return Focus(
      canRequestFocus: false,
      skipTraversal: true,
      onFocusChange: (focused) {
        if (!mounted) return;
        setState(() => _focused = focused);
        // Touch/mouse focus must not scroll or move anything under the
        // finger; only keyboard / D-pad focus scrolls into view.
        if (focused && _isKeyboardMode()) {
          WidgetsBinding.instance.addPostFrameCallback((_) {
            if (mounted && _focused) {
              Scrollable.ensureVisible(context,
                  alignment: 0.5, duration: const Duration(milliseconds: 120));
            }
          });
        }
      },
      child: DecoratedBox(
        position: DecorationPosition.foreground,
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(widget.radius),
          border: _focused && _keyboardMode
              ? Border.all(color: color, width: 3)
              : Border.all(color: Colors.transparent, width: 0),
        ),
        child: widget.child,
      ),
    );
  }
}

/// True when focus came from a keyboard / D-pad (ring should show); false for
/// touch and mouse interaction.
bool _isKeyboardMode() =>
    FocusManager.instance.highlightMode == FocusHighlightMode.traditional;

/// Public helper for other focus-aware widgets.
bool isKeyboardFocusMode() => _isKeyboardMode();
