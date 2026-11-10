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
    # host=screen: the one bar, on the screen window.
    return h.d.native_bars[h.d.host_window].bar


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
    assert h.d.host_window in h.d.native_bars
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
    first = Recorder("First")
    h.launch(first)
    # Give the first app a menu the second won't have, so the bar's shape
    # tells us which app it's showing.
    extra = first.tb.NewMenu(140, "Extra")
    first.tb.AppendMenu(extra, "Something")
    first.tb.InsertMenu(extra, 0)
    first.tb.DrawMenuBar()
    assert "Extra" in [a.text() for a in bar_of(h).actions()]
    # A second app comes to the front; the bar must now show its menus,
    # which have no "Extra".
    h.launch(Recorder("Second"))
    assert "Extra" not in [a.text() for a in bar_of(h).actions()]


def test_native_state_change_updates_in_place(harness):
    # The bug: a change to an item's state (enabled, checked, text) must
    # not clear and rebuild the bar — a native menu bar loses its items
    # when it's rebuilt while in use.
    h = harness(menu_bar="native")
    app = Recorder("App")
    h.launch(app)
    edit = menu_of(h, "Edit")
    edit_qm = edit                      # hold the QMenu; a rebuild drops it
    # Rename an Edit item: a state change, not a shape change.
    app.tb.SetItem(app.tb.GetMHandle(Recorder.EDIT), 1, "Undo Typing")
    # The same QMenu is still in the bar (no rebuild), and the item's text
    # is the new one.
    assert edit_qm in [a.menu() for a in bar_of(h).actions()]
    assert edit.actions()[0].text() == "Undo Typing"


def test_native_items_have_no_macos_role(harness):
    # The bug: by default Qt gives items a heuristic role, and macOS moves
    # an "About"/"Preferences"/"Quit" item out of its menu into the app's
    # own menu — so the guest's Apple menu items disappear from it.
    h = harness(menu_bar="native")
    app = Recorder("App")
    h.launch(app)
    from PyQt6.QtGui import QAction
    for top in bar_of(h).actions():
        for act in top.menu().actions():
            if not act.isSeparator():
                assert act.menuRole() == QAction.MenuRole.NoRole


def test_native_desktop_mode(harness):
    # host="desktop": there's no screen window, so each window carries its
    # own app's native bar.
    h = harness(menu_bar="native", host="desktop")
    app = Recorder("App")
    proc = h.launch(app)
    before = len(proc.windows)
    view = h.d.views[proc.windows[0]]
    bar = h.d.native_bars[view].bar
    file = next(a.menu() for a in bar.actions() if a.text() == "File")
    file.actions()[NEW - 1].trigger()
    h.wait_until(lambda: len(proc.windows) == before + 1, what="File > New's window")
    assert hi_word(app.last("MenuSelect")[1]) == FILE
