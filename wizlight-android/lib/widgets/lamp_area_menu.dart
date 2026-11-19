import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

enum _LampArea {
  left('Left', Icons.west),
  right('Right', Icons.east),
  top('Top', Icons.north),
  bottom('Bottom', Icons.south),
  center('Centre', Icons.center_focus_strong);

  const _LampArea(this.label, this.icon);

  final String label;
  final IconData icon;
}

class LampAreaMenu extends StatefulWidget {
  const LampAreaMenu({
    super.key,
    required this.value,
    required this.onChanged,
    this.busy = false,
  });

  final String value;
  final ValueChanged<String>? onChanged;
  final bool busy;

  @override
  State<LampAreaMenu> createState() => _LampAreaMenuState();
}

class _LampAreaMenuState extends State<LampAreaMenu> {
  static const _gap = 6.0;
  static const _itemHeight = 48.0;
  static const _menuPadding = 8.0;
  final _field = GlobalKey();
  final _fieldFocus = FocusNode(debugLabel: 'Lamp area');
  bool _opening = false;

  @override
  void dispose() {
    _fieldFocus.dispose();
    super.dispose();
  }

  Rect get _fieldRect {
    final box = _field.currentContext!.findRenderObject()! as RenderBox;
    return box.localToGlobal(Offset.zero) & box.size;
  }

  Future<void> _open() async {
    if (_opening || widget.busy) return;
    _opening = true;
    try {
      final media = MediaQuery.of(context);
      final scroll = Scrollable.maybeOf(context)?.position;
      final bottom = media.size.height - media.padding.bottom - _menuPadding;
      final desiredHeight = math.min(
        _itemHeight * _LampArea.values.length + 2 * _menuPadding,
        media.size.height / 2,
      );
      final needed = _fieldRect.bottom + _gap + desiredHeight - bottom;
      if (needed > 0 && scroll != null) {
        await scroll.animateTo(
          math.min(scroll.pixels + needed, scroll.maxScrollExtent),
          duration: const Duration(milliseconds: 150),
          curve: Curves.easeOut,
        );
      }
      if (!mounted || widget.onChanged == null || widget.busy) return;

      final field = _fieldRect;
      final top = field.bottom + _gap;
      final height = bottom - top;
      if (height <= 0) return;
      final width = math.min(
          math.max(field.width, 156.0), media.size.width - 2 * _menuPadding);
      final left = field.left
          .clamp(_menuPadding, media.size.width - width - _menuPadding);
      final selected = await showGeneralDialog<String>(
        context: context,
        barrierDismissible: true,
        barrierLabel:
            MaterialLocalizations.of(context).modalBarrierDismissLabel,
        barrierColor: Colors.transparent,
        transitionDuration: Duration.zero,
        pageBuilder: (context, _, __) => Stack(children: [
          Positioned(
            left: left,
            top: top,
            width: width,
            child: ConstrainedBox(
              constraints: BoxConstraints(maxHeight: height),
              child: _buildMenu(context),
            ),
          ),
        ]),
      );
      if (mounted && selected != null && !widget.busy) {
        widget.onChanged?.call(selected);
      }
    } finally {
      _opening = false;
    }
  }

  Widget _buildMenu(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    return Shortcuts(
      shortcuts: const {
        SingleActivator(LogicalKeyboardKey.arrowUp): PreviousFocusIntent(),
        SingleActivator(LogicalKeyboardKey.arrowDown): NextFocusIntent(),
        SingleActivator(LogicalKeyboardKey.escape): DismissIntent(),
        SingleActivator(LogicalKeyboardKey.goBack): DismissIntent(),
      },
      child: Actions(
        actions: {
          DismissIntent: CallbackAction<DismissIntent>(
            onInvoke: (_) => Navigator.pop(context),
          ),
        },
        child: Material(
          key: const ValueKey('lamp-area-options'),
          color: colors.surfaceContainerHigh,
          elevation: 8,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
            side: BorderSide(color: colors.outlineVariant),
          ),
          clipBehavior: Clip.antiAlias,
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(_menuPadding),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                for (final area in _LampArea.values)
                  TextButton(
                    autofocus: area.name == widget.value,
                    style: ButtonStyle(
                      alignment: Alignment.centerLeft,
                      foregroundColor: WidgetStatePropertyAll(colors.onSurface),
                      minimumSize: const WidgetStatePropertyAll(
                        Size(0, _itemHeight),
                      ),
                      padding: const WidgetStatePropertyAll(
                        EdgeInsets.symmetric(horizontal: 12),
                      ),
                      backgroundColor: WidgetStatePropertyAll(
                        area.name == widget.value
                            ? colors.secondaryContainer
                            : null,
                      ),
                    ),
                    onPressed: () => Navigator.pop(context, area.name),
                    child: Row(children: [
                      Icon(area.icon, size: 20),
                      const SizedBox(width: 10),
                      Expanded(child: Text(area.label)),
                    ]),
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final area = _LampArea.values.byName(widget.value);
    return OutlinedButton(
      key: _field,
      focusNode: _fieldFocus,
      onPressed: widget.onChanged == null ? null : _open,
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(area.icon, size: 18),
          const SizedBox(width: 8),
          Flexible(child: Text(area.label)),
          const SizedBox(width: 4),
          const Icon(Icons.arrow_drop_down, size: 18),
        ],
      ),
    );
  }
}
