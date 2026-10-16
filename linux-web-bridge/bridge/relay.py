#!/usr/bin/env python3
"""
relay.py - core of the XREAL Air -> WebSocket relay.

No CLI, no GUI here: this is the library both `bridge.py` (headless) and
`xr-relay-gui.py` (windowed) use.

    glasses --hidraw/UDP--> Relay --> ws://127.0.0.1:8765 --> browser

The same port also serves the demo page, so a running relay is self-contained:
open http://127.0.0.1:8765/ and you get the viewer, already wired to the stream.
"""

import json
import math
import os
import random
import select
import socket
import struct
import sys
import threading
import time
import zlib

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HERE = os.path.dirname(os.path.abspath(__file__))


class RelayError(RuntimeError):
    """Carries a message meant to be shown to the user verbatim."""


def make_pose(source, yaw, pitch, roll, x=0.0, y=0.0, z=0.0, seq=0):
    return {
        "t": time.time(),
        "source": source,
        "seq": seq,
        "yaw": round(yaw, 4),
        "pitch": round(pitch, 4),
        "roll": round(roll, 4),
        "x": round(x, 5),
        "y": round(y, 5),
        "z": round(z, 5),
    }


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------
class _Client:
    def __init__(self, conn, addr):
        self.conn = conn
        self.addr = addr
        self.alive = True
        self.lock = threading.Lock()
        self.bytes_out = 0

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            try:
                chunk = self.conn.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    def handshake(self, request):
        headers = {}
        for line in request.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        key = headers.get("sec-websocket-key")
        if not key:
            return False
        import base64
        import hashlib
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()
        self.conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        return True

    def send_text(self, text):
        data = text.encode()
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(n)
        elif n < 65536:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        frame = bytes(header) + data
        with self.lock:
            if not self.alive:
                return False
            try:
                self.conn.sendall(frame)
                self.bytes_out += len(frame)
                return True
            except OSError:
                self.alive = False
                return False

    def read_loop(self):
        try:
            while self.alive:
                head = self._recv_exact(2)
                if not head:
                    break
                opcode = head[0] & 0x0F
                masked = head[1] & 0x80
                ln = head[1] & 0x7F
                if ln == 126:
                    ext = self._recv_exact(2)
                    if not ext:
                        break
                    ln = struct.unpack(">H", ext)[0]
                elif ln == 127:
                    ext = self._recv_exact(8)
                    if not ext:
                        break
                    ln = struct.unpack(">Q", ext)[0]
                mask = self._recv_exact(4) if masked else b""
                payload = self._recv_exact(ln) if ln else b""
                if payload is None:
                    break
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    with self.lock:
                        try:
                            self.conn.sendall(bytes([0x8A, len(payload)]) + payload)
                        except OSError:
                            break
        except Exception:
            pass
        finally:
            self.alive = False

    def close(self):
        self.alive = False
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Server: WebSocket + demo page + /status on one port
# ---------------------------------------------------------------------------
class WsServer:
    def __init__(self, host, port, log, on_clients=None):
        self.host = host
        self.port = port
        self.log = log
        self.on_clients = on_clients or (lambda n: None)
        self.clients = set()
        self.lock = threading.Lock()
        self.last = None
        self._stop = threading.Event()
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.srv.bind((host, port))
        except OSError as e:
            raise RelayError(
                f"Cannot use port {port} on {host} ({e.strerror or e}). "
                f"Another relay or app may be using it - try a different port."
            )
        self.srv.listen(8)
        self.srv.settimeout(0.5)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    # -- outgoing -----------------------------------------------------------
    def send(self, pose):
        self.last = pose
        payload = json.dumps(pose)
        with self.lock:
            dead = [c for c in self.clients if not c.send_text(payload)]
            for c in dead:
                self.clients.discard(c)

    @property
    def n_clients(self):
        with self.lock:
            return len(self.clients)

    # -- HTTP routing -------------------------------------------------------
    def _reply(self, conn, code, ctype, body):
        reason = {200: "OK", 404: "Not Found", 204: "No Content"}.get(code, "OK")
        head = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        try:
            conn.sendall(head + body)
        except OSError:
            pass

    def _serve_file(self, conn, name, ctype):
        path = os.path.join(HERE, name)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._reply(conn, 404, "text/plain", b"not found")
            return
        self._reply(conn, 200, ctype, body)

    def _status_json(self):
        return json.dumps(
            {
                "service": "xreal-web-bridge",
                "websocket": f"ws://{self.host}:{self.port}",
                "clients": self.n_clients,
                "last": self.last,
            },
            indent=2,
        ).encode()

    def _handle_http(self, conn, request):
        first = request.split("\r\n", 1)[0]
        parts = first.split()
        path = (parts[1] if len(parts) > 1 else "/").split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve_file(conn, "index.html", "text/html; charset=utf-8")
        elif path == "/status":
            self._reply(conn, 200, "application/json", self._status_json())
        elif path == "/favicon.ico":
            self._reply(conn, 204, "image/x-icon", b"")
        else:
            self._reply(conn, 404, "text/plain", b"not found")

    # -- accept loop --------------------------------------------------------
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            conn.settimeout(5)
            try:
                request = b""
                while b"\r\n\r\n" not in request and len(request) < 16384:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    request += chunk
                text = request.decode("latin-1", "replace")
                if "upgrade: websocket" not in text.lower():
                    try:
                        self._handle_http(conn, text)
                    finally:
                        conn.close()
                    continue
                conn.settimeout(None)
                cli = _Client(conn, addr)
                if not cli.handshake(text):
                    cli.close()
                    continue
            except Exception:
                try:
                    conn.close()
                except OSError:
                    pass
                continue

            with self.lock:
                self.clients.add(cli)
                last = self.last
            self.log(f"browser connected ({self.n_clients} client(s))")
            self.on_clients(self.n_clients)
            if last is not None:
                cli.send_text(json.dumps(last))
            threading.Thread(target=self._client_reader, args=(cli,), daemon=True).start()

    def _client_reader(self, cli):
        cli.read_loop()
        with self.lock:
            self.clients.discard(cli)
        self.log(f"browser disconnected ({self.n_clients} client(s))")
        self.on_clients(self.n_clients)
        cli.close()

    def close(self):
        self._stop.set()
        # shutdown() wakes the thread blocked in accept() straight away;
        # otherwise it lingers until its 0.5s socket timeout expires.
        try:
            self.srv.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.srv.close()
        except OSError:
            pass
        # Join the accept thread before returning. Until it exits it still holds
        # a reference to the listening socket, and the port stays busy - which
        # would make an immediate Disconnect -> Connect fail with
        # "Address already in use".
        if self._thread is not threading.current_thread():
            self._thread.join(1.0)

        with self.lock:
            clients = list(self.clients)
            self.clients.clear()
        for c in clients:
            c.close()


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def open_udp(host, port):
    """Bind the OpenTrack UDP listen socket. Raises RelayError."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as e:
        sock.close()
        raise RelayError(
            f"Cannot bind UDP {host}:{port} ({e.strerror or e}). "
            f"Something else is already listening there."
        )
    sock.setblocking(False)
    return sock


# The protocol below is from the reverse engineering by Void Computing
# (https://voidcomputing.hu/blog/worse-better-prettier/) and its reference
# implementation, badicsalex/ar-drivers-rs. It is *not* the same as the
# XREAL Light's, so do not copy Light code here.
#
# Host -> device command, always 64 bytes:
#
#   0x00   u8    head, always 0xaa
#   0x01   u32   CRC-32 (standard/zlib) of bytes [0x05 .. 0x05+length)
#   0x05   u16   length = 3 + len(data)
#   0x07   u8    command id
#   0x08   n     data
#
# The IMU stream is command 0x19; one data byte, 0x01 = on, 0x00 = off.
HID_VID = 0x3318
AIR_PID = 0x0424
AIR_IMU_INTERFACE = 3   # interface 3 = IMU + DSP  -> /dev/hidraw3
AIR_MCU_INTERFACE = 4   # interface 4 = MCU        -> /dev/hidraw4 (no pose data)
IMU_HEAD = 0xAA
IMU_CMD_ENABLE = 0x19
IMU_PACKET_SIZE = 0x40

# Sensor axes -> device axes, as (sign, source index) for each device axis.
# ar-drivers-rs uses (-x, +z, +y) for both gyro and accel.
SENSOR_AXES = ((-1.0, 0), (1.0, 2), (1.0, 1))

# Which device axis drives each euler angle: "yaw=z-,pitch=x-,roll=y+".
# This is the one thing that is mount/firmware specific - retune with --axis.
DEFAULT_AXIS_MAP = "yaw=z-,pitch=x-,roll=y+"


def build_imu_command(cmd_id, data=b""):
    """Serialise a 64-byte IMU-interface command (layout above)."""
    pkt = bytearray(IMU_PACKET_SIZE)
    pkt[0x00] = IMU_HEAD
    length = len(data) + 3
    struct.pack_into("<H", pkt, 0x05, length)
    pkt[0x07] = cmd_id
    pkt[0x08:0x08 + len(data)] = data
    crc = zlib.crc32(bytes(pkt[0x05:0x05 + length])) & 0xFFFFFFFF
    struct.pack_into("<I", pkt, 0x01, crc)
    return bytes(pkt)


def _i24(data, off):
    """3-byte little-endian two's-complement int (the IMU is full of these)."""
    v = data[off] | (data[off + 1] << 8) | (data[off + 2] << 16)
    return v - 0x1000000 if v & 0x800000 else v


