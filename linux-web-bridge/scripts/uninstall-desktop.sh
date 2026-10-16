#!/usr/bin/env bash
#
# uninstall-desktop.sh - undo install-desktop.sh.
#
# Removes the app-grid entry, the icon, the Desktop icon, and the dock pin.
# It does not touch the repo itself.
#
set -euo pipefail

APP_ID="io.github.jakedowns.XrRelay"
ENTRY="$APP_ID.desktop"

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"

rm -f "$HOME/.local/share/applications/$ENTRY" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg" \
      "$DESKTOP_DIR/$ENTRY" \
      "$HOME/.local/bin/xr-relay"

# drop the dock pin
if command -v gsettings >/dev/null 2>&1; then
  current="$(gsettings get org.gnome.shell favorite-apps 2>/dev/null || echo '[]')"
  updated="$(python3 - "$current" "$ENTRY" <<'PY'
import ast, sys

raw, entry = sys.argv[1].strip(), sys.argv[2]
if raw.startswith("@as "):
    raw = raw[4:]
try:
    apps = [a for a in ast.literal_eval(raw) if isinstance(a, str)]
except Exception:
    apps = []
print("[" + ", ".join("'%s'" % a for a in apps if a != entry) + "]")
PY
)"
  gsettings set org.gnome.shell favorite-apps "$updated" 2>/dev/null || true
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
fi

echo "Removed XR Relay from the app grid, the Desktop and the dock."
