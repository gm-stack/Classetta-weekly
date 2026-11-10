"""
macrt.ui.hardware - A window that shows the Mac's graphics hardware.

It opens from the debugger window's Hardware button, with --debug: for
each of the machine's screens, its resolution, its mode, its depth, where
it sits in the
desktop's global coordinates, where its pixels live in memory -- the
buffer's address and how many bytes a row takes -- and, when the screen
is in an indexed colour mode (eight bits a pixel or fewer, its pixels the
entries of a colour table), the table itself: a grid of square swatches,
addressed by their entries' high and low nibbles.

The screens are the System's graphics devices, in the DeviceList's order:
the main screen first, the others after it, each a GDevice whose gdRect
is its place in the desktop, whose gdMode is the card's mode, and whose
PixMap's baseAddr and rowBytes are its buffer.  The colour table is the
PixMap's pmTable, read the way Color QuickDraw reads it; a direct device
(16 or 32 bits a pixel, its pixels the colours themselves) has none, and
says so.

As the debugger window, this one never touches the processor: it reads
the machine's records, and only while it can take the System's lock --
which a guest thread holds while it runs, and gives up while it waits on
the GUI thread, as it does for a menu or a drag.  A lock it can't take
means it looks again on the next poll, rather than waiting for a guest
that is waiting on it.
"""

from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (QGridLayout, QLabel, QMainWindow, QScrollArea,
                             QVBoxLayout, QWidget)

from ..toolbox import lowmem
from ..toolbox.gdevice import GDevice, PixMap, ROW_BYTES_MASK, deref, read_table

POLL = 500                # ms between looks: the hardware changes slowly
POINT_SIZE = 13
CELL = 16                 # a swatch's side, in points: square
SPACING = 2               # the gap between swatches, in points
COLS = 16                 # swatches a row: 256 entries fill a 16 by 16 grid


@dataclass
class ScreenInfo:
    """One screen, as its GDevice and PixMap have it."""
    index: int
    name: str
    width: int
    height: int
    mode: int
    depth: int
    top: int
    left: int
    base: int             # the buffer's address, as the machine writes it
    base_str: str         # the same, hexed the machine's way (6 or 8 digits)
    row_bytes: int
    clut: list[int] | None        # 0xAARRGGBB by entry; None on a direct device


def _clut(mem, pm: PixMap) -> list[int] | None:
    """The screen's colour table, by entry; None on a direct device, whose
    pixels are their colours rather than indexes into one."""
    if pm.pixel_size > 8:
        return None
    h = pm.pm_table
    p = deref(mem, h)
    if not p:
        return []
    entries = max(0, min(mem.r16s(p + 6) + 1, 256))
    return list(read_table(mem, h)[:entries])


def screen_info(display) -> list[ScreenInfo] | None:
    """The machine's screens, in the DeviceList's order: the main screen
    first, the others after it, as their GDevices have them.  Called on the
    GUI thread; the System's lock is taken without blocking, so a guest
    that holds it while waiting on the GUI thread is never waited for.
    None when there was no look to take -- no colour QuickDraw at all, or
    the lock held -- and an empty list when there are no screens yet."""
    system = display.system
    if system.devices is None:
        return None
    lock = system.lock
    if not lock.acquire(blocking=False):
        return None
    try:
        mem = system.mem
        out = []
        gdh = mem.r32(lowmem.DeviceList)
        i = 0
        while gdh:
            gd = GDevice(mem, deref(mem, gdh))
            pm = deref(mem, gd.pmap)
            if not pm:
                break
            pm = PixMap(mem, pm)
            rect = gd.rect
            base = pm.base_addr & mem.mask
            out.append(ScreenInfo(
                index=i,
                name="main screen" if i == 0 else f"screen {i}",
                width=rect.width,
                height=rect.height,
                mode=gd.mode,
                depth=pm.pixel_size,
                top=rect.top,
                left=rect.left,
                base=base,
                base_str=f"${base:0{8 if mem.address32 else 6}X}",
                row_bytes=pm.row_bytes & ROW_BYTES_MASK,
                clut=_clut(mem, pm)))
            gdh = gd.next_gd
            i += 1
        return out
    finally:
        lock.release()


def mono(point_size: int = POINT_SIZE) -> QFont:
    font = QFont("Menlo")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(point_size)
    return font


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setFont(mono(POINT_SIZE - 1))
    label.setStyleSheet("color: #777")
    return label


class ColorGrid(QWidget):
    """A colour table as a grid of square swatches, addressed the way the
    entries are: a row of the high nibble across the top (0 to F), a
    column of the low nibble down the side (00 to F0), the swatch at
    their crossing.  A swatch's exact colour is on hover; its number,
    being its address, needs no label.  Sixteen a row, so 256 entries
    fill a 16 by 16 grid; a smaller table takes fewer rows."""

    def __init__(self, entries: list[int], parent: QWidget | None = None):
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(0)   # the swatches touch, left to right
        grid.setVerticalSpacing(SPACING)
        # The window gives the grid its full width; a stretch in an empty
        # trailing column takes the surplus, so the table sits at the left
        # rather than the swatches spreading to fill.
        grid.setColumnStretch(COLS + 1, 1)
        # The high nibble across the top, one character to a swatch:
        for c in range(COLS):
            header = QLabel(f"{c:X}", self)
            header.setFont(mono(POINT_SIZE - 2))
            header.setStyleSheet("color: #777")
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.setFixedSize(CELL, CELL)
            grid.addWidget(header, 0, c + 1)
        rows = (len(entries) + COLS - 1) // COLS
        for r in range(rows):
            # The low nibble down the side, two characters to a swatch:
            header = QLabel(f"{r << 4:02X}", self)
            header.setFont(mono(POINT_SIZE - 2))
            header.setStyleSheet("color: #777")
            header.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            header.setFixedSize(2 * CELL, CELL)
            grid.addWidget(header, r + 1, 0)
            for i in range(r * COLS, min((r + 1) * COLS, len(entries))):
                rgb = entries[i]
                colour = QColor(int(rgb >> 16 & 0xFF), int(rgb >> 8 & 0xFF), int(rgb & 0xFF))
                cell = QWidget(self)
                cell.setFixedSize(CELL, CELL)
                cell.setStyleSheet(f"background: {colour.name()}; border: 1px solid #888")
                cell.setToolTip(f"entry {i:02X}: {colour.name()}  "
                                f"(r {colour.red()} g {colour.green()} b {colour.blue()})")
                grid.addWidget(cell, r + 1, i % COLS + 1)
        self.setFixedHeight((rows + 1) * CELL + rows * SPACING)


