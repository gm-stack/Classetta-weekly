"""The trap trace: the line `--trace-traps` writes for every trap.

Nothing else in the suite reads arguments *for the trace*, so these tests
run `shape` and `result_text` over the whole of the trap tables rather
than over a few traps.  Renaming the function that reads a register out
of the CPU once broke the first trap a real game called -- `ReserveMem`,
whose arguments include a bit of its own trap word -- and every trap
signature takes its arguments a slightly different way, so it is the
whole sweep or nothing.
"""

from pathlib import Path

import pytest
from guest import Processor

from macrt.m68k import dispatch
from macrt.m68k import runtime                                     # noqa: F401 - the glue
from macrt.macfile import open_application

APPS = Path(__file__).parent / "apps"


def sig(name: str, result: str = "", result_reg: str | None = None,
        trap: int = 0xA985) -> dispatch.Signature:
    """A signature for `result_text`, which needs only its name, the trap
    it belongs to (a Pascal call answers on the stack, an OS trap in a
    register) and what it answers with."""
    return dispatch.Signature(name=name, trap=trap, dispatcher=None, selector=None,
                              stack="", result=result, regs=(), result_reg=result_reg,
                              extras="")


def test_every_described_trap_shapes_and_answers_for_the_trace(qapp):
    """Whatever the interfaces describe, the trace can print: a line that
    begins with the routine's name, and an answer that is text.  A reader
    that doesn't exist raises here -- and took the game down at its very
    first `ReserveMem` when it did."""
    g = Processor()
    for key, want in sorted(dispatch.BY_TRAP.items()):
        s, name, call = dispatch.shape(g, want.trap)
        # A @raw handler names its own call: LaunchApplication is Launch.
        assert name in (want.name, dispatch.EXTRA_NAMES.get(key, want.name)), \
            f"{want.name}: {name}"
        assert call.startswith(f"{name}("), f"{want.name}: {call}"
        assert dispatch.result_text(g, s) is not None


def test_every_routine_a_package_dispatches_shapes(qapp):
    """The same for the routines behind a trap word that picks them by
    selector, which no single trap word names: every selector in every
    package's table, in the register the package reads it from."""
    g = Processor()
    for _key, (package, _where, table) in sorted(dispatch.DISPATCHERS.items()):
        for selector, want in sorted(table.items()):
            g.cpu.set_d(0, selector & 0xFFFF)
            s, name, call = dispatch.shape(g, want.trap)
            assert call.startswith(f"{name}("), f"{package} ${selector:04X}: {call}"
            assert dispatch.result_text(g, s) is not None


def test_the_trace_reads_arguments_from_everywhere_they_can_sit(qapp):
    """Not a sweep: one line, read back.  The sweep above only proves
    nothing raises -- a reader that always answered `?` would pass it --
    so here are the ways an argument sits, with values that come back out
    of the line as they went in.

    BlockMove carries whether D0 counts bytes or longwords in a bit of
    the trap word, which is why its one line covers three of the ways.
    """
    g = Processor()
    g.cpu.set_a(0, 0x12345678)
    g.cpu.set_a(1, 0x9ABCDEF0)
    g.cpu.set_d(0, 3)
    assert dispatch.shape(g, 0xA02E)[2] == \
        "BlockMove(A0=$12345678, A1=$9ABCDEF0, D0=3, TrapBit<0x200>=0)"
    assert dispatch.shape(g, 0xA02E | 0x200)[2].endswith("TrapBit<0x200>=1)")


def test_the_two_halves_of_d0_are_the_two_arguments_they_are(qapp):
    """FlushEvents takes both event masks in one register.  A reader that
    took the whole of D0 twice would print a plausible line and say
    nothing about which events are being flushed."""
    g = Processor()
    g.cpu.set_d(0, 0x00020001)
    assert dispatch.shape(g, 0xA032)[2] == "FlushEvents(D0LowWord=1, D0HighWord=2)"


def test_a_register_an_os_trap_answers_with_is_not_an_argument(qapp):
    """`Out<...>` is where a routine leaves its answer, not something its
    caller passed.  DateToSeconds reads a DateTimeRecord and writes
    another: the line shows the one it reads."""
    g = Processor()
    g.cpu.set_a(0, 0x1000)
    assert dispatch.shape(g, 0xA9C7)[2] == "DateToSeconds(A0=$1000)"


def test_an_answer_sitting_in_a_register(qapp):
    """All of D0, or its high half as a signed short -- and a boolean kept
    the Mac way, where 0 is true, has to be said the way it means.  A
    trace with the sign wrong or a boolean the wrong way round is worse
    than one that says nothing."""
    g = Processor()
    g.cpu.set_d(0, 0xDEADBEEF)
    assert dispatch.result_text(g, sig("CompactMem", "l", "D0")) == "-559038737"
    assert dispatch.result_text(g, sig("CountADBs", "h", "D0")) == "-16657"
    assert dispatch.result_text(g, sig("OSEventAvail", "b", "D0Minus1Boolean")) == "0"
    g.cpu.set_d(0, 0)
    assert dispatch.result_text(g, sig("OSEventAvail", "b", "D0Minus1Boolean")) == "1"


