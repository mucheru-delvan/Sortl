"""Adw.Application: lifecycle, actions and the About dialog."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from sortl import APP_ID, APP_NAME, __version__
from sortl.ui.window import SortlWindow

WEBSITE = "https://github.com/mucheru-delvan/Sortl"


def default_journal_path() -> Path:
    """Journal location. Inside Flatpak this is ~/.var/app/<id>/data/sortl/."""
    return Path(GLib.get_user_data_dir()) / "sortl" / "journal.sqlite3"


class SortlApplication(Adw.Application):
    def __init__(self, journal_path: Path | None = None) -> None:
        super().__init__(
            application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS
        )
        self._journal_path = journal_path or default_journal_path()

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit_gracefully())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about)
        self.add_action(about_action)

    def quit_gracefully(self) -> None:
        # Route through the window so a run in progress finishes its current
        # file instead of being killed mid-move.
        window = self.props.active_window
        if window is not None:
            window.close()
        else:
            self.quit()

    def do_activate(self) -> None:
        window = self.props.active_window or SortlWindow(
            application=self, journal_path=self._journal_path
        )
        window.present()

    def _on_about(self, *_args: object) -> None:
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            developer_name="Delvan Mucheru",
            developers=["Delvan Mucheru"],
            version=__version__,
            copyright="© 2026 Delvan Mucheru",
            license_type=Gtk.License.MIT_X11,
            website=WEBSITE,
            issue_url=f"{WEBSITE}/issues",
        )
        about.present(self.props.active_window)


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    GLib.set_application_name(APP_NAME)
    return SortlApplication().run(sys.argv)
