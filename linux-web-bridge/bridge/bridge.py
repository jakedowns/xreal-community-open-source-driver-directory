#!/usr/bin/env python3
"""
bridge.py - headless CLI for the XREAL Air -> WebSocket relay.

The windowed version is `xr_relay_gui.py`; both use `relay.py`.

  python3 bridge.py                                  # simulate
  python3 bridge.py --list-hidraw                    # which node is which
  python3 bridge.py --source hidraw                  # auto-picks the IMU node
  python3 bridge.py --source hidraw --interface 3
  python3 bridge.py --source hidraw --raw -v         # decoded hexdump
  python3 bridge.py --source opentrack               # via XRLinuxDriver

Then open http://127.0.0.1:8765/ - the relay serves the demo page itself.
"""

import argparse
import os
import select
import sys
import time

import relay
from relay import Relay, RelayError


def raw_dump(node):
    """Show the handshake and every report, decoded where possible.

    This is the bring-up tool: it proves which node is talking and what the
    numbers look like, without the relay in the way.
    """
    fd = relay.open_hidraw(node["node"])   # raises RelayError with the fix
    vidpid = (f"{node['vid']:04x}:{node['pid']:04x}"
              if node["vid"] is not None else "????:????")
    print(f"[hid] {node['node']}  {vidpid}  interface {node['iface']}  {node['name']}")

    print("[hid] sending IMU stream off, then on:")
    for value, what in ((b"\x00", "off"), (b"\x01", "on")):
        pkt = relay.build_imu_command(relay.IMU_CMD_ENABLE, value)
        os.write(fd, pkt)
        print(f"[hid]   {what:<3} {pkt[:9].hex(' ')}")

    print("[hid] waiting for reports. Ctrl-C to stop.\n")
    count = 0
    shown = 0
    window_start = time.time()
    window_shown = 0
    MAX_PER_SEC = 5          # the device streams at ~900 Hz; do not flood the tty
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 1.0)
            if not ready:
                print(f"[hid] ... nothing for 1s (got {count} reports so far)")
                continue
            try:
                data = os.read(fd, relay.IMU_PACKET_SIZE)
            except BlockingIOError:
                continue
            count += 1

            now = time.time()
            if now - window_start >= 1.0:
                window_start, window_shown = now, 0
            # always show the first few, then rate-limit
            if count > 5:
                if window_shown >= MAX_PER_SEC:
                    continue
                window_shown += 1

            shown += 1
            stamp = time.strftime("%H:%M:%S")
            parsed = relay.parse_imu_packet(data)
            if parsed is None:
                print(f"{stamp} #{count:<6} ?    {len(data)}B {data.hex(' ')}")
                continue
            gyro = " ".join(f"{v:8.2f}" for v in parsed["gyro"])
            accel = " ".join(f"{v:8.4f}" for v in parsed["accel"])
            print(
                f"{stamp} #{count:<6} gyro [{gyro}] deg/s   accel [{accel}] g   "
                f"temp {parsed['temp']:5.1f}C"
            )
    except KeyboardInterrupt:
        print()
    finally:
        rate = count / max(time.time() - window_start + 0.001, 0.001)
        print(f"[hid] bye - {count} reports (showed {shown})")
        os.close(fd)


def tune(node):
    """Live gyro readout, to work out which device axis is yaw/pitch/roll.

    The wire format is settled; which axis means what depends on how the
    glasses sit on your head, so it has to be measured once on your hardware.
    """
    fd = relay.open_hidraw(node["node"])
    for value in (b"\x00", b"\x01"):
        os.write(fd, relay.build_imu_command(relay.IMU_CMD_ENABLE, value))

    print(
        f"""
Tuning {node['node']}. Do ONE movement at a time, watch which axis jumps, and
note its letter and sign. The axis with the longest bar is the one seeing it.

    1. turn your head slowly LEFT and RIGHT   -> that axis is yaw
    2. nod slowly up and down                 -> that axis is pitch
    3. tilt your head slowly side to side     -> that axis is roll

Then restart with, for example:

    python3 bridge.py --source hidraw --axis "yaw=z-,pitch=x+,roll=y-"

Ctrl-C to stop.
"""
    )
    peaks = [0.0, 0.0, 0.0]
    window = time.time()
    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            try:
                data = os.read(fd, relay.IMU_PACKET_SIZE)
            except (BlockingIOError, OSError):
                continue
            parsed = relay.parse_imu_packet(data)
            if parsed is None:
                continue
            for i in range(3):
                peaks[i] = max(peaks[i], abs(parsed["gyro"][i]))

            now = time.time()
            if now - window >= 0.5:
                window = now
                top = max(range(3), key=lambda i: peaks[i])
                print("   ".join(
                    f"{'xyz'[i]} {peaks[i]:6.1f} {'<= moving' if i == top else '         '}"
                    for i in range(3)
                ))
                peaks = [0.0, 0.0, 0.0]
    except KeyboardInterrupt:
        print()
    finally:
        os.close(fd)


