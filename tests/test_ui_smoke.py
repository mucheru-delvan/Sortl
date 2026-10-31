"""Drives the real GTK window headlessly. Skipped when GTK/a display is absent.

Run with:  xvfb-run -a env GSK_RENDERER=cairo pytest tests/test_ui_smoke.py
"""

import time
from collections.abc import Callable
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")

try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk
except (ValueError, ImportError):
    pytest.skip("GTK4/libadwaita typelibs not installed", allow_module_level=True)

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)

from sortl.ui.window import SortlWindow  # noqa: E402


def pump(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met while pumping main loop")
        context.iteration(False)
        time.sleep(0.005)


@pytest.fixture(scope="module")
def app() -> Adw.Application:
    application = Adw.Application(application_id="io.github.mucheru_delvan.SortlTest")
    application.register(None)
    return application


@pytest.fixture
def messy(tmp_path: Path) -> Path:
    root = tmp_path / "messy"
    root.mkdir()
    (root / "R&D <draft>.pdf").write_bytes(b"markup-hostile name")
    (root / "photo.jpg").write_bytes(b"jpg")
    (root / "song.mp3").write_bytes(b"mp3")
    (root / "mystery").write_bytes(b"no extension")
    return root


def make_window(app: Adw.Application, tmp_path: Path) -> SortlWindow:
    window = SortlWindow(application=app, journal_path=tmp_path / "j" / "journal.db")
    window.present()
    return window


def test_full_flow(app: Adw.Application, tmp_path: Path, messy: Path) -> None:
    window = make_window(app, tmp_path)
    assert window.current_page == "start"

    window.start_scan(messy)
    assert window.current_page == "scanning"
    pump(lambda: window.current_page == "preview")
    assert window._organize_button.get_label() == "Organize 4 Files"
    assert window._organize_button.get_sensitive()
    assert not window._preview_banner.get_revealed()

    window.organize()
    assert window.current_page == "running"
    pump(lambda: window.current_page == "done")

    assert window._done_page.get_title() == "Done"
    assert (messy / "Documents" / "R&D <draft>.pdf").exists()
    assert (messy / "Images" / "photo.jpg").exists()
    assert (messy / "Audio" / "song.mp3").exists()
    assert (messy / "Other" / "mystery").exists()
    assert not (messy / "photo.jpg").exists()

    window.show_history()
    pump(lambda: window.current_page == "history")
    assert len(window._history_rows) == 4
    assert not window._history_banner.get_revealed()

    window.check_integrity()
    pump(lambda: window._integrity_button.get_sensitive())
    window.destroy()


def test_second_scan_shows_conflicts_and_skips(
    app: Adw.Application, tmp_path: Path, messy: Path
) -> None:
    (messy / "Images").mkdir()
    (messy / "Images" / "photo.jpg").write_bytes(b"already here")

    window = make_window(app, tmp_path)
    window.start_scan(messy)
    pump(lambda: window.current_page == "preview")

    assert window._preview_banner.get_revealed()
    assert window._organize_button.get_label() == "Organize 3 Files"

    window.organize()
    pump(lambda: window.current_page == "done")
    assert (messy / "Images" / "photo.jpg").read_bytes() == b"already here"
    assert (messy / "photo.jpg").read_bytes() == b"jpg"
    assert "skipped" in window._done_page.get_description()
    window.destroy()


def test_empty_folder_disables_organize(
    app: Adw.Application, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    window = make_window(app, tmp_path)
    window.start_scan(empty)
    pump(lambda: window.current_page == "preview")

    assert not window._organize_button.get_sensitive()
    window.destroy()


def test_unsupported_filesystem_is_reported_not_attempted(
    app: Adw.Application,
    tmp_path: Path,
    messy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import errno

    import sortl.service as service

    def no_link(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "nope")

    window = make_window(app, tmp_path)
    window.start_scan(messy)
    pump(lambda: window.current_page == "preview")

    monkeypatch.setattr(service.os, "link", no_link)
    window.organize()
    pump(lambda: window.current_page == "done")

    assert window._done_page.get_title() == "Can't Organize This Folder"
    assert "hard links" in window._done_page.get_description()
    assert (messy / "photo.jpg").exists()
    window.destroy()


def test_history_empty_state(app: Adw.Application, tmp_path: Path) -> None:
    window = make_window(app, tmp_path)
    window.show_history()
    pump(lambda: window.current_page == "history")

    assert window._history_empty.get_visible()
    assert not window._history_scroller.get_visible()
    window.destroy()


def test_every_icon_name_exists_in_the_adwaita_theme() -> None:
    """A missing icon name silently renders a broken-image placeholder."""
    import re

    import sortl.ui.window as window_module
    from gi.repository import Gdk

    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    if not theme.has_icon("folder-symbolic"):
        pytest.skip("Adwaita icon theme not installed in this environment")

    source = Path(window_module.__file__).read_text()
    names = set(re.findall(r'"([a-z0-9-]+-symbolic)"', source))

    assert len(names) > 10
    assert [n for n in sorted(names) if not theme.has_icon(n)] == []


def test_scan_of_a_missing_folder_returns_to_start(
    app: Adw.Application, tmp_path: Path
) -> None:
    window = make_window(app, tmp_path)
    window.start_scan(tmp_path / "does-not-exist")
    assert window.current_page == "scanning"

    pump(lambda: window.current_page == "start")

    assert window._plan is None
    window.destroy()


def test_portal_only_folder_is_refused_before_scanning(
    app: Adw.Application, tmp_path: Path
) -> None:
    window = make_window(app, tmp_path)

    window.open_folder(Path("/run/flatpak/doc/abc123/Downloads"))

    assert window.current_page == "start"
    assert window._plan is None
    window.destroy()


def test_direct_folder_is_scanned(
    app: Adw.Application, tmp_path: Path, messy: Path
) -> None:
    window = make_window(app, tmp_path)

    window.open_folder(messy)
    pump(lambda: window.current_page == "preview")

    assert window._plan is not None
    window.destroy()
