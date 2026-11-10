"""The host's native menu bar: the front app's menus as real host menus, a
picked item turned into the guest's mouseDown-in-the-bar, and MenuSelect
answering with the item picked.  The native bar itself only exists on
macOS; these tests exercise the Display's translation of a pick into the
guest's menu flow, which is platform-independent."""

import sys

import pytest

from macrt.ui.display import DisplayConfig
from conftest import Recorder, hi_word

# The guest's File menu (conftest's Recorder): New, Close, (-, Quit.
FILE = Recorder.FILE
NEW, CLOSE, QUIT = 1, 2, 4


def bar_of(h):
    return h.d.native_menu.bar


def menu_of(h, title):
    return next(a.menu() for a in bar_of(h).actions() if a.text() == title)


def test_native_is_macos_only():
    if sys.platform == "darwin":
        DisplayConfig(menu_bar="native")          # no error on macOS
    else:
        with pytest.raises(ValueError, match="macOS"):
            DisplayConfig(menu_bar="native")
    # The other bars are always allowed.
    DisplayConfig(menu_bar="global")
    DisplayConfig(menu_bar="window")


def test_native_menu_shows_front_apps_menus(harness):
    h = harness(menu_bar="native")
    app = Recorder("App")
    h.launch(app)
    assert h.d.native_menu is not None
    titles = [a.text() for a in bar_of(h).actions()]
    assert "File" in titles and "Edit" in titles
    # No emulated menu bar strip on the screen.
    assert h.d.global_bar is None


def test_native_pick_reaches_the_guest(harness):
    h = harness(menu_bar="native")
    app = Recorder("App")
    proc = h.launch(app)
    before = len(proc.windows)
    # Pick File > New from the native bar; the guest's New makes a window.
    menu_of(h, "File").actions()[NEW - 1].trigger()
    h.wait_until(lambda: len(proc.windows) == before + 1, what="File > New's window")
    assert hi_word(app.last("MenuSelect")[1]) == FILE


def test_native_quit_closes_the_guest(harness):
    h = harness(menu_bar="native")
    app = Recorder("App")
    proc = h.launch(app)
    menu_of(h, "File").actions()[QUIT - 1].trigger()
    h.wait_until(lambda: proc not in h.d.processes, what="the app to quit")


def test_native_disabled_item_not_pickable(harness):
    h = harness(menu_bar="native")
    app = Recorder("App")
    h.launch(app)
    # Edit's first item (Undo) is disabled in conftest's Recorder.
    assert not menu_of(h, "Edit").actions()[0].isEnabled()


def test_native_bar_follows_the_front_app(harness):
    h = harness(menu_bar="native")
    p1 = h.launch(Recorder("First"))
    assert h.d.native_menu._last[0] == id(p1)
    p2 = h.launch(Recorder("Second"))
    # The second app is now in front; the bar was rebuilt for it.
    assert h.d.front_process is p2
    assert h.d.native_menu._last[0] == id(p2)


def test_native_desktop_mode(harness):
    # host="desktop" too: the native bar is the application's, not a
    # window's, so it works the same way there.
    h = harness(menu_bar="native", host="desktop")
    app = Recorder("App")
    proc = h.launch(app)
    before = len(proc.windows)
    menu_of(h, "File").actions()[NEW - 1].trigger()
    h.wait_until(lambda: len(proc.windows) == before + 1, what="File > New's window")
