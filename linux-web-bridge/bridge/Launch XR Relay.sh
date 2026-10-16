#!/usr/bin/env bash
#
# Launch XR Relay by double-clicking this file.
#
#   NOTE: GNOME Files (Nautilus 42+) opens .sh files in the *text editor* when
#   you double-click them - the "Executable Text Files" preference was removed
#   upstream, so there is no setting to turn that back on. Reliable ways to run
#   this by double-click:
#     * the "Launch XR Relay" icon on your Desktop  (see scripts/install-desktop.sh)
#     * the "XR Relay" entry in the app grid        (press Super, type XR Relay)
#     * right-click this file -> "Run as a Program" (if your build offers it)
#
#   From a terminal it just works:   ./"Launch XR Relay.sh"
#
# Runs the GUI; the relay stops when you close the window. Nothing is left
# running in the background and no service is installed.

set -uo pipefail

# Work out where we are, following symlinks, so it does not matter how we were
# started (double-click, symlink, or a shell in some other directory).
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/xr-relay"
LOG="$LOG_DIR/launch.log"
mkdir -p "$LOG_DIR" 2>/dev/null

# A GUI app launched by double-click has nowhere to print, so log to a file.
# From a terminal, keep the output on screen where you can see it.
if [ ! -t 1 ]; then
    printf -- '--- %s  launching from %s ---\n' "$(date -Is)" "$HERE" >>"$LOG"
    exec >>"$LOG" 2>&1
fi

fail() {
    echo "ERROR: $1" >&2
    # Say it out loud too - a silent double-click failure is the worst outcome.
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --width=460 --title="XR Relay" \
               --text="<b>XR Relay could not start</b>\n\n$1\n\nLog: $LOG" 2>/dev/null || true
    fi
    exit 1
}

[ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ] \
    || echo "WARNING: no DISPLAY/WAYLAND_DISPLAY - is this a desktop session?" >&2

command -v python3 >/dev/null 2>&1 || fail "python3 was not found in PATH."

if [ -f "$HERE/xr_relay_gui.py" ]; then
    APP="$HERE/xr_relay_gui.py"
elif [ -f "$HERE/bridge/xr_relay_gui.py" ]; then
    APP="$HERE/bridge/xr_relay_gui.py"
elif [ -x "$HOME/.local/bin/xr-relay" ]; then
    # This file was copied somewhere else; fall back to the installed launcher.
    exec "$HOME/.local/bin/xr-relay" "$@"
else
    fail "xr_relay_gui.py was not found next to this script ($HERE)."
fi

python3 -c 'import gi' >/dev/null 2>&1 \
    || fail "PyGObject is missing. Install it with:  sudo apt install python3-gi gir1.2-gtk-3.0"

echo "starting: python3 $APP $*"
exec python3 "$APP" "$@"
