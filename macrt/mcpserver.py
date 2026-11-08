"""
macrt.mcpserver - the debugger, and the script's powers, over MCP on localhost.

An MCP client -- Claude Code, or anything else that speaks the protocol --
connects over HTTP on localhost and gets the debugger's own commands plus what
a script can do: status, pause, step, continue, breakpoints, memory,
screenshots, keys and clicks.

    python -m macrt App.bin --debug --mcp         # http://127.0.0.1:8765/mcp
    python -m macrt App.bin --mcp 9000

and in the client's config:

    {"classetta": {"type": "http", "url": "http://127.0.0.1:8765/mcp"}}

Built on the SDK's MCPServer, whose StreamableHTTP transport does the wire,
the handshake and the schema.  What's here is the tools and how they reach a
running emulator:

  * The server answers each request with one JSON body.  The emulator is one
    machine and its state is read when asked about, not pushed as it changes,
    so nothing a session or an event stream would carry is carried: the
    session id exists only so a connect and a disconnect can be seen, and two
    clients can still look at the same game, each with its own.

  * Each tool is a method on Tools.  Its docstring is what the client reads,
    and its Annotated parameters carry the descriptions -- so the schema and
    the code can't drift apart, which a hand-written catalogue would.

  * Nothing here touches the core or a widget.  The guest thread owns the
    68k CPU and the GUI thread owns the screen; this is a third thread, and it
    only ever asks -- macrt.dbg for the debugger, and the display's bridge for
    anything on screen.  The rules in macrt.dbg about what's safe to read while
    the machine runs are the rules here.

  * A tool that can't be done answers rather than raising: the SDK turns a
    raised exception into "error executing tool X" and keeps the text on the
    server, and "ram:0012ABCD isn't loaded right now" is exactly the thing the
    client needs to be told.  So `reported` wraps every tool.
"""

from __future__ import annotations

import functools
import logging
import threading
from typing import Annotated, Any

from pydantic import Field

log = logging.getLogger("macrt.mcp")

SERVER_NAME = "classetta"
SERVER_VERSION = "1.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PATH = "/mcp"

INSTRUCTIONS = """Classetta is a classic Mac OS emulator running 68k code, with a debugger
on it.

Addresses. The machine's own are hex ($1234, 0x1234 or 1234) -- what the registers hold and
the listing shows. Ghidra counts the same bytes differently: its image is assembled
statically and the code runs relocated, in segments, under a different A5. So any address
may be written ram:<hex> and the debugger translates it through the running application's
segment map. Say which form you used. A ram: address whose segment isn't loaded is an
error, not a breakpoint that never fires.

State. status answers whether the program is running or stopped. While it runs there is no
consistent machine to read -- the guest thread is inside the CPU -- so pause first, then
status gives the registers, the code around the PC and why it stopped. pause, step and cont
are the run control; command takes a line of the debugger's own grammar (b, cb, bt, bmr,
bmw, dm, sm, ?) if you would rather type it than call a tool.

Driving. screenshot, press, type_text, click and the mouse tools drive the emulated Mac;
wait_window is how you get past a game's boot. A game's own functions can be called with
call_function once it is stopped or waiting for an event."""


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------
# An address, as a client writes it.  Said once, because it's the same answer
# for every tool that takes one, and because it's the part of the convention
# a client can't guess: the machine and Ghidra count the same byte apart.
ADDRESS = Annotated[str, Field(
    description="hex: $1234, 0x1234 or 1234.  Or ram:0012ABCD for the same byte "
                "as Ghidra counts it, which the debugger translates")]

# The two questions several tools ask the same way: how long a key or a click is
# held, and which of the two accesses a memory breakpoint is for.
HOLD = Annotated[float, Field(gt=0, description="seconds to hold (default 0.02)")]
ACCESS = Annotated[str, Field(description="read or write -- which access stops it")]


