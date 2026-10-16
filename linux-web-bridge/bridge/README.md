# XREAL Air → browser, over WebSocket

Feeds XREAL Air head pose into a web page without WebHID, without browser
flags, and without root. Same shape as
[`rust-ovr-cemuhook`](https://github.com/jakedowns/rust-ovr-cemuhook): read a
device, normalise it to a tiny wire format, serve it on localhost. Only the
transport differs (WebSocket instead of CEMUhook UDP).

```
glasses ──hidraw/UDP──▶ bridge.py ──ws://127.0.0.1:8765──▶ browser
```

## Why this exists

On Linux, native WebHID is blocked by file permissions (`/dev/hidraw*` is
`0600 root:root`), and real WebXR needs a patched Chromium build. This bridge
sidesteps all of that: Python reads the device with the permissions *you* grant,
and the browser just consumes JSON. It works in Firefox too — no WebHID needed.

## Run it

There are two front ends over the same core (`relay.py`): a windowed app and a
headless CLI. Both are stdlib-only — no packages to install.

### The windowed app (recommended)

```bash
./scripts/install-desktop.sh   # adds it to the app grid + dock entry
```

Then click **XR Relay** in the app grid (press <kbd>Super</kbd>, type "XR
Relay"), or run `xr-relay`. It is user-level only: it writes a `.desktop` file
and an icon under `~/.local/`, and never uses root or `sudo`.

The window has Connect/Disconnect, the source picker, live metrics and a log.
**The relay stops when you close the window** — nothing lingers in the
background, and no service is installed. Press **Connect**; the app starts the
relay *and* serves the demo page, so **Open demo page** jumps straight to it.

### The headless CLI

```bash
python3 bridge/bridge.py            # synthetic motion, works immediately
# then open http://127.0.0.1:8765/  (served by the relay itself)
```

You should see a wireframe room you look around in, an artificial horizon, a
compass tape, and live yaw/pitch/roll. Target is **~100 Hz, 0 ms latency**.

## Sources

### `--source simulate` (default)

Synthetic smooth head motion. Use it to prove the browser pipeline works before
touching device permissions.

### `--source opentrack` ← recommended for real data

Uses [XRLinuxDriver](https://github.com/wheaney/XRLinuxDriver), which already
reads the glasses, runs sensor fusion, owns the udev rules, and runs as a user
service. Enable its OpenTrack output:

```bash
xr_driver_cli --opentrack-app
xr_driver_cli --opentrack-app-ip 127.0.0.1
xr_driver_cli --opentrack-app-port 4242

python3 bridge/bridge.py --source opentrack
```

Payload: `6 × float64 (x, y, z, yaw, pitch, roll)` + `uint32` frame, degrees.
The bridge also accepts a bare JSON object on the same UDP port, so you can
hand-test without any glasses:

```bash
echo '{"yaw":15,"pitch":-5,"roll":2}' | nc -u -w0 127.0.0.1 4242
```

### `--source hidraw` ← standalone, no extra software

Reads the IMU directly from `/dev/hidrawN`. First grant access (see
`../scripts/70-xreal-air.rules`):

```bash
sudo cp scripts/70-xreal-air.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=hidraw
# unplug/replug the glasses
python3 bridge/bridge.py --source hidraw
```

If it can't open the device it prints the exact fix instead of a traceback.

**Which node?** The Air exposes four hidraw interfaces; only one carries the
IMU, and the others silently never send pose data:

```bash
$ python3 bridge/bridge.py --list-hidraw
hidraw nodes:
  /dev/hidraw3    3318:0424  if  3  Vendor Air  <- glasses IMU (use this one)
  /dev/hidraw4    3318:0424  if  4  Vendor Air  <- glasses MCU (never sends pose)
  /dev/hidraw5    3318:0424  if  5  Vendor Air  <- glasses
  /dev/hidraw6    3318:0424  if  6  Vendor Air  <- glasses
```

By default the IMU interface (3) is picked automatically; `--interface N` or
`--device /dev/hidrawN` override it. `-v` logs the handshake, so a silent
device says why instead of just sitting there.

**Bring-up tools:**

```bash
python3 bridge/bridge.py --source hidraw --raw     # decoded live hexdump
python3 bridge/bridge.py --source hidraw --tune    # guided axis mapping
```

**The protocol** (from [Void Computing's write-up](https://voidcomputing.hu/blog/worse-better-prettier/)
and [ar-drivers-rs](https://github.com/badicsalex/ar-drivers-rs) — it is *not*
the same as the XREAL Light's):

Host → device commands are always 64 bytes:

| offset | type | meaning |
|---|---|---|
| `0x00` | u8 | head, always `0xaa` |
| `0x01` | u32 | CRC-32 (zlib) of bytes `[0x05, 0x05+length)` |
| `0x05` | u16 | `length = 3 + len(data)` |
| `0x07` | u8 | command id |
| `0x08` | n | data |

The IMU stream is command `0x19`, enabled with a single data byte `0x01`.

Device → host reports are a different layout, with 3-byte signed ints and a
**per-packet multiplier/divisor** (so there is no hardcoded scaling):

| offset | type | meaning |
|---|---|---|
| `0x00` | u16 | header, `0x01 0x02` |
| `0x02` | u16 | temperature (raw) |
| `0x04` | u64 | timestamp, ns |
| `0x0C` | u16 | gyro multiplier |
| `0x0E` | u32 | gyro divisor |
| `0x12`,`0x15`,`0x18` | i24 | gyro x/y/z |
| `0x1B` | u16 | accel multiplier |
| `0x1D` | u32 | accel divisor |
| `0x21`,`0x24`,`0x27` | i24 | accel x/y/z |

Each reading is `raw × multiplier ÷ divisor` (deg/s and g). A sanity check:
with the glasses sitting still, the decoded accelerometer magnitude is 1.0 g.

**The axis mapping is the one thing to measure on your own hardware.** Run
`--tune`, move your head one way at a time, and note which letter moves. Then
pass it back, e.g.:

```bash
python3 bridge/bridge.py --source hidraw --axis "yaw=z-,pitch=x+,roll=y-"
```

Yaw drift is expected — there is no magnetometer, so yaw can only be
integrated. Pitch and roll are corrected against gravity.

## Wire format

One JSON object per WebSocket text frame:

```json
{"t": 1792140755.06, "source": "simulate", "seq": 141,
 "yaw": 23.86, "pitch": 14.24, "roll": 8.76, "x": 0, "y": 0, "z": 0}
```

Yaw/pitch/roll in **degrees**. `t` is epoch seconds (used for the latency
readout). Any client can consume this — the demo page is just one.

## Endpoints

| | |
|---|---|
| `ws://127.0.0.1:8765` | WebSocket stream |
| `http://127.0.0.1:8765/` | the demo page (`index.html`) |
| `http://127.0.0.1:8765/status` | JSON: clients, counts, last pose |

## Demo page

`index.html` — dependency-free (no CDN, no three.js), so it works offline.

- Wireframe room you look around in, drawn with a hand-rolled 3D projection.
- Artificial horizon (pitch ladder) + compass tape + reticle.
- Invert toggles for yaw/pitch/roll if the sign convention feels wrong.
- Auto-reconnect, live msg rate and latency.

Query params: `?host=…&port=…`.

## Notes / limits

- **This does not give you a WebXR `immersive-vr` session.** WebXR has no way
  to accept an external pose — the browser asks the OS XR runtime. Use this for
  your own renderer, for 2D/desktop content, or for prototyping. For real WebXR
  on Linux you need Monado + a patched Chromium.
- `ws://localhost` is exempt from mixed-content blocking, so this works from an
  `https://` page too. Any other host requires serving over `http://`.
- Reading hidraw does not take the device away from Chrome; hidraw allows
  multiple openers. Just don't have two processes both issuing the IMU-enable
  command at the same time.
- Want it to start by itself at login? That is deliberately *not* set up. If you
  later change your mind, a `.desktop` file in `~/.config/autostart/` is the
  least invasive option (no root, no systemd unit).
