#!/usr/bin/env python3
"""
USB HID controller viewer for F/A-18 Hornet.

Adds a host Qt window (on macrt's existing Qt instance) that reads a USB
HID joystick and shows its four tracked axes:

    elevator / aileron   a 2D stick -- a point inside a square
    rudder               a box with an indicator that moves left/right
    throttle             a vertical bar that fills up from the bottom

The window has a dropdown for the device and a dropdown for each tracked
axis, so any HID joystick can be wired up.  It is not yet fed into the
game -- this is the standalone viewer.

Run with hornet.sh (it passes --script hornet_patch.py).
"""

import queue
import threading

# NOTE: `import hid` runs hidapi's top-level hid_init(), which schedules the
# IOKit device manager on the run loop of the thread that imports it.  That
# must be the worker thread (the one that calls enumerate/open/read), not the
# script thread -- importing here would bind the manager to the wrong run
# loop and the device-added callback would trip a pointer-authentication
# check.  So the import is deferred into HidWorker._run (see _ensure_hid).
from PyQt6.QtCore import Qt, QObject, QPointF, pyqtSignal
from PyQt6.QtGui import QPainter, QColor, QPen, QBrush, QPolygonF
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QGridLayout, QLabel, QComboBox,
                             QGroupBox, QPushButton)

# --------------------------------------------------------------------------
# HID report-descriptor parsing
# --------------------------------------------------------------------------


def _usage_name(page, usage):
    """A human name for a HID usage (generic desktop + simulation)."""
    if page == 1:  # generic desktop
        names = {0x30: "X", 0x31: "Y", 0x32: "Z", 0x33: "Rx", 0x34: "Ry",
                 0x35: "Rz", 0x36: "Slider", 0x37: "Dial", 0x38: "Wheel",
                 0x39: "Hat"}
        if usage in names:
            return names[usage]
        if 0x3A <= usage <= 0x3F:
            return f"Button {usage - 0x39}"
        return f"Desktop 0x{usage:02x}"
    if page == 2:  # simulation controls
        names = {0x01: "Flight Stick", 0x02: "Throttle", 0x20: "Throttle",
                 0x21: "Brake", 0x30: "Rudder", 0x31: "Throttle",
                 0x40: "Flight Stick"}
        if usage in names:
            return names[usage]
        return f"Sim 0x{usage:02x}"
    return f"Page {page} 0x{usage:02x}"


def parse_report_descriptor(data):
    """The variable axes of a HID report descriptor, grouped by report ID.

    Returns {report_id: {"has_id": bool, "axes": [axis, ...]}}.  Each axis
    has its name, usage page, usage, bit offset and length *within its
    report* (a report with an ID is prefixed by the report-ID byte, so
    offsets run from the first data byte), and its range -- the logical
    range, or the physical range when the device declares no logical
    range (as a joystick's X/Y do, in centimetres).  Report IDs are
    tracked because they change how a report's bytes are laid out.
    """
    reports = {}
    i, n = 0, len(data)
    usage_page = 0
    logical_min = logical_max = physical_min = physical_max = 0
    report_size = report_count = 0
    report_id = 0
    usages = []
    usage_min = usage_max = None
    bit = 0
    while i < n:
        b = data[i]
        i += 1
        size = b & 3
        dtype = (b >> 2) & 3
        tag = (b >> 4) & 0xF
        val = 0
        for k in range(size):
            if i + k < n:
                val |= data[i + k] << (8 * k)
        i += size
        if dtype == 1:            # global item
            if tag == 0x00:
                usage_page = val
            elif tag == 0x01:
                logical_min = val
            elif tag == 0x02:
                logical_max = val
            elif tag == 0x03:
                physical_min = val
            elif tag == 0x04:
                physical_max = val
            elif tag == 0x07:
                report_size = val
            elif tag == 0x08:
                report_id = val
                bit = 0           # a new report: its data starts over
            elif tag == 0x09:
                report_count = val
        elif dtype == 2:          # local item
            if tag == 0x00:
                usages.append(val)
            elif tag == 0x01:
                usage_min = val
            elif tag == 0x02:
                usage_max = val
        elif dtype == 0:          # main item
            if tag == 0x08:       # Input
                rep = reports.setdefault(report_id,
                                          {"has_id": report_id != 0,
                                           "axes": []})
                # the usages this input covers: an explicit range, else
                # the listed usages
                if usage_min is not None:
                    covered = list(range(usage_min, usage_max + 1))
                else:
                    covered = usages
                if val & 0x01:    # Const: skip its bits
                    bit += report_count * report_size
                elif val & 0x04:  # Array: one item per usage
                    bit += report_count * report_size
                else:             # Variable: one item per usage
                    for u in covered:
                        lo, hi = logical_min, logical_max
                        if hi <= lo:                     # no logical range
                            lo, hi = physical_min, physical_max
                        rep["axes"].append({
                            "name": _usage_name(usage_page, u),
                            "page": usage_page, "usage": u,
                            "min": lo, "max": hi,
                            "bit_offset": bit, "bit_len": report_size})
                        bit += report_size
                # an Input consumes the local items set before it
                usages = []
                usage_min = usage_max = None
            elif tag in (0x09, 0x0A):   # Output / Feature: skip their bits
                bit += report_count * report_size
                usages = []
                usage_min = usage_max = None
            # Collection (0x0B), EndCollection (0x0C) and reserved main
            # items take no report space.  Some devices (the Thrustmaster
            # HOTAS) write a 3-byte-prefixed 4-byte logical/physical max,
            # leaving a stray 0x00 reserved main item; those must not
            # clobber a pending usage list, so they're left alone.
    return reports