def test_an_answer_left_on_the_stack_for_its_caller(qapp):
    """A Pascal function answers in the slot its caller left under the
    return address.  The sizes differ, and so do their signs; a Point is
    its v then its h, which is the order MacWrite's own coordinates come
    in, and the trace prints it the way a person reads a coordinate:
    h, v."""
    g = Processor()
    g.mem.w32(g.cpu.sp, 0xFFFF8000)
    assert dispatch.result_text(g, sig("BitAnd", "l")) == "-32768"
    g.mem.w16(g.cpu.sp, 0xFFF0)
    assert dispatch.result_text(g, sig("AngleFromSlope", "h")) == "-16"
    g.mem.w16(g.cpu.sp, 0x1234)                    # v, then h
    g.mem.w16(g.cpu.sp + 2, 0xFFF5)
    assert dispatch.result_text(g, sig("GetMouse", "P")) == "(-11,4660)"


def test_an_os_error_off_the_stack_is_a_signed_short(qapp):
    """The most common answer of all -- 318 routines return an OSErr this
    way -- and the one the trace got wrong for as long as it read the slot
    as a long: `-1` came out as the four billion its two unread bytes
    happened to hold.  `put_result` writes two bytes; the trace has to
    read the same two, signed, exactly as it does out of a register."""
    g = Processor()
    err = sig("BitMapToRegion", "e")
    g.mem.w32(g.cpu.sp, 0xDEADBEEF)                # the neighbour's bytes, unclean
    g.mem.w16(g.cpu.sp, 0xFFFF)                    # -1, as put_result leaves it
    assert dispatch.result_text(g, err) == "-1"
    g.mem.w16(g.cpu.sp, 0xFFE8)                    # -24, paramErr
    assert dispatch.result_text(g, err) == "-24"
    g.mem.w16(g.cpu.sp, 0)
    assert dispatch.result_text(g, err) == "0"     # and noErr is still noErr


def test_an_answer_that_cannot_be_read_is_a_question_mark(qapp):
    """The trace is written on the way past a trap that has already
    returned: a stack pointer the memory won't take stops the trace
    reading, and costs the line nothing but its value."""
    g = Processor()
    g.cpu.sp = 0xFFF00000                           # past the end of everything
    assert dispatch.result_text(g, sig("BitAnd", "l")) == "?"


def test_a_line_with_no_arguments_to_read(qapp):
    """Three traps whose lines carry no arguments, each said as `Name(?)`
    rather than as `Name()`, which would read as a routine called with
    nothing rather than as a call this tool can't see into: a trap word
    the interfaces don't describe, a handler that works the registers
    itself, and a stack it can't read from."""
    g = Processor()
    assert not dispatch.assigned(0xA819)
    assert dispatch.shape(g, 0xA819)[2] == f"{dispatch.trap_name(0xA819)}(?)"
    assert dispatch.shape(g, 0xA9F2)[2] == "Launch(?)"       # @raw, given a pointer
    g.cpu.sp = 0xFFF0000
    assert dispatch.shape(g, 0xA9AB)[2] == "AddResource(?, ?, ?, ?)"


def test_a_suppressed_trap_still_runs_and_writes_no_line(harness, caplog):
    """`--suppress-traps` is how hornet.sh keeps the WaitNextEvent lines
    from burying everything else: the trap goes on happening, and says
    nothing, while the traps around it are still traced."""
    import logging

    cap = []
    h = harness()
    f = open_application(APPS / "Hello.bin")
    proc = h.d.launch(f.name, None, app=f, trace=True, trace_suppress=("WaitNextEvent",))
    real = proc.runner._trace_line
    proc.runner._trace_line = cap.append
    with caplog.at_level(logging.DEBUG, logger="macrt.m68k"):
        h.wait_until(lambda: proc.windows and proc.windows[0].visible, timeout=10,
                     what="Hello's window")
    h.settle(400)                                   # many event loops' worth
    proc.runner._trace_line = real
    lines = [l for l in cap if "(" in l]              # `Name(args) -> value (...)`
    assert lines, "nothing was traced at all"
    assert not [l for l in lines if "trap WaitNextEvent" in l], lines[:3]
    assert len(lines) > 1, lines                    # and the rest still came


@pytest.mark.parametrize("trap,expect", [
    (0xA02E, "BlockMove"),                           # A0, A1, D0 and a bit of the word
    (0xA03B, "Delay"),                               # nothing to show: only an Out<
    (0xA8E8, "PtInRgn"),                             # answers a boolean
])
def test_a_traps_line_as_the_trace_writes_it(qapp, trap, expect):
    """The lines the trace is known by, at the trap words themselves
    rather than through the tables that generated them."""
    g = Processor()
    s, name, call = dispatch.shape(g, trap)
    assert name == expect and call.startswith(f"{expect}("), call
    assert isinstance(dispatch.result_text(g, s), str)


def test_a_package_shows_the_routine_its_selector_picked(qapp):
    """One trap word, many routines: D0 says which, and the trace line
    names that one and reads its arguments -- not the package's name with
    nothing in the brackets, which is what a trace before the selector was
    read used to say."""
    g = Processor()
    g.mem.w16(g.cpu.sp, 0)                            # GetStdFilterProc's answer slot
    g.cpu.set_d(0, 0x0203)                            # its selector
    s, name, call = dispatch.shape(g, 0xAA68)
    assert name == "GetStdFilterProc", call
    assert call.startswith("GetStdFilterProc("), call
    assert dispatch.result_text(g, s) == "0"          # an OSErr, from the stack slot
