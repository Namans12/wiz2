import 'package:flutter/material.dart';

class WizLightBackdrop extends StatelessWidget {
  const WizLightBackdrop({super.key});

  @override
  Widget build(BuildContext context) {
    return const RepaintBoundary(
      child: Stack(
        fit: StackFit.expand,
        children: [
          DecoratedBox(
            key: ValueKey('wizlight-background'),
            decoration: BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [
                  Color(0xFF07081A),
                  Color(0xFF140F33),
                  Color(0xFF2A1757)
                ],
              ),
            ),
          ),
          _Glow(Alignment(0.86, -0.9), 0.68, Color(0x4D7B5CF0)),
          _Glow(Alignment(-1.12, 0.04), 0.62, Color(0x242EC4D6)),
          _Glow(Alignment(0.96, 1.08), 0.64, Color(0x1AF29A4A)),
        ],
      ),
    );
  }
}

class _Glow extends StatelessWidget {
  const _Glow(this.center, this.radius, this.color);

  final Alignment center;
  final double radius;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: BoxDecoration(
        gradient: RadialGradient(
          center: center,
          radius: radius,
          colors: [color, color.withValues(alpha: 0)],
        ),
      ),
    );
  }
}