def extract(payload, axis):
    """One axis's value out of a report's payload (the data bytes, with
    the leading report-ID byte already removed for devices that use
    report IDs).  The axis's bit offset is counted from the first data
    byte."""
    bo, bl = axis["bit_offset"], axis["bit_len"]
    byte_i, bit_i = bo // 8, bo % 8
    total = (bo + bl + 7) // 8
    chunk = 0
    for b in range(total):
        if byte_i + b < len(payload):
            chunk |= payload[byte_i + b] << (8 * b)
    return (chunk >> bit_i) & ((1 << bl) - 1)


def norm01(value, axis):
    """An axis value to 0..1 over its logical range, clamped."""
    lo, hi = axis["min"], axis["max"]
    if hi <= lo:
        return 0.0
    v = (value - lo) / (hi - lo)
    return max(0.0, min(1.0, v))


def top_level_usage(data):
    """The (usage_page, usage) of the first top-level (application)
    collection: (1, 4) for a joystick, (1, 2) for a mouse, (1, 6) for a
    keyboard, (2, 0x20) for a throttle.  This is what separates a
    joystick from the other page-1 devices (mice and keyboards)."""
    i, n = 0, len(data)
    page = usage = 0
    depth = 0
    while i < n:
        b = data[i]
        i += 1
        size = b & 3
        dtype = (b >> 2) & 3
        tag = (b >> 4) & 0xF
        val = 0
        for k in range(size):
            if i + k < n:
                val |= data[i + k] << (8 * k)
        i += size
        if dtype == 1:            # global item
            if tag == 0x00:
                page = val
        elif dtype == 2:          # local item
            if tag == 0x00:
                usage = val
        elif dtype == 0:          # main item
            if tag == 0x0A:       # Collection
                if val == 0x01:   # Application
                    if depth == 0:
                        return (page, usage)
                depth += 1
            elif tag == 0x0C:     # End Collection
                depth = max(0, depth - 1)
    return (page, usage)


def is_game_device(top):
    """A device is a controller we want if it's a joystick (page 1,
    usage 4) or a simulation device (page 2).  Mice and keyboards are
    also page 1, so the usage is what separates them."""
    page, usage = top
    return (page == 1 and usage == 4) or page == 2


def game_axes(axes):
    """The analog axes of a device: variable inputs wider than one bit
    on the generic desktop or simulation pages (buttons are one bit)."""
    return [a for a in axes
            if a["page"] in (1, 2) and a["bit_len"] > 1]


# --------------------------------------------------------------------------
# Display widgets (1-bit: white ground, black lines and indicators)
# --------------------------------------------------------------------------

_BG = QColor("white")
_FG = QColor("black")


