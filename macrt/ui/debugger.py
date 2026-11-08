"""
macrt.ui.debugger - A window that watches the 68k processor.

It opens with --debug, alongside the Mac screen.  Pause and resume across
the top; the registers down the left; the breakpoints down the right; and
in the middle, a log of what's happened over the code around the program
counter, with the arrow at the line the PC is on.  Under that, a command
line: b, bt, bmr and bmw to break, the same with a c in front to clear, dm
and sm to read and write memory -- and ram: in front of any address to give
it the way Ghidra counts, which the debugger translates for you.

What those commands *do* is in macrt.dbg, shared with the MCP server, so a
command typed here and the same command over a socket can't mean two
things.  This file is the window: the asking, the drawing, and the log.

The window never touches the processor.  The guest thread owns it and is
inside the core's native code whenever instructions are running; reading
the core from here then would read it half-written.  So the window only
leaves requests with the machine's Debug object -- pause, step, go, add a
breakpoint -- and reads back the snapshot that object takes when the core
is known to be at rest.  What it shows may be a moment old; it's never
half an instruction.

Pausing isn't exact.  The core is taken at the next safe point -- the next
trap, or the end of the next slice of instructions -- rather than at some
instruction chosen beforehand, which is what a pause button wants.  A
breakpoint is exact, and works another way: a hook the core stops on, in
the middle of running, which the machine's Debug object arranges.  Each
stop says why in the log -- the pause button, a step, or the breakpoint
that fired.

Nor is the listing a live view: it's the registers and the bytes as they
were when the core stopped.  While the application runs on, the window
keeps showing where it was stopped, which is the point of stopping it.

Where the routine around the PC is already known -- --debug reads the
symbol table for names alone, and --trace-functions for the full trace --
it's named under the listing.  Nothing here
goes and fetch one itself.
"""

from __future__ import annotations

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPalette, QTextCursor
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                             QPlainTextEdit, QPushButton, QTextEdit,
                             QVBoxLayout, QWidget)

from ..dbg import (PAUSED, RUNNING, NOTHING, Commands, flag_line, mode,
                   parse_address, register_text, register_text_sample, trap_keys,
                   where)

POLL = 100                # ms between looks: the window's heartbeat
POINT_SIZE = 13
RAW_WIDTH = 20            # how wide the bytes column is, in characters
LOG_LINES = 500           # what the running log keeps: a game breaking in its
                          # own loop would otherwise fill it up


