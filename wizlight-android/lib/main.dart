import 'package:flutter/material.dart';

import 'screens/home_screen.dart';

void main() {
  runApp(const WizLightAndroidApp());
}

class WizLightAndroidApp extends StatelessWidget {
  const WizLightAndroidApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'WizLight Sync',
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xFF3B8ED0)),
        useMaterial3: true,
      ),
      home: const HomeScreen(),
    );
  }
}
