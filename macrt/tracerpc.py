"""
macrt.tracerpc - Classetta as a back end for the Ghidra debugger, over Trace RMI.

Ghidra's debugger splits the *trace* (a database of machine state) from the
*back end* (whatever produced it).  Trace RMI is the channel between the two:
Ghidra runs a small server (a plain socket speaking length-prefixed protobuf),
and a back end dials out to it, pushes the machine's state into the trace as it
changes, and answers the debugger's run-control and breakpoint calls.

This module is the back end for a running Classetta.  It is a third thread,
exactly like the MCP server: the guest thread owns the 68k CPU and the GUI
thread owns the screen, and this one only ever *asks* -- `macrt.dbg` for the
core, the running application's `AppMap` for where its code is.  It never
touches a register or a byte while the core is inside the CPU.

The addresses.  The trace is written in Ghidra's own counting -- the jank
image it analysed, the `ram:` space.  The core runs relocated, in segments,
under a different A5, so every byte, every program counter, every breakpoint
is translated through the running application's `AppMap` on the way in and
out.  The debugger's listing is therefore the *analysed* one: single-step
through named functions with their annotations, and a breakpoint placed on a
routine in the static listing lands on its live, relocated copy.

The state.  There is no push from the core -- a stop is a stop, and between
stops the machine is moving.  So a poll thread watches for the core coming to
rest (a pause, a step, or a breakpoint) and, on each new stop, records one
transaction: a snapshot, the registers, the program counter, and the code
around it.  While the core runs free the trace says it's running.

The methods the debugger calls back are defined with the same signatures as
Ghidra's own lldb agent -- the back end that ships with this release of
Ghidra -- and are registered as closures, not methods: the registry reads a
method's parameter schemas from its live annotations, so a `self` parameter
would be seen as a bogus one, and the annotation must be the real type object
rather than a string.
"""

import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from ghidratrace.client import (Address, AddressRange, Client, MethodRegistry,
                                RegVal, TraceObject)

from .dbg import Commands
from .m68k.cpu import Debug
from .symtab import JT_OFFSET

log = logging.getLogger(__name__)

# Ghidra's language for the 68k, as the debugger names it.  The Mac variant is
# the one the jank image was analysed with.
LANGUAGE = "68000:BE:32:mac"
COMPILER = "default"

# Where Ghidra's Trace RMI server listens, when --trace-rmi names none.  The
# debugger's server binds a fixed port per project; 10000 is the lldb agent's
# default, and a local Ghidra on this machine is the usual target.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 10000

# One guest, one thread, one frame: the whole machine is a single 68k core.
PID = 1
TID = 0
LEVEL = 0

PATH_PROCESS = f"Processes[{PID}]"
PATH_THREADS = f"Processes[{PID}].Threads"
PATH_THREAD = f"Processes[{PID}].Threads[{TID}]"
PATH_STACK = f"Processes[{PID}].Threads[{TID}].Stack"
PATH_FRAME = f"Processes[{PID}].Threads[{TID}].Stack[{LEVEL}]"
PATH_REGS = f"Processes[{PID}].Threads[{TID}].Stack[{LEVEL}].Registers"
PATH_MEMORY = f"Processes[{PID}].Memory"
PATH_BREAKS = f"Processes[{PID}].Breakpoints"
PATH_WATCHES = f"Processes[{PID}].Watchpoints"