class Tools:
    """Every tool, bound to one running display.

    Made once per server.  The Controller -- the script's powers -- is taken on
    first need rather than up front: the first call by symbol name reads the
    symbol table, which can mean starting Ghidra headless, and a client that
    only ever takes screenshots shouldn't cause that or pay for it."""

    def __init__(self, display, controller=None):
        self.display = display
        self._controller = controller
        from .dbg import Commands
        # The display's one set of commands, shared with the debugger window so
        # the two are looking at one dm page and one set of breakpoints rather
        # than two that happen to agree for now.
        self.commands = Commands.for_display(display)

    # -- the parts, taken as needed ---------------------------------------------
    def controller(self):
        if self._controller is None:
            from .ui.automation import Controller
            self._controller = Controller(self.display)
        return self._controller

    def gui(self, fn, *args):
        """Run fn on the GUI thread and wait: anything that touches a widget,
        or builds a map by reading the running application, goes this way."""
        bridge = getattr(self.display, "bridge", None)
        return fn(*args) if bridge is None else bridge.call(fn, *args)

    # -- the debugger --------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Whether the program is running.  When it's stopped: why, and the
        registers, flags, current application and the code around the PC.  When
        it's running that's all you get -- there's no consistent machine to read
        while the guest thread is inside the CPU -- so pause first."""
        return self.commands.status()

    def pause(self) -> dict[str, Any]:
        """Stop the program at the next safe point and report where it got to.
        Not exact -- a breakpoint is.  A game asleep waiting for an event is
        nudged awake so it reaches one; if it's inside a long Toolbox call it may
        not, and the answer says so."""
        return self.commands.pause()

    def step(self) -> dict[str, Any]:
        """Run exactly one more instruction, then stop again.  While the program
        runs free this stops it and says so, rather than running one instruction
        of a game nobody meant to single-step."""
        return self.commands.step()

    def cont(self) -> dict[str, Any]:
        """Let the program run on, still watched by the debugger, so it can be
        stopped again.  A breakpoint that's set is a reason to expect a stop."""
        return self.commands.go()

    def command(self, line: Annotated[
        str, Field(description="the command, as typed at the debugger's prompt")]) -> list[str]:
        """A line of the debugger's own command language, exactly as typed at its
        prompt, and the lines it answers with.  Use '?' for the list: b/cb, bt/cbt,
        bmr/cbmr, bmw/cbmw, dm, sm, pause, step, go, status, bp, regs."""
        return self.commands.run(line)

    def breakpoint(self, address: ADDRESS) -> str:
        """Break at an instruction, exactly there.  A ram: address is translated
        through the running application's map; an address in a segment that isn't
        loaded is refused rather than silently never firing."""
        return " ".join(self.commands.break_address([str(address)], False))

    def clear_breakpoint(self, address: ADDRESS) -> str:
        """Take a breakpoint off.  It says which one, in both addresses."""
        return " ".join(self.commands.break_address([str(address)], True))

    def break_trap(self, name: Annotated[str, Field(
        description="the trap's name (WaitNextEvent) or its word ($A860)")]) -> str:
        """Break before a Mac OS Toolbox trap is answered -- the way to see who
        calls HLock, or what the game asks the Resource Manager for."""
        return " ".join(self.commands.break_trap([name], False))

    def clear_break_trap(self, name: Annotated[str, Field(
        description="the trap's name or its word")]) -> str:
        """Take a trap breakpoint off."""
        return " ".join(self.commands.break_trap([name], True))

    def break_memory(self, address: ADDRESS,
                     mode: ACCESS = "write") -> str:
        """Break at the instruction that reads or writes an address: the classic
        way to find who changes a variable.  Memory breakpoints stop every access
        to that address, so take it off when you've found the culprit."""
        return " ".join(self.commands.break_memory(_mode(mode), [str(address)], False))

    def clear_break_memory(self, address: ADDRESS,
                           mode: ACCESS = "write") -> str:
        """Take a memory breakpoint off."""
        return " ".join(self.commands.break_memory(_mode(mode), [str(address)], True))

    def list_breakpoints(self) -> list[str]:
        """Every breakpoint set, as the debugger lists them: b, bt, bmr, bmw, one
        a line.  Empty means nothing is armed."""
        debug = self.commands.debug()
        return [] if debug is None else debug.bp_list()

    # -- memory ---------------------------------------------------------------------
    def read_memory(self, address: ADDRESS,
                    lines: Annotated[int, Field(
                        ge=1, le=64,
                        description="lines of 16 bytes (default 8)")] = 8) -> dict[str, Any]:
        """Memory as hex with its text alongside, sixteen bytes a line, plus the
        bytes and the ASCII separately.  Walking memory means asking again from
        `next_address`, which is the byte after the last one read here."""
        place = self.commands.place(str(address))
        count = int(lines) * 16
        text, next_at = self.commands.dump(place, int(lines))
        data = self.commands.read(place, count)
        return {"text": "\n".join(text), "next_address": f"0x{next_at:08X}",
                "bytes": data["bytes"], "ascii": data["ascii"],
                "truncated": data["truncated"]}

    def write_memory(self, address: ADDRESS,
                     value: Annotated[str, Field(
                         description="hex: 4A, 1234, DEADBEEF ($ or 0x optional). "
                                     "Its digits say its size")]) -> str:
        """Write memory.  How the value is written says how big it is -- two hex
        digits a byte, four a word, eight a long -- so `4A` writes one byte rather
        than scribbling over three nobody meant.  Says what was there before, so a
        write to the wrong place can be undone from the answer."""
        from .dbg import parse_value
        place = self.commands.place(str(address))
        number, size, name = parse_value(str(value))
        return " ".join(self.commands.write(place, number, size, name))

    def resolve_address(
            self, address: Annotated[str, Field(
                description="a live address, ram:<hex>, or a symbol's name")]) -> dict[str, Any]:
        """Both addresses for one byte -- Ghidra's and the machine's, which are
        different numbers for everything but low memory -- or a routine's name
        resolved to both."""
        text = str(address).strip()
        if _is_name(text):
            return self.find_symbol(text)
        place = self.commands.place(text)
        return {"live": f"0x{place.live:08X}",
                "ghidra": None if place.jank is None else f"ram:{place.jank:08X}",
                "shown": place.shown()}

    def dump_ram(self, path: Annotated[
            str, Field(description="the file to write")]) -> dict[str, Any]:
        """Write all of the Mac's RAM to a file -- the game's loaded code and all
        -- for looking at outside the emulator.  Several megabytes."""
        proc = self.gui(lambda: self.display.front_process)
        if proc is None:
            raise LookupError("no application is running")
        return {"path": path, "bytes": self.gui(proc.dump_memory, path)}

    # -- symbols, and calling into the game --------------------------------------------
    def find_symbol(self, name: Annotated[
            str, Field(description="the routine's name")]) -> dict[str, Any]:
        """A routine by name: its address in Ghidra, where it is in memory right
        now, and its prototype if one is known.  Needs a symbol table
        (--symbols, or --ghidra-project to read one), which is read once, on
        first use -- the first call may wait on Ghidra."""
        mac = self.controller()
        sym = mac.find(name)
        live = mac._patcher().app_map.resolve(sym["entry"])
        out = {"name": sym.get("name"), "ghidra": f"ram:{sym['entry']:08X}",
               "live": None if live is None else f"0x{live:08X}"}
        if sym.get("prototype"):
            out["prototype"] = sym["prototype"]
        return out

    def call_function(
            self,
            target: Annotated[str, Field(description="a symbol name, or ram:<hex>")],
            args: Annotated[list, Field(
                description="the arguments, as Python values, pushed per its "
                            "signature")] = None,
            result: Annotated[str, Field(
                description="how to read the answer: a Ghidra format, a register "
                            "like d3, or empty for raw D0")] = None
    ) -> dict[str, Any]:
        """Call one of the game's own routines and get its answer back.  Needs the
        game stopped or waiting for an event, not running its own code; use pause
        first if it's running."""
        value = self.controller().call(target, args or [], result=result)
        return {"target": target,
                "result": value if _jsonable(value) else str(value)}

    def regs(self) -> dict[str, Any]:
        """The registers as they are right now: d0-d7, a0-a6, sp, pc, sr.  When
        the debugger has the program stopped, these are the stopped machine's.
        Otherwise the program has to be waiting rather than running its own
        code -- pause first, or use `status`, which reads a real stop's."""
        stopped = self.commands.status()
        if stopped.get("paused"):
            # The debugger holds the core, so the script layer's own read of
            # it would refuse: the snapshot the stop took is the same
            # machine, and reading it is what the stop was for.
            view = self.commands.debug().get_view()
            if view is not None:
                out = {f"d{n}": v for n, v in enumerate(view["d"])}
                out.update({f"a{n}": v for n, v in enumerate(view["a"])})
                out["a7"] = out["sp"] = view["sp"]
                out["pc"], out["sr"] = view["pc"], view["sr"]
                return {k: f"0x{v:08X}" for k, v in out.items()}
        return {k: f"0x{v:08X}" for k, v in self.controller().regs().items()}

    # -- looking and driving -------------------------------------------------------------
    def screenshot(self, path: Annotated[str, Field(
        description="a file to save the PNG to as well")] = None) -> list:
        """What's on the emulated screen, as a PNG.  The picture comes back
        whatever else; a path saves it to a file too."""
        png = self.gui(_grab_png, self.display)
        out = {"png": len(png)}
        if path:
            with open(path, "wb") as f:
                f.write(png)
            out["path"] = path
        from mcp.server.mcpserver import Image
        return [Image(data=png, format="png"), out]

    def press(self, key: Annotated[str, Field(
        description="a, return, space, f5, down, cmd-q, shift-tab")],
        hold: HOLD = 0.02) -> str:
        """Press and release a key.  Named combinations carry their modifiers:
        cmd-q, shift-tab, ctrl-c."""
        self.controller().press(key, hold)
        return f"pressed {key}"

    def key_down(self, key: Annotated[str, Field(description="the key")]) -> str:
        """Hold a key down, and leave it down.  Key up releases it; a key left
        down looks like a stuck key to the application."""
        self.controller().key_down(key)
        return f"held {key} down"

    def key_up(self, key: Annotated[str, Field(description="the key")]) -> str:
        """Release a key that key_down held."""
        self.controller().key_up(key)
        return f"released {key}"

    def type_text(self, text: Annotated[str, Field(description="what to type")],
                  hold: Annotated[float, Field(gt=0, description="seconds per key")] = 0.02) -> str:
        """Type a string, one key at a time.  Click the field you want it in
        first -- keys go to whatever the application thinks has focus."""
        self.controller().type_text(text, hold)
        return f"typed {len(text)} characters"

    def click(self, h: Annotated[int, Field(description="vertical (row) coordinate")],
              v: Annotated[int, Field(description="horizontal (column) coordinate")],
              hold: HOLD = 0.02) -> str:
        """Click the mouse at Mac screen coordinates.  0,0 is the top left of the
        desktop, just under the menu bar; `h` is the row and `v` the column, as
        QuickDraw has it."""
        self.controller().click(int(h), int(v), hold)
        return f"clicked at {h},{v}"

    def mouse_down(self, h: Annotated[int, Field(description="row")],
                   v: Annotated[int, Field(description="column")]) -> str:
        """Press the mouse button at coordinates and hold it: dragging is
        mouse_down, a few mouse_moves, then mouse_up."""
        self.controller().mouse_down(int(h), int(v))
        return f"mouse button down at {h},{v}"

    def mouse_up(self, h: Annotated[int, Field(description="row")],
                 v: Annotated[int, Field(description="column")]) -> str:
        """Release the mouse button at coordinates."""
        self.controller().mouse_up(int(h), int(v))
        return f"mouse button released at {h},{v}"

    def mouse_move(self, h: Annotated[int, Field(description="row")],
                   v: Annotated[int, Field(description="column")]) -> str:
        """Move the mouse to coordinates, button up."""
        self.controller().mouse_move(int(h), int(v))
        return f"mouse moved to {h},{v}"

    # -- what's running ---------------------------------------------------------------
    def processes(self) -> list[str]:
        """The applications running under the emulator."""
        return self.controller().processes()

    def front(self) -> str:
        """Which application is frontmost -- the one keys and clicks go to."""
        return self.controller().front() or "(none)"

    def attach_process(self, name: Annotated[
        str, Field(description="the application's name")]) -> str:
        """Send input to the named application whether or not it's frontmost."""
        self.controller().attach(name)
        return f"targeting {name}"

    def windows(self) -> list[dict[str, str]]:
        """The visible windows of the application being targeted: app and title
        for each.  Empty means the game hasn't put one up yet."""
        return [{"app": app, "title": title} for app, title in self.controller().windows()]

    def wait_window(self,
                    title: Annotated[str, Field(
                        description="the title to wait for; empty for any window")] = None,
                    timeout: Annotated[float, Field(
                        gt=0, description="seconds (default 30)")] = 30.0) -> str:
        """Wait for the application to put up a window, with a given title if
        one is named.  A game's boot takes tens of seconds; this is how to know
        it's there rather than guessing with a sleep."""
        # An empty title means "any window", which is what someone means by
        # leaving it out; passed through, the Controller would wait for a window
        # actually named "" and time out.
        self.controller().wait_window(timeout, title or None)
        return "a window is up"

    def sleep(self, seconds: Annotated[float, Field(ge=0, description="how long")] = 0.0) -> str:
        """Wait a while.  The emulator keeps running meanwhile, which is the
        point: give the game time to finish what it started."""
        self.controller().sleep(float(seconds))
        return f"slept {seconds}s"

    # -- tracing ------------------------------------------------------------------------
    def trace_functions(self,
                        match: Annotated[str, Field(
                            description="comma-separated names to keep the log to")] = "") -> str:
        """Trace the game's own functions -- name, arguments, return value -- to
        the emulator's log.  The hooks go on as the game runs, so it takes a
        moment; trace_ready says when they're all on."""
        names = tuple(s.strip() for s in match.split(",") if s.strip())
        return f"tracing {self.controller().trace_functions(names)} functions"

    def trace_ready(self) -> bool:
        """Whether every matched function that's in memory has its hook on.  A
        hook on a segment that isn't loaded yet stays pending."""
        return self.controller().trace_ready()

    def trace_stop(self) -> str:
        """Stop tracing functions and take the hooks off."""
        self.controller().trace_stop()
        return "tracing stopped"

    def quit(self, confirm: Annotated[str, Field(description="yes")]) -> str:
        """Shut the emulator down.  Needs confirm='yes', because it isn't
        something to do by accident mid-session, and it takes the debugger with
        it."""
        if str(confirm).lower() not in ("yes", "y", "true"):
            raise ValueError("quit needs confirm='yes'")
        try:
            self.controller().quit()
        except Exception:                                            # noqa: BLE001
            # Shutdown closes the bridge before the things under it go away, so
            # a tool call landing mid-shutdown gets ProcessKilled out of it.
            # That *is* the answer here: it asked for the shutdown, and the
            # shutdown is happening.
            pass
        return "the emulator is shutting down"


