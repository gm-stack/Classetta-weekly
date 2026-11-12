"""The Trace RMI back end: Classetta as a Ghidra debugger's machine.

A fake Trace RMI server on a real socket plays Ghidra's side: it answers the
handshake and the object-tree build the way the debugger's server does, and it
can call the agent back the way the debugger calls a back end.  The agent is
the real `macrt.tracerpc.Agent`, driven by a stub machine (a debug hold, an
application map, a memory) rather than a running game, so the wire protocol and
the jank<->live address translation are checked without a JVM.

What is checked: the handshake names the back end and the 68k language; the
object tree the debugger expects is built, with the register overlay space; a
stop is recorded as a snapshot, the registers in Ghidra's names, the program
counter in the jank space, and the code page around it; and a breakpoint the
debugger asks for lands on the live, relocated copy of the byte it named.
"""

import queue
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from ghidratrace import trace_rmi_pb2 as pb
from ghidratrace.util import recv_delimited, send_delimited

from macrt.tracerpc import (PATH_PROCESS, PATH_REGS, Agent,
                            build_regvals, RamTranslator)


# ---------------------------------------------------------------------------
# The machine, stubbed: a debug hold, an application map, a memory
# ---------------------------------------------------------------------------
# The layout is a small one with a single code segment, so the translation is
# easy to check by hand:
#   jank 0x00000..0x0FFFF  low memory, the same at live
#   jank 0x10000..0x100FF  the one segment, loaded at live 0x50000..0x500FF
#   jank 0x10100..0x101FF  the globals below A5, at live 0x3FF00..0x3FFFF
#   jank 0x10200          A5, at live 0x40000
SYSTEM_RAM = 0x10000
SEG_JANK = 0x10000
SEG_SIZE = 0x100
SEG_LIVE = 0x50000
LIVE_A5 = 0x40000
BELOW_A5 = 0x100
JANK_A5 = 0x10200


class FakeMem:
    def __init__(self, size=0x20000):
        self.data = bytearray(size)

    def read(self, addr, n):
        return bytes(self.data[addr:addr + n])

    def write(self, addr, data):
        self.data[addr:addr + len(data)] = data


class FakeCpu:
    def __init__(self):
        self.pc = SEG_LIVE + 0x40
        self.sp = 0x18000
        self.sr = 0x2000
        self._d = [0] * 8
        self._a = [0] * 7

    def set_d(self, n, v):
        self._d[n] = v

    def set_a(self, n, v):
        self._a[n] = v

    def d(self, n):
        return self._d[n]

    def a(self, n):
        return self._a[n]


class FakeDebug:
    """A debugger hold that a test can put into a stopped state on demand."""

    def __init__(self):
        self.cpu = FakeCpu()
        self.stopped = False
        self._view = None
        self.reason = ""
        self.bp_addr = set()
        self.bp_memw = set()
        self.bp_memr = set()

    def is_stopped(self):
        return self.stopped

    def get_view(self):
        return self._view

    def get_reason(self):
        return self.reason

    def add_bp_addr(self, addr):
        self.bp_addr.add(addr)

    def clear_bp_addr(self, addr):
        self.bp_addr.discard(addr)

    def add_bp_memw(self, addr):
        self.bp_memw.add(addr)

    def clear_bp_memw(self, addr):
        self.bp_memw.discard(addr)

    def add_bp_memr(self, addr):
        self.bp_memr.add(addr)

    def clear_bp_memr(self, addr):
        self.bp_memr.discard(addr)


class FakeCommands:
    """The `macrt.dbg` surface the agent uses, answered from the stubs."""

    def __init__(self, debug, app_map, mem):
        self._debug = debug
        self._app_map = app_map
        self._mem = mem

    def debug(self):
        return self._debug

    def app_map(self, build=False):
        return self._app_map

    def mem(self):
        return self._mem

    def go(self):
        pass

    def pause(self):
        pass

    def step(self):
        pass

    def run(self, line):
        return [line]


class FakeSegment:
    def __init__(self, id, jank_base, size):
        self.id = id
        self.jank_base = jank_base
        self.size = size