# The object tree, as Ghidra's Trace RMI wants it.  This is the lldb agent's
# schema, renamed at the root: it is known to satisfy the Java side's
# interface matching, so it is reused rather than reinvented.
SCHEMA_XML = """<context>
	<schema name="MacSession" elementResync="NEVER" attributeResync="NEVER">
		<interface name="EventScope" />
		<interface name="FocusScope" />
		<interface name="Aggregate" />
		<element schema="VOID" />
		<attribute name="Processes" schema="ProcessContainer" required="yes" fixed="yes" />
		<attribute name="Available" schema="AvailableContainer" required="yes" fixed="yes" />
		<attribute name="_event_thread" schema="OBJECT" hidden="yes" />
		<attribute name="_focus" schema="Selectable" required="yes" hidden="yes" />
		<attribute name="_display" schema="STRING" hidden="yes" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute schema="VOID" />
	</schema>
	<schema name="Selectable" elementResync="NEVER" attributeResync="NEVER">
		<element schema="OBJECT" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute schema="VOID" />
	</schema>
	<schema name="BreakpointContainer" canonical="yes" elementResync="NEVER" attributeResync="NEVER">
		<element schema="BreakpointSpec" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute schema="VOID" />
	</schema>
	<schema name="WatchpointContainer" canonical="yes" elementResync="NEVER" attributeResync="NEVER">
		<element schema="WatchpointSpec" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute schema="VOID" />
	</schema>
	<schema name="AvailableContainer" canonical="yes" elementResync="ALWAYS" attributeResync="NEVER">
		<element schema="Attachable" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute schema="VOID" />
	</schema>
	<schema name="ProcessContainer" canonical="yes" elementResync="NEVER" attributeResync="NEVER">
		<element schema="Process" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute schema="VOID" />
	</schema>
	<schema name="BreakpointSpec" canonical="yes" elementResync="NEVER" attributeResync="NEVER">
		<interface name="BreakpointSpec" />
		<interface name="Togglable" />
		<element schema="BreakpointLocation" />
		<attribute name="Expression" schema="STRING" required="yes" hidden="yes" />
		<attribute-alias from="_expression" to="Expression" />
		<attribute name="Kinds" schema="STRING" required="yes" hidden="yes" />
		<attribute-alias from="_kinds" to="Kinds" />
		<attribute name="_display" schema="STRING" hidden="yes" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute name="Enabled" schema="BOOL" required="yes" />
		<attribute-alias from="_enabled" to="Enabled" />
		<attribute name="Commands" schema="STRING" />
		<attribute name="Condition" schema="STRING" />
		<attribute name="Hit Count" schema="INT" />
		<attribute name="Ignore Count" schema="INT" />
		<attribute name="Pending" schema="BOOL" />
		<attribute name="Silent" schema="BOOL" />
		<attribute name="Temporary" schema="BOOL" />
		<attribute schema="VOID" />
	</schema>
	<schema name="WatchpointSpec" canonical="yes" elementResync="NEVER" attributeResync="NEVER">
		<interface name="BreakpointSpec" />
		<interface name="BreakpointLocation" />
		<interface name="Togglable" />
		<attribute name="Expression" schema="STRING" required="yes" hidden="yes" />
		<attribute-alias from="_expression" to="Expression" />
		<attribute name="Kinds" schema="STRING" required="yes" hidden="yes" />
		<attribute-alias from="_kinds" to="Kinds" />
		<attribute name="_display" schema="STRING" hidden="yes" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute name="Enabled" schema="BOOL" required="yes" />
		<attribute-alias from="_enabled" to="Enabled" />
		<attribute name="Range" schema="RANGE" />
		<attribute-alias from="_range" to="Range" />
		<attribute name="Condition" schema="STRING" />
		<attribute name="Hit Count" schema="INT" />
		<attribute name="Ignore Count" schema="INT" />
		<attribute schema="VOID" />
	</schema>
	<schema name="Attachable" elementResync="NEVER" attributeResync="NEVER">
		<element schema="VOID" />
		<attribute name="PID" schema="LONG" />
		<attribute-alias from="_pid" to="PID" />
		<attribute name="_display" schema="STRING" hidden="yes" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute schema="VOID" />
	</schema>
	<schema name="Process" elementResync="NEVER" attributeResync="NEVER">
		<interface name="Activatable" />
		<interface name="Process" />
		<interface name="Aggregate" />
		<interface name="ExecutionStateful" />
		<element schema="VOID" />
		<attribute name="Threads" schema="ThreadContainer" required="yes" fixed="yes" />
		<attribute name="Breakpoints" schema="BreakpointContainer" required="yes" fixed="yes" />
		<attribute name="Watchpoints" schema="WatchpointContainer" required="yes" fixed="yes" />
		<attribute name="Exit Code" schema="LONG" />
		<attribute-alias from="_exit_code" to="Exit Code" />
		<attribute name="Memory" schema="Memory" required="yes" fixed="yes" />
		<attribute name="PID" schema="LONG" hidden="yes" />
		<attribute-alias from="_pid" to="PID" />
		<attribute name="State" schema="EXECUTION_STATE" required="yes" hidden="yes" />
		<attribute-alias from="_state" to="State" />
		<attribute name="_display" schema="STRING" hidden="yes" />
		<attribute name="_short_display" schema="STRING" hidden="yes" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute schema="VOID" />
	</schema>
	<schema name="Memory" canonical="yes" elementResync="NEVER" attributeResync="NEVER">
		<interface name="Memory" />
		<element schema="MemoryRegion" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute schema="VOID" />
	</schema>
	<schema name="BreakpointLocation" elementResync="NEVER" attributeResync="NEVER">
		<interface name="BreakpointLocation" />
		<element schema="VOID" />
		<attribute name="Range" schema="RANGE" />
		<attribute-alias from="_range" to="Range" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute name="Enabled" schema="BOOL" required="yes" />
		<attribute-alias from="_enabled" to="Enabled" />
		<attribute schema="VOID" />
	</schema>
	<schema name="ThreadContainer" canonical="yes" elementResync="NEVER" attributeResync="NEVER">
		<element schema="Thread" />
		<attribute name="_order" schema="INT" hidden="yes" />
	</schema>
	<schema name="Thread" elementResync="NEVER" attributeResync="NEVER">
		<interface name="Activatable" />
		<interface name="Thread" />
		<interface name="ExecutionStateful" />
		<interface name="Aggregate" />
		<element schema="VOID" />
		<attribute name="Stack" schema="Stack" required="yes" fixed="yes" />
		<attribute name="TID" schema="LONG" />
		<attribute-alias from="_tid" to="TID" />
		<attribute name="Name" schema="STRING" />
		<attribute name="State" schema="EXECUTION_STATE" required="yes" hidden="yes" />
		<attribute-alias from="_state" to="State" />
		<attribute name="_display" schema="STRING" hidden="yes" />
		<attribute name="_short_display" schema="STRING" hidden="yes" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute schema="VOID" />
	</schema>
	<schema name="MemoryRegion" elementResync="NEVER" attributeResync="NEVER">
		<interface name="MemoryRegion" />
		<element schema="VOID" />
		<attribute name="Offset" schema="STRING" fixed="yes" />
		<attribute name="Object File" schema="STRING" fixed="yes" />
		<attribute name="_readable" schema="BOOL" required="yes" hidden="yes" />
		<attribute name="_writable" schema="BOOL" required="yes" hidden="yes" />
		<attribute name="_executable" schema="BOOL" required="yes" hidden="yes" />
		<attribute name="Range" schema="RANGE" required="yes" hidden="yes" />
		<attribute-alias from="_range" to="Range" />
		<attribute name="_display" schema="STRING" hidden="yes" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute schema="VOID" />
	</schema>
	<schema name="Stack" canonical="yes" elementResync="NEVER" attributeResync="NEVER">
		<interface name="Stack" />
		<element schema="StackFrame" />
		<attribute name="_order" schema="INT" hidden="yes" />
	</schema>
	<schema name="StackFrame" elementResync="NEVER" attributeResync="NEVER">
		<interface name="Activatable" />
		<interface name="StackFrame" />
		<interface name="Aggregate" />
		<element schema="VOID" />
		<attribute name="Registers" schema="RegisterValueContainer" required="yes" fixed="yes" />
		<attribute name="PC" schema="ADDRESS" required="yes" />
		<attribute-alias from="_pc" to="PC" />
		<attribute name="Function" schema="STRING" hidden="yes" />
		<attribute-alias from="_function" to="Function" />
		<attribute name="_display" schema="STRING" hidden="yes" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute schema="VOID" />
	</schema>
	<schema name="RegisterValueContainer" attributeResync="ONCE">
		<interface name="RegisterContainer" />
		<attribute name="General Purpose Registers" schema="RegisterBank" />
		<attribute name="general" schema="RegisterBank" />
		<attribute name="system" schema="RegisterBank" />
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute schema="VOID" />
	</schema>
	<schema name="RegisterBank" canonical="yes" elementResync="ONCE" attributeResync="NEVER">
		<attribute name="_order" schema="INT" hidden="yes" />
		<attribute schema="Register" />
	</schema>
	<schema name="Register">
		<interface name="Register" />
	</schema>
</context>
"""