def parse_axis_map(spec):
    """'yaw=z-,pitch=x-,roll=y+' -> {'yaw': (2, -1.0), ...}"""
    idx = {"x": 0, "y": 1, "z": 2}
    out = {}
    for part in (spec or "").split(","):
        if "=" not in part:
            continue
        name, _, val = part.partition("=")
        name, val = name.strip().lower(), val.strip().lower()
        axis = val.replace("+", "").replace("-", "")
        if name in ("yaw", "pitch", "roll") and axis in idx:
            out[name] = (idx[axis], -1.0 if "-" in val else 1.0)
    missing = {"yaw", "pitch", "roll"} - set(out)
    if missing:
        raise RelayError(
            "Bad --axis map %r: missing %s. Use e.g. \"%s\""
            % (spec, ", ".join(sorted(missing)), DEFAULT_AXIS_MAP)
        )
    return out


def list_hidraw():
    """Enumerate /dev/hidraw* with their USB VID/PID and interface number."""
    out = []
    base = "/sys/class/hidraw"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        link = os.path.join(base, name, "device")
        try:
            hdev = os.path.realpath(link)
            uevent = {}
            with open(os.path.join(link, "uevent")) as fh:
                for line in fh:
                    if "=" in line:
                        k, v = line.rstrip("\n").split("=", 1)
                        uevent[k] = v
        except OSError:
            continue

        vid = pid = iface = None
        bits = uevent.get("HID_ID", "").split(":")
        if len(bits) == 3:
            try:
                vid, pid = int(bits[1], 16), int(bits[2], 16)
            except ValueError:
                pass
        # the sysfs interface dir looks like "3-3:1.3" => config 1, interface 3
        iface_dir = os.path.basename(os.path.dirname(hdev))
        if ":" in iface_dir and "." in iface_dir:
            try:
                iface = int(iface_dir.split(":")[1].split(".")[1], 16)
            except (IndexError, ValueError):
                pass

        out.append({
            "node": "/dev/" + name,
            "name": uevent.get("HID_NAME", "?"),
            "vid": vid, "pid": pid, "iface": iface,
            "uniq": uevent.get("HID_UNIQ") or "",
        })
    return out


