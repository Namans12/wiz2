# Releasing WizLight

Releases are built and published by `.github/workflows/release.yml` when you push a tag `vX.Y.Z`. One release carries:

- `WizLight-<version>-Setup.exe` and `WizLight-<version>-portable.zip` (Windows)
- `WizLight-<version>-macOS-arm64.dmg` and `WizLight-<version>-macOS-intel.dmg` (macOS, unsigned)
- `WizLight-<version>.apk` (Android)
- `SHA256SUMS.txt`

## Cut a release

1. Bump the version everywhere:

   ```sh
   python scripts/bump_version.py 1.1.0            # add --build N to also set the Android build number
   ```

2. Open a PR, let CI go green, merge it into `master`.
3. Tag the merge commit and push the tag:

   ```sh
   git checkout master && git pull
   git tag v1.1.0
   git push origin v1.1.0
   ```

4. Watch the **Release** run in the Actions tab. When all build jobs pass, the `publish` job creates the GitHub Release with generated notes, install notes and checksums.

The workflow fails if the tag does not match `src/__init__.py` (and `wizlight-android/pubspec.yaml`). Tags containing `-rc`, `-beta` or `-alpha` (for example `v1.1.0-rc1`, which releases version `1.1.0`) are marked as pre-releases.

## Testing the pipeline without publishing

The same workflow runs, build jobs only, on:

- any pull request that touches `.github/workflows/release.yml`, `packaging/**`, `wizlight-android/android/**`, `wizlight-android/pubspec.yaml` or `requirements*.txt`;
- **Actions > Release > Run workflow** (`workflow_dispatch`) on any branch.

Artifacts are attached to the run for 14 days. The `publish` job only runs for tag pushes.

## Android signing secrets

The APK is signed with the debug key unless all four repository secrets exist (Settings > Secrets and variables > Actions). Without them the build still succeeds and prints a warning. Android refuses to update an app signed with a different key, so create the real key before the first public release and keep it forever.

| Secret | Value |
| --- | --- |
| `ANDROID_KEYSTORE_BASE64` | the keystore file, base64 encoded |
| `ANDROID_KEYSTORE_PASSWORD` | keystore password |
| `ANDROID_KEY_ALIAS` | key alias |
| `ANDROID_KEY_PASSWORD` | key password |

Create the keystore once (keep a backup somewhere safe; never commit it):

```sh
keytool -genkey -v -keystore wizlight-release.jks -keyalg RSA -keysize 2048 -validity 10000 -alias wizlight
base64 -w0 wizlight-release.jks        # macOS: base64 -i wizlight-release.jks
```

For local release builds, create `wizlight-android/android/key.properties` (git-ignored, as are `*.jks` and `*.keystore`):

```properties
storeFile=/absolute/path/to/wizlight-release.jks
storePassword=...
keyAlias=wizlight
keyPassword=...
```

`build.gradle.kts` also reads the environment variables `ANDROID_KEYSTORE_PATH`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS` and `ANDROID_KEY_PASSWORD`, which is how CI passes the secrets. With neither source present, release builds fall back to the debug key.

## macOS notes

The DMGs are not notarized. PyInstaller ad-hoc signs the app, which is enough to run on Apple Silicon after the user clicks **Open Anyway** in System Settings > Privacy & Security. Apple Silicon and Intel builds are separate, built natively on `macos-26` and `macos-26-intel` runners. See `packaging/macos_README.md` for signing and notarization with a Developer ID.