class FakeLayout:
    def __init__(self):
        self.segments = (FakeSegment(0, SEG_JANK, SEG_SIZE),)
        self.jank_a5 = JANK_A5
        self.below_a5 = BELOW_A5
        self.jt_size = 8
        self.strs_base = None
        self.strs = b""

    def segment_at(self, addr):
        for s in self.segments:
            if s.jank_base <= addr < s.jank_base + s.size:
                return s
        return None


class FakeAppMap:
    def __init__(self):
        self.live_a5 = LIVE_A5
        self.strs_live = None
        self.bases = {0: SEG_LIVE}
        self.layout = FakeLayout()

    def resolve(self, addr):
        if addr < SYSTEM_RAM:
            return addr
        seg = self.layout.segment_at(addr)
        if seg is not None:
            base = self.bases.get(seg.id)
            return None if base is None else base + (addr - seg.jank_base)
        return None

    def ghidra_at(self, live):
        if live < SYSTEM_RAM:
            return live
        base = self.bases.get(0)
        if base is not None and base <= live < base + SEG_SIZE:
            return SEG_JANK + (live - base)
        return None


# ---------------------------------------------------------------------------
# The fake Ghidra: a Trace RMI server that answers and calls back
# ---------------------------------------------------------------------------
class FakeServer:
    """Ghidra's side of the channel.  It answers every request the agent
    sends, and it can call the agent back the way the debugger does.  One
    thread does all the socket I/O, so a call-back and the agent's own
    requests never race over the socket."""

    def __init__(self):
        self.lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(1)
        self.port = self.lsock.getsockname()[1]
        self.received = []
        self._id = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._conn = None
        self._invoke_q = queue.Queue()
        self._results = {}

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        try:
            conn, _ = self.lsock.accept()
        except OSError:
            return
        conn.settimeout(1.0)
        self._conn = conn
        while not self._stop.is_set():
            # A call-back the test queued?
            try:
                name, req = self._invoke_q.get(timeout=0.05)
            except queue.Empty:
                try:
                    msg = self._recv(conn)
                except socket.timeout:
                    continue
                except OSError:
                    break
                self._record(msg)
                reply = self._reply(msg)
                if reply is not None:
                    self._send(conn, reply)
                continue
            # Send the call-back and wait for its reply, answering the
            # agent's own requests that arrive in between.
            self._send(conn, req)
            while not self._stop.is_set():
                try:
                    msg = self._recv(conn)
                except socket.timeout:
                    continue
                except OSError:
                    break
                self._record(msg)
                if msg.WhichOneof("msg") == "xreply_invoke_method":
                    self._results[name] = msg
                    break
                reply = self._reply(msg)
                if reply is not None:
                    self._send(conn, reply)

    def _recv(self, conn):
        return recv_delimited(conn, pb.RootMessage(), 0)

    def _send(self, conn, msg):
        try:
            send_delimited(conn, msg)
        except OSError:
            pass

    def _record(self, msg):
        with self._lock:
            self.received.append((msg.WhichOneof("msg"), msg))

    def _next_id(self):
        with self._lock:
            self._id += 1
            return self._id

    # request -> the reply field the client waits for.  An empty reply still
    # has to be present, so each is SetInParent()d: a RootMessage with no
    # field set decodes as None and the client raises ProtocolError.
    _REPLIES = {
        "request_negotiate": "reply_negotiate",
        "request_create_trace": "reply_create_trace",
        "request_create_root_object": "reply_create_object",
        "request_create_object": "reply_create_object",
        "request_insert_object": "reply_insert_object",
        "request_remove_object": "reply_remove_object",
        "request_set_value": "reply_set_value",
        "request_retain_values": "reply_retain_values",
        "request_get_object": "reply_get_object",
        "request_get_values": "reply_get_values",
        "request_start_tx": "reply_start_tx",
        "request_end_tx": "reply_end_tx",
        "request_create_overlay": "reply_create_overlay",
        "request_set_memory_state": "reply_set_memory_state",
        "request_put_bytes": "reply_put_bytes",
        "request_delete_bytes": "reply_delete_bytes",
        "request_put_register_value": "reply_put_register_value",
        "request_delete_register_value": "reply_delete_register_value",
        "request_snapshot": "reply_snapshot",
        "request_activate": "reply_activate",
        "request_disassemble": "reply_disassemble",
    }

    def _reply(self, msg):
        oneof = msg.WhichOneof("msg")
        if oneof not in self._REPLIES:
            return None
        r = pb.RootMessage()
        reply = getattr(r, self._REPLIES[oneof])
        reply.SetInParent()
        if oneof == "request_negotiate":
            r.reply_negotiate.description = "fake-ghidra"
        elif oneof in ("request_create_root_object", "request_create_object"):
            r.reply_create_object.object.id = self._next_id()
        elif oneof == "request_snapshot":
            r.reply_snapshot.snap.snap = 0
        elif oneof == "request_put_bytes":
            r.reply_put_bytes.written = len(msg.request_put_bytes.data)
        return r

    def invoke(self, name, args, timeout=5.0):
        """Call the agent back, the way the debugger does.  `args` is a list of
        `(name, pb.Value)`.  Returns the reply message."""
        req = pb.RootMessage()
        req.xrequest_invoke_method.name = name
        # The client binds an object argument to the trace the call is made
        # on: without the trace's id it can't resolve the path.
        req.xrequest_invoke_method.oid.id = 1
        for an, av in args:
            a = req.xrequest_invoke_method.arguments.add()
            a.name = an
            a.value.CopyFrom(av)
        self._invoke_q.put((name, req))
        deadline = time.monotonic() + timeout
        while name not in self._results:
            if time.monotonic() > deadline:
                raise TimeoutError(f"no reply to invoke {name}")
            time.sleep(0.01)
        return self._results.pop(name)

    def stop(self):
        self._stop.set()
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            self.lsock.close()
        except OSError:
            pass

    def saw(self, name):
        with self._lock:
            return [m for n, m in self.received if n == name]


def wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# The fixture: a server and a connected agent on the stub machine
# ---------------------------------------------------------------------------
@pytest.fixture
def setup():
    server = FakeServer().start()
    mem = FakeMem()
    debug = FakeDebug()
    app_map = FakeAppMap()
    commands = FakeCommands(debug, app_map, mem)
    agent = Agent(SimpleNamespace(), "127.0.0.1", server.port)
    agent.commands = commands
    agent.connect(timeout=10.0)
    try:
        yield SimpleNamespace(server=server, agent=agent, debug=debug,
                              mem=mem, app_map=app_map)
    finally:
        agent.close()
        server.stop()


# ---------------------------------------------------------------------------
# The connection and the object tree
# ---------------------------------------------------------------------------
def test_handshake_and_tree(setup):
    server = setup.server
    # The handshake named the back end.
    neg = server.saw("request_negotiate")
    assert len(neg) == 1
    assert neg[0].request_negotiate.description == "classetta"
    # The trace was made in the 68k language.
    ct = server.saw("request_create_trace")
    assert len(ct) == 1
    assert ct[0].request_create_trace.language.id == "68000:BE:32:mac"
    # The root object is the MacSession schema.
    root = server.saw("request_create_root_object")
    assert len(root) == 1
    assert root[0].request_create_root_object.root_schema == "MacSession"
    # The object tree the debugger expects.
    created = [m.request_create_object.path.path
               for m in server.saw("request_create_object")]
    for p in ("Processes", "Available", "Processes[1]",
              "Processes[1].Threads", "Processes[1].Threads[0]",
              "Processes[1].Threads[0].Stack", "Processes[1].Memory",
              "Processes[1].Breakpoints", "Processes[1].Watchpoints",
              "Processes[1].Threads[0].Stack[0]",
              "Processes[1].Threads[0].Stack[0].Registers"):
        assert p in created, f"missing object {p}"
    # The register overlay space, on the register container.
    ov = server.saw("request_create_overlay")
    assert len(ov) == 1
    assert ov[0].request_create_overlay.baseSpace == "register"
    assert ov[0].request_create_overlay.name == PATH_REGS