# ---------------------------------------------------------------------------
# The object types, as Ghidra's Trace RMI names them.  A method's parameter
# is matched to the Java side by the schema of its type, so these must carry
# the same names the schema declares.  (The lldb agent defines the same set.)
# ---------------------------------------------------------------------------
class Attachable(TraceObject):
    pass


class AvailableContainer(TraceObject):
    pass


class BreakpointContainer(TraceObject):
    pass


class BreakpointLocation(TraceObject):
    pass


class BreakpointSpec(TraceObject):
    pass


class Memory(TraceObject):
    pass


class Process(TraceObject):
    pass


class ProcessContainer(TraceObject):
    pass


class RegisterValueContainer(TraceObject):
    pass


class Stack(TraceObject):
    pass


class StackFrame(TraceObject):
    pass


class Thread(TraceObject):
    pass


class ThreadContainer(TraceObject):
    pass


class WatchpointContainer(TraceObject):
    pass


class WatchpointSpec(TraceObject):
    pass


# ---------------------------------------------------------------------------
# jank (ram:) <-> live range translation, on top of a running AppMap
# ---------------------------------------------------------------------------
class RamTranslator:
    """Where a byte is, in the two countings.

    `AppMap.resolve` and `AppMap.ghidra_at` answer for a single address.  A
    range is another matter: it can span several segments, each relocated by
    its own delta, with unmapped gaps between them.  `jank_chunks` walks a
    range in Ghidra's counting and yields the live pieces it covers --
    `(jank_start, live_start, length)` -- so a read or a write is applied
    piece by piece.
    """

    def __init__(self, app_map):
        self.m = app_map

    # -- one address ---------------------------------------------------------
    def jank_to_live(self, jank: int) -> int | None:
        """Where the byte Ghidra calls `jank` is now, or None."""
        return self.m.resolve(jank)

    def live_to_jank(self, live: int) -> int | None:
        """The Ghidra address behind a live one, or None."""
        return self.m.ghidra_at(live)

    # -- the contiguous piece holding a jank address -------------------------
    def _piece(self, jank: int):
        """`(live, jank_lo, jank_hi)`: the live address of `jank`, and the
        jank range of the contiguous piece it sits in.  None when the byte is
        not part of the application (system code, a heap thunk)."""
        m, layout = self.m, self.m.layout
        if jank < 0x10000:                                    # low memory: the same
            return jank, 0, 0x10000
        seg = layout.segment_at(jank)
        if seg is not None:
            base = self.m.bases.get(seg.id)
            if base is not None:
                return base + (jank - seg.jank_base), seg.jank_base, \
                    seg.jank_base + seg.size
        if layout.strs_base is not None and self.m.strs_live is not None and \
                layout.strs_base <= jank < layout.strs_base + len(layout.strs):
            return self.m.strs_live + (jank - layout.strs_base), \
                layout.strs_base, layout.strs_base + len(layout.strs)
        below_lo = layout.jank_a5 - layout.below_a5
        if below_lo <= jank < layout.jank_a5:                 # the globals below A5
            return self.m.live_a5 - layout.below_a5 + (jank - below_lo), \
                below_lo, layout.jank_a5
        if layout.jank_a5 <= jank < layout.jank_a5 + JT_OFFSET + layout.jt_size:
            return self.m.live_a5 + (jank - layout.jank_a5), \
                layout.jank_a5, layout.jank_a5 + JT_OFFSET + layout.jt_size
        return None

    # -- a range -------------------------------------------------------------
    def jank_chunks(self, jank_lo: int, jank_hi: int):
        """The live pieces under `[jank_lo, jank_hi)`, in order:
        `(jank_start, live_start, length)`.  Gaps the application doesn't
        cover are skipped, not zero-filled."""
        cur = jank_lo
        while cur < jank_hi:
            piece = self._piece(cur)
            if piece is None:
                cur += 1
                continue
            live, _plo, phi = piece
            end = min(phi, jank_hi)
            # `live` is where `cur` itself is: the chunk starts there and runs
            # on contiguously, so no further offset.
            yield cur, live, end - cur
            cur = end