class DebuggerWindow(QMainWindow):
    """The window: buttons across the top, the registers down the left, the
    breakpoints down the right, and in the middle a log of what's happened
    over the listing, with a command line under it."""

    def __init__(self, display):
        super().__init__()
        self.display = display
        # What a command does: macrt.dbg.  The one set for this display, so
        # the MCP server and this window share it -- one dm page, one set of
        # breakpoints -- rather than two that agree until someone pages
        # through memory from both.
        self.commands = Commands.for_display(display)
        self.setWindowTitle("Classetta debugger")
        self.resize(1200, 620)
        self._shown = None              # what's on screen: not redrawn each poll
        self._logged_view = None        # the stop whose reason the log has: logged once
        self._closing = False           # the display's shutdown: let it close
        central = QWidget(self)
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        controls = QHBoxLayout()
        outer.addLayout(controls)
        self.play = QPushButton("Pause")
        self.step = QPushButton("Step")
        self.play.clicked.connect(self._toggled)
        self.step.clicked.connect(self._stepped)
        self.play.setEnabled(False)
        self.step.setEnabled(False)
        controls.addWidget(self.play)
        controls.addWidget(self.step)
        self.status = QLabel(NOTHING)
        self.status.setStyleSheet("color: #777")
        controls.addStretch(1)
        controls.addWidget(self.status)

        # Every panel is plain monospace text.  A table would look tidier,
        # but its columns fight each other for width and clip a value to
        # "$0000000"; text is exactly as wide as the numbers are.
        panels = QHBoxLayout()
        outer.addLayout(panels)
        self.registers = panel(mono(POINT_SIZE - 1))
        self.listing = panel(mono())
        self.log = panel(mono(POINT_SIZE - 1))
        self.breakpoints = panel(mono(POINT_SIZE - 1))
        # The register panel starts no wider than the widest register line
        # it can hold; it takes no part in the squeezing, and the listing
        # has the rest.  A routine name under the PC can be longer than
        # any of those, so _draw widens the panel for one it measures --
        # once, and never back: a name is never clipped, and the panel
        # doesn't shiver as the names change.
        self._registers_metrics = QFontMetrics(self.registers.font())
        self._registers_margin = (2 * self.registers.document().documentMargin()
                                  + 2 * self.registers.frameWidth())
        width = round(self._registers_metrics.horizontalAdvance(register_text_sample())
                      + self._registers_margin)
        self.registers.setFixedWidth(width)
        # The breakpoints panel takes the same width: a line is a command's
        # letters and one number, or "bt" and a trap's name, and the measure
        # that fits a register line fits either.
        self.breakpoints.setFixedWidth(width)
        panels.addWidget(self.registers, 0)
        panels.addWidget(rule(), 0)

        # The middle: the log over the listing, each half the height, with
        # the command line under them.  What happened, what runs next, and
        # what to do about it -- in the order a debugger works through them.
        middle = QVBoxLayout()
        panels.addLayout(middle, 1)
        middle.addWidget(self.log, 1)
        middle.addWidget(hrule(), 0)
        middle.addWidget(self.listing, 1)
        self.command = QLineEdit()
        self.command.setFont(mono(POINT_SIZE - 1))
        self.command.returnPressed.connect(self._command)
        middle.addWidget(self.command, 0)

        panels.addWidget(rule(), 0)
        panels.addWidget(self.breakpoints, 0)

        self._timer = QTimer(self, interval=POLL)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self._log("""Classetta Debugger:
b <addr>: break at addr
bt <trap>: break at trap (specify name)
bmr/bmw <addr>: break on memory read / memory write at addr
prepend c to command to clear breakpoint i.e. cb <addr>

dm <addr>: display memory at address
sm <addr> <value>: set memory at address

For any <addr>:
    bare hex for machine address
    ram:<hex> for an address that will match Ghidra program dump
"""
                  "")

    # -- the machine, which there may not be one of yet -----------------------
    def _debug(self):
        """The processor's debugger hold, or None.  A processor exists only
        once some application's 68k code has started: a session of Python
        guests alone, or none yet at all, has nothing to watch.  So this is
        asked every poll, and a window opened before the game boots starts
        watching the moment there's something to watch."""
        from ..m68k.runtime import machine_exists
        system = self.display.system
        return system.machine.debug if machine_exists(system) else None

    # -- the buttons ----------------------------------------------------------
    def _toggled(self) -> None:
        debug = self._debug()
        if debug is None:
            return
        if debug.is_stopped():
            debug.request_go()
        else:
            debug.request_pause()

    def _stepped(self) -> None:
        debug = self._debug()
        if debug is not None:
            debug.request_step()

    def closeEvent(self, event):  # noqa: N802
        """The window doesn't go away at the user's wish: it's the machine's
        hold on the processor, and closing it while held would strand the
        core -- and a debugger one can lose by accident isn't much of one.
        It closes only when the display says so, at shutdown, when its job
        is to let go of the processor before the applications are killed."""
        if self._closing:
            debug = self._debug()
            if debug is not None:
                debug.detach()          # let a held core go, and stop slicing
            self._timer.stop()
            super().closeEvent(event)
        else:
            event.ignore()

    def force_close(self) -> None:
        """Close the window past its refusal: the display's way, and the
        only way."""
        self._closing = True
        self.close()

    # -- the command line -------------------------------------------------------
    # One command a line, entered as typed: b and cb and their kindred, in
    # the MacsBug letters, dm and sm for memory, and ram: in front of an
    # address to name it Ghidra's way.  What they do lives in macrt.dbg,
    # shared with the MCP server so the two can't come to disagree about what
    # a command means; what's here is the typing, and the log the answer goes
    # into.
    def _command(self) -> None:
        text = self.command.text().strip()
        self.command.clear()
        if not text:
            return
        self._log(f"> {text}")
        try:
            for line in self.commands.run(text):
                self._log(line)
        except Exception as e:                                   # noqa: BLE001
            # A command is no reason to lose the window, and with it the
            # hold on the core: what went wrong is the answer, not a crash.
            self._log(f"{type(e).__name__}: {e}")
        self.refresh_breakpoints()

    # -- the log ----------------------------------------------------------------
    def mcp_log(self, task: str, result) -> None:
        """A request that came in over the MCP server, and what it answered.
        It goes into the same log a typed command goes into, so the two can
        be read side by side; the server's thread posts it here, on the GUI
        one, which is the window's."""
        self._log(f"mcp> {task}")
        if result:
            self._log(f"    {result}")

    def _log(self, text: str) -> None:
        """A line added to what's happened.  The log is kept to what a window
        is tall enough to scroll through; a game that breaks at a trap in its
        own loop would otherwise fill it, and it's the last lines that say
        where things are."""
        self.log.appendPlainText(text)
        if self.log.blockCount() > LOG_LINES:
            self.log.setPlainText("\n".join(self.log.toPlainText().splitlines()[-LOG_LINES:]))
        scrollbar = self.log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # -- drawing --------------------------------------------------------------
    def refresh(self) -> None:
        debug = self._debug()
        if debug is None:
            self.play.setEnabled(False)
            self.step.setEnabled(False)
            self._show_state(NOTHING, None, "")
            return
        if not debug.attached:            # the window opened before there was
            debug.attach()                # a processor: start watching now
        held = debug.is_stopped()
        self.play.setEnabled(True)
        self.step.setEnabled(held)
        # The last snapshot stays up while the application runs on: it's
        # where the core was stopped, and there's nothing newer to show.
        self._show_state(PAUSED if held else RUNNING, debug.get_view(), debug.get_reason())
        self.refresh_breakpoints()

    def refresh_breakpoints(self) -> None:
        """The list of them, rewritten only when it's changed.  It's the
        machine's own sets that say what's broken on, so what's listed is
        what's armed, not what was typed."""
        debug = self._debug()
        lines = "\n".join(debug.bp_list()) if debug is not None else ""
        if self.breakpoints.toPlainText() != lines:
            self.breakpoints.setPlainText(lines)

    def _show_state(self, state: str, view: dict | None, reason: str) -> None:
        """Redraw only when something's changed, so a window watching a busy
        application doesn't repaint itself a dozen times a second.  The view
        is recognised by identity -- a new snapshot is a new dict -- and the
        one shown is kept, so its id can't be reused by a dict that isn't it.

        Every new stop is one the log hasn't said yet: what it was for, and
        where the core is -- the address, and the routine around it when a
        symbol table knows one.  The machine says why; the window has the
        snapshot, so the where is added here."""
        if self._shown is not None and self._shown[0] == state \
                and self._shown[1] is view:
            return
        self._shown = (state, view)
        self.status.setText(state)
        self.play.setText("Resume" if state == PAUSED else "Pause")
        if view is None:
            self._clear()
        else:
            if state == PAUSED and view is not self._logged_view:
                self._logged_view = view
                # A step says nothing: the window moving one instruction on
                # is itself the news, and a line each time would be noise.
                if reason:
                    self._log(f"{reason}{where(view)}")
            self._draw(view)

    def _clear(self) -> None:
        if self.registers.toPlainText():
            self.registers.setPlainText("")
        if self.listing.toPlainText():
            self.listing.setPlainText("")

    def _draw(self, view: dict) -> None:
        # The PC's Ghidra address, when a map is to hand: the byte it points
        # at, counted the way the dump counts it.  None when there's no map
        # yet, which is not an error -- the line simply stays the machine's
        # own.
        jank = self.commands.to_jank(view["pc"])
        text = register_text(view, None if jank is None else f"ram:{jank:08X}")
        self._fit_registers(text)
        self.registers.setPlainText(text)
        self._draw_listing(view)

    def _fit_registers(self, text: str) -> None:
        """Widen the register panels for a line wider than the sample they
        were measured with -- a routine's name under the PC can be longer
        than any register line.  Only ever wider: a name is never clipped,
        and the panels don't shiver as the names change."""
        need = max(self._registers_metrics.horizontalAdvance(line)
                   for line in text.splitlines()) + self._registers_margin
        current = self.registers.width()
        if need > current:
            # Both panels together, so the window keeps its look.  The
            # listing, which is what gives way, has the stretch.
            self.registers.setFixedWidth(round(need))
            self.breakpoints.setFixedWidth(round(need))

    def _draw_listing(self, view: dict) -> None:
        """The code around the program counter, the arrow marking the line
        it's on.  The lines before it are what's in memory there, not what
        ran to get here."""
        pc = view["pc"]
        lines = [f"{'=>' if row.address == pc else '  '} {row.address:06X}  "
                 f"{row.raw:<{RAW_WIDTH}}  {row.text}" for row in view["listing"]]
        at = next((n for n, row in enumerate(view["listing"]) if row.address == pc), None)
        if view.get("symbol"):
            lines += ["", f"in  {view['symbol']}"]
        self.listing.setPlainText("\n".join(lines))
        self.listing.setExtraSelections([] if at is None else [current_line(self.listing, at)])


