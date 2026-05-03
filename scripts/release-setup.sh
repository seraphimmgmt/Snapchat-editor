#!/usr/bin/env bash
# One-shot setup for first-time GitHub push + signing secrets.
#
# Run this AFTER you've authenticated `gh` once:
#     /opt/homebrew/bin/gh auth login
# Pick: GitHub.com → HTTPS → "Login with a web browser" — follow the prompts.
#
# What this script does:
#   1. Force-pushes the local main branch to seraphimmgmt/Snapchat-editor
#      (the remote currently has only initial scaffolding; we overwrite it)
#   2. Sets the two repo secrets the release workflow needs to sign the
#      auto-updater artifacts:
#        TAURI_SIGNING_PRIVATE_KEY
#        TAURI_SIGNING_PRIVATE_KEY_PASSWORD
#   3. Verifies the secrets are set
#
# After this, you can either:
#   - Click "Run workflow" on GitHub Actions → "release" → main, to do a
#     test build (artifacts download from the run page; no public release)
#   - Or `git tag v4.0.0 && git push --tags` to publish the v4.0.0 release
#     and update latest.json so existing installs get the update notification.

set -euo pipefail

GH=/opt/homebrew/bin/gh
REPO=seraphimmgmt/Snapchat-editor
KEY_FILE="$HOME/.tauri/snapcap_updater.key"

if ! "$GH" auth status >/dev/null 2>&1; then
  echo "✗ gh not authenticated. Run: $GH auth login"
  exit 1
fi

if [[ ! -f "$KEY_FILE" ]]; then
  echo "✗ Signing key not found at $KEY_FILE"
  exit 1
fi

echo "→ Pushing local main to $REPO (force; remote only has scaffolding)…"
git push -u origin main --force

echo
echo "→ Setting repo secret TAURI_SIGNING_PRIVATE_KEY"
"$GH" secret set TAURI_SIGNING_PRIVATE_KEY --repo "$REPO" < "$KEY_FILE"

echo "→ Setting repo secret TAURI_SIGNING_PRIVATE_KEY_PASSWORD"
echo "    (paste the passphrase you used when generating the key —"
echo "     leave blank and press Enter if you used no passphrase)"
read -rs -p "    Passphrase: " PASS
echo
printf '%s' "$PASS" | "$GH" secret set TAURI_SIGNING_PRIVATE_KEY_PASSWORD --repo "$REPO"

echo
echo "→ Verifying secrets are set:"
"$GH" secret list --repo "$REPO"

echo
echo "✓ Done. Next:"
echo "  - Test build (no release):"
echo "      $GH workflow run release.yml --repo $REPO --ref main"
echo "  - Or watch the manual run page:"
echo "      https://github.com/$REPO/actions/workflows/release.yml"
echo "  - Tag the release for public download + auto-update:"
echo "      git tag v4.0.0"
echo "      git push --tags"