def describe_hidraw(nodes):
    """Human-readable node table, used in error messages and --list-hidraw."""
    if not nodes:
        return "  (no /dev/hidraw* nodes - are the glasses plugged in?)"
    lines = []
    for n in nodes:
        vidpid = (f"{n['vid']:04x}:{n['pid']:04x}"
                  if n["vid"] is not None else "????:????")
        iface = f"if {n['iface']:>2}" if n["iface"] is not None else "if  ?"
        if n["vid"] == HID_VID and n["iface"] == AIR_IMU_INTERFACE:
            tag = "  <- glasses IMU (use this one)"
        elif n["vid"] == HID_VID and n["iface"] == AIR_MCU_INTERFACE:
            tag = "  <- glasses MCU (never sends pose)"
        elif n["vid"] == HID_VID:
            tag = "  <- glasses"
        else:
            tag = ""
        lines.append(f"  {n['node']:<15} {vidpid}  {iface}  {n['name']}{tag}")
    return "\n".join(lines)


def pick_hidraw(device=None, interface=None):
    """Decide which node to open. Returns the node dict, or None if unusable."""
    glasses = [n for n in list_hidraw() if n["vid"] == HID_VID]
    if device:
        for n in glasses:
            if n["node"] == device:
                return n
        if os.path.exists(device):
            return {"node": device, "name": "?", "vid": None, "pid": None, "iface": None}
        return None
    if not glasses:
        return None
    if interface is not None:
        for n in glasses:
            if n["iface"] == interface:
                return n
        return None
    for n in glasses:
        if n["iface"] == AIR_IMU_INTERFACE:
            return n
    return glasses[0]