class StickWidget(QWidget):
    """A square with a point: elevator (vertical) / aileron (horizontal).
    Both axes are -1 (down / left) to +1 (up / right)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._x = 0.0   # aileron, -1..+1
        self._y = 0.0   # elevator, -1..+1
        self.setMinimumSize(120, 120)

    def set_position(self, x, y):
        self._x = max(-1.0, min(1.0, x))
        self._y = max(-1.0, min(1.0, y))
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), _BG)
        p.setPen(QPen(_FG, 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        # centre crosshair
        p.setPen(QPen(_FG, 1, Qt.PenStyle.DashLine))
        cx, cy = self.width() / 2, self.height() / 2
        p.drawLine(int(cx), 1, int(cx), self.height() - 1)
        p.drawLine(1, int(cy), self.width() - 1, int(cy))
        # the point
        px = (self._x + 1) / 2 * (self.width() - 2) + 1
        py = (1 - (self._y + 1) / 2) * (self.height() - 2) + 1
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(_FG))
        p.drawEllipse(QPointF(px, py), 4, 4)
        p.end()


class RudderWidget(QWidget):
    """A box with an indicator that moves left/right.  -1 (left) to
    +1 (right)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._v = 0.0
        self.setMinimumSize(160, 44)

    def set_position(self, v):
        self._v = max(-1.0, min(1.0, v))
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), _BG)
        p.setPen(QPen(_FG, 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        # centre tick
        cx = self.width() / 2
        p.drawLine(int(cx), 2, int(cx), self.height() - 3)
        # the indicator: a filled triangle from the top
        x = (self._v + 1) / 2 * (self.width() - 6) + 3
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(_FG))
        p.drawPolygon(QPolygonF([QPointF(x - 5, 3), QPointF(x + 5, 3),
                                 QPointF(x, 13)]))
        p.end()


class ThrottleWidget(QWidget):
    """A vertical bar that fills up from the bottom.  0 (empty) to 1
    (full)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._v = 0.0
        self.setMinimumSize(44, 120)

    def set_position(self, v):
        self._v = max(0.0, min(1.0, v))
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), _BG)
        p.setPen(QPen(_FG, 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        # fill from the bottom
        h = self.height() - 2
        fill = int(self._v * h)
        if fill > 0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(_FG))
            p.drawRect(2, self.height() - 1 - fill, self.width() - 4, fill)
        p.end()


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------


class _HidSignals(QObject):
    """Worker-to-window messages.  The worker runs on its own thread, so
    these cross threads: Qt delivers them to the window's thread (the GUI
    thread) automatically."""

    pool_ready = pyqtSignal(object)      # the filtered device pool
    values = pyqtSignal(object)          # {pool index: {axis index: raw}}
    status = pyqtSignal(str)


class HidWorker:
    """Owns every hidapi call, on a dedicated worker thread.

    hidapi's macOS backend is not thread-safe the way a Python wrapper
    would be: hid_init schedules the IOKit device manager on the run loop
    of the thread that first calls it, and hid_enumerate fires the
    manager's device-added callback *synchronously*, which schedules each
    device on that same loop.  If that thread is the GUI thread, the
    manager lands on the main run loop -- the one Qt drives through its
    own QEventDispatcher, not the native CFRunLoop -- and the
    CFRunLoopAddSource from the device-added callback trips a
    pointer-authentication check (__CFCheckCFInfoPACSignature traps).  So
    every hid call -- init, enumerate, open, read, close -- happens on a
    worker thread that owns its own run loop and nothing else drives it.
    Commands come in over a queue; results go out over the signals."""

    def __init__(self, signals: _HidSignals):
        self._sig = signals
        self._cmd = queue.Queue()
        self._hid = None                 # the hid module, imported on the worker
        self._devices = {}               # pool index -> open hid.device
        self._pool = []
        self._last_raw = {}              # (pool index, axis index) -> value
        self._thread = threading.Thread(
            target=self._run, name="hid-worker", daemon=True)

    # -- the GUI thread ---------------------------------------------------
    def start(self):
        self._thread.start()
        self.rescan()

    def stop(self):
        self._cmd.put(("stop", None))

    def rescan(self):
        self._cmd.put(("rescan", None))

    def open_device(self, di):
        self._cmd.put(("open", di))

    def set_used(self, used):
        """Close every open device not in `used` (a set of pool indices)."""
        self._cmd.put(("set_used", set(used)))

    # -- the worker thread ------------------------------------------------
    def _ensure_hid(self):
        """Import hid on the worker thread (once).  `import hid` runs
        hidapi's top-level hid_init(), which schedules the IOKit device
        manager on the run loop of the thread that imports it -- so it
        must be this thread, the one that calls enumerate/open/read, or
        the device-added callback schedules on the wrong run loop and
        trips a pointer-authentication check."""
        if self._hid is None:
            import hid
            self._hid = hid
        return self._hid

    def _run(self):
        """The worker loop: poll the command queue, then the open
        devices, on a small cadence.  hid_init runs here (on first use),
        so the IOKit manager is scheduled on this thread's run loop --
        not the main one."""
        import time
        self._ensure_hid()
        while True:
            try:
                name, arg = self._cmd.get(timeout=0.02)
            except queue.Empty:
                name, arg = None, None
            if name == "stop":
                self._close_all()
                return
            if name == "rescan":
                self._rescan()
            elif name == "open":
                self._open(arg)
            elif name == "set_used":
                self._set_used(arg)
            self._poll()

    def _rescan(self):
        """Enumerate, and keep only devices that are joysticks or
        simulation devices with at least one analog axis.  Each device is
        opened briefly to read its descriptor, then closed; the open
        handles used for polling are created on selection."""
        hid = self._ensure_hid()
        self._close_all()
        pool = []
        seen = set()
        try:
            devices = hid.enumerate()
        except Exception as e:                    # noqa: BLE001
            self._pool = []
            self._sig.status.emit(f"enumerate failed: {e}")
            self._sig.pool_ready.emit([])
            return
        for info in devices:
            key = info.get("path") or (info["vendor_id"], info["product_id"])
            if key in seen:
                continue
            seen.add(key)
            dev = hid.device()
            try:
                if info.get("path"):
                    dev.open_path(info["path"])
                else:
                    dev.open(info["vendor_id"], info["product_id"])
            except Exception:                     # noqa: BLE001
                continue
            try:
                rd = bytes(dev.get_report_descriptor())
            except Exception:                     # noqa: BLE001
                dev.close()
                continue
            finally:
                try:
                    dev.close()
                except Exception:                 # noqa: BLE001
                    pass
            reports = parse_report_descriptor(rd)
            if not is_game_device(top_level_usage(rd)):
                continue
            # flatten the analog axes across all reports, tagging each with
            # the report it belongs to (a device that uses report IDs
            # prefixes every report with the report-ID byte)
            gaxes = []
            for rid, rep in reports.items():
                for a in game_axes(rep["axes"]):
                    gaxes.append({**a, "report_id": rid,
                                  "has_id": rep["has_id"]})
            if not gaxes:
                continue
            name = (f"{info.get('manufacturer_string', '')} "
                    f"{info.get('product_string', '')}").strip()
            if not name:
                name = f"0x{info['vendor_id']:04x}:0x{info['product_id']:04x}"
            pool.append({"name": name, "info": info, "axes": gaxes,
                         "reports": reports,
                         "uses_ids": any(r["has_id"] for r in reports.values())})
        self._pool = pool
        self._sig.status.emit(
            f"{len(pool)} device(s): "
            + "; ".join(p["name"] for p in pool))
        self._sig.pool_ready.emit(pool)

    def _open(self, di):
        """Open a pool device for polling.  Several tracked axes may share
        one device, so it's opened once."""
        if di < 0 or di >= len(self._pool) or di in self._devices:
            return
        info = self._pool[di]["info"]
        hid = self._ensure_hid()
        dev = hid.device()
        try:
            if info.get("path"):
                dev.open_path(info["path"])
            else:
                dev.open(info["vendor_id"], info["product_id"])
        except Exception as e:                    # noqa: BLE001
            self._sig.status.emit(f"open failed: {e}")
            return
        dev.set_nonblocking(1)
        self._devices[di] = dev

    def _set_used(self, used):
        """Close every open device whose pool index isn't in `used`."""
        for di in [d for d in self._devices if d not in used]:
            dev = self._devices.pop(di)
            try:
                dev.close()
            except Exception:                     # noqa: BLE001
                pass

    def _close_all(self):
        for dev in self._devices.values():
            try:
                dev.close()
            except Exception:                     # noqa: BLE001
                pass
        self._devices = {}

    def _poll(self):
        """Read every open device; emit the axes whose values changed."""
        changed = {}
        for di, dev in list(self._devices.items()):
            pool = self._pool[di]
            axes = pool["axes"]
            while True:
                try:
                    report = dev.read(64)
                except Exception:                 # noqa: BLE001
                    break
                if not report:
                    break
                data = bytes(report)
                # a device that uses report IDs prefixes each report with
                # the report-ID byte; find which report this is
                rid = data[0] if pool["uses_ids"] else 0
                payload = data[1:] if pool["uses_ids"] else data
                for ai, axis in enumerate(axes):
                    if axis["report_id"] != rid:
                        continue
                    v = extract(payload, axis)
                    if self._last_raw.get((di, ai)) != v:
                        self._last_raw[(di, ai)] = v
                        changed.setdefault(di, {})[ai] = v
        if changed:
            self._sig.values.emit(changed)