# ---------------------------------------------------------------------------
# The registers, from a stop's snapshot, in Ghidra's names and byte order
# ---------------------------------------------------------------------------
def build_regvals(view: dict, tr: RamTranslator):
    """The core's registers as `RegVal`s, for `put_registers`.

    Values are big-endian, as Ghidra wants them.  The address registers and
    the program counter hold *addresses*, so they are translated into Ghidra's
    counting -- the trace is self-consistent in the jank space, and a register
    the debugger dereferences lands where the analysed program says.  A
    register pointing outside the application is left at its live value: it is
    the best there is, and the trace's memory won't have it anyway.  The data
    registers are data, not addresses, and are written as they are.
    """
    def addr(x: int) -> int:
        j = tr.live_to_jank(x)
        return j if j is not None else x

    vals = []
    for i in range(8):
        vals.append(RegVal(f"D{i}", view["d"][i].to_bytes(4, "big")))
    for i in range(7):
        vals.append(RegVal(f"A{i}", addr(view["a"][i]).to_bytes(4, "big")))
    vals.append(RegVal("SP", addr(view["sp"]).to_bytes(4, "big")))
    vals.append(RegVal("PC", addr(view["pc"]).to_bytes(4, "big")))
    vals.append(RegVal("SR", view["sr"].to_bytes(2, "big")))
    f = view["flags"]
    for name, key in (("XF", "X"), ("NF", "N"), ("ZF", "Z"), ("VF", "V"),
                      ("CF", "C"), ("TF", "trace"), ("SVF", "supervisor")):
        vals.append(RegVal(name, bytes([1 if f.get(key) else 0])))
    vals.append(RegVal("IPL", bytes([f.get("irq", 0) & 0x7])))
    return vals


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------
class Agent:
    """A running Classetta, to a Ghidra debugger, over Trace RMI.

    `connect` dials the debugger's Trace RMI server, builds the object tree,
    and starts the poll thread that records each stop.  Everything the
    debugger asks -- run control, breakpoints, memory, registers -- is answered
    here, through `macrt.dbg`, on a worker thread.  Nothing here touches the
    core or a widget directly.
    """

    def __init__(self, display, host: str, port: int,
                 language: str = LANGUAGE, compiler: str = COMPILER):
        self.display = display
        self.host = host
        self.port = port
        self.language = language
        self.compiler = compiler
        self.commands = Commands.for_display(display)
        self.client = None
        self.trace = None
        self.tr: RamTranslator | None = None
        self._stop = threading.Event()
        self._poll_thread = None
        self._sock = None
        # Serialises trace mutations between the poll thread and the method
        # worker.  Re-entrant: a stop's recording holds it and then calls
        # _set_state and _put_breakpoints, which take it again.
        self._op_lock = threading.RLock()
        self._frame_proxy = None
        self._proc_obj = None
        self._thread_obj = None

    # -- the connection ------------------------------------------------------
    def connect(self, timeout: float = 30.0) -> None:
        """Dial the debugger, build the tree, and start recording stops.

        Blocks until the running application is to hand -- the trace's
        addresses are translated against its `AppMap`, and there is no map
        before some 68k code is running.  A connection that can't be made
        raises, and says why.
        """
        self._sock = socket.create_connection((self.host, self.port),
                                              timeout=timeout)
        registry = MethodRegistry(ThreadPoolExecutor(max_workers=1))
        self._register_methods(registry)
        # The handshake names the back end and exchanges the method table.
        self.client = Client(self._sock, "classetta", registry)

        self._wait_for_app(timeout)
        self._build_tree()
        self._poll_thread = threading.Thread(target=self._poll_loop,
                                             name="trace-rmi", daemon=True)
        self._poll_thread.start()
        log.info("Trace RMI: connected to %s:%d", self.host, self.port)

    def _wait_for_app(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.commands.debug() is not None and \
                    self.commands.app_map(build=True) is not None:
                self.tr = RamTranslator(self.commands.app_map(build=False))
                return
            time.sleep(0.1)
        raise RuntimeError(
            "no running 68k application to attach to: start the game first")

    def close(self) -> None:
        self._stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2.0)
        if self.client is not None:
            try:
                self.client.close()
            except Exception:                                       # noqa: BLE001
                pass
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:                                       # noqa: BLE001
                pass

    # -- the object tree -----------------------------------------------------
    def _build_tree(self) -> None:
        t = self.trace = self.client.create_trace(
            "classetta", self.language, self.compiler, extra=None)
        with t.open_tx("Create Root Object"):
            root = t.create_root_object(SCHEMA_XML, "MacSession")
            root.set_value("_display", "Classetta")
            t.create_object("Processes").insert()
            t.create_object("Available").insert()
            proc = t.create_object(PATH_PROCESS)
            proc.set_value("PID", PID)
            proc.set_value("State", "STOPPED")
            proc.set_value("_display", "Classetta")
            proc.set_value("_short_display", f"[{PID}]")
            proc.insert()
            t.create_object(PATH_THREADS).insert()
            thread = t.create_object(PATH_THREAD)
            thread.set_value("TID", TID)
            thread.set_value("Name", "68k")
            thread.set_value("State", "STOPPED")
            thread.set_value("_display", "68k")
            thread.insert()
            t.create_object(PATH_STACK).insert()
            t.create_object(PATH_MEMORY).insert()
            t.create_object(PATH_BREAKS).insert()
            t.create_object(PATH_WATCHES).insert()
            t.create_object(PATH_FRAME).insert()
            t.create_object(PATH_REGS).insert()
            t.create_overlay_space("register", PATH_REGS)
        self._proc_obj = Process.from_path(t, PATH_PROCESS)
        self._thread_obj = Thread.from_path(t, PATH_THREAD)
        self._frame_proxy = t.proxy_object_path(PATH_FRAME)

    # -- recording a stop ----------------------------------------------------
    def _record_stop(self, view: dict, debug) -> None:
        t = self.trace
        reason = debug.get_reason() or "stopped"
        with self._op_lock:
            try:
                with t.open_tx(reason):
                    t.snapshot(reason)
                    self._frame_proxy.set_value("PC", self._addr(view["pc"]))
                    if view.get("symbol"):
                        self._frame_proxy.set_value("Function", view["symbol"])
                    t.put_registers(PATH_REGS, build_regvals(view, self.tr))
                    self._put_code_page(view)
                self._set_state("STOPPED")
                self._put_breakpoints()
            except Exception as e:                                 # noqa: BLE001
                log.warning("Trace RMI: recording the stop failed: %s", e)

    def _addr(self, live: int) -> Address:
        j = self.tr.live_to_jank(live)
        return Address("ram", j if j is not None else live)

    def _put_code_page(self, view: dict) -> None:
        """The bytes around the PC, in Ghidra's counting, so the listing has
        its code.  The window is taken in the jank space and walked piece by
        piece, so a window that straddles a segment boundary is still right."""
        jank_pc = self.tr.live_to_jank(view["pc"])
        if jank_pc is None:
            return
        mem = self.commands.mem()
        lo, hi = jank_pc - Debug.BACK, jank_pc + Debug.AHEAD
        for jank_start, live_start, n in self.tr.jank_chunks(lo, hi):
            try:
                data = mem.read(live_start, n)
            except Exception:                                       # noqa: BLE001
                continue
            self.trace.put_bytes(Address("ram", jank_start), data)

    def _put_breakpoints(self) -> None:
        """The breakpoints the core has, into the trace's Breakpoints."""
        debug = self.commands.debug()
        if debug is None:
            return
        t = self.trace
        with self._op_lock:
            try:
                keys = []
                with t.open_tx("Breakpoints"):
                    for i, (kind, addr) in enumerate(self._bp_items(debug)):
                        j = self.tr.live_to_jank(addr)
                        if j is None:
                            continue
                        spec = t.create_object(f"{PATH_BREAKS}[{i}]")
                        spec.set_value("Kinds", kind)
                        spec.set_value("Enabled", True)
                        spec.set_value("_display", f"${j:08X}")
                        loc = t.create_object(f"{PATH_BREAKS}[{i}].0")
                        loc.set_value("Range", Address("ram", j).extend(1))
                        loc.set_value("Enabled", True)
                        loc.insert()
                        spec.insert()
                        keys.append(f"[{i}]")
                    t.proxy_object_path(PATH_BREAKS).retain_values(keys)
            except Exception as e:                                 # noqa: BLE001
                log.warning("Trace RMI: refreshing breakpoints failed: %s", e)

    def _bp_items(self, debug):
        """The core's breakpoints as `(kind, live_addr)`, code first."""
        for a in sorted(debug.bp_addr):
            yield ("execute", a)
        for a in sorted(debug.bp_memw):
            yield ("write", a)
        for a in sorted(debug.bp_memr):
            yield ("read", a)

    def _set_state(self, state: str) -> None:
        with self._op_lock:
            self._proc_obj.set_value("State", state)
            self._thread_obj.set_value("State", state)

    # -- the poll thread -----------------------------------------------------
    def _poll_loop(self) -> None:
        """Watch for the core coming to rest, and record each new stop.

        There is no push from the core: a stop is a stop, and the snapshot it
        takes is what is read.  The view's identity changes on each stop, so
        that is the change watched.  A transition back to running is noted so
        the trace says the machine is moving again."""
        last_view = None
        was_stopped = False
        while not self._stop.is_set():
            debug = self.commands.debug()
            if debug is None:
                time.sleep(0.1)
                continue
            stopped = debug.is_stopped()
            if stopped:
                view = debug.get_view()
                if view is not None and view is not last_view:
                    last_view = view
                    self._record_stop(view, debug)
            elif was_stopped:
                self._set_state("RUNNING")
            was_stopped = stopped
            time.sleep(0.05)

    # -- the methods the debugger calls back ---------------------------------
    # Each is a closure over `agent`, with the lldb agent's signature, so the
    # registry sees the real parameter types and no `self`.
    def _register_methods(self, registry) -> None:
        agent = self
        m = registry.method

        # -- refresh: the debugger asks for a fresh copy of a piece of state --
        def refresh_processes(node: ProcessContainer) -> None:
            agent._set_state("STOPPED" if agent._is_stopped() else "RUNNING")

        def refresh_threads(node: ThreadContainer) -> None:
            agent._set_state("STOPPED" if agent._is_stopped() else "RUNNING")

        def refresh_stack(node: Stack) -> None:
            agent._refresh_frame()

        def refresh_registers(node: RegisterValueContainer) -> None:
            agent._refresh_frame()

        def refresh_memory(node: Memory) -> None:
            pass

        def refresh_breakpoints(node: BreakpointContainer) -> None:
            agent._put_breakpoints()

        m(action="refresh", display="Refresh Processes")(refresh_processes)
        m(action="refresh", display="Refresh Threads")(refresh_threads)
        m(action="refresh", display="Refresh Stack")(refresh_stack)
        m(action="refresh", display="Refresh Registers")(refresh_registers)
        m(action="refresh", display="Refresh Memory")(refresh_memory)
        m(action="refresh", display="Refresh Breakpoints")(refresh_breakpoints)

        # -- activate: the debugger points at an object ----------------------
        def activate_process(process: Process) -> None:
            pass

        def activate_thread(thread: Thread) -> None:
            pass

        def activate_frame(frame: StackFrame) -> None:
            pass

        m(action="activate", display="Activate Process")(activate_process)
        m(action="activate", display="Activate Thread")(activate_thread)
        m(action="activate", display="Activate Frame")(activate_frame)

        # -- run control -----------------------------------------------------
        def _continue(process: Process) -> None:
            agent.commands.go()

        def interrupt(process: Process) -> None:
            agent.commands.pause()

        def step_into(thread: Thread, n: int = 1) -> None:
            agent.commands.step()

        m(name="continue", action="resume", display="Continue")(_continue)
        m(action="interrupt", display="Interrupt")(interrupt)
        m(action="step_into", display="Step Into")(step_into)

        # -- breakpoints and watchpoints -------------------------------------
        def break_address(process: Process, address: Address) -> None:
            live = agent.tr.jank_to_live(address.offset)
            if live is None:
                raise RuntimeError(f"${address.offset:08X} isn't loaded right now")
            agent.commands.debug().add_bp_addr(live)

        def break_read_range(process: Process, range: AddressRange) -> None:
            live = agent.tr.jank_to_live(range.min)
            if live is None:
                raise RuntimeError(f"${range.min:08X} isn't loaded right now")
            agent.commands.debug().add_bp_memr(live)

        def break_write_range(process: Process, range: AddressRange) -> None:
            live = agent.tr.jank_to_live(range.min)
            if live is None:
                raise RuntimeError(f"${range.min:08X} isn't loaded right now")
            agent.commands.debug().add_bp_memw(live)

        def break_access_range(process: Process, range: AddressRange) -> None:
            live = agent.tr.jank_to_live(range.min)
            if live is None:
                raise RuntimeError(f"${range.min:08X} isn't loaded right now")
            debug = agent.commands.debug()
            debug.add_bp_memr(live)
            debug.add_bp_memw(live)

        def toggle_breakpoint(breakpoint: BreakpointSpec, enabled: bool) -> None:
            agent._clear_or_keep(breakpoint, enabled)

        def delete_breakpoint(breakpoint: BreakpointSpec) -> None:
            agent._clear_breakpoint(breakpoint)

        m(action="break_sw_execute", display="Set Breakpoint")(break_address)
        m(action="break_read", display="Set Read Watchpoint")(break_read_range)
        m(action="break_write", display="Set Write Watchpoint")(break_write_range)
        m(action="break_access", display="Set Access Watchpoint")(
            break_access_range)
        m(action="toggle", display="Toggle Breakpoint")(toggle_breakpoint)
        m(action="delete", display="Delete Breakpoint")(delete_breakpoint)

        # -- memory and registers --------------------------------------------
        def read_mem(process: Process, range: AddressRange) -> None:
            mem = agent.commands.mem()
            with agent._op_lock:
                with agent.trace.open_tx("Read Memory"):
                    for jank_start, live_start, n in agent.tr.jank_chunks(
                            range.min, range.max + 1):
                        try:
                            data = mem.read(live_start, n)
                        except Exception:                           # noqa: BLE001
                            agent.trace.set_memory_state(
                                Address("ram", jank_start).extend(n), "error")
                            continue
                        agent.trace.put_bytes(Address("ram", jank_start), data)

        def write_mem(process: Process, address: Address, data: bytes) -> None:
            mem = agent.commands.mem()
            off = 0
            jank = address.offset
            while off < len(data):
                piece = agent.tr._piece(jank)
                if piece is None:
                    jank += 1
                    off += 1
                    continue
                live, _plo, phi = piece
                end = min(phi, jank + (len(data) - off))
                n = end - jank
                mem.write(live, data[off:off + n])
                jank = end
                off += n

        def write_reg(frame: StackFrame, name: str, value: bytes) -> None:
            # Offered only while stopped, so the core is at rest and its
            # registers may be written.  Address registers arrive in Ghidra's
            # counting and are translated back to live ones first.
            cpu = agent.commands.debug().cpu
            n = int.from_bytes(value, "big")
            if name in ("PC", "SP") or name.startswith("A"):
                live = agent.tr.jank_to_live(n)
                if live is not None:
                    n = live
            if name == "PC":
                cpu.pc = n
            elif name == "SP":
                cpu.sp = n
            elif name == "SR":
                cpu.sr = n
            elif name.startswith("D"):
                cpu.set_d(int(name[1:]), n)
            elif name.startswith("A"):
                cpu.set_a(int(name[1:]), n)
            else:
                raise RuntimeError(f"no such register: {name}")
            agent._refresh_frame()

        def execute(cmd: str, to_string: bool = False) -> Optional[str]:
            """A line of the debugger's own command language, run as typed:
            the same `b`, `dm`, `sm`, `status` the window and the MCP server
            take."""
            lines = agent.commands.run(cmd)
            return "\n".join(lines) if to_string else None

        m(action="read_mem", display="Read Memory")(read_mem)
        m(action="write_mem", display="Write Memory")(write_mem)
        m(action="write_reg", display="Write Register")(write_reg)
        m(action="execute", display="Execute")(execute)

    # -- helpers for the methods ---------------------------------------------
    def _is_stopped(self) -> bool:
        debug = self.commands.debug()
        return debug is not None and debug.is_stopped()

    def _refresh_frame(self) -> None:
        debug = self.commands.debug()
        if debug is None:
            return
        view = debug.get_view()
        if view is None:
            return
        with self._op_lock:
            self._frame_proxy.set_value("PC", self._addr(view["pc"]))
            if view.get("symbol"):
                self._frame_proxy.set_value("Function", view["symbol"])
            self.trace.put_registers(PATH_REGS, build_regvals(view, self.tr))

    def _bp_range(self, breakpoint: BreakpointSpec):
        """The breakpoint's range, read back from the trace, or None."""
        vals = breakpoint.get_values("Range")
        if not vals:
            return None
        return vals[0].value

    def _clear_breakpoint(self, breakpoint: BreakpointSpec) -> None:
        rng = self._bp_range(breakpoint)
        if rng is None:
            return
        live = self.tr.jank_to_live(rng.min)
        if live is None:
            return
        debug = self.commands.debug()
        debug.clear_bp_addr(live)
        debug.clear_bp_memw(live)
        debug.clear_bp_memr(live)

    def _clear_or_keep(self, breakpoint: BreakpointSpec, enabled: bool) -> None:
        # Disabling clears it from the core; re-enabling would need its kind,
        # which the trace's Kinds attribute carries.  For now, a toggle off
        # clears, and a toggle on is a no-op the debugger can re-place.
        if not enabled:
            self._clear_breakpoint(breakpoint)


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------
def serve(display, host: str, port: int) -> Optional[Agent]:
    """Connect a running Classetta to a Ghidra debugger's Trace RMI server at
    `host:port`, and keep it recording.  Returns the agent, for `close`;
    None if the dial failed, with the reason said on the log."""
    agent = Agent(display, host, port)
    try:
        agent.connect()
    except OSError as e:
        log.error("Trace RMI: can't reach %s:%d: %s", host, port, e)
        return None
    except RuntimeError as e:
        log.error("Trace RMI: %s", e)
        return None
    return agent