def open_hidraw(path):
    """Open the glasses' IMU node read-write. Raises RelayError with the fix."""
    try:
        return os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except FileNotFoundError:
        raise RelayError(
            f"{path} does not exist.\nNodes seen now:\n{describe_hidraw(list_hidraw())}"
        )
    except PermissionError:
        raise RelayError(
            f"Permission denied opening {path}.\n"
            "  Fix once with:\n"
            "    sudo cp scripts/70-xreal-air.rules /etc/udev/rules.d/\n"
            "    sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=hidraw\n"
            "  then unplug/replug the glasses."
        )
    except OSError as e:
        raise RelayError(f"Cannot open {path}: {e.strerror or e}")


def gen_simulate(rate=100.0, stop=None, log=None):
    """Synthetic smooth head motion - proves the pipeline before permissions."""
    stop = stop or threading.Event()
    dt = 1.0 / max(rate, 1.0)
    t0 = time.time()
    seq = 0
    while not stop.is_set():
        t = time.time() - t0
        yaw = 42.0 * math.sin(0.31 * t) + 9.0 * math.sin(1.7 * t)
        pitch = 16.0 * math.sin(0.23 * t + 0.7) + 5.0 * math.sin(2.1 * t)
        roll = 13.0 * math.sin(0.47 * t + 1.3) + 3.5 * math.sin(2.9 * t)
        yaw += random.gauss(0, 0.15)
        pitch += random.gauss(0, 0.15)
        roll += random.gauss(0, 0.15)
        seq += 1
        yield make_pose("simulate", yaw, pitch, roll, seq=seq)
        stop.wait(dt)


def _parse_opentrack(buf):
    """6 x float64 (x,y,z,yaw,pitch,roll) + uint32 frame."""
    if len(buf) < 48:
        return None
    for endian in ("<", ">"):
        try:
            vals = struct.unpack(endian + "6d", buf[:48])
        except struct.error:
            continue
        if all(math.isfinite(v) and abs(v) < 1e5 for v in vals):
            return vals
    return None


def gen_opentrack(sock, stop=None, log=None):
    stop = stop or threading.Event()
    log = log or (lambda m: None)
    warned = False
    while not stop.is_set():
        try:
            r, _, _ = select.select([sock], [], [], 0.5)
        except (OSError, ValueError):
            break
        if not r:
            if not warned:
                log("waiting for UDP pose packets...")
                warned = True
            continue
        try:
            buf, _ = sock.recvfrom(2048)
        except OSError:
            break
        if buf[:1] == b"{":                      # hand-test: {"yaw":10,...}
            try:
                j = json.loads(buf)
                yield make_pose("opentrack", j.get("yaw", 0), j.get("pitch", 0),
                                j.get("roll", 0), j.get("x", 0), j.get("y", 0), j.get("z", 0))
                continue
            except Exception:
                pass
        vals = _parse_opentrack(buf)
        if vals is None:
            log(f"ignored {len(buf)}B UDP packet (not OpenTrack format)")
            continue
        x, y, z, yaw, pitch, roll = vals
        yield make_pose("opentrack", yaw, pitch, roll, x, y, z)


