"""Main window: choose folder, preview, organize, review history."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from sortl import APP_ID
from sortl.journal import JournalEntry
from sortl.models import OperationStatus
from sortl.planner import Plan
from sortl.recovery import RecoveryFinding
from sortl.service import (
    ATTENTION_STATUSES,
    FolderCheckError,
    RunSummary,
    count_needing_attention,
    group_by_category,
    inspect_history,
    load_history,
    run_plan,
    scan_folder,
)
from sortl.ui.threads import call_in_main, run_in_thread

log = logging.getLogger("sortl")

# Rendering thousands of rows would freeze the UI; the data is never truncated,
# only what we draw.
MAX_FILE_ROWS_PER_CATEGORY = 100
MAX_HISTORY_ROWS = 200

CATEGORY_ICONS = {
    "Images": "image-x-generic-symbolic",
    "Documents": "x-office-document-symbolic",
    "Spreadsheets": "x-office-spreadsheet-symbolic",
    "Archives": "package-x-generic-symbolic",
    "Audio": "audio-x-generic-symbolic",
    "Videos": "video-x-generic-symbolic",
    "Code": "utilities-terminal-symbolic",
    "Other": "folder-symbolic",
}

STATUS_LABELS = {
    OperationStatus.PLANNED: "Planned",
    OperationStatus.IN_PROGRESS: "In progress",
    OperationStatus.COMPLETED: "Moved",
    OperationStatus.ROLLED_BACK: "Rolled back",
    OperationStatus.RECOVERY_REQUIRED: "Needs attention",
}


def plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _format_time(iso_timestamp: str) -> str:
    try:
        return datetime.fromisoformat(iso_timestamp).astimezone().strftime(
            "%Y-%m-%d %H:%M"
        )
    except ValueError:
        return iso_timestamp


def _row(title: str, subtitle: str = "") -> Adw.ActionRow:
    """ActionRow for user data. Markup is off: file names may contain & or <."""
    row = Adw.ActionRow(title=title, subtitle=subtitle, use_markup=False)
    row.set_title_lines(1)
    return row


class SortlWindow(Adw.ApplicationWindow):
    def __init__(self, *, application: Adw.Application, journal_path: Path):
        super().__init__(
            application=application,
            title="Sortl",
            default_width=720,
            default_height=640,
        )
        self.set_size_request(360, 360)

        self._journal_path = Path(journal_path)
        self._plan: Plan | None = None
        self._cancel = threading.Event()
        self._running = False
        self._close_when_idle = False
        self._return_page = "start"
        self._preview_groups: list[Adw.PreferencesGroup] = []
        self._history_rows: list[Gtk.Widget] = []
        self._history_entries: tuple[JournalEntry, ...] = ()

        self._build_chrome()
        self._build_start_page()
        self._build_scanning_page()
        self._build_preview_page()
        self._build_running_page()
        self._build_done_page()
        self._build_history_page()

        self.connect("close-request", self._on_close_request)
        self._show("start")

    # ------------------------------------------------------------- chrome --

    def _build_chrome(self) -> None:
        self._toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        self._back_button = Gtk.Button(
            icon_name="go-previous-symbolic", tooltip_text="Back", visible=False
        )
        self._back_button.connect("clicked", lambda _b: self._show(self._return_page))
        header.pack_start(self._back_button)

        menu = Gio.Menu()
        menu.append("About Sortl", "app.about")
        menu.append("Quit", "app.quit")
        menu_button = Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            menu_model=menu,
            primary=True,
            tooltip_text="Main Menu",
        )
        header.pack_end(menu_button)

        self._history_button = Gtk.Button(
            icon_name="document-open-recent-symbolic",
            tooltip_text="History",
        )
        self._history_button.connect("clicked", lambda _b: self.show_history())
        header.pack_end(self._history_button)

        toolbar.add_top_bar(header)
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        toolbar.set_content(self._stack)
        self._toasts.set_child(toolbar)
        self.set_content(self._toasts)

    def _add_page(self, name: str, widget: Gtk.Widget) -> None:
        self._stack.add_named(widget, name)

    def _show(self, name: str) -> None:
        self._stack.set_visible_child_name(name)
        self._back_button.set_visible(name == "history")
        self._history_button.set_sensitive(name not in ("scanning", "running"))

    @property
    def current_page(self) -> str:
        return self._stack.get_visible_child_name() or ""

    def _toast(self, message: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=message, use_markup=False))

    # --------------------------------------------------------- start page --

    def _build_start_page(self) -> None:
        theme = Gtk.IconTheme.get_for_display(self.get_display())
        icon = APP_ID if theme.has_icon(APP_ID) else "folder-symbolic"

        button = Gtk.Button(
            label="Choose Folder…",
            halign=Gtk.Align.CENTER,
            css_classes=["suggested-action", "pill"],
        )
        button.connect("clicked", lambda _b: self.choose_folder())

        page = Adw.StatusPage(
            icon_name=icon,
            title="Organize a Folder",
            description=(
                "Sortl sorts the files in a folder into category folders. "
                "You preview every move before anything changes."
            ),
            child=button,
        )
        self._add_page("start", page)

    def choose_folder(self) -> None:
        dialog = Gtk.FileDialog(title="Choose a Folder to Organize")
        dialog.select_folder(self, None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error as exc:
            quark = Gtk.dialog_error_quark()
            if exc.matches(quark, Gtk.DialogError.DISMISSED) or exc.matches(
                quark, Gtk.DialogError.CANCELLED
            ):
                return
            self._toast("Couldn't open that folder.")
            return

        path = folder.get_path() if folder is not None else None
        log.info("Folder chosen: %s", path)
        if path is None:
            self._toast("That location isn't a local folder.")
            return

        self.start_scan(Path(path))

    # ------------------------------------------------------ scanning page --

    def _build_scanning_page(self) -> None:
        spinner = Gtk.Spinner(
            spinning=True, width_request=32, height_request=32, halign=Gtk.Align.CENTER
        )
        page = Adw.StatusPage(
            title="Scanning Folder…",
            description="Reading files to build a preview. Nothing is changed.",
            child=spinner,
        )
        self._add_page("scanning", page)

    def start_scan(self, root: Path) -> None:
        log.info("Scanning %s", root)
        self._show("scanning")
        run_in_thread(
            lambda: scan_folder(root), self._on_scan_done, self._on_scan_error
        )

    def _on_scan_error(self, exc: Exception) -> None:
        log.error("Scan failed: %r", exc, exc_info=exc)
        self._show("start")
        detail = str(exc) or exc.__class__.__name__
        self._toast(f"Couldn't scan that folder: {detail[:200]}")

    def _on_scan_done(self, plan: Plan) -> None:
        self._plan = plan
        self._populate_preview(plan)
        self._show("preview")

    # ------------------------------------------------------- preview page --

    def _build_preview_page(self) -> None:
        self._preview_banner = Adw.Banner(title="", revealed=False)
        self._preview_page = Adw.PreferencesPage(vexpand=True)

        again = Gtk.Button(label="Choose Another Folder", css_classes=["flat"])
        again.connect("clicked", lambda _b: self.choose_folder())
        self._organize_button = Gtk.Button(
            label="Organize", css_classes=["suggested-action"]
        )
        self._organize_button.connect("clicked", lambda _b: self.organize())

        bar = Gtk.ActionBar()
        bar.pack_start(again)
        bar.pack_end(self._organize_button)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self._preview_banner)
        box.append(self._preview_page)
        box.append(bar)
        self._add_page("preview", box)

    def _populate_preview(self, plan: Plan) -> None:
        for group in self._preview_groups:
            self._preview_page.remove(group)
        self._preview_groups.clear()

        movable = [op for op in plan.operations if not op.conflict]
        conflicts = [op for op in plan.operations if op.conflict]
        categories = group_by_category(movable)

        summary = Adw.PreferencesGroup()
        folder_row = _row("Folder", str(plan.root))
        folder_row.set_subtitle_selectable(True)
        summary.add(folder_row)
        if movable:
            text = (
                f"{plural(len(movable), 'file', 'files')} into "
                f"{plural(len(categories), 'folder', 'folders')}"
            )
        else:
            text = "Nothing to organize"
        summary.add(
            _row(
                "Summary",
                text + ". Only files directly inside this folder are "
                "moved; subfolders are left alone.",
            )
        )
        self._add_group(summary)

        if categories:
            group = Adw.PreferencesGroup(title="Will Be Organized")
            for name, operations in categories.items():
                expander = Adw.ExpanderRow(
                    title=name,
                    subtitle=plural(len(operations), "file", "files"),
                    use_markup=False,
                )
                expander.add_prefix(
                    Gtk.Image.new_from_icon_name(
                        CATEGORY_ICONS.get(name, "folder-symbolic")
                    )
                )
                for operation in operations[:MAX_FILE_ROWS_PER_CATEGORY]:
                    expander.add_row(_row(operation.source.name))
                hidden = len(operations) - MAX_FILE_ROWS_PER_CATEGORY
                if hidden > 0:
                    expander.add_row(_row(f"…and {hidden} more"))
                group.add(expander)
            self._add_group(group)

        if conflicts:
            group = Adw.PreferencesGroup(
                title="Will Be Skipped",
                description="A file with the same name already exists in "
                "the destination folder. Sortl never overwrites files.",
            )
            for operation in conflicts[:MAX_FILE_ROWS_PER_CATEGORY]:
                group.add(_row(operation.source.name))
            self._add_group(group)

        if plan.errors:
            group = Adw.PreferencesGroup(title="Couldn't Be Read")
            for error in plan.errors[:MAX_FILE_ROWS_PER_CATEGORY]:
                group.add(_row(error.source.name, error.message))
            self._add_group(group)

        skipped = len(conflicts) + len(plan.errors)
        self._preview_banner.set_revealed(skipped > 0)
        self._preview_banner.set_title(
            f"{plural(skipped, 'file', 'files')} will be skipped."
        )
        self._organize_button.set_label(
            f"Organize {plural(len(movable), 'File', 'Files')}"
            if movable
            else "Organize"
        )
        self._organize_button.set_sensitive(bool(movable))

    def _add_group(self, group: Adw.PreferencesGroup) -> None:
        self._preview_page.add(group)
        self._preview_groups.append(group)

    # -------------------------------------------------------- running page --

    def _build_running_page(self) -> None:
        self._progress = Gtk.ProgressBar(width_request=280)
        self._progress_label = Gtk.Label(
            ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=40
        )
        self._cancel_button = Gtk.Button(label="Stop", halign=Gtk.Align.CENTER)
        self._cancel_button.connect("clicked", lambda _b: self._request_cancel())

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER
        )
        box.append(self._progress)
        box.append(self._progress_label)
        box.append(self._cancel_button)

        page = Adw.StatusPage(
            title="Organizing…",
            description="Every move is verified and recorded before the next.",
            child=box,
        )
        self._add_page("running", page)

    def organize(self) -> None:
        if self._plan is None or self._running:
            return

        plan = self._plan
        log.info("Organizing %s (%d operations)", plan.root, len(plan.operations))
        self._running = True
        self._cancel.clear()
        self._progress.set_fraction(0.0)
        self._progress_label.set_text("Starting…")
        self._cancel_button.set_sensitive(True)
        self._cancel_button.set_label("Stop")
        self._show("running")

        def progress(done: int, total: int, path: Path) -> None:
            call_in_main(self._on_progress, done, total, path.name)

        run_in_thread(
            lambda: run_plan(
                plan, self._journal_path, progress=progress, cancel=self._cancel
            ),
            self._on_run_done,
            self._on_run_error,
            daemon=False,
        )

    def _on_progress(self, done: int, total: int, name: str) -> None:
        self._progress.set_fraction(done / total if total else 1.0)
        self._progress_label.set_text(f"{done} of {total}: {name}")

    def _request_cancel(self) -> None:
        self._cancel.set()
        self._cancel_button.set_sensitive(False)
        self._cancel_button.set_label("Stopping…")

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        if not self._running:
            return False
        # Never abandon a move mid-flight: finish the current file, then close.
        self._close_when_idle = True
        self._request_cancel()
        self._progress_label.set_text("Finishing the current file…")
        return True

    def _on_run_error(self, exc: Exception) -> None:
        log.error("Run failed: %r", exc, exc_info=exc)
        self._running = False
        if isinstance(exc, FolderCheckError):
            self._set_done(
                "dialog-error-symbolic", "Can't Organize This Folder", str(exc)
            )
        else:
            self._set_done(
                "dialog-error-symbolic",
                "Something Went Wrong",
                f"Sortl stopped before moving anything it wasn't sure about "
                f"({exc.__class__.__name__}). Open History to check.",
            )
        self._finish_run()

    def _on_run_done(self, summary: RunSummary) -> None:
        self._running = False
        self._describe_summary(summary)
        self._finish_run()

    def _finish_run(self) -> None:
        self._show("done")
        if self._close_when_idle:
            self.close()

    # ---------------------------------------------------------- done page --

    def _build_done_page(self) -> None:
        again = Gtk.Button(
            label="Organize Another Folder",
            halign=Gtk.Align.CENTER,
            css_classes=["suggested-action", "pill"],
        )
        again.connect("clicked", lambda _b: self.choose_folder())
        history = Gtk.Button(
            label="View History", halign=Gtk.Align.CENTER, css_classes=["flat"]
        )
        history.connect("clicked", lambda _b: self.show_history())

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.CENTER
        )
        box.append(again)
        box.append(history)

        self._done_page = Adw.StatusPage(child=box)
        self._add_page("done", self._done_page)

    def _set_done(self, icon: str, title: str, description: str) -> None:
        self._done_page.set_icon_name(icon)
        self._done_page.set_title(title)
        self._done_page.set_description(description)

    def _describe_summary(self, summary: RunSummary) -> None:
        notes: list[str] = []
        if summary.skipped_conflicts:
            notes.append(
                f"{plural(len(summary.skipped_conflicts), 'file was', 'files were')} "
                "skipped because the name already existed"
            )
        if summary.blocked:
            notes.append(
                f"{plural(summary.blocked, 'file was', 'files were')} left alone "
                "because it changed after the preview"
            )
        if summary.planning_errors:
            notes.append(
                f"{plural(len(summary.planning_errors), 'file', 'files')} "
                "couldn't be read"
            )
        extra = (". " + "; ".join(notes) + ".") if notes else ""

        if summary.halted:
            self._set_done(
                "dialog-warning-symbolic",
                "Stopped: Needs Attention",
                f"{plural(summary.completed, 'file', 'files')} moved before "
                "something unexpected happened. No file was deleted. Open "
                "History and choose Check Integrity to see what is safe.",
            )
        elif summary.cancelled:
            self._set_done(
                "process-stop-symbolic",
                "Stopped",
                f"{summary.completed} of {summary.total} files moved. The "
                f"rest were left untouched{extra}",
            )
        else:
            self._set_done(
                "emblem-ok-symbolic",
                "Done",
                f"{plural(summary.completed, 'file', 'files')} organized into "
                f"{plural(summary.categories_used, 'folder', 'folders')}{extra or '.'}",
            )

    # ------------------------------------------------------- history page --

    def _build_history_page(self) -> None:
        self._history_banner = Adw.Banner(
            title="", button_label="Check Integrity", revealed=False
        )
        self._history_banner.connect(
            "button-clicked", lambda _b: self.check_integrity()
        )

        self._history_list = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"]
        )
        self._history_empty = Adw.StatusPage(
            icon_name="document-open-recent-symbolic",
            title="No History Yet",
            description="Moves you make with Sortl will be recorded here.",
            vexpand=True,
        )
        clamp = Adw.Clamp(
            maximum_size=640,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
            child=self._history_list,
        )
        self._history_scroller = Gtk.ScrolledWindow(vexpand=True, child=clamp)

        self._integrity_button = Gtk.Button(label="Check Integrity")
        self._integrity_button.connect("clicked", lambda _b: self.check_integrity())
        self._history_bar = Gtk.ActionBar()
        self._history_bar.pack_end(self._integrity_button)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self._history_banner)
        box.append(self._history_empty)
        box.append(self._history_scroller)
        box.append(self._history_bar)
        self._add_page("history", box)

    def show_history(self) -> None:
        current = self.current_page
        if current != "history":
            self._return_page = current or "start"
        run_in_thread(
            lambda: load_history(self._journal_path),
            self._on_history_loaded,
            lambda exc: self._toast("Couldn't read the history."),
        )

    def _on_history_loaded(self, entries: tuple[JournalEntry, ...]) -> None:
        self._history_entries = entries
        self._render_history(entries, {})
        self._show("history")

    def _render_history(
        self,
        entries: tuple[JournalEntry, ...],
        findings: dict[str, RecoveryFinding],
    ) -> None:
        for row in self._history_rows:
            self._history_list.remove(row)
        self._history_rows.clear()

        has_entries = bool(entries)
        self._history_empty.set_visible(not has_entries)
        self._history_scroller.set_visible(has_entries)
        self._history_bar.set_visible(has_entries)

        attention = count_needing_attention(entries)
        self._history_banner.set_revealed(attention > 0)
        self._history_banner.set_title(
            f"{plural(attention, 'operation needs', 'operations need')} attention."
        )

        for entry in entries[:MAX_HISTORY_ROWS]:
            finding = findings.get(entry.operation_id)
            lines = [
                f"{STATUS_LABELS.get(entry.status, entry.status.value)}"
                f" · {_format_time(entry.created_at)}"
            ]
            if finding is not None:
                lines.append(finding.message)
            elif entry.error:
                lines.append(entry.error)

            row = _row(entry.destination.name, "\n".join(lines))
            row.set_tooltip_text(f"{entry.source}\n→ {entry.destination}")
            if entry.status in ATTENTION_STATUSES:
                warning = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
                warning.add_css_class("warning")
                row.add_suffix(warning)
            self._history_list.append(row)
            self._history_rows.append(row)

        hidden = len(entries) - MAX_HISTORY_ROWS
        if hidden > 0:
            row = _row(f"…and {hidden} older entries")
            self._history_list.append(row)
            self._history_rows.append(row)

    def check_integrity(self) -> None:
        self._integrity_button.set_sensitive(False)
        self._integrity_button.set_label("Checking…")
        self._history_banner.set_button_label("Checking…")

        def done(findings: dict[str, RecoveryFinding]) -> None:
            self._integrity_button.set_sensitive(True)
            self._integrity_button.set_label("Check Integrity")
            self._history_banner.set_button_label("Check Integrity")
            self._render_history(self._history_entries, findings)
            self._toast("Integrity check finished. No files were changed.")

        def failed(exc: Exception) -> None:
            self._integrity_button.set_sensitive(True)
            self._integrity_button.set_label("Check Integrity")
            self._history_banner.set_button_label("Check Integrity")
            self._toast("The integrity check couldn't finish.")

        run_in_thread(lambda: inspect_history(self._journal_path), done, failed)

