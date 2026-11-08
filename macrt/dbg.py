"""
macrt.dbg - what a debugger does, wherever the commands are typed.

The window and the MCP server both hand a line of text to Commands and get
lines of text back.  What a command does -- breakpoints set and cleared,
the registers read, memory dumped and written -- is here; how it's shown is
theirs.  Kept in one place so a client across a socket and the window on the
desktop can't come to disagree about what `bmw $1234` means, or about why
the core stopped.

Nothing in here touches Qt, and nothing needs a booted Mac to be tried:
given a display with a machine under it -- even a stand-in one -- the whole
command grammar runs.

Addresses.  A plain number is a live one: where the byte is in the Mac's
memory right now, which is what the registers hold and the listing shows.
Ghidra's addresses are another thing.  The JankLoader image it was loaded
from is assembled once, statically, and the code runs somewhere else again,
segmented and relocated, with the globals under a different A5.  Writing
`ram:` in front of a number says "Ghidra counts it this way", and it's
translated through the running application's map -- the same AppMap the
function tracer and the hook engine use.  Every command that takes an
address takes either form: b, bmr, bmw, dm, sm.  What it came out as is said
back (`breakpoint at $00010ABC (ram:00012345)`), because the address you
typed and the address the machine has are not the same number, and a
debugger that hides that is a debugger that has you breaking in the wrong
place.

A ram: address whose segment isn't loaded is an error rather than a
breakpoint that never fires.  A map made before that segment loaded is read
again first -- the live jump table is where the truth is -- and only then is
"not loaded" believed.
"""

from __future__ import annotations

from typing import NamedTuple

from .memory import BusError

PAUSED, RUNNING, NOTHING = "paused", "running", "nothing to watch yet"
NO_PROCESSOR = "no processor yet: nothing to break on"
JANK_PREFIX = "ram:"        # Ghidra's own name for the address space it loaded

BYTES_PER_LINE = 16         # what a dump line holds, as MacsBug's dm has it
DUMP_LINES = 8              # what a dm with no length shows
DUMP_LINES_MAX = 64         # and the most it will show however big a number
# A value's size comes from how it was written: 2 hex digits is a byte.
VALUE_SIZES = ((2, 1, "byte"), (4, 2, "word"), (8, 4, "long"))

HELP = """commands:  (c in front of a break clears it)
  b   / cb   <addr>        break at an instruction
  bt  / cbt  <trap>        break at a Toolbox trap, by name or by word
  bmr / cbmr <addr>        break on a read of an address
  bmw / cbmw <addr>        break on a write to an address
  dm  <addr> [lines]       dump memory: 16 bytes and their text a line
  sm  <addr> <value>       write memory: 1, 2 or 4 bytes by hex digits
  pause   step   go        hold the core, one instruction, let it run
  status                   the registers, and why it stopped
  bp                       the breakpoints set
An address is hex ($ or 0x optional).  ram:<hex> is Ghidra's address for
the same byte, and works wherever an address does."""


class Place(NamedTuple):
    """An address, and where it came from.  `live` is what the machine
    uses; `jank` is the Ghidra address it was written as, when it was
    written that way -- or the Ghidra address a live one turned out to be,
    when a map knows it."""

    live: int
    jank: int | None = None

    def shown(self) -> str:
        """The address as to be read: the machine's, with the Ghidra one
        next to it when the two differ."""
        here = f"${self.live:08X}"
        if self.jank is None or self.jank == self.live:
            return here
        return f"{here} ({JANK_PREFIX}{self.jank:08X})"


def parse_address(text: str) -> int:
    """An address as typed: hex, with or without the $ or a 0x.  A number
    in a debugger is hex -- a MacsBug address is -- and the letters in one
    (a $2C00's C) rule out reading it any other way."""
    bare = text.strip().lstrip("$")
    if bare.lower().startswith("0x"):
        bare = bare[2:]
    if not bare:
        raise ValueError("no address given")
    try:
        return int(bare, 16)
    except ValueError:
        raise ValueError(f"{text} is not an address") from None


def is_jank(text: str) -> bool:
    """Whether what was typed is a Ghidra address rather than a live one."""
    return text.strip().lower().startswith(JANK_PREFIX)