def parse_imu_packet(data):
    """Decode a device->host IMU report, or None if this is not one.

    Layout (from ar-drivers-rs):
      0x00 u16  header, 0x01 0x02
      0x02 u16  temperature (raw, ICM-20602)
      0x04 u64  timestamp, nanoseconds
      0x0c u16  gyro multiplier     0x0e u32 gyro divisor
      0x12/0x15/0x18  i24 gyro x/y/z
      0x1b u16  accel multiplier    0x1d u32 accel divisor
      0x21/0x24/0x27  i24 accel x/y/z

    Each reading is raw * multiplier / divisor, in deg/s and g. The multiplier
    and divisor arrive with every packet, so no hardcoded scaling is needed.
    """
    if len(data) < 0x36 or data[0] != 0x01 or data[1] != 0x02:
        return None
    g_mul, g_div = struct.unpack_from("<HI", data, 0x0C)
    a_mul, a_div = struct.unpack_from("<HI", data, 0x1B)
    if not g_div or not a_div:
        return None
    g_src = [_i24(data, o) * g_mul / g_div for o in (0x12, 0x15, 0x18)]
    a_src = [_i24(data, o) * a_mul / a_div for o in (0x21, 0x24, 0x27)]
    return {
        "gyro": [sign * g_src[i] for sign, i in SENSOR_AXES],   # deg/s, device axes
        "accel": [sign * a_src[i] for sign, i in SENSOR_AXES],  # g, device axes
        "gyro_raw": g_src,
        "accel_raw": a_src,
        "g_mul": g_mul, "g_div": g_div, "a_mul": a_mul, "a_div": a_div,
        "temp": struct.unpack_from("<H", data, 0x02)[0] / 326.8 + 25.0,
    }