# ---------------------------------------------------------------------------
# A stop, recorded
# ---------------------------------------------------------------------------
def _view():
    return {
        "pc": SEG_LIVE + 0x40,          # jank 0x10040
        "sp": 0x18000,                  # low memory: the same
        "sr": 0x2000,
        "d": [0, 1, 2, 3, 4, 5, 6, 7],
        "a": [SEG_LIVE, 0, 0, 0, 0, 0, 0],   # A0 = the segment's base
        "flags": {"X": False, "N": False, "Z": False, "V": False,
                  "C": False, "supervisor": True, "trace": False, "irq": 0},
        "app_name": "Test",
        "listing": "",
        "symbol": "process_game_input",
        "symbols": False,
    }


def test_stop_recorded(setup):
    server, debug = setup.server, setup.debug
    debug._view = _view()
    debug.reason = "pause"
    debug.stopped = True
    # The registers are sent after the snapshot in the same transaction, so
    # wait for them rather than the snapshot.
    assert wait_for(lambda: server.saw("request_put_register_value"))
    # The registers, in Ghidra's names.
    regs = server.saw("request_put_register_value")
    assert len(regs) >= 1
    names = [v.name for v in regs[0].request_put_register_value.values]
    for n in ("D0", "D7", "A0", "A6", "SP", "PC", "SR",
              "XF", "NF", "ZF", "VF", "CF", "TF", "SVF", "IPL"):
        assert n in names, f"missing register {n}"
    # The program counter is in the jank space: live 0x50040 is 0x10040.
    pc_sets = [m for m in server.saw("request_set_value")
               if m.request_set_value.value.key == "PC"]
    assert pc_sets
    assert pc_sets[0].request_set_value.value.value.address_value.offset \
        == 0x10040
    # The code page around it, in the jank space.  It is sent after the
    # registers in the same transaction, so wait for it.
    assert wait_for(lambda: server.saw("request_put_bytes"))
    puts = server.saw("request_put_bytes")
    assert puts
    assert puts[0].request_put_bytes.start.offset == 0x10040 - 10


# ---------------------------------------------------------------------------
# A call-back from the debugger
# ---------------------------------------------------------------------------
def test_breakpoint_callback(setup):
    server, debug = setup.server, setup.debug
    proc = pb.Value()
    proc.child_desc.path.path = PATH_PROCESS
    addr = pb.Value()
    addr.address_value.space = "ram"
    addr.address_value.offset = 0x10080    # jank; live 0x50080
    reply = server.invoke("break_address", [("process", proc),
                                            ("address", addr)])
    assert not reply.xreply_invoke_method.error
    assert SEG_LIVE + 0x80 in debug.bp_addr


# ---------------------------------------------------------------------------
# The translation and the registers, on their own
# ---------------------------------------------------------------------------
def test_translation_round_trip():
    tr = RamTranslator(FakeAppMap())
    # A segment byte, both ways.
    assert tr.jank_to_live(0x10040) == SEG_LIVE + 0x40
    assert tr.live_to_jank(SEG_LIVE + 0x40) == 0x10040
    # Low memory (below 0x10000) is the same.
    assert tr.jank_to_live(0x910) == 0x910
    assert tr.live_to_jank(0x910) == 0x910
    # Outside the application: None.
    assert tr.jank_to_live(0x20000) is None
    assert tr.live_to_jank(0x60000) is None


def test_jank_chunks_span_a_gap():
    tr = RamTranslator(FakeAppMap())
    # 0xFF00 is low memory, 0x10000 is the segment: a range that crosses the
    # boundary yields two live pieces, with the gap between them skipped.
    chunks = list(tr.jank_chunks(0xFF00, 0x10010))
    assert chunks == [(0xFF00, 0xFF00, 256), (0x10000, SEG_LIVE, 16)]


def test_build_regvals_translates_addresses():
    tr = RamTranslator(FakeAppMap())
    vals = {v.name: v.value for v in build_regvals(_view(), tr)}
    # Data registers are data: written as they are.
    assert vals["D1"] == (1).to_bytes(4, "big")
    # Address registers and the PC are translated into the jank space.
    assert vals["PC"] == (0x10040).to_bytes(4, "big")
    assert vals["A0"] == (0x10000).to_bytes(4, "big")
    # SP is low memory: the same.
    assert vals["SP"] == (0x18000).to_bytes(4, "big")
    # SR is two bytes.
    assert vals["SR"] == (0x2000).to_bytes(2, "big")
