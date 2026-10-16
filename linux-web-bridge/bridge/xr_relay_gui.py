#!/usr/bin/env python3
"""
xr-relay-gui.py - windowed XR Relay.

A normal GTK application: it appears in the Ubuntu dock, has a title, and the
relay stops when you close the window. Nothing is left running in the
background.

    Connect / Disconnect  - start/stop the relay
    Source                - simulate | OpenTrack UDP | hidraw device
    Live                  - clients, messages, rate, latency + attitude gauge
    Log                   - every event, including the exact fix when something
                            fails (port busy, device permissions, ...)
"""

import os
import queue
import sys
import time

import gi

try:
    import cairo
except ImportError:  # only needed for the offscreen screenshot hook
    cairo = None

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay  # noqa: E402

APP_ID = "io.github.jakedowns.XrRelay"
HERE = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(HERE, "assets", "xr-relay.svg")

SOURCES = [
    ("simulate", "Simulate (no hardware needed)"),
    ("opentrack", "OpenTrack UDP  (via XRLinuxDriver)"),
    ("hidraw", "hidraw device  (direct IMU)"),
]

C_OK, C_WARN, C_BAD, C_OFF = "#3fb950", "#d29922", "#f85149", "#8b949e"


class RelayWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="XR Relay")
        self.set_default_size(600, 680)
        self.set_size_request(520, 560)
        try:
            self.set_icon_from_file(ICON_PATH)
        except Exception:
            pass

        self.log_q = queue.Queue()
        self.relay = relay.Relay(log=self.log_q.put)
        self._last_error = None
        self._ui_running = False

        self._build()
        self.connect("destroy", self._on_destroy)
        GLib.timeout_add(120, self._tick)
        GLib.timeout_add(120, self._drain_log)

        self.log("XR Relay ready. Pick a source, then press Connect.")
        self.log("Tip: 'Simulate' proves the pipeline works before you touch device permissions.")
        self._selftest_hook()

    # Dev/CI hooks, all optional and independent:
    #   XR_RELAY_AUTOSTART=1        press Connect on startup
    #   XR_RELAY_SOURCE=hidraw      which source to Connect with
    #   XR_RELAY_INVERT=yaw,pitch   tick those invert boxes
    #   XR_RELAY_RECENTER=<ms>      press Recenter after that delay
    #   XR_RELAY_SHOT=/path.png     render the window to PNG, then quit
    #   XR_RELAY_SHOT_DELAY=<ms>    how long to let it run first (default 2200)
    def _selftest_hook(self):
        if os.environ.get("XR_RELAY_AUTOSTART"):
            self.combo.set_active_id(os.environ.get("XR_RELAY_SOURCE", "simulate"))
            self._on_connect()

        for axis in os.environ.get("XR_RELAY_INVERT", "").split(","):
            axis = axis.strip()
            if axis in self.invert_btns:
                self.invert_btns[axis].set_active(True)

        recenter = os.environ.get("XR_RELAY_RECENTER")
        if recenter:
            GLib.timeout_add(int(recenter), lambda: (self._on_recenter(), False)[1])

        shot = os.environ.get("XR_RELAY_SHOT")
        if not shot:
            return

        def capture():
            # Render the content offscreen: works on Wayland and X11 alike,
            # and needs no compositor, so it is reliable in CI too.
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            target = self.get_child() or self
            alloc = target.get_allocation()
            print(
                f"[selftest] target={type(target).__name__} alloc={alloc.width}x{alloc.height}"
                f"@{alloc.x},{alloc.y} realized={target.get_realized()}"
                f" mapped={target.get_mapped()} visible={target.get_visible()}",
                flush=True,
            )
            surface = cairo.ImageSurface(
                cairo.FORMAT_ARGB32, max(alloc.width, 1), max(alloc.height, 1)
            )
            cr = cairo.Context(surface)
            target.draw(cr)
            surface.write_to_png(shot)
            print(f"[selftest] wrote {shot}", flush=True)
            self.get_application().quit()
            return False

        GLib.timeout_add(int(os.environ.get("XR_RELAY_SHOT_DELAY", "2200")), capture)

    # ------------------------------------------------------------------ UI
    def _build(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_border_width(12)
        self.add(outer)

        # status banner
        self.status_lbl = Gtk.Label(xalign=0)
        self.status_lbl.set_markup("<span foreground='%s'>●</span>  <b>Stopped</b>" % C_OFF)
        outer.pack_start(self.status_lbl, False, False, 0)

        self.detail_lbl = Gtk.Label(xalign=0)
        self.detail_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.detail_lbl.set_markup("<span foreground='#8b949e' size='small'>relay not running</span>")
        outer.pack_start(self.detail_lbl, False, False, 0)

        # ---- source controls
        src_frame = Gtk.Frame(label="Source")
        outer.pack_start(src_frame, False, False, 0)
        grid = Gtk.Grid(column_spacing=8, row_spacing=6, margin=10)
        src_frame.add(grid)

        grid.attach(Gtk.Label(label="Mode", xalign=1), 0, 0, 1, 1)
        self.combo = Gtk.ComboBoxText()
        for sid, label in SOURCES:
            self.combo.append(sid, label)
        self.combo.set_active_id("simulate")
        self.combo.connect("changed", lambda *_: self._sync_sensitivity())
        grid.attach(self.combo, 1, 0, 3, 1)

        grid.attach(Gtk.Label(label="Device", xalign=1), 0, 1, 1, 1)
        devbox = Gtk.Box(spacing=6)
        self.device_combo = Gtk.ComboBoxText()
        self.device_combo.set_hexpand(True)
        devbox.pack_start(self.device_combo, True, True, 0)
        rescan = Gtk.Button(label="Rescan")
        rescan.set_tooltip_text("Look for the glasses and list every hidraw interface")
        rescan.connect("clicked", lambda *_: self._refresh_devices(log_it=True))
        devbox.pack_start(rescan, False, False, 0)
        grid.attach(devbox, 1, 1, 3, 1)
        self._refresh_devices()

        grid.attach(Gtk.Label(label="UDP port", xalign=1), 0, 2, 1, 1)
        self.udp_spin = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.udp_spin.set_value(4242)
        grid.attach(self.udp_spin, 1, 2, 1, 1)
        grid.attach(Gtk.Label(label="WS port", xalign=1), 2, 2, 1, 1)
        self.ws_spin = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.ws_spin.set_value(8765)
        grid.attach(self.ws_spin, 3, 2, 1, 1)

        btns = Gtk.Box(spacing=8, margin_top=4)
        self.connect_btn = Gtk.Button(label="Connect")
        self.connect_btn.get_style_context().add_class("suggested-action")
        self.connect_btn.connect("clicked", self._on_connect)
        self.disconnect_btn = Gtk.Button(label="Disconnect")
        self.disconnect_btn.set_sensitive(False)
        self.disconnect_btn.connect("clicked", self._on_disconnect)
        btns.pack_start(self.connect_btn, False, False, 0)
        btns.pack_start(self.disconnect_btn, False, False, 0)
        grid.attach(btns, 0, 3, 4, 1)

        # ---- live metrics
        live_frame = Gtk.Frame(label="Live")
        outer.pack_start(live_frame, False, False, 0)
        live = Gtk.Box(spacing=14, margin=10)
        live_frame.add(live)

        gauge = Gtk.DrawingArea()
        gauge.set_size_request(190, 116)
        gauge.connect("draw", self._on_draw)
        self.gauge = gauge
        live.pack_start(gauge, False, False, 0)

        m = Gtk.Grid(column_spacing=10, row_spacing=4)
        live.pack_start(m, True, True, 0)
        self.metrics = {}
        rows = [("clients", "Browsers"), ("msgs", "Messages"), ("hz", "Rate"),
                ("lat", "Latency"), ("up", "Uptime"),
                ("yaw", "Yaw"), ("pitch", "Pitch"), ("roll", "Roll")]
        for i, (key, label) in enumerate(rows):
            m.attach(Gtk.Label(label=label, xalign=1), 0, i, 1, 1)
            v = Gtk.Label(label="—", xalign=0)
            v.set_markup("<span foreground='#58a6ff'>—</span>")
            m.attach(v, 1, i, 1, 1)
            self.metrics[key] = v

        link = Gtk.Box(spacing=6, margin_top=6)
        self.url_lbl = Gtk.Label(xalign=0)
        self.url_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.url_lbl.set_markup("<span foreground='#8b949e' size='small'>—</span>")
        link.pack_start(self.url_lbl, True, True, 0)
        copy_btn = Gtk.Button(label="Copy URL")
        copy_btn.connect("clicked", self._on_copy)
        open_btn = Gtk.Button(label="Open demo page")
        open_btn.connect("clicked", self._on_open)
        link.pack_start(copy_btn, False, False, 0)
        link.pack_start(open_btn, False, False, 0)
        m.attach(link, 0, len(rows), 2, 1)

        # ---- view controls: recenter + per-axis inversion (as on the demo page)
        view = Gtk.Box(spacing=8, margin_top=6)
        self.recenter_btn = Gtk.Button(label="Recenter")
        self.recenter_btn.set_tooltip_text(
            "Treat wherever the glasses are pointing right now as zero"
        )
        self.recenter_btn.connect("clicked", self._on_recenter)
        self.recenter_btn.set_sensitive(False)
        view.pack_start(self.recenter_btn, False, False, 0)
        view.pack_start(Gtk.Label(label="invert:", xalign=0), False, False, 0)
        self.invert_btns = {}
        for axis in ("yaw", "pitch", "roll"):
            cb = Gtk.CheckButton(label=axis)
            cb.set_tooltip_text(f"Flip the sign of {axis}")
            cb.connect("toggled", self._on_invert, axis)
            view.pack_start(cb, False, False, 0)
            self.invert_btns[axis] = cb
        m.attach(view, 0, len(rows) + 1, 2, 1)

        # ---- log
        log_frame = Gtk.Frame(label="Log")
        outer.pack_start(log_frame, True, True, 0)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.set_shadow_type(Gtk.ShadowType.IN)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_name("relay-log")
        self.log_buf = self.log_view.get_buffer()
        sw.add(self.log_view)
        log_frame.add(sw)

        self._sync_sensitivity()

    def _refresh_devices(self, log_it=False):
        """List the glasses' hidraw interfaces so the right one is pickable."""
        want = self.device_combo.get_active_id()
        self.device_combo.remove_all()
        self.device_combo.append("auto", "Auto-detect (glasses IMU interface)")

        nodes = [n for n in relay.list_hidraw() if n["vid"] == relay.HID_VID]
        for n in nodes:
            label = f"{n['node']}  -  interface {n['iface']}"
            if n["iface"] == relay.AIR_IMU_INTERFACE:
                label += "   (IMU - has the pose)"
            elif n["iface"] == relay.AIR_MCU_INTERFACE:
                label += "   (MCU - never sends pose)"
            self.device_combo.append(n["node"], label)

        ids = {"auto"} | {n["node"] for n in nodes}
        self.device_combo.set_active_id(want if want in ids else "auto")
        if not self.device_combo.get_active_id():
            self.device_combo.set_active_id("auto")

        if log_it:
            self.log(f"found {len(nodes)} glasses interface(s):")
            for line in relay.describe_hidraw(relay.list_hidraw()).splitlines():
                self.log(line)
            if not nodes:
                self.log("  none - plugged in? see scripts/70-xreal-air.rules")

        return nodes

    def _sync_sensitivity(self):
        src = self.combo.get_active_id()
        self.device_combo.set_sensitive(src == "hidraw")
        self.udp_spin.set_sensitive(src == "opentrack")
        self.ws_spin.set_sensitive(not self._ui_running)
        self.combo.set_sensitive(not self._ui_running)

    # -------------------------------------------------------------- logging
    def log(self, msg):
        self.log_q.put(msg)

    def _drain_log(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                stamp = time.strftime("%H:%M:%S")
                text = "".join(f"{stamp}  {ln}\n" for ln in str(msg).splitlines())
                end = self.log_buf.get_end_iter()
                self.log_buf.insert(end, text)
                # keep it bounded
                if self.log_buf.get_line_count() > 500:
                    start = self.log_buf.get_start_iter()
                    cut = self.log_buf.get_iter_at_line(self.log_buf.get_line_count() - 400)
                    self.log_buf.delete(start, cut)
                mark = self.log_buf.create_mark(None, self.log_buf.get_end_iter(), False)
                self.log_view.scroll_mark_onscreen(mark)
        except queue.Empty:
            pass
        return True

    # ------------------------------------------------------------- actions
    def _on_connect(self, *_):
        if self._ui_running:
            return
        src = self.combo.get_active_id()
        picked = self.device_combo.get_active_id()
        try:
            self.relay.start(
                src,
                ws_port=int(self.ws_spin.get_value()),
                udp_port=int(self.udp_spin.get_value()),
                device=None if picked in (None, "auto") else picked,
            )
        except relay.RelayError as e:
            self._fail(str(e))
            return
        self._ui_running = True
        self._last_error = None
        self.detail_lbl.set_markup(
            "<span foreground='#8b949e' size='small'>%s</span>" % self._escape(self._detail_text())
        )
        self._sync_sensitivity()

    def _on_disconnect(self, *_):
        self.relay.stop()
        self._set_stopped("stopped by user")

    def _detail_text(self):
        s = self.relay.snapshot()
        bits = []
        if s["udp"]:
            bits.append(f"listening UDP {s['udp']}")
        if s["device"]:
            bits.append(f"device {s['device']}")
        bits.append(f"serving http://127.0.0.1:{self.ws_spin.get_value_as_int()}/")
        return " - ".join(bits)

    def _fail(self, msg):
        self._last_error = msg
        self.log(msg)
        first = str(msg).splitlines()[0]
        self.status_lbl.set_markup(
            "<span foreground='%s'>●</span>  <b>Failed to start</b>" % C_BAD
        )
        self.detail_lbl.set_markup(
            "<span foreground='%s' size='small'>%s</span>" % (C_BAD, self._escape(first))
        )
        self._ui_running = False
        self._sync_sensitivity()

    def _set_stopped(self, why=""):
        self._ui_running = False
        self.status_lbl.set_markup("<span foreground='%s'>●</span>  <b>Stopped</b>" % C_OFF)
        self.detail_lbl.set_markup(
            "<span foreground='#8b949e' size='small'>%s</span>" % self._escape(why)
        )
        for v in self.metrics.values():
            v.set_markup("<span foreground='#58a6ff'>—</span>")
        self.url_lbl.set_markup("<span foreground='#8b949e' size='small'>—</span>")
        self.gauge.queue_draw()
        self._sync_sensitivity()

    @staticmethod
    def _escape(s):
        return GLib.markup_escape_text(str(s))

    def _on_copy(self, *_):
        s = self.relay.snapshot()
        if not s["ws_url"]:
            return
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        cb.set_text(s["ws_url"], -1)
        self.log(f"copied {s['ws_url']}")

    def _on_open(self, *_):
        s = self.relay.snapshot()
        if not s["ws_url"]:
            self.log("start the relay first")
            return
        port = s["ws_url"].rsplit(":", 1)[-1]
        url = f"http://127.0.0.1:{port}/"
        self.log(f"opening {url}")
        try:
            Gtk.show_uri_on_window(self, url, Gdk.CURRENT_TIME)
        except Exception as e:
            self.log(f"could not open browser: {e}")

    def _on_recenter(self, *_):
        if self.relay.recenter():
            self.log("recentered: current pose is now zero")
        else:
            self.log("start the relay first")

    def _on_invert(self, btn, axis):
        self.relay.set_invert(axis, btn.get_active())
        self.log(f"invert {axis}: {'on' if btn.get_active() else 'off'}")

    def _on_destroy(self, *_):
        self.relay.stop()
        self.get_application().quit()

    # ---------------------------------------------------------------- tick
    def _tick(self):
        s = self.relay.snapshot()

        # async failure (raised inside the relay thread)
        if s["error"] and s["error"] != self._last_error:
            self._fail(s["error"])

        running = s["running"]
        if running:
            n = s["clients"]
            if n:
                dot, word = C_OK, f"Relay running — {n} browser{'s' if n != 1 else ''} connected"
            else:
                dot, word = C_WARN, "Relay running — waiting for a browser"
            self.status_lbl.set_markup(f"<span foreground='{dot}'>●</span>  <b>{word}</b>")
            self.detail_lbl.set_markup(
                "<span foreground='#8b949e' size='small'>%s</span>" % self._escape(self._detail_text())
            )
            self.url_lbl.set_markup(
                "<span foreground='#8b949e' size='small'>%s</span>" % self._escape(s["ws_url"] or "")
            )
        elif self._ui_running and not s["error"]:
            self._set_stopped("relay thread ended")

        self.connect_btn.set_sensitive(not running)
        self.disconnect_btn.set_sensitive(running)
        self.recenter_btn.set_sensitive(running)
        self._ui_running = running
        self._sync_sensitivity()

        last = s["last"] or {}
        self._pose = {
            "yaw": last.get("yaw", 0.0),
            "pitch": last.get("pitch", 0.0),
            "roll": last.get("roll", 0.0),
            "have": bool(last),
        }

        def setv(key, text, colour="#58a6ff"):
            self.metrics[key].set_markup(f"<span foreground='{colour}'>{self._escape(text)}</span>")

        if running:
            setv("clients", str(s["clients"]), C_OK if s["clients"] else C_WARN)
            setv("msgs", f"{s['msgs']}")
            setv("hz", f"{s['hz']:.1f} Hz")
            setv("lat", "—" if s["latency_ms"] is None else f"{s['latency_ms']:.0f} ms")
            up = s["uptime"]
            setv("up", f"{int(up//60)}m {int(up%60):02d}s")
            setv("yaw", f"{self._pose['yaw']:.1f}°")
            setv("pitch", f"{self._pose['pitch']:.1f}°")
            setv("roll", f"{self._pose['roll']:.1f}°")
        self.gauge.queue_draw()
        return True

    # -------------------------------------------------------------- gauge
    def _on_draw(self, widget, cr):
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        cr.set_source_rgb(0.043, 0.059, 0.086)
        cr.paint()

        pose = getattr(self, "_pose", None) or {"have": False}
        cr.rectangle(1, 1, w - 2, h - 2)
        cr.set_source_rgb(0.19, 0.21, 0.25)
        cr.set_line_width(1)
        cr.stroke()

        cx, cy = w / 2.0, h / 2.0
        if not pose["have"]:
            cr.select_font_face("monospace")
            cr.set_font_size(10)
            cr.set_source_rgb(0.33, 0.35, 0.38)
            cr.move_to(cx - 46, cy + 3)
            cr.show_text("no pose data")
            return

        pitch = -pose["pitch"] * 0.9
        roll = pose["roll"] * 3.14159265 / 180.0

        cr.save()
        cr.translate(cx, cy + pitch)
        cr.rotate(roll)
        cr.set_source_rgba(0.25, 0.72, 0.31, 0.9)
        cr.set_line_width(1.6)
        cr.move_to(-w, 0)
        cr.line_to(-14, 0)
        cr.move_to(14, 0)
        cr.line_to(w, 0)
        cr.stroke()

        cr.set_source_rgba(0.25, 0.72, 0.31, 0.35)
        cr.set_line_width(1)
        for d in (-20, -10, 10, 20):
            y = -d * 0.9
            half = 16 if abs(d) == 20 else 9
            cr.move_to(-half - 12, y)
            cr.line_to(-12, y)
            cr.move_to(12, y)
            cr.line_to(half + 12, y)
        cr.stroke()
        cr.restore()

        cr.set_source_rgba(0.9, 0.93, 0.95, 0.9)
        cr.set_line_width(1.2)
        cr.arc(cx, cy, 3, 0, 6.2831853)
        cr.stroke()

        cr.select_font_face("monospace")
        cr.set_font_size(9)
        cr.set_source_rgb(0.55, 0.58, 0.62)
        cr.move_to(6, 12)
        cr.show_text(f"yaw {pose['yaw']:6.1f}")
        cr.move_to(6, h - 6)
        cr.show_text(f"pitch {pose['pitch']:6.1f}   roll {pose['roll']:6.1f}")


class XrRelayApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        win = self.get_active_window()
        if win is None:
            win = RelayWindow(self)
            win.show_all()
        win.present()


def _install_css():
    # Prefer CSS over the deprecated widget.modify_font()/override_font().
    prov = Gtk.CssProvider()
    prov.load_from_data(b"textview#relay-log { font-family: Monospace; font-size: 9pt; }")
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(
            screen, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


def main():
    GLib.set_prgname(APP_ID)
    GLib.set_application_name("XR Relay")
    _install_css()
    app = XrRelayApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
