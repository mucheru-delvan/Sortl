"""Sortl: organize a folder into category folders, safely and verifiably."""

APP_ID = "io.github.mucheru_delvan.Sortl"
APP_NAME = "Sortl"
__version__ = "0.1.0"


def main() -> int:
    """Launch the GTK application."""
    from sortl.ui.app import run

    return run()
