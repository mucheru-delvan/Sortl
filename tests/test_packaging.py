"""Guards against packaging drift. Pure file checks; no GTK needed."""

import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import sortl

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def test_every_module_is_listed_in_meson() -> None:
    listed = set(re.findall(r"'(sortl/[^']+\.py)'", (SRC / "meson.build").read_text()))
    on_disk = {
        str(path.relative_to(SRC)) for path in (SRC / "sortl").rglob("*.py")
    }

    assert on_disk == listed


def test_versions_agree_everywhere() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    meson = re.search(r"version:\s*'([^']+)'", (ROOT / "meson.build").read_text())
    metainfo = ET.parse(
        ROOT / "data" / f"{sortl.APP_ID}.metainfo.xml"
    ).getroot()
    newest_release = metainfo.find("releases/release")

    assert meson is not None and newest_release is not None
    assert (
        pyproject["project"]["version"]
        == meson.group(1)
        == sortl.__version__
        == newest_release.get("version")
    )


def test_app_id_matches_all_file_names() -> None:
    data = ROOT / "data"

    assert (data / f"{sortl.APP_ID}.desktop").exists()
    assert (data / f"{sortl.APP_ID}.metainfo.xml").exists()
    assert (data / "icons/hicolor/scalable/apps" / f"{sortl.APP_ID}.svg").exists()
    assert (ROOT / f"{sortl.APP_ID}.yaml").exists()
    assert f"app_id = '{sortl.APP_ID}'" in (ROOT / "meson.build").read_text()