def parse_value(text: str) -> tuple[int, int, str]:
    """A value to write, as typed: its bytes, how many, and what they're
    called.  How it's written says how big it is -- two hex digits a byte,
    so $12 is a byte, $1234 a word, $12345678 a long -- which is what makes
    `sm $100 4A` not scribble over three bytes the user never meant."""
    bare = text.strip().lstrip("$")
    if bare.lower().startswith("0x"):
        bare = bare[2:]
    if not bare:
        raise ValueError("no value given")
    try:
        value = int(bare, 16)
    except ValueError:
        raise ValueError(f"{text} is not a value") from None
    for digits, size, name in VALUE_SIZES:
        if len(bare) <= digits:
            return value, size, name
    raise ValueError(f"{text} is more than a long: a value is 1 to 8 hex digits")


class Commands:
    """The debugger's commands, run against a display.

    The display is asked for the machine every time, because there isn't
    one until some application's 68k code starts, and a client that connects
    before the game has booted should start getting answers the moment there
    is something to answer about."""

    def __init__(self, display):
        self.display = display
        self.dump_at: int | None = None     # where the last dm read up to
        self.dump_lines = DUMP_LINES

    @classmethod
    def for_display(cls, display) -> "Commands":
        """The one set of commands for a display, made on first ask.

        The window and the MCP server both want a Commands, and they must be
        the same one: dm pages on from where the last dm read to, and that
        memory belongs to whoever looked last.  Kept on the display so its
        life is the display's."""
        found = getattr(display, "dbg_commands", None)
        if found is None or found.display is not display:
            found = display.dbg_commands = cls(display)
        return found

    # -- the machine, which there may not be one of yet -----------------------
    def debug(self):
        """The processor's debugger hold, or None: a session of Python
        guests alone, or none yet at all, has nothing to watch."""
        from .m68k.runtime import machine_exists
        system = getattr(self.display, "system", None)
        if system is None or not machine_exists(system):
            return None
        return system.machine.debug

    def mem(self):
        debug = self.debug()
        return None if debug is None else debug.cpu.mem

    def attach(self) -> bool:
        """Start watching the core, so a pause takes effect at the next safe
        point rather than whenever the application next calls a trap.
        Returns whether there was a core to watch.  Only pause asks for it:
        a status poll that switched the core to slicing every few
        instructions would slow the game for someone only looking."""
        debug = self.debug()
        if debug is None:
            return False
        if not debug.attached:
            debug.attach()
        return True

    def detach(self) -> bool:
        """Stop watching: let a held core go, and stop slicing.  The window
        does this itself as it closes; a client with no window has to say
        so, or the core stays held however long it sits there."""
        debug = self.debug()
        if debug is None:
            return False
        if debug.attached:
            debug.detach()
        return True

    # -- addresses -------------------------------------------------------------
    def place(self, text: str) -> Place:
        """An address as typed, in the machine's own terms.  `ram:` in front
        makes it Ghidra's, and translates it; a live one gets the Ghidra
        address added when a map already in hand knows it, so the answer
        says both without going and building a map for the sake of it."""
        text = (text or "").strip()
        if is_jank(text):
            jank = parse_address(text[len(JANK_PREFIX):])
            return Place(self.to_live(jank), jank)
        live = parse_address(text)
        return Place(live, self.to_jank(live))

    def to_live(self, jank: int) -> int:
        """Where the byte Ghidra calls `jank` is right now."""
        app_map = self.app_map(build=True)
        if app_map is None:
            raise ValueError(
                f"{JANK_PREFIX} needs a running 68k application to translate against")
        live = app_map.resolve(jank)
        if live is None:
            app_map.refresh()         # a segment loaded since the map was made
            live = app_map.resolve(jank)
        if live is None:
            raise ValueError(f"{JANK_PREFIX}{jank:08X} isn't loaded right now")
        return live

    def to_jank(self, live: int) -> int | None:
        """The Ghidra address behind a live one, when a map is already to
        hand: said back so a live address can be looked up in Ghidra.  None
        when there's no map, which is not an error worth reporting."""
        app_map = self.app_map(build=False)
        if app_map is None:
            return None
        try:
            return app_map.ghidra_at(live)
        except Exception:                                     # noqa: BLE001
            return None               # a stand-in map needn't answer

    def app_map(self, build: bool):
        """The running application's map between Ghidra's addresses and
        live ones, or None.

        Whichever map is already in hand comes first: the function tracer's,
        the debugger's namer's, the hook engine's, or the trap trace's --
        they're made as those things start, and reading one is free.  With
        none of them and `build`, one is made from the application's own
        resources, which costs a pass over its CODE resources and is done
        once (the Runner keeps it)."""
        for runner in self._runners():
            for which in ("tracer", "namer", "patcher"):
                found = getattr(getattr(runner, which, None), "app_map", None)
                if found is not None:
                    return found
            found = getattr(runner, "_trace_app_map", None)
            if found is not None:
                return found
        if not build:
            return None
        for runner in self._runners():
            if getattr(runner.process, "app", None) is not None:
                # Building one reads the live jump table, so ask the GUI
                # thread -- which answers right away when this thread *is*
                # it.  Reading an address from here is fine (the RAM is one
                # bytearray); making a map isn't, and the automation layer
                # goes the same way for the same reason.
                bridge = getattr(self.display, "bridge", None)
                return bridge.call(runner.trace_map) if bridge is not None \
                    else runner.trace_map()
        return None

    def _runners(self) -> list:
        """The applications with 68k code, the one the core is running
        first: it's the one whose names and segments the debugger is
        showing, and the one Ghidra was pointed at."""
        out: list = []
        debug = self.debug()
        first = getattr(debug.cpu, "debug_runner", None) if debug is not None else None
        if first is not None:
            out.append(first)
        for proc in getattr(self.display, "processes", ()) or ():
            runner = getattr(proc, "runner", None)
            if runner is not None and all(runner is not r for r in out):
                out.append(runner)
        return out

    # -- running ---------------------------------------------------------------
    def pause(self, wait: float = 1.0) -> dict:
        """Stop the core at the next safe point, and report where it got to.
        Not exact, and can't be: the core is taken between two instructions
        where the runtime is in Python anyway.  A breakpoint is exact."""
        debug = self.debug()
        if debug is None:
            return {"state": NOTHING, "message": NO_PROCESSOR}
        if not debug.attached:
            debug.attach()            # else a pause waits out a trap-free loop
        if not debug.is_stopped():
            debug.request_pause()
            if not self._wait(lambda: debug.is_stopped(), wait):
                status = self.status()
                # The core takes the pause at its next safe point, and an
                # application inside a long Toolbox call -- or one that has
                # stopped answering events -- may not reach one for a while.
                # Better to say that than to have gone quiet about it.
                status["message"] = ("still running: it hasn't reached a safe "
                                     "point yet -- try again, or set a breakpoint")
                return status
        return self.status()

    def step(self, wait: float = 1.0) -> dict:
        """One more instruction, then stopped again.  While it's running
        free there's nothing to step from, so this stops it and says so
        rather than running one instruction of an application nobody meant
        to single-step."""
        debug = self.debug()
        if debug is None:
            return {"state": NOTHING, "message": NO_PROCESSOR}
        if not debug.is_stopped():
            status = self.pause(wait)
            if status.get("paused"):
                status["message"] = ("it was running, so it's stopped here; "
                                     "step again to move one instruction")
            return status
        before = debug.get_view()
        debug.request_step()
        self._wait(lambda: debug.is_stopped() and debug.get_view() is not before,
                   wait)
        return self.status()

    def go(self, wait: float = 0.5) -> dict:
        """Let it run on, still watched, so it can be stopped again."""
        debug = self.debug()
        if debug is None:
            return {"state": NOTHING, "message": NO_PROCESSOR}
        was = debug.get_view()
        debug.request_go()
        # Wait for it to have left the stop it was in.  A moment's work -- the
        # guest thread is waiting on the very condition this notifies -- and
        # it's what makes the answer mean something: without it, go would
        # usually report the machine as still paused, which is a lie about a
        # thing that has already been set in motion.
        self._wait(lambda: not debug.is_stopped() or debug.get_view() is not was,
                   wait)
        return self.status()

    def _wait(self, cond, seconds: float) -> bool:
        """Wait for the core to get there, if this thread may wait at all.

        On the GUI thread it may not: a guest stopped in a trap can be asking
        this very thread to run something for it, and then each would wait for
        the other -- a debugger's Pause button must not be able to freeze the
        Mac it's watching.  Nothing is lost by not waiting.  The window takes
        its snapshot every POLL anyway and will show the stop the moment it
        lands, and only the MCP server -- a thread with no such entanglement,
        and a client that wants the answer in the reply -- does the waiting."""
        bridge = getattr(self.display, "bridge", None)
        if bridge is not None and bridge.on_gui_thread():
            return cond()
        import time
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if cond():
                return True
            time.sleep(0.002)
        return False

    # -- what state it's in ------------------------------------------------------
    def status(self) -> dict:
        """Where things are.  Stopped, everything there is to know: why, and
        the registers, and the code around the PC.  Running, that's the
        answer -- there's no consistent machine to read while the guest
        thread is inside the core, which is the whole reason for asking."""
        debug = self.debug()
        if debug is None:
            return {"state": NOTHING, "paused": False, "running": False,
                    "attached": False, "message": NO_PROCESSOR}
        held = debug.is_stopped()
        out = {"state": PAUSED if held else RUNNING, "paused": held,
               "running": not held, "attached": debug.attached,
               "breakpoints": debug.bp_list()}
        if not held:
            out["message"] = "the program is running: pause to see where"
            return out
        view = debug.get_view()
        if view is None:
            out["message"] = "stopped, but with no snapshot taken yet"
            return out
        out["reason"] = debug.get_reason()
        out["where"] = where(view)
        out["pc"] = hexed(view["pc"])
        out["sp"] = hexed(view["sp"])
        out["sr"] = hexed(view["sr"], 4)
        out["flags"] = flag_line(view["flags"])
        out["flag_bits"] = dict(view["flags"])
        out["mode"] = mode(view["flags"])
        out["d"] = [hexed(v) for v in view["d"]]
        out["a"] = [hexed(v) for v in view["a"]]
        out["app_name"] = view.get("app_name") or ""
        out["symbols"] = bool(view.get("symbols"))
        if view.get("symbol"):
            out["symbol"] = view["symbol"]
        out["listing"] = [{"address": hexed(row.address), "raw": row.raw,
                           "text": row.text, "at_pc": row.address == view["pc"]}
                          for row in view["listing"]]
        return out

    def status_text(self) -> list[str]:
        """The same, as lines for a log: the reason, where, and the whole
        row of registers under it."""
        status = self.status()
        if not status.get("paused"):
            return [status.get("message", status["state"])]
        view = self.debug().get_view()
        return [f"{status.get('reason', '')}{status['where']}".strip()] \
            + register_text(view).splitlines()

    # -- memory --------------------------------------------------------------------
    def dump(self, place: Place, lines: int) -> tuple[list[str], int]:
        """Memory as text: sixteen bytes a line, their ASCII after them, and
        where the next line would start.  Returns the lines and the address
        after the last byte read, so another dm without an address reads on."""
        mem = self._mem()
        lines = max(1, min(int(lines), DUMP_LINES_MAX))   # however it was asked
        rows, ended = dump_rows(mem, place.live, lines)
        out = []
        if place.jank is not None and place.jank != place.live:
            # Say up front which of the two numbers the columns are: they're
            # the machine's, and Ghidra's are another set entirely.
            out.append(f"{JANK_PREFIX}{place.jank:08X} is ${place.live:08X}")
        next_at = place.live
        for at, data in rows:
            next_at = at + len(data)
            out.append(dump_line(at, data))
        if not rows:
            out.append(f"${place.live:08X} is outside RAM")
        elif ended:
            out.append(f"end of RAM at ${next_at:08X}")
        return out, next_at

    def write(self, place: Place, value: int, size: int, name: str) -> list[str]:
        """A value written, and what was there before it: a write that turns
        out to have been the wrong place is legible that way.

        An address the machine won't take raises BusError rather than being
        answered here, so a caller that reports failures by exception -- the
        MCP server, where a client reads `isError` -- can tell one from a
        write that happened.  `run` catches it and answers it, which is what
        someone typing `sm` at the prompt wants."""
        mem = self._mem()
        at = place.live
        was = read(mem, at, size)
        write(mem, at, value, size)
        return [f"wrote ${value:0{2 * size}X} to {place.shown()} ({name}), "
                f"was {' '.join(f'{b:02X}' for b in was)}"]

    def read(self, place: Place, count: int) -> dict:
        """Memory as data rather than text, for a client that wants the
        bytes: hex, ASCII, and the address after what was read."""
        mem = self._mem()
        rows, ended = dump_rows(mem, place.live, (count + BYTES_PER_LINE - 1)
                                // BYTES_PER_LINE)
        data = b"".join(d for _, d in rows)
        return {"address": hexed(place.live), "requested": count,
                "read": len(data), "bytes": data.hex().upper(),
                "ascii": ascii_of(data), "truncated": ended}

    def _mem(self):
        mem = self.mem()
        if mem is None:
            raise ValueError(NO_PROCESSOR)
        return mem

    # -- the commands ---------------------------------------------------------------
    def run(self, line: str) -> list[str]:
        """A command as typed at the prompt, and the lines it answers with.
        Nothing it does raises: a debugger's user is typing at a machine
        that's stopped, and the answer has to say what went wrong there."""
        parts = (line or "").split()
        if not parts:
            return []
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("?", "help"):
            return HELP.splitlines()
        # The words first, because c means continue and also starts a cleared
        # breakpoint: "continue" is the same as "g" whatever letter it starts
        # with, and a bare c with an address after it clears that one, the way
        # c in front of any other break letter does.
        if cmd in ("g", "go", "continue") or (cmd == "c" and not args):
            return [go_said(self.go())]
        if cmd in ("s", "step"):
            return [go_said(self.step())]
        if cmd == "pause":
            return [go_said(self.pause())]
        if cmd in ("status", "regs"):
            return self.status_text()
        if cmd in ("bp", "bpl"):
            debug = self.debug()
            return debug.bp_list() if debug is not None else [NO_PROCESSOR]
        if cmd == "dm":
            return _answered(self.dump_command, args)
        if cmd == "sm":
            return _answered(self.write_command, args)
        # dm and sm need a processor too, but say so in terms of the address
        # they couldn't read; the rest is answered here.
        if cmd[0] == "c" and len(cmd) > 1:
            kind, clearing = cmd[1:], True
        elif cmd == "c":                    # c <addr> clears that breakpoint
            kind, clearing = "b", True      # ...which is what c means with one
        else:
            kind, clearing = cmd, False
        try:
            if kind == "b":
                return self.break_address(args, clearing)
            if kind == "bt":
                return self.break_trap(args, clearing)
            if kind in ("bmr", "bmw"):
                return self.break_memory(kind[2], args, clearing)
            return [f"don't know the command {cmd}"]
        except ValueError as e:
            return [str(e)]
        except BusError as e:
            return [str(e)]

    def dump_command(self, args: list[str]) -> list[str]:
        """dm: an address, or nothing to read on from where the last dump
        left off -- paging through memory shouldn't mean typing the address
        again each time."""
        if args:
            place = self.place(args[0])
            if len(args) > 1:
                self.dump_lines = _lines(args[1])
        elif self.dump_at is None:
            raise ValueError("no address given")
        else:
            place = Place(self.dump_at, self.to_jank(self.dump_at))
        lines, next_at = self.dump(place, self.dump_lines)
        self.dump_at = next_at
        return lines

    def write_command(self, args: list[str]) -> list[str]:
        if len(args) < 2:
            raise ValueError("no value given")
        place = self.place(args[0])
        value, size, name = parse_value(args[1])
        return self.write(place, value, size, name)

    # -- breakpoints -----------------------------------------------------------------
    def break_address(self, args: list[str], clearing: bool) -> list[str]:
        debug = self._debug_for("break")
        place = self.place(args[0] if args else "")
        if clearing:
            debug.clear_bp_addr(place.live)
        else:
            debug.add_bp_addr(place.live)
        return [f"{'cleared breakpoint' if clearing else 'breakpoint'} "
                f"at {place.shown()}"]

    def break_memory(self, letter: str, args: list[str], clearing: bool) -> list[str]:
        debug = self._debug_for("break")
        place = self.place(args[0] if args else "")
        add, clear = (debug.add_bp_memr, debug.clear_bp_memr) if letter == "r" \
            else (debug.add_bp_memw, debug.clear_bp_memw)
        (clear if clearing else add)(place.live)
        what = f"{'read' if letter == 'r' else 'write'} of {place.shown()}"
        return [f"{'cleared breakpoint on' if clearing else 'breakpoint on'} {what}"]

    def break_trap(self, args: list[str], clearing: bool) -> list[str]:
        debug = self._debug_for("break")
        name = args[0] if args else ""
        keys = sorted(trap_keys(name))
        if not keys:
            return [f"no trap called {name}"]
        for key in keys:
            (debug.clear_bp_trap if clearing else debug.add_bp_trap)(key)
        shown = f"_{dispatch_name(keys[0])}" if len(keys) == 1 else name
        return [f"{'cleared breakpoint on' if clearing else 'breakpoint on'} "
                f"trap {shown}"]

    def _debug_for(self, doing: str):
        debug = self.debug()
        if debug is None:
            raise ValueError(NO_PROCESSOR)
        return debug


# ---------------------------------------------------------------------------
# The text of it: registers, dumps, trap names
# ---------------------------------------------------------------------------
def hexed(value: int, digits: int = 8) -> str:
    return f"0x{value & (0xFFFFFFFF if digits > 4 else 0xFFFF):0{digits}X}"


def _answered(do, args: list[str]) -> list[str]:
    """A command run where nothing it says is an exception: what's wrong is
    the answer, and the caller -- a window, or a socket -- has no use for a
    traceback."""
    try:
        return do(args)
    except ValueError as e:
        return [str(e)]
    except BusError as e:
        return [str(e)]


def _lines(text: str) -> int:
    """How many lines a dump should be, from what was typed after the
    address."""
    try:
        n = int(text, 10)
    except ValueError:
        raise ValueError(f"{text} is not a number of lines") from None
    if n < 1:
        raise ValueError("a dump is at least a line")
    return min(n, DUMP_LINES_MAX)


def dump_rows(mem, at: int, lines: int) -> tuple[list[tuple[int, bytes]], bool]:
    """The bytes of a dump, a line at a time, and whether they ran out:
    reading off the end of RAM stops the dump there rather than failing it,
    because half a dump of what's there beats no dump at all."""
    rows: list[tuple[int, bytes]] = []
    for n in range(lines):
        row = at + n * BYTES_PER_LINE
        try:
            rows.append((row, bytes(mem.read(row, BYTES_PER_LINE))))
        except BusError:
            data = bytearray()
            for k in range(BYTES_PER_LINE):
                try:
                    data.append(mem.r8(row + k))
                except BusError:
                    break
            if data:
                rows.append((row, bytes(data)))
            return rows, True
    return rows, False


def dump_line(at: int, data: bytes) -> str:
    """One line of a dump: the address, the bytes in two groups of eight,
    and their text -- printable characters as they are, everything else a
    dot, which is what makes a string stand out of a field of numbers.
    Every column is a fixed width, including the text, so the last line of
    RAM -- a part one -- is still in the table."""
    shown = data[:BYTES_PER_LINE]
    hexed = " ".join(f"{b:02X}" for b in shown[:8])
    tail = " ".join(f"{b:02X}" for b in shown[8:])
    return (f"{at:08X}  {hexed:<{8 * 3 - 1}}  {tail:<{8 * 3 - 1}}  "
            f"{ascii_of(shown):<{BYTES_PER_LINE}}")


def ascii_of(data: bytes) -> str:
    """The bytes as text: Mac Roman's high half can be letters, but which
    ones depends on the font, so anything over ~ is a dot and only plain
    printing characters show as themselves."""
    return "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in data)


def read(mem, at: int, size: int) -> bytes:
    return mem.read(at, size)


def write(mem, at: int, value: int, size: int) -> None:
    {1: mem.w8, 2: mem.w16, 4: mem.w32}[size](at, value)


def go_said(status: dict) -> str:
    """What a run command got: where the core is now, or that it's away."""
    if status.get("paused"):
        return f"{status.get('reason', '')}{status['where']}".strip()
    return status.get("message", status["state"])


def where(view: dict) -> str:
    """Where a stop is, said the way the user asked for it: the address, and
    the routine the PC sits in with its offset from the entry -- when a
    symbol table knows one.  (The snapshot carries the symbol already in
    that shape; see Debug.snapshot.)  A PC with nothing around it to name
    says whether that's because no table is loaded, or the address is simply
    outside any routine one was made for."""
    place = f"${view['pc'] & 0xFFFFFF:06X}"
    if view.get("symbol"):
        return f" at {place}, {view['symbol']}"
    if not view.get("symbols"):
        return f" at {place}  (no symbol table loaded)"
    return f" at {place}  (not in a known routine)"


def register_text(view: dict, pc_ram: str | None = None) -> str:
    """The registers as they're written: hex, the flags by name, the mode
    and interrupt level the status register holds.  Under the PC, the
    routine it sits in, when a symbol table knows one -- the line is left
    blank when it doesn't, so SP stays where it is.  The PC's own line
    carries its Ghidra address when one is known: `pc_ram`, as `ram:XXXXXXXX`
    -- the byte the PC points at, counted the way the dump counts it, which
    is how it's looked up there.  Over the data registers, the application
    the low-memory globals say is current: whose code these registers are
    running."""
    flags = view["flags"]
    lines = [f" PC  {view['pc']:08X}" + (f"  {pc_ram}" if pc_ram else ""),
             f"     {view['symbol']}" if view.get("symbol") else ""]
    lines += [f" SP  {view['sp']:08X}",
              f" SR  {view['sr']:04X}  {flag_line(flags)}  {mode(flags)}", "",
              "CurApName:", view.get("app_name") or "(no application)", ""]
    for n, v in enumerate(view["d"]):
        lines.append(f" D{n}  {v:08X}")
    lines.append("")
    for n, v in enumerate(view["a"]):
        lines.append(f" A{n}  {v:08X}")
    return "\n".join(lines)


def mode(flags: dict) -> str:
    """The part of the status register that isn't a condition code."""
    level = flags["irq"]
    return ("sup" if flags["supervisor"] else "usr") + \
           (f" irq{level}" if level else "") + (" trace" if flags["trace"] else "")


def register_text_sample() -> str:
    """The longest line register_text can ever write, so the panel can be
    made exactly wide enough for it: a flag is a letter whether it's set or
    not, and the mode line has every part of it."""
    return " SR  FFFF  XNZVC  sup irq7 trace"


def flag_line(flags: dict) -> str:
    """The condition codes as MacsBug writes them: the ones that are set,
    among the ones that aren't."""
    return "".join(c if flags[c] else c.lower() for c in "XNZVC")


_TRAP_NAMES: dict[str, set[int]] | None = None


def trap_keys(name: str) -> set[int]:
    """The trap keys a command's argument names: a routine's name with or
    without its underscore, or a trap word itself -- a $ or 0x always makes
    a word, and a bare hex number is one when it's in the A-line range
    where every trap word is.  Nothing for a name the trap table doesn't
    know, so the caller can say so."""
    text = name.strip()
    bare = text.lstrip("$")
    explicit = text.startswith("$") or bare.lower().startswith("0x")
    try:
        word = int(bare[2:] if bare.lower().startswith("0x") else bare, 16)
    except ValueError:
        word = None
    if word is not None and (explicit or 0xA000 <= word <= 0xAFFF):
        return {dispatch_trap_key(word)}
    return set(reverse_trap_names().get(text.lstrip("_"), ()))


def reverse_trap_names() -> dict[str, set[int]]:
    """Trap names to the keys they are: a break on a name catches every word
    that is one.  Built once, from the trap table the disassembler uses."""
    global _TRAP_NAMES
    if _TRAP_NAMES is None:
        from .m68k import dispatch
        _TRAP_NAMES = {}
        for key, name in dispatch.NAMES.items():
            _TRAP_NAMES.setdefault(name, set()).add(key)
    return _TRAP_NAMES


def dispatch_trap_key(word: int) -> int:
    from .m68k import dispatch
    return dispatch.trap_key(word)


def dispatch_name(key: int) -> str:
    from .m68k import dispatch
    return dispatch.trap_name(key)
