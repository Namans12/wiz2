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
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        brightness: Brightness.dark,
        scaffoldBackgroundColor: const Color(0xFF0D1422),
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xFF80B7FF),
          brightness: Brightness.dark,
          surface: const Color(0xFF172234),
        ),
        appBarTheme: const AppBarTheme(backgroundColor: Color(0xFF0D1422)),
        filledButtonTheme: FilledButtonThemeData(
            style: FilledButton.styleFrom(
          minimumSize: const Size(64, 56),
          textStyle: const TextStyle(
              fontSize: 16, fontWeight: FontWeight.w600, fontFamily: 'Roboto'),
        )),
        outlinedButtonTheme: OutlinedButtonThemeData(
            style: OutlinedButton.styleFrom(
          minimumSize: const Size(64, 52),
        )),
        visualDensity: VisualDensity.standard,
        useMaterial3: true,
      ),
      home: const HomeScreen(),
    );
  }
}
