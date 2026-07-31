"""
Where things live -- the single source of truth for every path in this project.

There are exactly two roots, and conflating them is the bug this module exists
to prevent:

  bundle_dir()   READ-ONLY program files: index.html, icons, the seed archive
                 that shipped with the build.
  data_dir()     WRITABLE archive: operations.json, shapes.geojson,
                 manifest.json, and the .cache/ scratch space.

In a normal dev checkout the two happen to overlap (`bundle_dir()/data ==
data_dir()`), which is why nothing has ever noticed the difference. Inside a
frozen PyInstaller onefile exe they are wildly different places, and getting
them wrong loses data.

WHY THE FROZEN RULES ARE WHAT THEY ARE
--------------------------------------
A onefile exe unpacks itself into a temporary directory (``sys._MEIPASS``) and
**deletes that directory when the process exits**. So:

  * ``Path(__file__).parent / "data"`` inside a frozen app points *into* the
    temp extraction dir. A downloaded update would be written there and then
    silently evaporate on quit. This is failure mode #1.
  * ``Path(sys.executable).parent / "data"`` -- next to the .exe -- survives,
    but the exe is frequently in ``C:\\Program Files`` or on a read-only share,
    where writes fail outright (or, worse, get shunted into the VirtualStore).
    This is failure mode #2.

The only location that is both persistent and reliably writable for a normal
user is the per-user application-data directory, so that is what a frozen
build uses: ``%LOCALAPPDATA%/sj-mosquito-maps/data``.

Dev mode deliberately keeps ``<repo>/data`` exactly as it was. ``data/`` is
committed -- it IS the archive, not a cache -- and the git workflow, CI gate
and scheduled job all depend on the fetcher writing into the working tree.
Nothing about a dev run may change.

``SJMVCD_DATA_DIR`` overrides the choice in *both* modes. It is the escape
hatch for tests, for a user who wants the archive on another drive, and for
anyone running the frozen app from a USB stick.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Used for the per-user data directory name in a frozen build. Kept as a
# constant because renaming it strands every existing user's archive.
APP_NAME = "sj-mosquito-maps"

#: Environment variable that overrides data_dir() in every mode.
DATA_DIR_ENV = "SJMVCD_DATA_DIR"

# <repo>/sjmvcd/paths.py -> <repo>. Only meaningful when running from source;
# when frozen we ask PyInstaller instead (see bundle_dir).
_REPO_ROOT = Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    """
    True when running inside a PyInstaller (or similar) frozen build.

    PyInstaller's bootloader sets ``sys.frozen``; cx_Freeze and py2exe set it
    too, so this stays true for any of them. Everything else in this module
    branches on this one predicate, so there is exactly one place to look when
    frozen and dev behaviour diverge.
    """
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """
    The read-only root that program files were shipped in.

    Frozen: ``sys._MEIPASS``, the temporary directory the onefile exe unpacked
    itself into. Treat everything under it as read-only and *ephemeral* -- it
    is deleted when the process exits.

    Dev: the repository root.

    Serve index.html and friends from here. Never write here.
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
        # onedir builds (and non-PyInstaller freezers) have no _MEIPASS; the
        # resources sit beside the executable instead.
        return Path(sys.executable).resolve().parent
    return _REPO_ROOT


def _user_data_root() -> Path:
    """
    Per-user, persistent, writable application-data directory for this OS.

    Windows is the only platform we actually ship a frozen build for, but the
    other branches cost three lines and stop a Mac/Linux experiment from
    dumping an archive into the current working directory.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base)
        return Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    # XDG: honour the spec's variable, fall back to its documented default.
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))


def data_dir() -> Path:
    """
    Where operations.json / shapes.geojson / manifest.json LIVE and are WRITTEN.

    Resolution order:

      1. ``$SJMVCD_DATA_DIR`` if set and non-empty -- honoured in both modes.
      2. Frozen: ``<per-user app data>/sj-mosquito-maps/data``.
      3. Dev: ``<repo>/data`` -- unchanged from before this module existed, so
         the committed archive and the git workflow keep working exactly as
         they do today.

    This function does not create the directory. Creation belongs to the code
    that is about to write (``fetch_data.main``) or to ``seed_data_dir()``, so
    that a pure reader (``verify_data.py``, a GET of /data/*) can never have
    the side effect of conjuring an empty archive directory into existence.
    """
    override = os.environ.get(DATA_DIR_ENV)
    if override and override.strip():
        return Path(override).expanduser().resolve()
    if is_frozen():
        return _user_data_root() / APP_NAME / "data"
    return _REPO_ROOT / "data"


def cache_dir() -> Path:
    """
    Scratch space for raw Wayback captures.

    Deliberately *inside* data_dir(): it is regenerable, but it is large and
    write-heavy, so it has to land somewhere writable, and keeping it next to
    the archive means one directory to point at, back up, or delete. Gitignored
    in dev; in a frozen build it lives under the user's app data with the rest.
    """
    return data_dir() / ".cache"


def seed_data_dir() -> tuple[int, Path]:
    """
    Copy the archive that shipped with the build into ``data_dir()``.

    Returns ``(files_copied, data_dir())``.

    THE ONE RULE: copy a file only when it is **missing** from the destination.
    Never overwrite. The bundled seed is a snapshot frozen at build time; the
    user's copy may be months newer because they have pressed "update maps".
    Overwriting would silently roll their archive back to the build date, and
    operations that have since rolled off the district's page cannot be
    re-fetched from anywhere. A stale seed must lose to a live archive every
    time.

    Safe to call in dev mode, where it is a genuine no-op: the source directory
    *is* the destination directory, which is detected and short-circuited
    before anything is copied. Safe to call repeatedly; the second call copies
    nothing.

    ``.cache/`` and other subdirectories are not seeded -- the cache is
    regenerable scratch and would triple the size of the installer.
    """
    destination = data_dir()
    source = bundle_dir() / "data"

    # Nothing shipped (source build with no data/, or a stripped bundle).
    if not source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        return 0, destination

    # Dev mode, or a data-dir override that points back at the bundle: source
    # and destination are the same place, so "copying" would be a no-op at best
    # and a self-overwrite at worst. Compare resolved paths first (works even
    # when the destination does not exist yet), then os.path.samefile to catch
    # the symlink / directory-junction case.
    if source.resolve() == destination.resolve():
        return 0, destination
    if destination.is_dir():
        try:
            if os.path.samefile(source, destination):
                return 0, destination
        except OSError:
            pass

    destination.mkdir(parents=True, exist_ok=True)

    copied = 0
    for entry in sorted(source.iterdir()):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        target = destination / entry.name
        if target.exists():
            continue  # <- the whole point: never clobber a newer archive
        shutil.copy2(entry, target)
        copied += 1
    return copied, destination


def describe() -> str:
    """One-line summary of the resolved layout, for startup logs and support."""
    return (
        f"{'frozen' if is_frozen() else 'dev'} | "
        f"bundle={bundle_dir()} | data={data_dir()}"
        + (f" (via ${DATA_DIR_ENV})" if os.environ.get(DATA_DIR_ENV) else "")
    )


if __name__ == "__main__":  # pragma: no cover - manual inspection aid
    print(describe())
    print(f"cache = {cache_dir()}")