class HidControllerWindow(QMainWindow):
    """A host Qt window that reads USB HID controllers and shows four
    tracked axes.  Each tracked axis (aileron, elevator, rudder,
    throttle) is mapped to its own device *and* one of that device's
    analog axes, so a stick, rudder pedals and a throttle -- three
    separate devices -- can be combined.

    The device pool is filtered to controllers only: a device is kept
    if it's a joystick (generic desktop, usage 4) or a simulation
    device, and its report descriptor has at least one analog axis.
    That filter is also what keeps us away from the keyboard, trackpad
    and audio devices.

    All hidapi calls run on a dedicated worker thread (see HidWorker) --
    hidapi's macOS backend schedules IOKit on the run loop of whichever
    thread first calls it, so they must run on a thread that owns its own
    loop, never the main one Qt drives."""

    TRACKED = ("aileron", "elevator", "rudder", "throttle")

    def __init__(self):
        super().__init__()
        self.setWindowTitle("USB HID Controller")
        self._pool = []            # [{"name","info","axes"}] filtered devices
        self._dev_idx = {}         # tracked name -> pool index (-1 = none)
        self._axis_idx = {}        # tracked name -> index into device axes
        self._last = {}            # tracked name -> last raw value

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        # -- per-axis device + axis rows --------------------------------
        grid = QGridLayout()
        grid.addWidget(QLabel("Axis"), 0, 0)
        grid.addWidget(QLabel("Device"), 0, 1)
        grid.addWidget(QLabel("Controller axis"), 0, 2)
        for row, name in enumerate(self.TRACKED, start=1):
            grid.addWidget(QLabel(name.capitalize()), row, 0)
            dev_box = QComboBox()
            dev_box.setMinimumWidth(260)
            dev_box.currentIndexChanged.connect(
                lambda idx, n=name: self._on_device(n, idx))
            setattr(self, f"_{name}_dev", dev_box)
            grid.addWidget(dev_box, row, 1)
            ax_box = QComboBox()
            ax_box.setMinimumWidth(140)
            ax_box.currentIndexChanged.connect(
                lambda idx, n=name: self._on_axis(n, idx))
            setattr(self, f"_{name}_ax", ax_box)
            grid.addWidget(ax_box, row, 2)
        outer.addLayout(grid)

        rescan_row = QHBoxLayout()
        self.rescan_btn = QPushButton("Rescan devices")
        self.rescan_btn.clicked.connect(self._rescan)
        rescan_row.addWidget(self.rescan_btn)
        rescan_row.addStretch(1)
        outer.addLayout(rescan_row)

        # -- the three displays -----------------------------------------
        disp = QHBoxLayout()
        self.stick = StickWidget()
        self.rudder = RudderWidget()
        self.throttle = ThrottleWidget()
        for w, label in ((self.stick, "Elevator / Aileron"),
                         (self.rudder, "Rudder"),
                         (self.throttle, "Throttle")):
            box = QGroupBox(label)
            v = QVBoxLayout(box)
            v.addWidget(w)
            disp.addWidget(box)
        disp.addStretch(1)
        outer.addLayout(disp)

        # -- value readout ----------------------------------------------
        self.values = QLabel("scanning for devices...")
        self.values.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.values)

        # -- the worker ---------------------------------------------------
        self._sig = _HidSignals(self)
        self._sig.pool_ready.connect(self._on_pool_ready)
        self._sig.values.connect(self._on_values)
        self._sig.status.connect(self._on_status)
        self._worker = HidWorker(self._sig)
        self._worker.start()

    # -- worker signals (GUI thread) -------------------------------------
    def _on_pool_ready(self, pool):
        self._pool = pool
        self._refresh_device_boxes()
        self._remap()
        # (re)open every device a tracked axis points at
        for name in self.TRACKED:
            di = self._dev_idx.get(name, -1)
            if 0 <= di < len(self._pool):
                self._worker.open_device(di)

    def _on_values(self, changed):
        """changed: {pool index: {axis index: raw value}} for the axes
        that moved since the last report."""
        got = False
        for di, axes_map in changed.items():
            if di < 0 or di >= len(self._pool):
                continue
            for ai, v in axes_map.items():
                for name in self.TRACKED:
                    if (self._dev_idx.get(name) == di
                            and self._axis_idx.get(name) == ai):
                        self._last[name] = v
                        got = True
        if got:
            self._update_displays()

    def _on_status(self, text):
        self.values.setText(text)

    # -- the device pool (GUI thread) -------------------------------------
    def _refresh_device_boxes(self):
        """Rebuild every axis's device dropdown from the pool, keeping
        the current selection where it still exists."""
        for name in self.TRACKED:
            box = getattr(self, f"_{name}_dev")
            box.blockSignals(True)
            cur = self._dev_idx.get(name, -1)
            box.clear()
            box.addItem("-- none --", -1)
            for i, p in enumerate(self._pool):
                box.addItem(p["name"], i)
            box.setCurrentIndex(cur if 0 <= cur < len(self._pool) else 0)
            box.blockSignals(False)
            self._populate_axis_box(name)

    def _populate_axis_box(self, name):
        """Fill one axis's controller-axis dropdown from its device's
        analog axes, keeping the selection where it still exists."""
        box = getattr(self, f"_{name}_ax")
        box.blockSignals(True)
        cur = self._axis_idx.get(name, 0)
        box.clear()
        di = self._dev_idx.get(name, -1)
        if di < 0 or di >= len(self._pool):
            box.addItem("(no device)", -1)
            box.setCurrentIndex(0)
            self._axis_idx[name] = -1
        else:
            for i, a in enumerate(self._pool[di]["axes"]):
                box.addItem(a["name"], i)
            box.setCurrentIndex(cur if 0 <= cur < box.count() else 0)
            self._axis_idx[name] = box.currentIndex()
        box.blockSignals(False)

    def _remap(self):
        """Apply sensible defaults to any tracked axis that has no mapping
        yet, so a rescan doesn't clobber a deliberate choice.  Two passes:
        first by device-name hints (a "rudder"/"pedal" device for rudder,
        a "throttle" device for throttle, a "stick"/"joystick" device for
        aileron/elevator), then by axis-name hints, so the three separate
        devices in a HOTAS setup each get their own axis."""
        # device-name keywords per tracked axis, in priority order
        dev_hints = {"rudder": ("rudder", "pedal"),
                     "throttle": ("throttle",),
                     "aileron": ("joystick", "stick", "control"),
                     "elevator": ("joystick", "stick", "control")}
        # axis-name hints per tracked axis, in priority order
        axis_hints = {"aileron": ("X",), "elevator": ("Y",),
                      "rudder": ("Rudder", "Rz"),
                      "throttle": ("Throttle", "Z", "Slider")}
        for name in self.TRACKED:
            if 0 <= self._dev_idx.get(name, -1) < len(self._pool):
                continue            # already mapped; leave it
            di, pick = self._find_default(name, dev_hints[name],
                                          axis_hints[name])
            if di is None:
                continue
            self._set_mapping(name, di, pick)

    def _find_default(self, name, dev_hints, axis_hints):
        """A (device index, axis index) for a tracked axis, or (None,
        None).  Tries a device whose name matches a hint, then any device
        with a matching axis, then any device with an axis at all."""
        for di, p in enumerate(self._pool):
            low = p["name"].lower()
            if any(h in low for h in dev_hints):
                pick = self._pick_axis(p, axis_hints)
                if pick is not None:
                    return di, pick
        for di, p in enumerate(self._pool):
            pick = self._pick_axis(p, axis_hints)
            if pick is not None:
                return di, pick
        for di, p in enumerate(self._pool):
            if p["axes"]:
                return di, 0
        return None, None

    def _pick_axis(self, pool_entry, axis_hints):
        """The index of the best-matching axis on a device, or None."""
        names = [a["name"] for a in pool_entry["axes"]]
        for h in axis_hints:
            if h in names:
                return names.index(h)
        return None

    def _set_mapping(self, name, di, pick):
        self._dev_idx[name] = di
        self._axis_idx[name] = pick
        box = getattr(self, f"_{name}_dev")
        box.blockSignals(True)
        box.setCurrentIndex(di + 1)      # +1 for the "-- none --"
        box.blockSignals(False)
        self._populate_axis_box(name)

    def _rescan(self):
        self._worker.rescan()

    # -- selection ------------------------------------------------------
    def _on_device(self, name, idx):
        di = getattr(self, f"_{name}_dev").itemData(idx)
        self._dev_idx[name] = di if di is not None else -1
        self._populate_axis_box(name)
        if 0 <= self._dev_idx[name] < len(self._pool):
            self._worker.open_device(self._dev_idx[name])
        self._close_unused()

    def _on_axis(self, name, idx):
        ai = getattr(self, f"_{name}_ax").itemData(idx)
        self._axis_idx[name] = ai if ai is not None else -1

    def _close_unused(self):
        """Ask the worker to close devices no tracked axis references."""
        used = {d for d in self._dev_idx.values()
                if 0 <= d < len(self._pool)}
        self._worker.set_used(used)

    def _update_displays(self):
        def val(name):
            di = self._dev_idx.get(name)
            ai = self._axis_idx.get(name)
            if di is None or di < 0 or di >= len(self._pool):
                return None
            if ai is None or ai < 0 or name not in self._last:
                return None
            axis = self._pool[di]["axes"][ai]
            return norm01(self._last[name], axis)

        aileron = val("aileron")
        elevator = val("elevator")
        rudder = val("rudder")
        throttle = val("throttle")

        # Mirror axis state into the control-override hook (defined at
        # the bottom of this file) so the hook reads HID values live.
        for name, v in (("aileron", aileron), ("elevator", elevator),
                        ("rudder", rudder), ("throttle", throttle)):
            if v is not None:
                _hid_axes[name] = v

        if aileron is not None and elevator is not None:
            self.stick.set_position((aileron - 0.5) * 2,
                                    (elevator - 0.5) * 2)
        if rudder is not None:
            self.rudder.set_position((rudder - 0.5) * 2)
        if throttle is not None:
            self.throttle.set_position(throttle)

        def fmt(name):
            v = val(name)
            return f"{v:+.2f}" if v is not None else "  -- "
        self.values.setText(
            f"Aileron {fmt('aileron')}   Elevator {fmt('elevator')}   "
            f"Rudder {fmt('rudder')}   Throttle {fmt('throttle')}")

    def closeEvent(self, e):
        self._worker.stop()
        super().closeEvent(e)