# ---------------------------------------------------------------------------
# Registering them
# ---------------------------------------------------------------------------
def reported(fn):
    """A tool that answers when it can't do the thing.

    The SDK turns an exception escaping a tool into `Error executing tool X` and
    keeps the real message on the server, which is right for a crash and wrong
    for this: `ram:0012ABCD isn't loaded right now`, `no trap called Nonesuch`
    and `the game is running its own code` are the answer the client came for.
    So each failure is handed back as an error result carrying its own words.

    Every request, and its answer, is also written into the debugger window's
    log when one is open: the client's side of the same conversation a typed
    command logs, so what came over the socket can be read where the machine
    stopped.  Posted to the GUI thread -- the window is a widget, and this
    thread is not it -- and dropped rather than raised if there's no window,
    or it's already gone."""
    @functools.wraps(fn)           # keeps the name, the docstring, the annotations
    def run(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception as e:                                   # noqa: BLE001
            log.info("%s: %s", fn.__name__, e)
            result = _error(f"{type(e).__name__}: {e}")
        # fn is the bound method the tool was registered as, so its Tools is
        # what the display is, and the window to log into.  A bound method
        # carries its owner as __self__, not self.
        _to_the_debugger(getattr(fn, "__self__", None), _task_of(fn, args, kwargs), result)
        return result
    return run


def _task_of(fn, args, kwargs) -> str:
    """The request as it was made: the tool's arguments, in the order the
    schema lists them.  A tool's signature is its interface, so its parameters
    say what was asked for; the SDK hands over whatever the client sent, and
    what it didn't send is the parameter's own default."""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn.__name__
    # p.kind's name, not the enum: 3.14 took the public ParameterKind alias
    # away, and the names are the same in every version.
    name = {p: v for p, v in kwargs.items() if p in params}
    for p in params.values():
        if p.name in name or p.kind.name == "VAR_POSITIONAL":
            continue
        if p.kind.name == "VAR_KEYWORD":
            name.update(kwargs)
            continue
        if args:
            name[p.name] = args[0]
            args = args[1:]
        elif p.default is not inspect.Parameter.empty:
            name[p.name] = p.default
    return " ".join(f"{n}={v!r}" for n, v in name.items())


def _to_the_debugger(tools, task: str, result) -> None:
    """The request and its answer into the debugger window's log, or nothing
    if there's no window.  `display.debugger` is set the moment the window is
    made and cleared by Qt when it goes away, so a read of it here is the
    question of whether there is one.  The window is a widget, and this thread
    is not the GUI's, so the line is posted rather than written; a window
    already gone is dropped rather than raised -- a log line is no reason to
    fail the answer it's about."""
    display = tools.display
    window = getattr(display, "debugger", None)
    if window is None:
        return
    bridge = getattr(display, "bridge", None)
    try:
        if bridge is None:
            window.mcp_log(task, _answer(result))
        else:
            bridge.post(window.mcp_log, task, _answer(result))
    except Exception:                                          # noqa: BLE001
        pass


def _answer(result) -> str:
    """What the tool said, as a line: its words, joined.  An error result
    carries them in its content, which is where they are read from; a plain
    value is itself the answer."""
    from mcp.types import CallToolResult
    if isinstance(result, CallToolResult):
        parts = []
        for c in result.content:
            if getattr(c, "type", None) == "text" and c.text:
                parts.append(c.text)
        return "\n".join(parts) if parts else "(no answer)"
    return str(result)


def _error(text: str):
    from mcp.types import CallToolResult, TextContent
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def build(display, controller=None):
    """The SDK's server, with every tool on it.

    The tools are registered as bound methods wrapped in `reported`; the
    wrapper is functools.wraps'd, so the name, docstring and parameter
    annotations the schema is generated from come straight through."""
    from mcp.server.mcpserver import MCPServer

    tools = Tools(display, controller)
    # MCPServer's constructor calls logging.basicConfig for the whole process.
    # This program has already set its own one log (macrt.ansi, on the "macrt"
    # logger, colourised, plus the run's file), so a handler landing on the root
    # would print every line the emulator logs twice.  It's taken back off, and
    # the SDK is given the emulator's own handlers instead -- its format, and
    # into the run's log file with everything else.
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    quiet = _quiet()
    try:
        server = MCPServer(name=SERVER_NAME, version=SERVER_VERSION,
                           instructions=INSTRUCTIONS, log_level=quiet)
    finally:
        root.handlers[:] = before
        root.setLevel(level)
    _voice_for_the_sdk(quiet)
    for name, method in _tools(tools):
        server.add_tool(reported(method), name=name)
    return server, tools


def _voice_for_the_sdk(quiet: str) -> None:
    """Where the SDK's own complaints go, and how loud.

    Under its own name -- `mcp.server.transport_security` and friends -- so
    they're identifiable as the socket's rather than the emulator's, on the
    emulator's handlers so they reach the screen and the run's log in the one
    format, and not propagated, so each is said once however the root logger
    happens to be set up.

    Loud enough for the socket's problems to be seen, quiet enough not to talk
    over the game: the SDK logs a crash per refused request, so an Origin
    someone keeps getting wrong would fill the log."""
    sdk = logging.getLogger("mcp")
    sdk.setLevel(logging.DEBUG if quiet == "DEBUG" else logging.WARNING)
    voices = logging.getLogger("macrt").handlers
    if not voices:
        voices = [logging.StreamHandler()]       # nothing set the emulator's log up
    sdk.handlers[:] = list(voices)
    sdk.propagate = False
    sdk.propagate = False


def _quiet() -> str:
    """What to tell the SDK and uvicorn to log: warnings, unless the run is
    tracing.

    The emulator has one voice for its log (macrt.ansi) and "Application
    startup complete" isn't something it should be saying between the game's
    lines.  Under --trace, what the socket is doing is exactly what someone
    wants to see, so the level follows the run's then.

    The handler's level, not the logger's: macrt's logger is deliberately left
    at DEBUG so its log file takes everything, and asking it would make every
    run a traced one."""
    console = logging.getLogger("macrt")
    levels = [h.level for h in console.handlers if h.level != logging.NOTSET]
    return "DEBUG" if levels and min(levels) <= logging.DEBUG else "WARNING"


def _tools(tools) -> list[tuple[str, callable]]:
    """The tool methods, in the order a reader would want them listed: run
    control, memory, symbols, driving, tracing.  Named explicitly -- a tool is
    an interface with someone else's config written against it, so which ones
    there are should be a decision, not whatever methods happen to exist."""
    names = ["status", "pause", "step", "cont", "command",
             "breakpoint", "clear_breakpoint", "break_trap", "clear_break_trap",
             "break_memory", "clear_break_memory", "list_breakpoints",
             "read_memory", "write_memory", "resolve_address", "dump_ram",
             "find_symbol", "call_function", "regs",
             "screenshot", "press", "key_down", "key_up", "type_text",
             "click", "mouse_down", "mouse_up", "mouse_move",
             "processes", "front", "attach_process", "windows", "wait_window",
             "sleep", "trace_functions", "trace_ready", "trace_stop", "quit"]
    return [(name, getattr(tools, name)) for name in names]


# ---------------------------------------------------------------------------
# The server, on its own thread
# ---------------------------------------------------------------------------
class McpServer:
    """The HTTP server, on a thread with an event loop of its own.

    The emulator's GUI thread runs Qt's loop and the guest threads run 68k
    code, so there's no loop here to hand; uvicorn needs one and gets its own.
    Every tool then runs on a worker thread, asks the GUI thread for whatever
    it needs, and the two loops never meet -- which is the same arrangement a
    --script runs under.

    The thread is a daemon, so a client that never disconnects can't keep the
    process alive after the Mac has been closed; stop() is the tidy way."""

    def __init__(self, display, controller=None, host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT):
        self.display = display
        self.host, self.port = host, port
        self.server, self.tools = build(display, controller)
        self._uvicorn = None
        self._thread: threading.Thread | None = None
        self.started = False

    def start(self) -> str:
        """Bind and listen; returns the URL to point a client at, or "" if the
        port was taken.

        Binding is the moment that says whether the port was free, so a second
        emulator doesn't silently take another one from the one in the client's
        config: it says so and comes back without a server, and demo.main turns
        that into a failed start rather than a session that looks connected and
        answers nothing."""
        if self.started:
            return self.url
        try:
            # Stateful, not stateless: a session id on the first answer is
            # what says a client is connected, and the DELETE it sends on
            # close is what says it's gone -- with no session there is no
            # connect to see and no disconnect to see, only a run of
            # indistinguishable POSTs.  Each request still gets its one
            # JSON body back; the client carries the id it was given.
            app = self.server.streamable_http_app(
                streamable_http_path=PATH, json_response=True,
                host=self.host, transport_security=_security(self.host))
        except ImportError as e:                       # the SDK, or uvicorn, missing
            log.error("the MCP server needs the mcp package: %s", e)
            log.error("install it with:  pip install mcp")
            return ""
        import uvicorn
        # The client's traffic, into the debugger window's log: a connect is
        # the first request that carries no session id yet, and a disconnect
        # the DELETE that takes the session down -- the window's log is where
        # what came over the socket is read, and this is what came in.
        app = _watched(app, self._say)
        # uvicorn's noise is the server's own setting, as it is when the SDK
        # runs itself.  WARNING by default (in build()): the emulator has one log
        # and nobody wants "Application startup complete" between the game's
        # lines -- but --trace-level or a raised level reaches it too.
        self._uvicorn = uvicorn.Server(uvicorn.Config(
            app, host=self.host, port=self.port,
            log_level=self.server.settings.log_level.lower(),
            lifespan="on", access_log=False))
        self._thread = threading.Thread(target=self._serve, name="classetta:mcp",
                                        daemon=True)
        self._thread.start()
        if not self._wait_until_listening():
            log.error("the MCP server couldn't listen on %s:%d -- is another "
                      "emulator using that port?", self.host, self.port)
            self._uvicorn = None
            return ""
        self.port = self._listening_port()
        self.started = True
        # The first line of the conversation, into the debugger window's log
        # when there is one: what is listening, and where -- so the window
        # says why a client will be able to reach it, before any of them does.
        self._say(f"mcp: listening on {self.url}")
        # Said as the thing to paste, because that's what happens next: the
        # client's config wants this exact stanza, and the port is the one that
        # was actually bound rather than the one asked for (which matters when
        # 0 was asked for).
        log.info("MCP server on %s", self.url)
        log.info('add to the MCP client config:  {"classetta": {"type": "http", '
                 '"url": "%s"}}', self.url)
        return self.url

    def _serve(self) -> None:
        try:
            self._uvicorn.run()
        except (Exception, SystemExit) as e:                        # noqa: BLE001
            # A port taken, a bind refused.  uvicorn logs it and then calls
            # sys.exit, which isn't an Exception -- and a SystemExit escaping a
            # thread is the kind of alarming traceback nobody needs to see for a
            # thing start() has already reported.
            log.debug("the MCP server stopped: %s", e)

    def _wait_until_listening(self, seconds: float = 5.0) -> bool:
        """Until uvicorn says it's serving, or dies trying.  Without this a
        taken port would look fine for a moment and then the log would say the
        URL works when nothing is behind it."""
        import time
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._uvicorn.started:
                return True
            if not self._thread.is_alive():
                return False
            time.sleep(0.01)
        return self._uvicorn.started

    def _listening_port(self) -> int:
        """The port it actually got, which is what was asked for unless 0 was."""
        sockets = getattr(self._uvicorn, "servers", None) or []
        for sock in sockets:
            for s in getattr(sock, "sockets", []) or []:
                return s.getsockname()[1]
        return self.port

    def _say(self, line: str) -> None:
        """A line into the debugger window's log, or nowhere if there's no
        window.  The server's thread isn't the GUI's, so it's posted; a
        window already gone is dropped rather than raised -- a log line is
        no reason to fail the traffic it's about."""
        window = getattr(self.display, "debugger", None)
        if window is None:
            return
        bridge = getattr(self.display, "bridge", None)
        try:
            if bridge is None:
                window.mcp_log(line, "")
            else:
                bridge.post(window.mcp_log, line, "")
        except Exception:                                          # noqa: BLE001
            pass

    def stop(self) -> None:
        """Stop listening, and wait briefly for the thread to finish.  A tool
        still running is left to answer or be abandoned; the emulator is going
        away, and its threads with it."""
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._thread is not None:
            self._thread.join(2.0)
        self._uvicorn, self._thread = None, None
        self.started = False

    @property
    def url(self) -> str:
        """Where to point a client, or "" if it isn't listening.  Empty rather
        than the address it tried: a caller that shows or logs this would
        otherwise show a URL with nothing behind it."""
        return "" if not self.started else f"http://{self.host}:{self.port}{PATH}"

    def tool_names(self) -> list[str]:
        """What's on offer, for the log and for a test to check against."""
        return [name for name, _ in _tools(self.tools)]


def _security(host: str):
    """Who may talk to this server.

    The socket is bound to localhost, but a web page can reach localhost too --
    DNS rebinding turns a site the user visited into 127.0.0.1, and then that
    page is driving a Mac emulator, clicking and typing.  So the Host and Origin
    headers are checked, by the SDK's own middleware, against the names a
    localhost page can honestly have.  Anything else is refused: 421 for a Host
    that isn't this machine, 403 for an Origin that isn't."""
    from mcp.server.transport_security import TransportSecuritySettings
    localhost = f"{host}:*"
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[localhost, "localhost:*", "[::1]:*"],
        allowed_origins=[f"http://{localhost}", "http://localhost:*",
                         "http://[::1]:*", "null", "app://obsidian.md"])


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _watched(app, say) -> Any:
    """The SDK's app, with the client's traffic said into the debugger log.

    A client connects with its first request, which carries no session id
    yet, and disconnects with the DELETE that takes its session down; the
    server is stateful for no more than that -- the id is what makes the two
    moments visible, and every other request is traffic of a client already
    said, and says nothing.  The body is read on the way through and handed
    back in a new message, so the SDK sees it once and only once.  A request
    that dies on its way in -- a 421 for a web page that resolved localhost
    to this port -- is still said: it reached the door, and the log is where
    the door is watched."""
    async def watched(scope, receive, send):
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return
        method = scope.get("method", "")
        body = b""
        if method == "POST":
            while True:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    break
                body += message.get("body", b"")
                if not message.get("more_body", False):
                    break
        session = _session_header(scope)
        if method == "POST" and not session:
            say(f"mcp: client connected ({_client_of(scope)})")
        elif method == "DELETE" and session:
            say(f"mcp: client disconnected ({_client_of(scope)})")
        # The body, handed back as one message; whatever comes after it --
        # the SDK asks receive() again on its stream paths -- goes to the
        # original, so a disconnect still arrives as a disconnect.
        sent = False

        async def receive_back():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()
        await app(scope, receive_back, send)
    return watched


