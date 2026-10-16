#!/usr/bin/env bash
#
# install-monado.sh — build & install Monado with XREAL Air 3DoF support.
#
# WHY FROM SOURCE (and not `apt install monado-service`):
#   Ubuntu 22.04 ships Monado 21.0.0 (~Aug 2021).
#   Ubuntu 24.04 ships Monado 21.0.0+git2905  (still the 21.0.0 series).
#   XREAL/Nreal Air support only landed in the 2023+ series (MRs !1798, !1989,
#   !2150, and Air 2 Ultra in !2436 for 25.1.0). The distro packages therefore
#   CANNOT drive XREAL Air at all — you must build v25.1.0 or newer.
#   The upstream PPA only provides builds for Ubuntu 20.04.
#
# Requires root (apt, /usr/local, /etc/udev).
# Usage:
#   sudo ./install-monado.sh
#   sudo MONADO_TAG=v25.1.0 INSTALL_PREFIX=/usr/local ./install-monado.sh
#
set -euo pipefail

MONADO_TAG="${MONADO_TAG:-v25.1.0}"
INSTALL_PREFIX="${INSTALL_PREFIX:-/usr/local}"
REAL_USER="${SUDO_USER:-$(id -un)}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
SRC_DIR="${SRC_DIR:-$REAL_HOME/src}"
JOBS="${JOBS:-$(nproc)}"

if [[ $EUID -ne 0 ]]; then
  echo "error: run me as root ->  sudo $0" >&2
  exit 1
fi

echo "==> Monado $MONADO_TAG -> $INSTALL_PREFIX   (source in $SRC_DIR)"

# ---------------------------------------------------------------------------
# 1. Build dependencies
#    Core set is installed strictly; extras are installed one-by-one so a
#    renamed/unavailable package does not abort the whole run.
# ---------------------------------------------------------------------------
echo "==> Installing build dependencies"
apt-get update -qq

CORE_DEPS=(
  build-essential git cmake ninja-build pkg-config python3
  libeigen3-dev
  libusb-1.0-0-dev libhidapi-dev libudev-dev libdrm-dev
  libvulkan-dev vulkan-tools
  libglvnd-dev libegl1-mesa-dev libgles2-mesa-dev libgl1-mesa-dev
  libx11-dev libxrandr-dev libxinerama-dev libxi-dev
  libxcb1-dev libxcb-randr0-dev libxcb-glx0-dev libxcb-shm0-dev libxcb-xfixes0-dev
  libwayland-dev wayland-protocols
  libbsd-dev libdbus-1-dev libsystemd-dev
  glslang-tools
)
apt-get install -y --no-install-recommends "${CORE_DEPS[@]}" \
  || { echo "core deps failed; fix the name(s) above and re-run" >&2; exit 1; }

# Optional / may be renamed between releases.
EXTRA_DEPS=( libsdl2-dev libopencv-dev )
for p in "${EXTRA_DEPS[@]}"; do
  apt-get install -y --no-install-recommends "$p" >/dev/null 2>&1 \
    && echo "    + $p" || echo "    - $p (skipped)"
done

# OpenXR loader (handy for non-Chromium apps; Chromium's patched build bundles its own)
for p in libopenxr-loader1 libopenxr-dev; do
  apt-get install -y --no-install-recommends "$p" >/dev/null 2>&1 \
    && echo "    + $p" || echo "    - $p (skipped)"
done

# ---------------------------------------------------------------------------
# 2. Fetch + build Monado
# ---------------------------------------------------------------------------
mkdir -p "$SRC_DIR"
if [[ -d "$SRC_DIR/monado/.git" ]]; then
  echo "==> Reusing existing clone at $SRC_DIR/monado"
  git -C "$SRC_DIR/monado" fetch --tags --quiet
else
  echo "==> Cloning Monado"
  git clone https://gitlab.freedesktop.org/monado/monado.git "$SRC_DIR/monado"
fi
git -C "$SRC_DIR/monado" checkout "$MONADO_TAG"

echo "==> Configuring"
cmake -S "$SRC_DIR/monado" -B "$SRC_DIR/monado/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DCMAKE_INSTALL_PREFIX="$INSTALL_PREFIX" \
  -DXRT_HAVE_OPENGL=ON \
  -DXRT_HAVE_VULKAN=ON \
  -DXRT_FEATURE_SERVICE=ON

echo "==> Building (-j$JOBS)  — this takes a while on 4 cores / 3.8 GiB RAM"
ninja -C "$SRC_DIR/monado/build" -j"$JOBS"

echo "==> Installing"
ninja -C "$SRC_DIR/monado/build" install
ldconfig

# ---------------------------------------------------------------------------
# 3. hidraw permissions (without this Monado gets EACCES, exactly like Chrome)
# ---------------------------------------------------------------------------
RULES_SRC="$(dirname "$(readlink -f "$0")")/70-xreal-air.rules"
if [[ -f "$RULES_SRC" ]]; then
  echo "==> Installing udev rule"
  install -m 0644 "$RULES_SRC" /etc/udev/rules.d/70-xreal-air.rules
  udevadm control --reload-rules
  udevadm trigger --subsystem-match=hidraw || true
else
  echo "==> NOTE: $RULES_SRC not found; skipping udev rule" >&2
fi

# ---------------------------------------------------------------------------
# 4. Where things landed
# ---------------------------------------------------------------------------
RUNTIME_JSON="$(find "$INSTALL_PREFIX" -name 'openxr_monado*.json' -print -quit 2>/dev/null || true)"
cat <<EOF

============================================================
Done.

  Monado service : $INSTALL_PREFIX/bin/monado-service
  Runtime manifest: ${RUNTIME_JSON:-(none found — check install output)}
  Build dir       : $SRC_DIR/monado/build

Next steps:
  1. Unplug/replug the glasses, then verify permissions:
       ls -l /dev/hidraw*        # expect crw-rw---- root plugdev
  2. Start the service (as your normal user, not root):
       monado-service
  3. Point apps at this runtime:
       export XR_RUNTIME_JSON=${RUNTIME_JSON:-<path above>}
  4. Check it sees the Air (3DoF):
       $INSTALL_PREFIX/bin/monado-cli probe
============================================================
EOF