# --------------------------------------------------------------------------
# Entry point -- runs on the script thread; the window is created on the
# GUI thread so it lives with the rest of macrt's Qt.
# --------------------------------------------------------------------------

_window = None


def _create_window(display):
    global _window
    if _window is not None:
        return
    w = HidControllerWindow()
    w.show()
    _window = w


if "display" in globals():
    display.bridge.post(_create_window, display)


# --------------------------------------------------------------------------
# HID control override.
#
# process_game_input hands its per-frame control state to every caller
# through an output buffer (buffer+1 = action code, buffer+2 = roll,
# buffer+3 = pitch -- both signed bytes, +roll = right, +pitch = up),
# and its A5 globals are scratch it re-derives each frame.  Overwriting
# the buffer bytes at the routine's return therefore replaces *all*
# control input -- mouse and keyboard alike -- with our HID values, with
# no joystick device configured in-game.
# --------------------------------------------------------------------------

# Axis state shared with the HID viewer window.  axis01 is 0..1
# normalized (0.5 = centered); absent entries mean "axis unassigned",
# and the hook then falls back to writing the neutral value.
_hid_axes = {"aileron": 0.5, "elevator": 0.5, "rudder": 0.5,
             "throttle": 0.0}

# Last throttle state we commanded the game into -- ("mil", pct) or
# ("ab", level).  The engine functions are called only on change.
_eng_state = None


