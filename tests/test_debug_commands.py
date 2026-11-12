"""The debugger's commands: dm and sm, and ram: addresses.

What a command *does* lives in macrt.dbg, shared by the window and the MCP
server, so it's tried here directly -- and, for the parts that only mean
something against a real application, against test_symtab's fixture: an app
whose code is loaded in segments at wherever the Segment Loader put them,
which is exactly the thing a Ghidra address has to be translated through.

The addresses matter here more than anywhere.  A live address is where the
byte is now; a `ram:` address is where Ghidra says it is, and the two are
different numbers for everything but low memory.  So each test checks both:
that the right byte was touched, and that the answer names both addresses so
the person typing isn't left to guess which one the machine used.
"""

import struct
from types import SimpleNamespace

import pytest

from macrt.dbg import (DUMP_LINES_MAX, Commands, Place, ascii_of, dump_line,
                       is_jank, parse_address, parse_value)
from macrt.memory import RAM
from macrt.m68k.cpu import CPU, Debug

from test_debugger import CODE, Guest, machine
from test_symtab import fixture_app, jank_fixture, launch_fixture, table  # noqa: F401

DATA = 0x2000         # where the dumps look: past the code, plain RAM


@pytest.fixture
def rig():
    """A machine, a display stand-in that knows of it, and its commands."""
    mem, cpu = machine()
    debug = Debug(cpu)
    display = SimpleNamespace(system=SimpleNamespace(machine=SimpleNamespace(debug=debug)),
                              processes=[], bridge=None, debugger=None,
                              dbg_commands=None)
    for n in range(48):
        mem.write(DATA + n, bytes([0x41 + n]))
    return mem, Commands(display)


# ---------------------------------------------------------------------------
# dm
# ---------------------------------------------------------------------------
def test_dm_shows_sixteen_bytes_a_line_with_their_text(rig):
    """A dump you can read: the address, the bytes in two groups of eight,
    and what they'd print as.  The ASCII column is the point of it -- a
    string has to stand out of a field of numbers."""
    mem, commands = rig
    lines = commands.run(f"dm ${DATA:06X}")
    assert lines[0] == (f"{DATA:08X}  "
                        f"{' '.join(f'{b:02X}' for b in range(0x41, 0x49))}  "
                        f"{' '.join(f'{b:02X}' for b in range(0x49, 0x51))}  "
                        f"{'ABCDEFGHIJKLMNOP'}")
    assert len(lines) == 8, lines           # the default is eight lines
    assert all(len(line) == len(lines[0]) for line in lines), lines


def test_dm_asks_for_the_number_of_lines_it_wants(rig):
    mem, commands = rig
    assert len(commands.run(f"dm ${DATA:06X} 3")) == 3
    assert len(commands.run(f"dm ${DATA:06X} 1")) == 1
    # However big the ask, the dump stays a size the answer can carry.
    assert len(commands.run(f"dm ${DATA:06X} 5000")) == DUMP_LINES_MAX


def test_dm_with_no_address_reads_on_from_where_it_stopped(rig):
    """Paging through memory shouldn't mean typing the address again; the
    second dm starts where the first one ended."""
    mem, commands = rig
    first = commands.run(f"dm ${DATA:06X} 2")
    again = commands.run("dm")
    assert again[0].startswith(f"{DATA + 32:08X}"), again
    assert again[1].startswith(f"{DATA + 48:08X}"), again
    assert again[0][16:] != first[0][16:]        # different bytes, not a repeat


def test_dm_stops_at_the_end_of_RAM_instead_of_failing(rig):
    """Reading off the end is half a dump of what's there, which is worth
    more than an error and nothing."""
    mem, commands = rig
    past = mem.size - 6
    lines = commands.run(f"dm ${past:06X} 4")
    assert lines[0].startswith(f"{past:08X}")
    assert lines[-1] == f"end of RAM at ${mem.size:08X}", lines
    assert len(lines) == 2                       # the six that are there, and why
    assert lines[0][10:33].split() == ["00"] * 6, lines    # not padded to sixteen


def test_dm_outside_RAM_says_so(rig):
    mem, commands = rig
    lines = commands.run(f"dm ${mem.size + 0x100:06X}")
    assert lines == [f"${mem.size + 0x100:08X} is outside RAM"], lines


def test_dm_of_nothing_typed_is_answered(rig):
    """No address, and nothing before it to read on from."""
    mem, commands = rig
    assert commands.run("dm") == ["no address given"]