def gen_hidraw(fd, stop=None, log=None, verbose=False, axis_map=None, where="hidraw",
               control=None):
    """Stream the XREAL Air IMU as yaw/pitch/roll.

    The glasses stay quiet until they get a correctly checksummed 'enable IMU
    stream' command, so most of this is about failing loudly when the handshake
    does not happen instead of just sitting there.
    """
    stop = stop or threading.Event()
    log = log or (lambda m: None)
    axes = parse_axis_map(axis_map or DEFAULT_AXIS_MAP)

    def read_packet(timeout):
        try:
            ready, _, _ = select.select([fd], [], [], timeout)
        except (OSError, ValueError):
            return None
        if not ready:
            return None
        try:
            return os.read(fd, IMU_PACKET_SIZE)
        except BlockingIOError:
            return None
        except OSError as e:
            log(f"{where}: read failed: {e.strerror or e}")
            return None

    # -- handshake: stream off (also flushes stale packets), then on ---------
    acked = False
    for value, what in ((b"\x00", "off"), (b"\x01", "on")):
        pkt = build_imu_command(IMU_CMD_ENABLE, value)
        try:
            os.write(fd, pkt)
        except OSError as e:
            raise RelayError(
                f"Cannot write to {where}: {e.strerror or e}\n"
                "  Is something else holding the glasses? "
                "(XRLinuxDriver, Chrome, an old bridge.py)"
            )
        log(f"{where}: sent IMU stream {what}: {pkt[:9].hex(' ')}")

        ack = None
        deadline = time.time() + 1.0
        while time.time() < deadline and not stop.is_set():
            d = read_packet(0.1)
            if d and d[0] == IMU_HEAD:
                ack = d
                break
        if ack is None:
            log(f"{where}: no ack to IMU stream {what} (waited 1s on {where})")
        elif what == "on":
            acked = True
            log(f"{where}: ack ok: {ack[:9].hex(' ')}")

    # -- stream ------------------------------------------------------------
    yaw = pitch = roll = 0.0
    ref_pitch = ref_roll = 0.0       # gravity reference at the last recenter
    last = time.time()
    n_reports = n_odd = 0
    first_seen = False
    warned_at = 0.0

    # Yaw has no absolute reference, so a resting gyro bias integrates straight
    # into drift (this unit reads about -0.8 deg/s, i.e. 48 deg/min). Average
    # the gyro over the first fraction of a second and subtract it.
    calib_from = time.time()
    calib = []                       # the samples, not the mean
    CALIB_SECONDS = 0.6
    bias = None                      # None until decided

    while not stop.is_set():
        data = read_packet(0.25)

        if data is None:
            silent_for = time.time() - last
            if n_reports == 0 and silent_for > 3.0 and time.time() - warned_at > 15.0:
                warned_at = time.time()
                log(
                    f"{where}: no IMU data after {silent_for:.0f}s"
                    f"{'' if acked else ' (the enable command was never acked)'}.\n"
                    "  Things to check:\n"
                    "  * wrong interface? the nodes right now are:\n"
                    + describe_hidraw(list_hidraw()) + "\n"
                    "  * is something else holding the glasses? "
                    "(XRLinuxDriver, Chrome, another bridge.py)\n"
                    "  * run:  python3 bridge/bridge.py --list-hidraw"
                )
            continue

        parsed = parse_imu_packet(data)
        if parsed is None:
            n_odd += 1
            if verbose and n_odd <= 8:
                log(f"{where}: ignoring non-IMU report ({len(data)}B): {data[:24].hex(' ')}")
            continue

        n_reports += 1
        if not first_seen:
            first_seen = True
            log(
                f"{where}: first IMU report: {data[:40].hex(' ')}\n"
                f"{where}: gyro mul/div={parsed['g_mul']}/{parsed['g_div']}  "
                f"accel mul/div={parsed['a_mul']}/{parsed['a_div']}  "
                f"temp={parsed['temp']:.1f}C"
            )

        now = time.time()
        dt = min(max(now - last, 0.0), 0.05)
        last = now

        gyro, accel = parsed["gyro"], parsed["accel"]
        if verbose and n_reports % 100 == 0:
            g = " ".join(f"{v:8.2f}" for v in gyro)
            a = " ".join(f"{v:8.4f}" for v in accel)
            log(f"{where}: gyro [{g}] deg/s | accel [{a}] g | {n_reports} reports")

        yaw += gyro[axes["yaw"][0]] * axes["yaw"][1] * dt
        pitch += gyro[axes["pitch"][0]] * axes["pitch"][1] * dt
        roll += gyro[axes["roll"][0]] * axes["roll"][1] * dt

        # The accelerometer gives an absolute pitch/roll reference. There is no
        # magnetometer, so yaw can only ever be integrated, and it will drift.
        norm = math.sqrt(sum(v * v for v in accel)) or 1.0
        ax, ay, az = (v / norm for v in accel)
        pitch_acc = math.degrees(math.atan2(-ax, math.sqrt(ay * ay + az * az)))
        roll_acc = math.degrees(math.atan2(ay, az))

        if control is not None and control.take_recenter():
            # Zero the pose, and treat the way gravity points *right now* as
            # level. Without moving the reference too, the accelerometer would
            # drag pitch and roll straight back to where they were.
            yaw = pitch = roll = 0.0
            ref_pitch, ref_roll = pitch_acc, roll_acc
            log(f"{where}: recentered (level now {ref_pitch:.1f} / {ref_roll:.1f})")

        # Time-based blend: the device streams at ~900 Hz, so a per-sample
        # constant would make the filter rate-dependent.
        alpha = math.exp(-dt / 0.5)          # 0.5s time constant
        pitch = alpha * pitch + (1.0 - alpha) * (pitch_acc - ref_pitch)
        roll = alpha * roll + (1.0 - alpha) * (roll_acc - ref_roll)

        yield make_pose("hidraw", yaw, pitch, roll)