def _call_game(name, *args):
    """Call a game routine, refreshing the segment map on a miss.

    macrt.call resolves through the app map's live-segment table,
    which is only as fresh as its last hook install -- and this
    routine's segment can load after that.  The engine's own tracer
    uses the same refresh-then-retry idiom (inject.py).  Returns
    (ok, result); on a miss the caller's commanded state does NOT
    advance, so the next frame retries until the segment appears."""
    try:
        return True, macrt.call(name, args=args)
    except LookupError:
        try:
            macrt._patcher().app_map.refresh()
        except Exception:
            pass
        try:
            return True, macrt.call(name, args=args)
        except LookupError:
            print(f"[hp] {name} isn't loaded yet; will retry")
            return False, None


def _axis_byte(v01):
    """Normalized 0..1 axis to the game's signed byte, +-127 clamped."""
    if v01 is None:
        return 0
    b = round((v01 - 0.5) * 254)
    return max(-127, min(127, b)) & 0xFF


@macrt.hook("process_game_input", phase="return")
def hid_controls(h):
    m = macrt.d.system.mem
    a4 = h.regs()["a4"] & m.mask
    base = m.r32((h.regs()["a5"] - 0x6c2e) & m.mask) & m.mask
    # Roll axis (buffer+2): stick X.  The game reads pitch at +3 next;
    # the elevator mapping is applied in the same write path below.
    m.w8((a4 + 2) & m.mask, _axis_byte(_hid_axes.get("aileron")))
    m.w8((a4 + 3) & m.mask, _axis_byte(_hid_axes.get("elevator")))

    # Rudder: the absolute deflection lives at g_FlightStateBase+0x1186
    # (signed word, -5..+5).  Keyboard keys nudge it one unit per press
    # via _RudderLfFunc/_RudderRtFunc; the game's own analog-joystick
    # mapping scales an axis byte the same way.  Overwriting the word
    # with our pedal value gives full analog authority.
    if base:
        rudder = _hid_axes.get("rudder")
        if rudder is not None:
            defl = round((rudder - 0.5) * 2 * 5)
            m.w16((base + 0x1186) & m.mask, defl & 0xFFFF)

        # Throttle: the axis is inverted and segmented --
        #   +1.00 (idle)      -> 50  (game idle position)
        #   +0.25 (mil stop)  -> 100 military power
        #   0.00 (full AB)    -> afterburner level 6
        # We no longer touch the engine state directly: every write
        # path we had was canceling the burner as soon as it lit.
        # Instead the hook maps the axis to a commanded (mode, level)
        # and calls the game's OWN functions on edge only:
        #   _StartEngines(base, pct)      -- military band
        #   _StartAfterburn(base, level)  -- past the mil stop
        # _StartAfterburn re-lights idempotently (engine check, sets
        # +0x114a=100, AB bit, AB sounds, +0x1150=10*level; the AB
        # entry sound plays only when +0x1150 was 0), and
        # _StartEngines with the burner lit performs the game's own
        # AB-cancel (stops AB sounds, clears the bit and +0x1150) --
        # exactly the "+ returns to 100%" behavior.  Calling them only
        # on transition means the cancel path can never fire behind an
        # active burner.
        throttle = _hid_axes.get("throttle")
        if throttle is not None:
            global _eng_state
            if throttle >= 0.25:
                cmd = ("mil", round(50 + (1.0 - throttle) * 50 / 0.75))
            else:
                cmd = ("ab", max(1, min(6, round((0.25 - throttle)
                                                 * 6 / 0.25))))
            if cmd != _eng_state:
                if cmd[0] == "mil":
                    ok, _ = _call_game("_StartEngines",
                                       base, min(100, cmd[1]))
                else:
                    ok, _ = _call_game("_StartAfterburn", base, cmd[1])
                if ok:
                    _eng_state = cmd


print("[hp] control-override hook registered:",
      macrt.hooks_pending() or "pending first pump")