class HardwareWindow(QMainWindow):
    """The machine's screens: each one's resolution, mode, depth, place in
    the desktop, and where its pixels are in memory, with its colour
    table's swatches under it while it's in an indexed colour mode.
    Refreshed on a poll, and redrawn only when something has changed --
    a game that animates its palette changes one entry at a time, and the
    window follows it without repainting itself a dozen times a second."""

    def __init__(self, display):
        super().__init__()
        self.display = display
        self.setWindowTitle("Classetta hardware")
        self.resize(760, 620)
        self._shown = None           # the screens the window is showing
        self._closing = False        # the display's shutdown: let it close
        self._panels: list[QWidget] = []    # what's on screen, to take down on a redraw
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        body = QWidget(scroll)
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(8, 8, 8, 8)
        self._body.setSpacing(10)
        scroll.setWidget(body)
        self.setCentralWidget(scroll)
        self._timer = QTimer(self, interval=POLL)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def closeEvent(self, event):  # noqa: N802
        """The window closes at the user's wish: it holds nothing the
        machine needs -- it only looks -- so the X takes it, unlike the
        debugger window, which holds the processor and refuses it.  Its poll
        goes with it: while the window is closed it costs the machine
        nothing, and the debugger's Hardware button opens it again."""
        self._timer.stop()
        super().closeEvent(event)

    def showEvent(self, event):  # noqa: N802
        """Shown again after a close: the poll came back with it.  The first
        show already has the timer running from the constructor, and
        start() on a running timer is a no-op."""
        self._timer.start()
        super().showEvent(event)

    def force_close(self) -> None:
        """The display's way of closing it, at shutdown, when the machine
        it's looking at is about to go.  A window the user already closed
        is closed twice, which is nothing."""
        self.close()

    # -- the looking ------------------------------------------------------------
    def refresh(self) -> None:
        """One look at the machine's screens, and a redraw if it has
        changed.  What it can't look at -- the machine not yet made, or
        its lock held by a guest that's waiting on this thread -- is a
        look for next time, not an error."""
        try:
            if self.display.shutting_down:
                return
            screens = screen_info(self.display)
            if screens is None:
                return                 # no look to take: not yet, or the lock's held
            if screens == self._shown:
                return
            self._shown = screens
            self._draw(screens)
        except Exception as e:  # noqa: BLE001 - a failed look is a look for next time
            # A window that crashes on a look loses the hardware with it;
            # what went wrong goes in the title instead, and the next poll
            # tries again.
            self.setWindowTitle(f"Classetta hardware ({type(e).__name__}: {e})")

    def _draw(self, screens: list[ScreenInfo]) -> None:
        """The panels, rebuilt: the last ones taken down first, so a change
        to one screen's palette doesn't leave two of its grids on screen.
        (The layout's findChildren finds nothing: its widgets are parented
        to the body, not to it, so the panels are kept in a list of their
        own.)"""
        for w in self._panels:
            self._body.removeWidget(w)
            w.deleteLater()
        self._panels = []
        widgets = [_header()]
        if not screens:
            widgets.append(_note("no graphics devices yet"))
        for s in screens:
            widgets.append(_info_panel(s))
            if s.clut is None:
                widgets.append(_note("direct colour: the pixels are their colours"))
            elif not s.clut:
                widgets.append(_note("no colour table"))
            else:
                widgets.append(ColorGrid(s.clut))
        for w in widgets:
            self._body.addWidget(w)
            self._panels.append(w)
        self._body.addStretch(1)


def _header() -> QLabel:
    label = QLabel("The machine's screens: resolution, mode, depth, place, and "
                   "where the pixels are in memory.\n"
                   "Addresses are the machine's own; a 24-bit Mac writes six "
                   "digits, a 32-bit one eight.")
    label.setFont(mono(POINT_SIZE - 1))
    return label


def _info_panel(s: ScreenInfo) -> QWidget:
    """One screen's facts, as fixed-width text: a table would fight the
    swatches for width, and text is exactly as wide as the numbers are."""
    lines = [
        f"{s.name}  ({s.width}x{s.height}, {s.depth} bits a pixel)",
        f"  mode      {s.mode:#06x}",
        f"  size      {s.width} x {s.height} pixels",
        f"  position  top {s.top}, left {s.left} (global)",
        f"  memory    {s.base_str}  ({s.row_bytes} bytes a row, "
        f"{s.row_bytes * s.height} bytes in all)",
    ]
    text = QLabel("\n".join(lines))
    text.setFont(mono())
    text.setStyleSheet("border: 1px solid #999; padding: 6px")
    return text


def open_hardware(display) -> HardwareWindow:
    """The window, shown.  Kept on the display so it stays alive while it's
    on screen, and raised if it's already there."""
    window = getattr(display, "hardware", None)
    if window is None:
        window = display.hardware = HardwareWindow(display)
    window.show()
    window.raise_()
    return window
