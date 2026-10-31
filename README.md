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
  touches anything, and halts the whole run on the first unexpected failure.

Only files directly inside the chosen folder are moved; subfolders are left alone.

## Layout

| Path | Purpose |
| --- | --- |
| `src/sortl/{planner,executor,journal,recovery,models}.py` | GTK-free core |
| `src/sortl/service.py` | Orchestration used by the UI (progress, cancel, probe) |
| `src/sortl/ui/` | GTK4 + libadwaita front-end |
| `data/` | Desktop entry, AppStream metainfo, icons |
| `io.github.mucheru_delvan.Sortl.yaml` | Flatpak manifest |

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

## Build and run the Flatpak

```sh
flatpak install flathub org.gnome.Platform//51 org.gnome.Sdk//51
flatpak-builder --user --install --force-clean build-dir io.github.mucheru_delvan.Sortl.yaml
flatpak run io.github.mucheru_delvan.Sortl
```

Sortl has direct access to Downloads, Documents and Desktop. Folders chosen
elsewhere arrive through the file chooser portal, which can't move files
reliably (it intermittently reports a just-moved file as missing), so Sortl
refuses them with an explanation. Grant another folder with Flatseal or
`flatpak override --user --filesystem=/path/to/folder io.github.mucheru_delvan.Sortl`.
The journal lives in `~/.var/app/io.github.mucheru_delvan.Sortl/data/sortl/`.

## License

MIT, see `LICENSE`.
