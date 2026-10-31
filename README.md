# Sortl

Sortl tidies a messy folder (Downloads, say) by sorting its files into
category folders such as Images, Documents and Archives. It shows you exactly
what will move before anything changes, and it is built to never lose a file.

## How it stays safe

- **Preview first.** Nothing moves until you confirm the plan.
- **No overwrites.** If the destination name already exists, the file is skipped.
- **Verified moves.** Each move is a hard link plus a verified removal of the
  old name, checked by file identity and content hash, and recorded in a
  SQLite journal before and after.
- **Recoverable.** If something is interrupted, the history view can inspect
  every journaled operation read-only and tell you what state it is in.
- **Fails closed.** Sortl checks that the folder supports hard links before it
  touches anything (some USB drives and network shares don't), and halts the
  whole run on the first unexpected failure.

Only files directly inside the chosen folder are moved; subfolders are left alone.

## Install (Debian/Ubuntu)

Needs Ubuntu 24.04 / Debian 13 or newer (libadwaita 1.5+, Python 3.12+).
Download the `.deb` from the Releases page, then:

```sh
sudo apt install ./sortl_0.1.1-1_all.deb
```

Sortl is not sandboxed, so it can organize any folder you can write to. Its
move history is kept in `~/.local/share/sortl/journal.sqlite3`. Remove it with
`sudo apt remove sortl`.

## Build the .deb

```sh
sudo apt install meson ninja-build dpkg-dev appstream desktop-file-utils
packaging/deb/build-deb.sh        # writes dist/sortl_<version>-1_all.deb
```

The build runs the test-suite's desktop-file and AppStream validators first.

## Layout

| Path | Purpose |
| --- | --- |
| `src/sortl/{planner,executor,journal,recovery,models}.py` | GTK-free core |
| `src/sortl/service.py` | Orchestration used by the UI (progress, cancel, hard-link check) |
| `src/sortl/ui/` | GTK4 + libadwaita front-end |
| `data/` | Desktop entry, AppStream metainfo, icons, screenshots |
| `packaging/deb/build-deb.sh` | Builds the `.deb` package |

## Run from source

Needs GTK 4, libadwaita 1.5+ and PyGObject from your distro (for example
`python3-gi gir1.2-gtk-4.0 gir1.2-adw-1` on Debian/Ubuntu).

```sh
PYTHONPATH=src python3 -c "from sortl import main; main()"
```

## Test

```sh
pip install pytest
pytest                      # core + service + packaging checks
# UI smoke tests need a display; headless:
dbus-run-session -- xvfb-run -a env GSK_RENDERER=cairo pytest tests/test_ui_smoke.py
```

## License

MIT, see `LICENSE`.