# ---------------------------------------------------------------------------
# Relay: glue + metrics
# ---------------------------------------------------------------------------
class Control:
    """Runtime knobs the UI can change while a source is streaming.

    Deliberately separate from the generators, so "recenter" and per-axis
    inversion behave the same for every source (simulate, OpenTrack, hidraw).
    """

    AXES = ("yaw", "pitch", "roll")

    def __init__(self):
        self._lock = threading.Lock()
        self._invert = {a: False for a in self.AXES}
        self._recenter = threading.Event()

    def set_invert(self, axis, value):
        if axis in self._invert:
            with self._lock:
                self._invert[axis] = bool(value)

    def invert(self):
        with self._lock:
            return dict(self._invert)

    def request_recenter(self):
        self._recenter.set()

    def take_recenter(self):
        """True at most once per request."""
        if self._recenter.is_set():
            self._recenter.clear()
            return True
        return False


class Relay:
    """Start/stop the whole pipeline and report metrics. Thread-safe reads."""

    SOURCES = ("simulate", "opentrack", "hidraw")

    def __init__(self, log=None):
        self._log = log or (lambda m: None)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._srv = None
        self._thread = None
        self._gen = None
        self._udp = None
        self._fd = None
        self.control = Control()
        self.reset_metrics()

    # -- view controls ------------------------------------------------------
    def recenter(self):
        """Treat the pose the glasses are in right now as zero.

        Returns False when nothing is streaming, so a stale request cannot
        fire later on the next start.
        """
        if not self.running:
            return False
        self.control.request_recenter()
        return True

    def set_invert(self, axis, value):
        self.control.set_invert(axis, value)

    def invert(self):
        return self.control.invert()

    def _apply_controls(self, pose):
        """Apply the recenter offset, then per-axis sign inversion.

        Done here rather than in the generators so every source behaves the
        same, and so the metrics match exactly what the browser receives.

        hidraw is the exception: its generator consumes the recenter request
        itself (it also has to move the gravity reference), so by the time we
        get here take_recenter() is already False and the offset stays 0.
        """
        if self.control.take_recenter():
            self._offsets = (
                pose.get("yaw", 0.0),
                pose.get("pitch", 0.0),
                pose.get("roll", 0.0),
            )
            self._log(
                "recentered: yaw/pitch/roll now read from "
                f"{self._offsets[0]:.1f} / {self._offsets[1]:.1f} / "
                f"{self._offsets[2]:.1f}"
            )
        off_yaw, off_pitch, off_roll = self._offsets
        inv = self.control.invert()
        return {
            **pose,
            "yaw": (pose.get("yaw", 0.0) - off_yaw) * (-1.0 if inv["yaw"] else 1.0),
            "pitch": (pose.get("pitch", 0.0) - off_pitch) * (-1.0 if inv["pitch"] else 1.0),
            "roll": (pose.get("roll", 0.0) - off_roll) * (-1.0 if inv["roll"] else 1.0),
        }

    # -- metrics ------------------------------------------------------------
    def reset_metrics(self):
        with self._lock:
            self._offsets = (0.0, 0.0, 0.0)
            self._running = False
            self._source = None
            self._msgs = 0
            self._times = []
            self._last = None
            self._error = None
            self._started_at = 0.0
            self._clients = 0
            self._cfg = {}

    @property
    def running(self):
        with self._lock:
            return self._running

    def snapshot(self):
        with self._lock:
            times = list(self._times)
            last = self._last
            now = time.time()
            rate = 0.0
            if len(times) > 1:
                span = times[-1] - times[0]
                if span > 0:
                    rate = (len(times) - 1) / span
            return {
                "running": self._running,
                "source": self._source,
                "clients": self._clients,
                "msgs": self._msgs,
                "hz": rate,
                "latency_ms": (now - last["t"]) * 1000.0 if last else None,
                "last": last,
                "uptime": now - self._started_at if self._started_at else 0.0,
                "error": self._error,
                "ws_url": self._cfg.get("ws_url"),
                "udp": self._cfg.get("udp"),
                "device": self._cfg.get("device"),
            }

    def _bump(self, pose):
        with self._lock:
            self._msgs += 1
            self._times.append(time.time())
            if len(self._times) > 200:
                self._times.pop(0)
            self._last = pose

    def _set_clients(self, n):
        with self._lock:
            self._clients = n

    # -- lifecycle ----------------------------------------------------------
    def start(self, source, ws_host="127.0.0.1", ws_port=8765,
              udp_host="127.0.0.1", udp_port=4242, device=None,
              rate=100.0, max_hz=120.0, interface=None, axis_map=None,
              verbose=False):
        with self._lock:
            if self._running:
                raise RelayError("Relay is already running.")
        if source not in self.SOURCES:
            raise RelayError(f"Unknown source {source!r}")

        node = None
        if source == "hidraw":
            # Resolve the node up front so the UI can show which one was picked
            # and so a bad choice is an immediate, specific error.
            node = pick_hidraw(device, interface)
            if node is None:
                if device:
                    raise RelayError(
                        f"No usable hidraw node for {device}.\n"
                        f"Nodes seen now:\n{describe_hidraw(list_hidraw())}"
                    )
                raise RelayError(
                    "No XREAL glasses found on any /dev/hidraw* node.\n"
                    f"Nodes seen now:\n{describe_hidraw(list_hidraw())}\n"
                    "  (plugged in? try `python3 bridge/bridge.py --list-hidraw`)"
                )

        self._stop.clear()
        self.reset_metrics()          # also clears _error
        with self._lock:
            self._running = True
            self._source = source
            self._started_at = time.time()
            self._cfg = {
                "ws_url": f"ws://{ws_host}:{ws_port}",
                "udp": f"{udp_host}:{udp_port}" if source == "opentrack" else None,
                "device": node["node"] if node else None,
            }

        try:
            # prepare the source *before* claiming the port, so permission and
            # in-use errors are immediate and specific
            if source == "opentrack":
                self._udp = open_udp(udp_host, udp_port)
                self._log(f"listening for OpenTrack UDP on {udp_host}:{udp_port}")
                self._gen = gen_opentrack(self._udp, self._stop, self._log)
            elif source == "hidraw":
                self._fd = open_hidraw(node["node"])
                iface = f" (USB interface {node['iface']})" if node["iface"] is not None else ""
                self._log(f"opened {node['node']}{iface} - {node['name']}")
                self._gen = gen_hidraw(
                    self._fd, self._stop, self._log,
                    verbose=verbose, axis_map=axis_map, where=node["node"],
                    control=self.control,
                )
            else:
                self._gen = gen_simulate(rate, self._stop, self._log)

            self._srv = WsServer(ws_host, ws_port, self._log, self._set_clients)
        except Exception:
            self._cleanup_handles()
            with self._lock:
                self._running = False
            raise

        self._log(f"relay running: ws://{ws_host}:{ws_port}  (source: {source})")
        self._max_dt = 1.0 / max(max_hz, 1.0)
        self._thread = threading.Thread(
            target=self._run, args=(self._gen, self._srv), daemon=True
        )
        self._thread.start()

    def _run(self, gen, srv):
        last_send = 0.0
        try:
            for pose in gen:
                if self._stop.is_set():
                    break
                now = time.time()
                if now - last_send < self._max_dt:
                    continue
                last_send = now
                pose = self._apply_controls(pose)
                srv.send(pose)
                self._bump(pose)
        except RelayError as e:
            with self._lock:
                self._error = str(e)
            self._log("error: " + str(e).replace("\n", "\n         "))
        except Exception as e:                       # pragma: no cover
            with self._lock:
                self._error = f"{type(e).__name__}: {e}"
            self._log("error: " + self._error)
        finally:
            if self._srv is not None:
                self._srv.close()
            with self._lock:
                self._running = False
            self._log("relay stopped")

    def _cleanup_handles(self):
        if self._gen is not None:
            try:
                self._gen.close()
            except Exception:
                pass
            self._gen = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self._udp is not None:
            try:
                self._udp.close()
            except OSError:
                pass
            self._udp = None

    def stop(self, timeout=2.0):
        self._stop.set()
        if self._srv is not None:
            self._srv.close()
            self._srv = None
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self._cleanup_handles()
        with self._lock:
            self._running = False
