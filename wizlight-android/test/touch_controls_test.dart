import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:wizlight_android/widgets/focus_ring.dart';
import 'package:wizlight_android/widgets/remote_slider.dart';

Finder _ringBox() => find.descendant(
    of: find.byType(FocusRing), matching: find.byType(DecoratedBox));

bool _ringPainted(WidgetTester tester) {
  final box = tester.widget<DecoratedBox>(_ringBox().first);
  final deco = box.decoration as BoxDecoration;
  final border = deco.border as Border?;
  return border != null && border.top.width > 0;
}

void main() {
  testWidgets('single tap toggles a FocusRing-wrapped switch, no ring',
      (tester) async {
    var on = false;
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(
            body: StatefulBuilder(
      builder: (context, setState) => FocusRing(
          child: SwitchListTile(
              title: const Text('Toggle'),
              value: on,
              onChanged: (v) => setState(() => on = v))),
    ))));
    await tester.tap(find.byType(SwitchListTile));
    await tester.pumpAndSettle();
    expect(on, isTrue);
    expect(_ringPainted(tester), isFalse);
    await tester.tap(find.byType(Switch));
    await tester.pumpAndSettle();
    expect(on, isFalse);
    expect(_ringPainted(tester), isFalse);
  });

  testWidgets('first drag on RemoteSlider changes value, no focus', (tester) async {
    var value = 0.0;
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(
            body: StatefulBuilder(
      builder: (context, setState) => RemoteSlider(
          label: 'Level',
          value: value,
          min: 0,
          max: 100,
          onChanged: (v) => setState(() => value = v)),
    ))));
    await tester.drag(find.byType(Slider), const Offset(120, 0));
    await tester.pumpAndSettle();
    expect(value, greaterThan(10));
    expect(FocusManager.instance.primaryFocus?.debugLabel,
        isNot('Slider: Level'));
    final before = value;
    await tester.tap(find.byType(Slider));
    await tester.pumpAndSettle();
    expect(value, isNot(before));
  });

  testWidgets('ring hidden after touch, shown after D-pad focus',
      (tester) async {
    final node = FocusNode();
    addTearDown(node.dispose);
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(
            body: Column(children: [
      FocusRing(
          child: TextButton(
              focusNode: node, onPressed: () {}, child: const Text('Btn'))),
      const SizedBox(height: 20),
    ]))));
    await tester.tap(find.byType(TextButton));
    await tester.pumpAndSettle();
    expect(_ringPainted(tester), isFalse);

    node.unfocus();
    await tester.pump();
    await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
    await tester.pump();
    node.requestFocus();
    await tester.pumpAndSettle();
    expect(FocusManager.instance.highlightMode, FocusHighlightMode.traditional);
    expect(_ringPainted(tester), isTrue);

    // Touching again hides it.
    await tester.tap(find.byType(TextButton));
    await tester.pumpAndSettle();
    expect(_ringPainted(tester), isFalse);
  });
}