def test_a_dump_line_writes_a_short_one_as_wide_as_a_full_one():
    """The columns line up whether the row is full or the last few bytes of
    RAM, so a dump of a run of them is still a table."""
    full, short = dump_line(0x1000, bytes(range(16))), dump_line(0x1000, bytes(range(4)))
    assert len(full) == len(short), (full, short)
    # The columns, cut at the same offsets, hold what they should in each.
    assert full[:8] == short[:8] == "00001000"
    assert full[10:33].split() == ["00", "01", "02", "03", "04", "05", "06", "07"]
    assert short[10:33].split() == ["00", "01", "02", "03"]
    assert full[35:58].split() == ["08", "09", "0A", "0B", "0C", "0D", "0E", "0F"]
    assert short[35:58] == " " * 23                   # nothing for the second group
    # The text column: bytes 0..15 are control codes, so dots, and the short
    # row's four dots sit in the same column with the rest of it blank.
    assert full[60:] == "." * 16
    assert short[60:64] == "...." and short[64:] == " " * 12
    printable = dump_line(0x1000, b"ABCDEFGHIJKLMNOP")
    assert printable[60:] == "ABCDEFGHIJKLMNOP"


def test_unprintable_bytes_show_as_dots():
    """Printing characters as themselves; everything else -- NUL, the high
    half of Mac Roman, control codes -- a dot."""
    assert ascii_of(b"\x00A\xff~ \x1b") == ".A.~ ."


# ---------------------------------------------------------------------------
# sm
# ---------------------------------------------------------------------------
def test_a_value_is_as_big_as_the_digits_that_wrote_it(rig):
    """The whole point of counting hex digits: `sm $2000 4A` is one byte and
    leaves the three after it alone, because a value that didn't ask to be a
    long shouldn't become one."""
    mem, commands = rig
    at = 0x3000
    mem.write(at, bytes([1, 2, 3, 4, 5, 6, 7, 8]))
    assert commands.run(f"sm ${at:06X} 4A") == [f"wrote $4A to ${at:08X} (byte), was 01"]
    assert commands.run(f"sm ${at + 2:06X} 1234") == \
        [f"wrote $1234 to ${at + 2:08X} (word), was 03 04"]
    assert commands.run(f"sm ${at + 4:06X} DEADBEEF") == \
        [f"wrote $DEADBEEF to ${at + 4:08X} (long), was 05 06 07 08"]
    assert bytes(mem._b[at:at + 8]) == bytes([0x4A, 0x02, 0x12, 0x34,
                                              0xDE, 0xAD, 0xBE, 0xEF])


def test_a_write_says_what_was_there_before_it(rig):
    """A write to the wrong place is legible when the answer carries the
    old bytes: you can put them back from the log."""
    mem, commands = rig
    mem.write(0x3100, b"\xAA\xBB")
    said = commands.run("sm $3100 1234")[0]
    assert "was AA BB" in said, said


def test_a_value_writes_big_endian(rig):
    """The Mac is big-endian; the digits go down in the order they're
    written, which is what makes a dump of what you wrote read the same."""
    mem, commands = rig
    commands.run("sm $3200 11223344")
    assert mem.r32(0x3200) == 0x11223344
    assert struct.unpack_from(">I", mem._b, 0x3200)[0] == 0x11223344


def test_a_value_too_big_or_missing_is_answered(rig):
    mem, commands = rig
    assert "more than a long" in commands.run("sm $3300 1234567890")[0]
    assert commands.run("sm $3300") == ["no value given"]
    assert commands.run("sm") == ["no value given"]
    assert commands.run("sm $3300 zz")[0] == "zz is not a value"


def test_a_write_past_the_end_of_RAM_is_answered(rig):
    mem, commands = rig
    lines = commands.run(f"sm ${mem.size - 2:06X} 11223344")
    assert "outside RAM" in lines[0], lines


def test_parse_value_counts_digits():
    assert parse_value("4A") == (0x4A, 1, "byte")
    assert parse_value("$4a") == (0x4A, 1, "byte")
    assert parse_value("0x4A") == (0x4A, 1, "byte")
    assert parse_value("1234") == (0x1234, 2, "word")
    assert parse_value("12345678") == (0x12345678, 4, "long")
    assert parse_value("FFF") == (0xFFF, 2, "word")     # three digits: a word
    for bad in ("", "$", "nonsense"):
        with pytest.raises(ValueError):
            parse_value(bad)