# where(), register_text() and the rest of the text a stop is written in come
# from macrt.dbg, where the MCP server writes the same stop the same way.


def current_line(listing, row: int) -> QTextEdit.ExtraSelection:
    """The selection covering one line of the listing: the instruction the
    PC is on, written in red.  (A wash of colour behind it instead was the
    first attempt, and read as grey mush -- the text carries the arrow
    anyway.  ExtraSelection is only defined on QTextEdit, in Qt's own
    headers, though QPlainTextEdit is what takes it.)"""
    block = listing.document().findBlockByNumber(row)
    selection = QTextEdit.ExtraSelection()
    selection.cursor = QTextCursor(block)
    selection.cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                  QTextCursor.MoveMode.KeepAnchor)
    selection.format.setForeground(current_text_colour(listing))
    return selection


def current_text_colour(widget) -> QColor:
    """Red, but the red that reads: dark ink on a light panel, a bright one
    if the desktop is a dark theme.  The rest of the window takes the
    palette's own text colour, so this follows it rather than assuming;
    either way it stays far enough from the panel behind it to be seen."""
    base = widget.palette().color(QPalette.ColorRole.Base)
    if base.lightness() < 128:
        return QColor(255, 120, 120)
    return QColor(170, 0, 0)


def panel(font: QFont) -> QPlainTextEdit:
    """A read-only panel of fixed-width text, which is what all of this is."""
    field = QPlainTextEdit()
    field.setReadOnly(True)
    field.setFont(font)
    field.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    field.setFrameShape(QFrame.Shape.NoFrame)
    return field


def rule() -> QFrame:
    """The line between the two panels."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def hrule() -> QFrame:
    """The line between the log and the listing."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def mono(point_size: int = POINT_SIZE) -> QFont:
    font = QFont("Menlo")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(point_size)
    return font


def open_debugger(display) -> DebuggerWindow:
    """The window, shown.  Kept on the display so it stays alive while it's
    on screen, and raised if it's already there."""
    window = getattr(display, "debugger", None)
    if window is None:
        window = display.debugger = DebuggerWindow(display)
    window.show()
    window.raise_()
    return window