def main():
    p = argparse.ArgumentParser(
        description="XREAL Air head pose -> WebSocket relay",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "axis mapping:\n"
            f"  default is \"{relay.DEFAULT_AXIS_MAP}\". If turning your head\n"
            "  left/right moves the wrong thing, change it, e.g. --axis yaw=y+,pitch=x-,roll=z+\n"
        ),
    )
    p.add_argument("--source", choices=Relay.SOURCES, default="simulate")
    p.add_argument("--ws-host", default="127.0.0.1")
    p.add_argument("--ws-port", type=int, default=8765)
    p.add_argument("--udp-host", default="127.0.0.1")
    p.add_argument("--udp-port", type=int, default=4242, help="OpenTrack UDP listen port")
    p.add_argument("--device", default=None,
                   help="hidraw node (default: auto-detect the glasses' IMU interface)")
    p.add_argument("--interface", type=int, default=None,
                   help="pick the glasses' USB interface (3 = IMU, 4 = MCU)")
    p.add_argument("--axis", default=None,
                   help="euler mapping, e.g. \"yaw=z-,pitch=x-,roll=y+\"")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="log IMU detail (first packet, every 100th, odd reports)")
    p.add_argument("--list-hidraw", action="store_true",
                   help="list the /dev/hidraw* nodes and exit")
    p.add_argument("--raw", action="store_true",
                   help="hidraw only: decoded hexdump, no relay")
    p.add_argument("--tune", action="store_true",
                   help="hidraw only: guided axis mapping, no relay")
    p.add_argument("--rate", type=float, default=100.0, help="simulate sample rate (Hz)")
    p.add_argument("--max-hz", type=float, default=120.0, help="WebSocket broadcast cap (Hz)")
    args = p.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    def log(msg):
        print(f"{time.strftime('%H:%M:%S')}  {msg}")

    if args.list_hidraw:
        print("hidraw nodes:")
        print(relay.describe_hidraw(relay.list_hidraw()))
        return 0

    if args.axis:
        try:
            relay.parse_axis_map(args.axis)     # fail fast on typos
        except RelayError as e:
            print(str(e), file=sys.stderr)
            return 2

    if args.raw or args.tune:
        if args.source != "hidraw":
            print("--raw/--tune only apply to --source hidraw", file=sys.stderr)
            return 2
        node = relay.pick_hidraw(args.device, args.interface)
        if node is None:
            print("No XREAL glasses found. Nodes seen now:", file=sys.stderr)
            print(relay.describe_hidraw(relay.list_hidraw()), file=sys.stderr)
            return 2
        try:
            if args.tune:
                tune(node)
            else:
                raw_dump(node)
        except RelayError as e:
            print(str(e), file=sys.stderr)
            return 2
        return 0

    r = Relay(log=log)
    try:
        r.start(
            args.source,
            ws_host=args.ws_host, ws_port=args.ws_port,
            udp_host=args.udp_host, udp_port=args.udp_port,
            device=args.device, interface=args.interface,
            rate=args.rate, max_hz=args.max_hz,
            axis_map=args.axis, verbose=args.verbose,
        )
    except RelayError as e:
        print(str(e), file=sys.stderr)
        return 2

    print(f"{time.strftime('%H:%M:%S')}  demo page: http://{args.ws_host}:{args.ws_port}/")
    print("               Ctrl-C to stop")
    try:
        while r.running:
            time.sleep(1.0)
            s = r.snapshot()
            last = s["last"] or {}
            print(
                f"{time.strftime('%H:%M:%S')}  "
                f"{s['msgs']:>7} msgs  {s['hz']:5.1f} Hz  "
                f"{s['clients']} client(s)  "
                f"yaw={last.get('yaw', 0):7.1f} pitch={last.get('pitch', 0):7.1f} "
                f"roll={last.get('roll', 0):7.1f}"
            )
            if s["error"]:
                print(f"               error: {s['error']}", file=sys.stderr)
                break
    except KeyboardInterrupt:
        print()
    finally:
        r.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