def _session_header(scope) -> str:
    """The session id the request carries, or "" for none: the first request
    of a connection has no id yet, and that's what says a client has arrived."""
    for key, value in scope.get("headers", []):
        if key == b"mcp-session-id":
            return value.decode("latin-1").strip()
    return ""


def _client_of(scope) -> str:
    """Who it was, as far as a localhost socket says: the client's address."""
    client = scope.get("client")
    return f"{client[0]}:{client[1]}" if client else "unknown"


def _grab_png(display) -> bytes:
    """The emulated screen as PNG bytes.  On the GUI thread, via gui(): it's a
    widget's pixels being asked for.

    Under --host screen the whole Mac screen is one widget and that's what
    comes back.  In the default desktop mode each Mac window is its own host
    window, so there's no single screen to grab -- and the front application's
    front window is the thing someone asking for a screenshot means."""
    from PyQt6.QtCore import QBuffer, QIODevice
    widget = getattr(display, "screen_widget", None)
    if widget is None:
        widget = _front_view(display)
        if widget is None:
            raise RuntimeError("nothing is on screen to capture, and a whole-"
                               "screen shot needs --host screen")
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    widget.grab().save(buffer, "PNG")
    return bytes(buffer.data())


def _front_view(display):
    """The frontmost application's frontmost window's widget, or None.  The
    display's z_order is front to back and yields visible windows only."""
    for proc, rec in getattr(display, "z_order", lambda: [])():
        if proc is not display.front_process:
            continue
        view = display.views.get(rec)
        if view is not None and view.isVisible():
            return view
    return None


def _mode(mode: str) -> str:
    """read or write, as the letter the debugger's own sets use."""
    text = str(mode or "write").strip().lower()
    if text.startswith("r"):
        return "r"
    if text.startswith("w"):
        return "w"
    raise ValueError(f"{mode} is neither read nor write")


def _is_name(text: str) -> bool:
    """Whether what was written is a symbol's name rather than an address: no
    ram:, no $, no 0x, and not hex digits.  (A name is how Ghidra is read, so
    resolve_address("DrawGame") should work as readily as the number.)"""
    bare = text.strip()
    if not bare or bare.lower().startswith("ram:"):
        return False
    if bare.startswith("$") or bare.lower().startswith("0x"):
        return False
    try:
        int(bare, 16)
    except ValueError:
        return bare[0].isalpha()
    return False        # all hex digits: an address, which is the usual case


def _jsonable(value) -> bool:
    import numbers
    return value is None or isinstance(value, (str, bool, int, numbers.Real))


def serve(display, controller=None, host: str = DEFAULT_HOST,
          port: int = DEFAULT_PORT) -> McpServer:
    """Start the server on the running display and say where it is."""
    server = McpServer(display, controller, host=host, port=port)
    server.start()
    display.mcp_server = server
    return server
