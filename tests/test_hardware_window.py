"""The hardware window: the machine's screens, as their GDevices have them.

screen_info reads the DeviceList the way Color QuickDraw walks it -- the
main screen first, the others after it -- and takes each screen's facts
from its GDevice's gdRect and gdMode, and its PixMap's baseAddr, rowBytes
and pixelSize.  Its colour table is the PixMap's pmTable, read by entry,
and it's there only while the screen is in an indexed colour mode: a
direct device (16 or 32 bits a pixel) has none.

The window itself is checked the way the debugger's is: it shows what
screen_info says, and follows a change -- a palette entry set, a depth
changed -- on its next look, without redrawing while nothing has moved.
"""

from macrt.toolbox.system import System
from macrt.ui.hardware import HardwareWindow, screen_info


class Display:
    """The window's view of the machine: its System, and whether it's gone."""

    def __init__(self, system):
        self.system = system

    @property
    def shutting_down(self):
        return False


def info(system):
    return screen_info(Display(system))


def test_no_colour_quickdraw_has_no_look(qapp):
    """A Mac Plus has no colour QuickDraw, so no graphics devices, and no
    look to take: screen_info says so with None, not an empty list."""
    system = System((512, 342), model="plus")
    assert info(system) is None


def test_one_screen(qapp):
    """The main screen, as its GDevice and PixMap have it: its size, its
    mode, its depth, its place in the desktop, and where its buffer is."""
    system = System((640, 480), model="ii", depth=8)
    screens = info(system)
    assert len(screens) == 1
    s = screens[0]
    assert s.name == "main screen"
    assert (s.width, s.height) == (640, 480)
    assert s.depth == 8
    assert (s.top, s.left) == (0, 0)
    assert s.row_bytes == 640
    assert s.base == system.screen_bits.base
    # 8 bits: its pixels are the entries of a 256-colour table, white first
    # and black last, as the standard table has them.
    assert s.clut is not None and len(s.clut) == 256
    assert s.clut[0] == 0xFFFFFFFF
    assert s.clut[255] == 0xFF000000


def test_three_screens_side_by_side(qapp):
    """The other screens' gdRects sit round the main one's: the first to
    its left (the desktop reaches negative there), the rest to its right,
    and each has its card's buffer of its own, below the main one's in
    RAM."""
    system = System((512, 342), model="ii", depth=4, screens=3)
    screens = info(system)
    assert len(screens) == 3
    assert screens[0].left == 0
    assert screens[1].left == -512
    assert screens[2].left == 512
    # 4 bits: a 16-colour table, the standard 4-bit colours.
    assert all(s.clut is not None and len(s.clut) == 16 for s in screens)
    # Each buffer below the main one's, the same size.
    assert screens[0].base > screens[1].base > screens[2].base


def test_direct_device_has_no_table(qapp):
    """16 and 32 bits a pixel: the pixels are their colours, not indexes
    into a table, so there's no colour table to show."""
    for depth in (16, 32):
        system = System((640, 480), model="ii", depth=depth)
        s = info(system)[0]
        assert s.depth == depth
        assert s.clut is None


def test_one_bit_table(qapp):
    """One bit a pixel: two entries, white and black, as on a Mac."""
    system = System((512, 342), model="ii", depth=1)
    assert info(system)[0].clut == [0xFFFFFFFF, 0xFF000000]


def test_window_follows_a_palette_change(qapp):
    """The window shows what screen_info says, and follows a change on its
    next look: a palette entry set by the program is there when the window
    looks again."""
    system = System((512, 342), model="ii", depth=4, screens=3)
    window = HardwareWindow(Display(system))
    window.show()
    qapp.processEvents()
    assert window._shown is not None and len(window._shown) == 3

    # Entry 1's red, green and blue, set to white: the next look shows it.
    from macrt.toolbox.gdevice import GDevice, PixMap, deref
    gdh = system.devices.screens[0]
    pm = PixMap(system.mem, deref(system.mem, GDevice(system.mem, deref(system.mem, gdh)).pmap))
    p = deref(system.mem, pm.pm_table)
    for off in (18, 20, 22):          # entry 1's r, g, b (the entries start at p+8)
        system.mem.w16(p + off, 0xFFFF)
    window.refresh()
    qapp.processEvents()
    assert window._shown[0].clut[1] == 0xFFFFFFFF


def test_window_follows_a_depth_change(qapp):
    """A depth change rewrites the buffer and its table: the window's next
    look shows the new depth and the new table's size.  (8 bits wouldn't
    fit in a 4-bit buffer, so the change is to 2.)"""
    system = System((512, 342), model="ii", depth=4, screens=3)
    window = HardwareWindow(Display(system))
    window.show()
    qapp.processEvents()
    assert system.set_depth(2)
    window.refresh()
    qapp.processEvents()
    assert window._shown[0].depth == 2
    assert len(window._shown[0].clut) == 4


def test_no_redraw_while_unchanged(qapp):
    """A look that finds nothing new doesn't redraw: the window's panels
    are the same widgets, not rebuilt."""
    system = System((512, 342), model="ii", depth=8)
    window = HardwareWindow(Display(system))
    window.show()
    qapp.processEvents()
    panels = list(window._panels)
    window.refresh()
    qapp.processEvents()
    assert window._panels == panels
    assert all(a is b for a, b in zip(panels, window._panels))


def test_redraw_replaces_the_panels(qapp):
    """A look that finds a change rebuilds the panels: the old ones are
    gone, not left behind the new ones."""
    system = System((512, 342), model="ii", depth=4, screens=3)
    window = HardwareWindow(Display(system))
    window.show()
    qapp.processEvents()
    before = list(window._panels)
    from macrt.toolbox.gdevice import GDevice, PixMap, deref
    pm = PixMap(system.mem, deref(system.mem, GDevice(system.mem, deref(system.mem, system.devices.screens[0])).pmap))
    p = deref(system.mem, pm.pm_table)
    system.mem.w16(p + 18, 0xFFFF)      # entry 1's red, changed
    window.refresh()
    qapp.processEvents()
    assert window._panels != before
    assert not any(w in before for w in window._panels)