# ---------------------------------------------------------------------------
# ram: -- Ghidra's addresses, on the real fixture
#
# The fixture's code is loaded in segments, wherever the Segment Loader put
# them, so ram:00010000 is not $10000 here: it's wherever CODE 1 landed.  A
# translation that worked on a stub would be worth nothing -- this one has to
# read the live jump table, which is the same AppMap the tracer and the hook
# engine read.
# ---------------------------------------------------------------------------
@pytest.fixture
def game(harness, jank_fixture):            # noqa: F811 - the fixture is the point
    """The fixture application running, and commands on its display."""
    h = harness()
    proc = launch_fixture(h)
    return h, proc, jank_fixture, Commands(h.d)


def the_sym(lay, name):
    return next(s for s in table(lay) if s["name"] == name)


def test_a_ram_address_is_where_that_byte_actually_is(game):
    """The heart of it: Ghidra's address for AddTwo's first instruction, and
    the address the machine has for it -- different numbers, same byte."""
    h, proc, lay, commands = game
    entry = the_sym(lay, "AddTwo")["entry"]
    live = commands.to_live(entry)
    assert live != entry                       # the loader put it elsewhere
    # It's the routine's own code: the fixture's AddTwo begins move.w 4(a7),d0.
    assert proc.system.mem.r16(live) == 0x302F
    # And it's the same byte the hook engine's own map points at.
    assert live == proc.patcher().app_map.resolve(entry)
    assert commands.to_jank(live) == entry


def test_low_memory_is_the_same_number_in_both(game):
    h, proc, lay, commands = game
    assert commands.to_live(0x120) == 0x120


def test_every_command_takes_a_ram_address(game):
    """b, bmw, dm and sm all translate, and all say both addresses back."""
    h, proc, lay, commands = game
    entry = the_sym(lay, "AddTwo")["entry"]
    live = commands.to_live(entry)
    typed = f"ram:{entry:08X}"

    assert commands.run(f"b {typed}") == [f"breakpoint at ${live:08X} ({typed})"]
    assert proc.system.machine.debug.bp_addr == {live}
    assert commands.run(f"cb {typed}") == [f"cleared breakpoint at ${live:08X} ({typed})"]
    assert proc.system.machine.debug.bp_addr == set()

    commands.run(f"bmw {typed}")
    assert proc.system.machine.debug.bp_memw == {live}
    commands.run(f"cbmw {typed}")
    assert proc.system.machine.debug.bp_memw == set()

    dumped = commands.run(f"dm {typed} 1")
    assert dumped[0] == f"{typed} is ${live:08X}", dumped   # which column is which
    assert dumped[1].startswith(f"{live:08X}  30 2F"), dumped


def test_sm_takes_a_ram_address_of_data(game):
    """A ram: address written, not read -- on a global rather than on the
    fixture's own code, which it is still running."""
    h, proc, lay, commands = game
    globals_lo = lay.jank_a5 - lay.below_a5
    target = globals_lo + 40                   # clear of the fixture's own globals
    live = commands.to_live(target)
    assert live != target and live < proc.partition.a5
    said = commands.run(f"sm ram:{target:08X} DEADBEEF")[0]
    assert f"${live:08X} (ram:{target:08X})" in said, said
    assert proc.system.mem.r32(live) == 0xDEADBEEF


def test_a_ram_address_not_loaded_is_said_so(game):
    """CODE 3 is in the dump and never loaded.  A breakpoint there would
    never fire and look like a debugger that doesn't work; the honest answer
    is that the segment isn't there."""
    h, proc, lay, commands = game
    never = lay.segments[2].jank_base
    with pytest.raises(ValueError, match="isn't loaded right now"):
        commands.to_live(never)
    assert "isn't loaded right now" in commands.run(f"b ram:{never:08X}")[0]
    assert proc.system.machine.debug.bp_addr == set()


def test_a_live_address_names_its_ghidra_one(game):
    """The other way about, for free: where the machine's address sits in
    Ghidra, so a PC from a pause can be looked up."""
    h, proc, lay, commands = game
    entry = the_sym(lay, "AddTwo")["entry"]
    live = commands.to_live(entry)
    place = commands.place(f"${live:08X}")
    assert place.jank == entry
    assert place.shown() == f"${live:08X} (ram:{entry:08X})"


def test_ram_without_an_application_says_what_it_needs(rig):
    """Not an error at sea: on a machine with nothing mapped, `ram:` says it
    has no application to translate against."""
    mem, commands = rig
    assert "needs a running 68k application" in commands.run("b ram:10000")[0]


