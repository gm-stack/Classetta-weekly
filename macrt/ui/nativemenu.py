"""
macrt.ui.nativemenu - The front application's menus in the host's native
menu bar, instead of the one the host draws.

macOS only.  On macOS the system menu bar at the top of the screen belongs
to the frontmost application: while Classetta is active, its main menu is
shown, whichever of its windows the user is using.  So there is a single
native menu bar, on a host window of its own, showing the front app's menus
(the same menus the "global" bar would show), and it follows the front app
as the user switches windows.

The user picks an item from it; the Display turns that into a mouseDown in
the guest's menu bar, so the guest's usual FindWindow -> MenuSelect flow
runs unchanged, and MenuSelect answers with the item picked rather than
following the mouse.

The bar is rebuilt whenever the front app or its menus change (the
Display's refresh_bars).  Keyboard shortcuts are not set on the items: the
guest answers its own command keys through MenuKey, and a native shortcut
would double-fire with it.  Menus the app draws with its own MDEF, and the
MBDF's per-pull-down changes, are not reflected here: the native bar shows
the menu's items as they stand in the menu list.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMainWindow

from ..toolbox import constants as k
from ..toolbox.menus import MenuItem
from ..toolbox.qdraw import CHICAGO_SYMBOLS


class NativeMenuBar:
    """The host's native menu bar, showing the front app's menus."""

    def __init__(self, display):
        self.display = display
        # A native menu bar needs a window of its own to hang from.  On
        # macOS the bar is the application's main menu, shown whenever
        # Classetta is active — not this window's — so the window is kept
        # transparent, 1x1 and out of the focus chain: the bar shows at the
        # top of the screen, not in it, and it never takes focus from a game.
        self._host = QMainWindow()
        self._host.setWindowTitle("macrt")
        self._host.setWindowOpacity(0.0)
        self._host.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.bar = self._host.menuBar()
        self.bar.setNativeMenuBar(True)
        self._pending = None      # (process, MenuSelect's result) picked, not yet asked for
        self._last = None         # (process id, menu signature) the bar was last built for

    # -- the guest's menus, as native menus ---------------------------------
    def sync(self) -> None:
        """Rebuild the bar to match the front application's menus."""
        proc = self.display.front_process
        if proc is None:
            self._clear()
            self._last = None
            return
        menus = proc.bar_menus()
        key = (id(proc), self._signature(menus))
        if self._last == key:
            return
        self._last = key
        self._rebuild(proc, menus)

    @staticmethod
    def _signature(menus):
        return tuple(
            (m.id, m.title, tuple((it.text, it.cmd, it.mark, it.style, it.enabled, it.submenu)
                                  for it in m.items))
            for m in menus)

    def _rebuild(self, proc, menus) -> None:
        self._clear()
        for i, menu in enumerate(menus):
            # The guest's first menu is its Apple menu; on macOS the first
            # menu is the application's, named for the app.
            title = proc.name if (i == 0 and menu.is_apple) else self._title(menu)
            self._fill(self.bar.addMenu(title), proc, menu)

    def _title(self, menu) -> str:
        if menu.id == k.kHMHelpMenuID:
            return "Help"
        return menu.title.translate(CHICAGO_SYMBOLS) if menu.title else ""

    def _fill(self, qmenu, proc, menu) -> None:
        for i, item in enumerate(menu.items, start=1):
            if item.is_separator:
                qmenu.addSeparator()
                continue
            if item.submenu:
                sub = proc.menus.get(item.submenu)
                if sub is not None:
                    self._fill(qmenu.addMenu(item.shown), proc, sub)
                continue
            act = self._action(qmenu, item)
            act.triggered.connect(lambda _checked, m=menu.id, n=i: self._choose(m, n))
            qmenu.addAction(act)

    def _action(self, parent, item: MenuItem) -> QAction:
        # Parented to its menu: an unparented QAction is collected when the
        # next item's replaces it, and the menu loses it.
        act = QAction(item.shown, parent)
        act.setEnabled(item.enabled)
        if item.mark == k.checkMark:
            act.setCheckable(True)
            act.setChecked(True)
        elif item.mark == k.diamondMark:
            act.setText("◆ " + item.shown)
        return act

    def _clear(self) -> None:
        self.bar.clear()

    # -- a native item picked ----------------------------------------------
    def _choose(self, menu_id: int, item: int) -> None:
        proc = self.display.front_process
        if proc is None:
            return
        result = (menu_id & 0xFFFF) << 16 | item
        self._pending = (proc, result)
        self.display.native_menu_choice(proc, result)

    def take_pending(self, proc):
        """The result picked for proc's next MenuSelect, or None."""
        if self._pending is not None and self._pending[0] is proc:
            self._pending, result = None, self._pending[1]
            return result
        return None

    def show(self) -> None:
        self._host.resize(1, 1)
        self._host.move(0, 0)
        self._host.show()

    def close(self) -> None:
        self._host.close()
        self._host.deleteLater()
