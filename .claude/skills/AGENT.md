# AGENT.md — wizlight-build
# Read this file when running multi-step autonomous work or when an error occurs.

## Agent role
Drive the WizLight change → build → verify → package loop for the Android app and
the Python desktop app, keeping the Android (Kotlin) and desktop (Python) color
pipelines in parity, and produce installable artifacts with honest verification.

## Allowed tools (this environment)
- Bash / PowerShell: file ops, installs, builds (Flutter, Gradle, PyInstaller), syntax checks
- Read: reading files
- Write: new files only
- Edit: editing existing files
- Glob / Grep: searching the codebase
- TaskCreate / TaskUpdate: tracking multi-step work
> Note: there is no `present_files`, `create_file`, `str_replace`, or `view` tool here.
> Report final artifacts by stating their absolute path; the user opens them directly.

## Loop logic
When executing the SKILL.md workflow:
1. Execute each step.
2. After each step, append a one-line checkpoint to `.claude\skills\state\checkpoint.log`:
   `[STEP_N] | [OK|FAILED] | [YYYY-MM-DD] | brief note`
3. If a step fails, retry once before escalating.
4. Never skip a step, even if it seems unnecessary.

## Checkpoint format
```
STEP 4 | OK     | 2026-06-23 | Edited ScreenCaptureService.kt: CCT split
STEP 5 | OK     | 2026-06-23 | flutter build apk --release -> app-release.apk (46MB)
STEP 6 | FAILED | 2026-06-23 | Gradle lock — retrying
STEP 6 | OK     | 2026-06-23 | Retry succeeded
```

## Retry rules
- Build/Gradle errors: retry once. If it fails again, run `flutter clean` then rebuild once more; if still failing, stop and paste the real error.
- Bash path errors on `C:\...`: switch to PowerShell instead of retrying verbatim.
- Python import/syntax errors: `python -c "import ast; ast.parse(open('<file>',encoding='utf-8').read())"` to localize, fix, re-check.
- Output validation failure (CHECKS.md): fix the specific item, re-run all checks; do not report success until all pass.

## Escalation rules (stop and ask the user)
- The same step fails 3 times.
- A decision is needed that SKILL.md doesn't cover (e.g. release signing keystore, version bump).
- A build would require a host this machine isn't (e.g. macOS `.app`/`.dmg`) — report the recipe, don't fake the artifact.
- The change would alter color behavior for ALL devices (not just one profile) — confirm against the sRGB laptop baseline first.

## Do not
- Do not report a build as done before CHECKS.md passes.
- Do not claim a macOS artifact was built on this Windows host.
- Do not rename or hand-edit build outputs under `build\` or `dist\`.
- Do not add Python deps outside `requirements.txt`'s list (with platform markers) without asking.
- Do not let Android and desktop color math drift apart silently.