def test_a_map_made_before_a_segment_loaded_is_read_again(game):
    """The live jump table is the truth.  A map that didn't know about a
    segment yet gets refreshed rather than answering "not loaded"."""
    h, proc, lay, commands = game

    class SlowMap:
        """Knows nothing until asked once, then behaves -- the way a map
        built before the Segment Loader got there would."""

        def __init__(self, real):
            self.real, self.refreshes = real, 0
            self.shut = True

        def resolve(self, addr):
            return None if self.shut else self.real.resolve(addr)

        def ghidra_at(self, addr):
            return self.real.ghidra_at(addr)

        def refresh(self):
            self.refreshes += 1
            self.shut = False

    entry = the_sym(lay, "Twice")["entry"]
    runner = proc.runner
    real = runner.trace_map()
    runner._trace_app_map = slow = SlowMap(real)
    try:
        live = commands.to_live(entry)
        assert live == real.resolve(entry)
        assert slow.refreshes >= 1
    finally:
        runner._trace_app_map = real


# ---------------------------------------------------------------------------
# Addresses as typed
# ---------------------------------------------------------------------------
def test_ram_is_recognised_however_it_cased_it():
    assert is_jank("ram:10000") and is_jank("RAM:10000") and is_jank("  ram:1234")
    assert not is_jank("$10000") and not is_jank("10000")


def test_an_address_still_accepts_every_way_of_writing_hex():
    assert parse_address("$2C00") == parse_address("2C00") == 0x2C00
    assert parse_address("0x2c00") == 0x2C00


def test_a_place_says_both_or_only_what_it_has():
    assert Place(0x4000).shown() == "$00004000"
    assert Place(0x4000, 0x4000).shown() == "$00004000"      # low memory: one name
    assert Place(0x4000, 0x10000).shown() == "$00004000 (ram:00010000)"


# ---------------------------------------------------------------------------
# The window gets all of it, because it asks the same object
# ---------------------------------------------------------------------------
class StubSystem:
    def __init__(self):
        self.machine = None


class StubDisplay:
    def __init__(self):
        self.system = StubSystem()
        self.processes = []
        self.bridge = None


@pytest.fixture
def window(qapp):
    """The window on a guest thread running the loop, as test_debugger's own
    fixture has it -- so a dm of the code there is a dump of real code."""
    from macrt.ui.debugger import DebuggerWindow

    mem, cpu = machine()
    guest = Guest(cpu)
    display = StubDisplay()
    display.system.machine = SimpleNamespace(debug=guest.debug)
    win = DebuggerWindow(display)
    try:
        yield win, guest, guest.debug, mem
    finally:
        guest.close()
        win.force_close()
        qapp.processEvents()


def enter(win, text: str) -> str:
    """A command typed at the window; what the log gained from it."""
    before = win.log.toPlainText()
    win.command.setText(text)
    win._command()
    return win.log.toPlainText()[len(before):]


def test_the_window_dumps_and_writes_memory(window):
    """The window's command line runs the same commands, so dm and sm are
    there without the window having its own version of them."""
    win, guest, debug, mem = window
    win.refresh()
    log = enter(win, f"dm ${CODE:06X} 1")
    assert f"{CODE:08X}  52 40 54 40 58 40 5F 40  60 F6" in log, log
    assert "R@T@X@_@`" in log, log            # the same bytes as text
    log = enter(win, f"sm ${CODE + 0x100:06X} 1234")
    assert "wrote $1234" in log and "(word)" in log, log
    assert mem.r16(CODE + 0x100) == 0x1234


def test_the_window_lists_the_commands(window):
    win, guest, debug, mem = window
    log = enter(win, "?")
    for command in ("dm", "sm", "bt", "ram:"):
        assert command in log, log


def test_the_window_and_the_server_share_one_set_of_commands(window):
    """One dm page, one set of breakpoints: the window and the MCP server
    hold the same Commands on the display rather than two that happen to
    agree until someone pages through memory from both."""
    win, guest, debug, mem = window
    from macrt.dbg import Commands
    from macrt.mcpserver import Tools
    assert win.commands is win.display.dbg_commands
    assert Tools(win.display).commands is win.commands
    assert Commands.for_display(win.display) is win.commands


def test_the_window_answers_a_ram_address_it_cannot_translate(window):
    """No application, no translation -- and the log says that, rather than
    the window losing the command."""
    win, guest, debug, mem = window
    assert "needs a running 68k application" in enter(win, "b ram:10000")
    assert debug.bp_addr == set()


