"""
macrt.ui.nativemenu - The front application's menus in the host's native
menu bar, instead of the one the host draws.

macOS only.  On macOS the system menu bar at the top of the screen belongs
to the key window: whichever of Classetta's windows is active, its menu bar
is the one shown.  So each top-level window carries a native menu bar for
its application, and the one the user is using is the one macOS shows.  With
host="screen" that's a single bar on the screen window, following the front
app; with host="desktop" it's one per window, each showing its own app's
menus.

The user picks an item from it; the Display turns that into a mouseDown in
the guest's menu bar, so the guest's usual FindWindow -> MenuSelect flow
runs unchanged, and MenuSelect answers with the item picked rather than
following the mouse.

The bar is kept in step with the app's menus by the Display's refresh_bars.
A native menu bar doesn't survive being cleared and rebuilt while it's in
use — the items vanish — so a change that only alters an item's state (its
text, whether it's enabled, its check) is applied in place; only a change
to the menus' shape (how many there are, their titles, which items are lines
or submenus) rebuilds the bar.  Items are given no macOS role, so none of
them is moved out of the menu it's in.  Keyboard shortcuts are not set: the
guest answers its own command keys through MenuKey, and a native shortcut
would double-fire with it.  Menus the app draws with its own MDEF, and the
MBDF's per-pull-down changes, are not reflected here: the native bar shows
the menu's items as they stand in the menu list.
"""

from __future__ import annotations

from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMenuBar

from ..toolbox import constants as k
from ..toolbox.menus import MenuItem
from ..toolbox.qdraw import CHICAGO_SYMBOLS


class NativeMenuBar:
    """A native menu bar on one top-level window, showing a process's menus."""

    def __init__(self, display, widget):
        self.display = display
        self.widget = widget
        self.bar = QMenuBar(widget)
        self.bar.setNativeMenuBar(True)
        self.proc = None
        self._struct = None       # the menus' shape the bar was last built for

    # -- keeping the bar in step with the app's menus -----------------------
    def sync(self, proc) -> None:
        self.proc = proc
        if proc is None:
            self._clear()
            self._struct = None
            return
        menus = proc.bar_menus()
        struct = self._structure(menus)
        if struct == self._struct:
            # Same shape: only an item's state has changed, so update it in
            # place.  A native bar can't be cleared and rebuilt while it's
            # in use — the items would vanish.
            self._update_state(proc, menus)
            return
        self._struct = struct
        self._rebuild(proc, menus)

    @staticmethod
    def _structure(menus):
        """What the bar's shape depends on: how many menus, and in each,
        which items are lines and which open submenus.  An item's text,
        enabled state and check are state, not shape."""
        return tuple(
            (m.id, tuple((it.is_separator, it.submenu) for it in m.items))
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
                # A submenu the list doesn't hold: a dead item, added so the
                # bar's items stay in step with the menu's for in-place updates.
                act = self._action(qmenu, item)
                act.setEnabled(False)
                qmenu.addAction(act)
                continue
            act = self._action(qmenu, item)
            act.triggered.connect(lambda _checked, m=menu.id, n=i: self._choose(m, n))
            qmenu.addAction(act)

    def _action(self, parent, item: MenuItem) -> QAction:
        # Parented to its menu: an unparented QAction is collected when the
        # next item's replaces it, and the menu loses it.  No role, so macOS
        # leaves it in the menu it's in rather than moving an "About", "Quit"
        # or "Preferences" item into the application's own menu.
        act = QAction(item.shown, parent)
        act.setMenuRole(QAction.MenuRole.NoRole)
        act.setEnabled(item.enabled)
        if item.mark == k.checkMark:
            act.setCheckable(True)
            act.setChecked(True)
        elif item.mark == k.diamondMark:
            act.setText("◆ " + item.shown)
        return act

    def _clear(self) -> None:
        # clear() detaches the menus but leaves them alive; let them go.
        for a in self.bar.actions():
            if a.menu() is not None:
                a.menu().deleteLater()
        self.bar.clear()

    # -- a state-only change: update the items in place ---------------------
    def _update_state(self, proc, menus) -> None:
        qmenus = [a.menu() for a in self.bar.actions() if a.menu() is not None]
        for i, (qm, menu) in enumerate(zip(qmenus, menus)):
            title = proc.name if (i == 0 and menu.is_apple) else self._title(menu)
            if qm.title() != title:
                qm.setTitle(title)
            self._update_items(qm, proc, menu)

    def _update_items(self, qm, proc, menu) -> None:
        qactions = qm.actions()
        for i, item in enumerate(menu.items):
            if i >= len(qactions):
                break
            act = qactions[i]
            if item.is_separator:
                continue
            if item.submenu:
                sub = proc.menus.get(item.submenu)
                if sub is not None and act.menu() is not None:
                    if act.menu().title() != item.shown:
                        act.menu().setTitle(item.shown)
                    self._update_items(act.menu(), proc, sub)
                continue
            self._set_item_state(act, item)

    def _set_item_state(self, act: QAction, item: MenuItem) -> None:
        text = ("◆ " if item.mark == k.diamondMark else "") + item.shown
        if act.text() != text:
            act.setText(text)
        act.setEnabled(item.enabled)
        if item.mark == k.checkMark:
            if not act.isCheckable():
                act.setCheckable(True)
            act.setChecked(True)
        elif act.isCheckable():
            act.setCheckable(False)

    # -- a native item picked ----------------------------------------------
    def _choose(self, menu_id: int, item: int) -> None:
        if self.proc is None:
            return
        result = (menu_id & 0xFFFF) << 16 | item
        self.display.native_menu_choice(self.proc, result)

    def close(self) -> None:
        self.bar.deleteLater()
