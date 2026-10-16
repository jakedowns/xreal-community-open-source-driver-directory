#!/usr/bin/env bash
#
# install-desktop.sh - make "XR Relay" launchable without a terminal.
#
# Installs, all user-level (no root, nothing outside your home directory):
#
#   1. an app-grid entry        ~/.local/share/applications/<app id>.desktop
#   2. a matching icon          ~/.local/share/icons/hicolor/scalable/apps/
#   3. a double-clickable icon  on your Desktop (trusted, so it just launches)
#   4. a pinned entry           in the Ubuntu dock
#
# Re-running it is safe; it updates in place.
#
#   XR_RELAY_NO_DESKTOP=1  skip the Desktop icon
#   XR_RELAY_NO_DOCK=1     do not pin to the dock
#
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"     # repo root
BRIDGE="$DIR/bridge"
APP_ID="io.github.jakedowns.XrRelay"
ENTRY="$APP_ID.desktop"

BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"

mkdir -p "$BIN" "$APPS" "$ICONS"

chmod +x "$BRIDGE/xr-relay" "$BRIDGE/xr_relay_gui.py" "$BRIDGE/bridge.py" \
         "$BRIDGE/Launch XR Relay.sh" 2>/dev/null || true

# launcher: a symlink, so readlink -f finds the repo and code edits stay live
ln -sf "$BRIDGE/xr-relay" "$BIN/xr-relay"

# icon: the filename must match the app id for the theme lookup to find it
cp "$BRIDGE/assets/xr-relay.svg" "$ICONS/$APP_ID.svg"

# desktop entry: the basename must match the app id, and StartupWMClass must
# match the GTK application_id, so GNOME pairs the running window with this
# entry - that is what gives the dock the right name and icon.
cat > "$APPS/$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=XR Relay
GenericName=XREAL Air head tracking relay
Comment=Stream XREAL Air head pose to the browser over WebSocket
Exec=$BIN/xr-relay
Icon=$APP_ID
Terminal=false
Categories=Utility;
StartupNotify=true
StartupWMClass=$APP_ID
Keywords=xreal;webxr;headtracking;hid;relay;turn;
EOF
chmod +x "$APPS/$ENTRY"

# ---------------------------------------------------------------------------
# 3. A copy on the Desktop.
#
# Nautilus 42 removed the "Executable Text Files" preference, so double-clicking
# a .sh opens it in the text editor. A .desktop file is still launched by a
# double-click, provided it is executable AND marked trusted (otherwise you get
# an "Untrusted application launcher" prompt).
if [ -z "${XR_RELAY_NO_DESKTOP:-}" ] && [ -d "$DESKTOP_DIR" ]; then
  cp "$APPS/$ENTRY" "$DESKTOP_DIR/$ENTRY"
  chmod +x "$DESKTOP_DIR/$ENTRY"
  gio set "$DESKTOP_DIR/$ENTRY" metadata::trusted true 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 4. Pin it to the Ubuntu dock (GNOME keeps dock contents in favorite-apps).
if [ -z "${XR_RELAY_NO_DOCK:-}" ] && command -v gsettings >/dev/null 2>&1; then
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
if entry not in apps:
    apps.insert(0, entry)
print("[" + ", ".join("'%s'" % a for a in apps) + "]")
PY
)"
  gsettings set org.gnome.shell favorite-apps "$updated" 2>/dev/null || true
fi

# refresh caches (best effort - both are optional)
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPS" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi

cat <<EOF
Installed (no root used):
  app grid + dock   $APPS/$ENTRY
  icon              $ICONS/$APP_ID.svg
  launcher          $BIN/xr-relay -> $BRIDGE/xr-relay
  desktop icon      $DESKTOP_DIR/$ENTRY
  dock pinned       $(gsettings get org.gnome.shell favorite-apps 2>/dev/null | grep -c "$APP_ID" || echo 0) (1 = yes)

Ways to start it, no terminal needed:
  * click the XR Relay icon in the dock
  * double-click the icon on your Desktop
  * press Super and type "XR Relay"

To remove it again:  scripts/uninstall-desktop.sh
EOF