# ---------------------------------------------------------------------------
# Running: status, pause, step, go
# ---------------------------------------------------------------------------
def test_status_says_running_when_there_is_nothing_to_show(rig):
    """The answer the user asked for when it isn't paused: that it's
    running, and why there are no registers to be had."""
    mem, commands = rig
    status = commands.status()
    assert status["state"] == "running" and status["running"]
    assert "pause" in status["message"]
    assert "pc" not in status                 # no consistent machine to read


def test_status_of_no_processor_says_there_is_none():
    display = SimpleNamespace(system=None, processes=[], bridge=None)
    status = Commands(display).status()
    assert status["state"] == "nothing to watch yet"
    assert Commands(display).run("status")[0] == status["message"]


def test_pause_then_status_shows_the_whole_machine():
    from test_debugger import consistent
    mem, cpu = machine()
    guest = Guest(cpu)
    display = SimpleNamespace(system=SimpleNamespace(machine=SimpleNamespace(
        debug=guest.debug)), processes=[], bridge=None)
    commands = Commands(display)
    try:
        status = commands.pause()
        assert status["paused"], status
        assert status["reason"] == "Pause pressed"
        pc = int(status["pc"], 16)
        assert consistent(pc, int(status["d"][0], 16))
        assert len(status["d"]) == 8 and len(status["a"]) == 7
        assert len(status["listing"]) == guest.debug.BACK + guest.debug.AHEAD + 1
        assert int(next(r for r in status["listing"] if r["at_pc"])["address"], 16) == pc
        assert not status["symbols"] and "no symbol table" in status["where"]
        # The text answer says the same thing as the dict.
        assert commands.run("status")[0].startswith("Pause pressed at $")
    finally:
        guest.close()


def test_a_step_moves_one_instruction_and_leaves_the_machine_consistent():
    """The same check test_debugger makes of a step, through the shared
    commands: the PC lands on the next instruction, and D0 -- which the loop
    makes say exactly which instruction the machine is between -- agrees."""
    from test_debugger import AT, NEXT, LOOP_SIZE, consistent
    mem, cpu = machine()
    guest = Guest(cpu)
    display = SimpleNamespace(system=SimpleNamespace(machine=SimpleNamespace(
        debug=guest.debug)), processes=[], bridge=None)
    commands = Commands(display)
    try:
        first = int(commands.pause()["pc"], 16)
        after = int(commands.step()["pc"], 16)
        offset = (after - CODE) % LOOP_SIZE
        assert offset == NEXT[(first - CODE) % LOOP_SIZE], (hex(first), hex(after))
        assert consistent(after, int(_status_d0(commands), 16))
        assert AT[offset] == int(_status_d0(commands), 16)
    finally:
        guest.close()


def test_stepping_a_running_program_stops_it_and_says_so():
    """There's nothing to step from while it runs free, so the one step
    doesn't silently run one instruction of an application nobody meant to
    single-step: it stops, and the answer says to step again."""
    mem, cpu = machine()
    guest = Guest(cpu)
    display = SimpleNamespace(system=SimpleNamespace(machine=SimpleNamespace(
        debug=guest.debug)), processes=[], bridge=None)
    commands = Commands(display)
    try:
        status = commands.step()
        assert status["paused"]
        assert "step again" in status["message"], status
        moved = commands.step()
        assert "message" not in moved or "step again" not in moved["message"]
    finally:
        guest.close()


def test_go_lets_it_run_and_says_so():
    mem, cpu = machine()
    guest = Guest(cpu)
    display = SimpleNamespace(system=SimpleNamespace(machine=SimpleNamespace(
        debug=guest.debug)), processes=[], bridge=None)
    commands = Commands(display)
    try:
        commands.pause()
        status = commands.go()
        assert status["running"], status
        assert "running" in commands.run("go")[0], commands.run("go")
    finally:
        guest.close()


def _status_d0(commands) -> str:
    return commands.status()["d"][0]


def test_c_means_continue_and_cb_means_clear_a_breakpoint():
    """The one letter that would otherwise be ambiguous, decided the way the
    window already had it: a bare c runs, cb takes a breakpoint off."""
    mem, cpu = machine()
    debug = Debug(cpu)
    display = SimpleNamespace(system=SimpleNamespace(machine=SimpleNamespace(
        debug=debug)), processes=[], bridge=None)
    commands = Commands(display)
    commands.run("b $10004")
    assert debug.bp_addr == {0x10004}
    commands.run("c $10004")               # a clear, not a continue with junk
    assert debug.bp_addr == set()
    assert commands.run("continue")[0]      # the long way, and it runs
    assert commands.run("cb $10004")[0].startswith("cleared")
