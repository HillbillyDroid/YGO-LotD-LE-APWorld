"""Save-file manager for the YGO LotD-LE AP client.

Owns the on-disk lifecycle of `savegame.dat` so each AP world gets its own
isolated save and existing saves are never lost. See `SAVE_BACKUPS_PLAN.md`
for the full design.

Filesystem layout (under `%USERPROFILE%/Documents/ygo_lotd_ap/`):

    config.json                       # {steam_userdata_path, last_active_world}
    backups/
      pre_ap/savegame.dat             # one-time backup of pre-AP save
      <seed_name>_<slot_name>/
        savegame.dat                  # the world's working save
        meta.json                     # {seed_name, slot_name, ..., created_at, last_synced_at}
      orphan_<timestamp>/             # captured-but-unmatched saves

This module is pure-filesystem; it does NOT talk to pymem or the AP server.
The runtime client (`ygo_client.py`) glues it into Stage A/B of the connect
sequence in later steps.
"""
from __future__ import annotations

import datetime as _dt
import enum
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


LOTD_LE_APP_ID = "1150640"
SAVE_FILENAME = "savegame.dat"
CONFIG_FILENAME = "config.json"
META_FILENAME = "meta.json"
PRE_AP_DIRNAME = "pre_ap"
BACKUPS_DIRNAME = "backups"
ROOT_DIRNAME = "ygo_lotd_ap"

# Filesystem-sanitization for world keys.
_WORLD_KEY_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_world_component(value: str) -> str:
    """Make a string safe for use as a directory name on Windows."""
    cleaned = _WORLD_KEY_SAFE.sub("_", value).strip("._")
    return cleaned or "unnamed"


def make_world_key(seed_name: str, slot_name: str) -> str:
    """Build the canonical `<seed>_<slot>` world key used as a directory name."""
    return f"{_sanitize_world_component(seed_name)}_{_sanitize_world_component(slot_name)}"


@dataclass
class SaveManagerPaths:
    root: Path                      # %USERPROFILE%/Documents/ygo_lotd_ap
    config: Path                    # root/config.json
    backups: Path                   # root/backups
    pre_ap: Path                    # root/backups/pre_ap
    pre_ap_save: Path               # root/backups/pre_ap/savegame.dat


@dataclass
class SteamPaths:
    steam_root: Path                # e.g. C:/Program Files (x86)/Steam
    userdata_save: Path             # <steam_root>/userdata/<steamid>/1150640/remote/savegame.dat
    userdata_remote_dir: Path       # parent of userdata_save
    remotecache_vdf: Path           # <steam_root>/userdata/<steamid>/1150640/remotecache.vdf


# Steam syncstate codes observed in remotecache.vdf:
#   "1" — in sync (cloud enabled, file matches remote)
#   "4" — cloud disabled (or otherwise not syncing this file)
# Verified empirically on the dev machine 2026-05-08: toggling Steam Cloud
# off for app 1150640 changed savegame.dat's syncstate from "1" to "4".
# Other values (e.g. "2") presumably indicate transient mid-sync states; we
# treat anything other than "4" as "cloud likely on" for safety.
CLOUD_DISABLED_SYNCSTATE = "4"


class SaveManagerError(Exception):
    """Raised for unrecoverable filesystem-side failures."""


class WorldState(enum.Enum):
    """Outcome of comparing a world's backup against the live userdata save.

    Drives the Stage B `_reconcile` dispatch in `ygo_client.py` (Step 6).
    """
    FRESH_NEEDS_INIT = "fresh_needs_init"      # No backup yet; capture current
                                                # userdata save into the world dir.
    MATCHES_USERDATA = "matches_userdata"      # Backup hash == userdata hash; no-op.
    MISMATCH_NEEDS_SWAP = "mismatch_needs_swap"# Backup differs from userdata; swap.
    GAME_RUNNING_DEFERRED = "game_running"     # Game open in some other save; skip.


@dataclass
class WorldMeta:
    """Persisted metadata for one backed-up world."""
    world_key: str
    seed_name: str
    slot_name: str
    team: Optional[int] = None
    slot: Optional[int] = None
    created_at: str = ""
    last_synced_at: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "world_key": self.world_key,
            "seed_name": self.seed_name,
            "slot_name": self.slot_name,
            "team": self.team,
            "slot": self.slot,
            "created_at": self.created_at,
            "last_synced_at": self.last_synced_at,
        }
        if self.extra:
            d["extra"] = self.extra
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> "WorldMeta":
        return cls(
            world_key=raw.get("world_key", ""),
            seed_name=raw.get("seed_name", ""),
            slot_name=raw.get("slot_name", ""),
            team=raw.get("team"),
            slot=raw.get("slot"),
            created_at=raw.get("created_at", ""),
            last_synced_at=raw.get("last_synced_at", ""),
            extra=raw.get("extra", {}) or {},
        )


# Game process names to probe when checking if the game is running. Keep in
# sync with `memory.py::CANDIDATE_EXE_NAMES`. Duplicated rather than imported
# so this module stays free of pymem at import time.
GAME_PROCESS_NAMES = ("Lotd.exe", "LotdLE.exe", "YuGiOh.exe", "Yu-Gi-Oh!.exe")


def _is_game_running() -> bool:
    """Best-effort: is any LotD-LE process currently attachable via pymem?

    Returns True if pymem successfully attaches to any of the candidate
    process names, False otherwise. Detaches immediately so we don't hold
    the handle. Imports pymem lazily so the module remains importable in
    environments without pymem (offline tests, build-time scripts).
    """
    try:
        import pymem  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("pymem not installed; cannot check if game is running")
        return False
    for name in GAME_PROCESS_NAMES:
        try:
            pm = pymem.Pymem(name)
        except Exception:
            continue
        # Successfully attached — game is running. Drop the handle and return.
        try:
            pm.close_process()
        except Exception:
            pass
        return True
    return False


def _documents_dir() -> Path:
    """Resolve %USERPROFILE%/Documents. SHGetKnownFolderPath would be more
    correct but adds a ctypes dependency; the env-var path matches Steam's
    own convention and is sufficient here."""
    profile = os.environ.get("USERPROFILE")
    if not profile:
        raise SaveManagerError("USERPROFILE env var not set; cannot locate Documents")
    return Path(profile) / "Documents"


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_atomic(src: Path, dst: Path) -> None:
    """Copy `src` to `dst` via a tmp file in the same directory + os.replace.

    Atomic on Windows when both paths are on the same volume.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".swap_", suffix=".tmp", dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_path)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _find_steam_path() -> Path:
    """Locate the Steam install root.

    Order:
      1. `HKCU\\Software\\Valve\\Steam` -> `SteamPath` (string).
      2. `%PROGRAMFILES(X86)%/Steam`.
      3. `C:/Program Files (x86)/Steam`.
    """
    if sys.platform == "win32":
        try:
            import winreg  # type: ignore[import-not-found]
        except ImportError:
            winreg = None  # type: ignore[assignment]
        if winreg is not None:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                    raw, _ = winreg.QueryValueEx(key, "SteamPath")
                if raw:
                    candidate = Path(raw)
                    if candidate.exists():
                        return candidate
            except OSError:
                pass

    pf86 = os.environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)"
    candidate = Path(pf86) / "Steam"
    if candidate.exists():
        return candidate
    raise SaveManagerError(
        f"Could not locate Steam install root (registry miss + {candidate} not found)"
    )


def _find_userdata_save_path(steam_root: Path) -> SteamPaths:
    """Glob `userdata/*/<app_id>/remote/savegame.dat` to find the active steamid.

    Multi-steamid handling: pick the most-recently-modified `savegame.dat` so
    shared PCs land on the user who most recently played the game. This is a
    heuristic; the user can override by editing `config.json`.
    """
    userdata = steam_root / "userdata"
    if not userdata.exists():
        raise SaveManagerError(f"Steam userdata directory not found at {userdata}")

    candidates = sorted(userdata.glob(f"*/{LOTD_LE_APP_ID}/remote/{SAVE_FILENAME}"))
    if not candidates:
        raise SaveManagerError(
            f"No {SAVE_FILENAME} found under {userdata}/*/{LOTD_LE_APP_ID}/remote/. "
            "Has the game ever been launched on this machine?"
        )
    if len(candidates) > 1:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        logger.warning(
            "Multiple Steam users have a %s save under %s; picking most-recently-modified: %s",
            "LotD-LE", userdata, candidates[0],
        )
    save_path = candidates[0]
    remote_dir = save_path.parent
    # remotecache.vdf sits one level above the `remote/` directory.
    return SteamPaths(
        steam_root=steam_root,
        userdata_save=save_path,
        userdata_remote_dir=remote_dir,
        remotecache_vdf=remote_dir.parent / "remotecache.vdf",
    )


def _parse_savegame_syncstate(vdf_text: str) -> Optional[str]:
    """Extract the `syncstate` value of the `savegame.dat` block in a
    Steam `remotecache.vdf`.

    Steam's VDF is a tiny key-value format with quoted strings + braces.
    A full parser is overkill — we just regex the savegame block. Returns
    None if the file shape is unrecognized.
    """
    # Match the `"savegame.dat" { ... syncstate "<digits>" ... }` block.
    # `re.DOTALL` so the `.` inside the block can cross newlines.
    block_match = re.search(
        r'"savegame\.dat"\s*\{(.*?)\}', vdf_text, re.DOTALL,
    )
    if not block_match:
        return None
    inner = block_match.group(1)
    state_match = re.search(r'"syncstate"\s*"([^"]*)"', inner)
    if not state_match:
        return None
    return state_match.group(1)


def _now_iso() -> str:
    # Microsecond precision so two swaps in the same second still sort correctly
    # in `list_worlds()`. Lexicographic sort on ISO-8601 strings is correct.
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds")


class SaveManager:
    """Filesystem-side owner of the AP world's save backups.

    Construction is cheap: paths are computed but no filesystem mutation
    happens until `ensure_setup()` is called. This lets callers instantiate
    the manager during context init and defer side-effects to Stage A.
    """

    def __init__(
        self,
        *,
        root: Optional[Path] = None,
        cloud_disabled_override: bool = False,
    ) -> None:
        root_dir = root if root is not None else (_documents_dir() / ROOT_DIRNAME)
        self.paths = SaveManagerPaths(
            root=root_dir,
            config=root_dir / CONFIG_FILENAME,
            backups=root_dir / BACKUPS_DIRNAME,
            pre_ap=root_dir / BACKUPS_DIRNAME / PRE_AP_DIRNAME,
            pre_ap_save=root_dir / BACKUPS_DIRNAME / PRE_AP_DIRNAME / SAVE_FILENAME,
        )
        self._steam: Optional[SteamPaths] = None
        self._setup_done = False
        # Hard override for users who've confirmed they disabled cloud sync
        # but our heuristic detection misfires (e.g. corrupted/missing vdf).
        self._cloud_disabled_override = cloud_disabled_override

    # ------------------------------------------------------------------ public API

    def ensure_setup(self) -> None:
        """Idempotent: probe Steam, create directories, snapshot the pre-AP save.

        Safe to call multiple times. Cheap on subsequent calls (fast-path returns
        once `_setup_done` is set).
        """
        if self._setup_done:
            return

        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.paths.backups.mkdir(parents=True, exist_ok=True)
        self.paths.pre_ap.mkdir(parents=True, exist_ok=True)

        steam = self._resolve_steam_paths()

        # One-time pre-AP snapshot: the user's escape hatch back to their
        # pre-AP-world save. NEVER overwrite if it already exists.
        if not self.paths.pre_ap_save.exists():
            _copy_atomic(steam.userdata_save, self.paths.pre_ap_save)
            logger.info(
                "Captured pre-AP save snapshot: %s -> %s",
                steam.userdata_save, self.paths.pre_ap_save,
            )

        self._write_config()
        self._setup_done = True

    def is_cloud_disabled(self) -> bool:
        """Best-effort check: is Steam Cloud sync disabled for app 1150640?

        Returns True iff:
          - the user passed `cloud_disabled_override=True` at construction, OR
          - `remotecache.vdf` exists AND its `savegame.dat` block has
            `syncstate "4"` (empirically the cloud-disabled state).

        Treats every other case (missing vdf, unparseable block, any
        non-"4" syncstate) as "cloud probably on" so the safety gate fails
        closed.
        """
        if self._cloud_disabled_override:
            return True
        try:
            steam = self.steam
        except SaveManagerError:
            return False
        vdf_path = steam.remotecache_vdf
        if not vdf_path.exists():
            # No remotecache means Steam has never synced this app for this
            # user. Could mean cloud is off, but could also mean the user
            # just hasn't launched the game yet. Fail closed.
            logger.warning("remotecache.vdf not found at %s; assuming cloud on", vdf_path)
            return False
        try:
            text = vdf_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Could not read %s (%s); assuming cloud on", vdf_path, exc)
            return False
        state = _parse_savegame_syncstate(text)
        if state is None:
            logger.warning(
                "Could not parse savegame.dat block in %s; assuming cloud on", vdf_path,
            )
            return False
        return state == CLOUD_DISABLED_SYNCSTATE

    @property
    def steam(self) -> SteamPaths:
        """Lazily-resolved Steam paths. Calls ensure_setup if needed."""
        if self._steam is None:
            self._resolve_steam_paths()
        assert self._steam is not None
        return self._steam

    def world_dir(self, world_key: str) -> Path:
        return self.paths.backups / world_key

    def world_save(self, world_key: str) -> Path:
        return self.world_dir(world_key) / SAVE_FILENAME

    def world_meta_path(self, world_key: str) -> Path:
        return self.world_dir(world_key) / META_FILENAME

    # --- world-state evaluation --------------------------------------

    def get_world_state(
        self,
        seed_name: str,
        slot_name: str,
        *,
        game_running_override: Optional[bool] = None,
    ) -> tuple[WorldState, str]:
        """Decide what to do with a world's save based on the on-disk situation.

        Returns `(state, world_key)`. Pure read; no filesystem mutation.
        Caller (Step 6) routes the state through Stage B's dispatch.

        `game_running_override` lets callers that already attached pymem skip
        the redundant probe (e.g. the AP client knows the game is running
        because it already walked the pointer chain).
        """
        self.ensure_setup()
        world_key = make_world_key(seed_name, slot_name)
        world_save = self.world_save(world_key)
        userdata_save = self.steam.userdata_save

        running = (
            game_running_override
            if game_running_override is not None
            else _is_game_running()
        )

        if not world_save.exists():
            if running:
                # Don't touch the live save under a running game. The Stage B
                # caller will log a warning and the user can disconnect-then-
                # reconnect once the game is closed.
                return WorldState.GAME_RUNNING_DEFERRED, world_key
            return WorldState.FRESH_NEEDS_INIT, world_key

        # Backup exists — compare hashes.
        if not userdata_save.exists():
            # Edge case: backup exists but userdata save vanished. Treat as
            # mismatch so the swap logic restores the backup into userdata.
            return WorldState.MISMATCH_NEEDS_SWAP, world_key
        if _hash_file(world_save) == _hash_file(userdata_save):
            return WorldState.MATCHES_USERDATA, world_key
        return WorldState.MISMATCH_NEEDS_SWAP, world_key

    # --- world bring-up / swap ---------------------------------------

    def init_fresh_world(
        self,
        seed_name: str,
        slot_name: str,
        *,
        team: Optional[int] = None,
        slot: Optional[int] = None,
    ) -> WorldMeta:
        """Create a new `backups/<world_key>/` from the current userdata save.

        Used when `get_world_state()` returns `FRESH_NEEDS_INIT`. Caller is
        responsible for ensuring the game is closed (this method does not
        re-check; it's a small race window but the Stage B caller already
        observes `not running` to reach this branch).
        """
        self.ensure_setup()
        world_key = make_world_key(seed_name, slot_name)
        world_dir = self.world_dir(world_key)
        world_dir.mkdir(parents=True, exist_ok=True)

        _copy_atomic(self.steam.userdata_save, self.world_save(world_key))

        now = _now_iso()
        meta = WorldMeta(
            world_key=world_key,
            seed_name=seed_name,
            slot_name=slot_name,
            team=team,
            slot=slot,
            created_at=now,
            last_synced_at=now,
        )
        self._write_meta(meta)
        self._set_last_active_world(world_key)
        logger.info("Initialized new AP world save: %s", world_dir)
        return meta

    def swap_to_world(self, world_key: str) -> WorldMeta:
        """Activate `backups/<world_key>/savegame.dat` as the live userdata save.

        Captures the current userdata save first — into the matching backup
        if its hash matches one we already track, otherwise into a timestamped
        `orphan_<>/` directory so nothing is ever lost.

        REQUIRES the game to be closed. Aborts with `SaveManagerError` if
        any LotD-LE process is detected. The Stage B caller (Step 7) is
        responsible for prompting the user and polling until the game exits.
        """
        self.ensure_setup()
        world_save = self.world_save(world_key)
        if not world_save.exists():
            raise SaveManagerError(
                f"Cannot swap to {world_key}: backup file does not exist at {world_save}"
            )
        if _is_game_running():
            raise SaveManagerError(
                "Cannot swap save while the game is running — close the game first."
            )

        userdata_save = self.steam.userdata_save
        # 1. Capture whatever's currently in userdata into a safe place.
        if userdata_save.exists():
            self._capture_current_userdata(userdata_save)

        # 2. Copy this world's backup into userdata.
        _copy_atomic(world_save, userdata_save)

        # 3. Refresh meta + config.
        meta = self._read_meta(world_key) or WorldMeta(
            world_key=world_key, seed_name="", slot_name="",
            created_at=_now_iso(),
        )
        meta.last_synced_at = _now_iso()
        self._write_meta(meta)
        self._set_last_active_world(world_key)
        logger.info("Swapped userdata save to AP world: %s", world_key)
        return meta

    def restore_pre_ap(self) -> None:
        """Copy the one-time pre-AP snapshot back over userdata's `savegame.dat`.

        Like `swap_to_world`, requires the game to be closed and captures
        the current userdata first.
        """
        self.ensure_setup()
        if not self.paths.pre_ap_save.exists():
            raise SaveManagerError(
                f"Pre-AP snapshot missing at {self.paths.pre_ap_save}; nothing to restore"
            )
        if _is_game_running():
            raise SaveManagerError(
                "Cannot restore pre-AP save while the game is running."
            )
        userdata_save = self.steam.userdata_save
        if userdata_save.exists():
            self._capture_current_userdata(userdata_save)
        _copy_atomic(self.paths.pre_ap_save, userdata_save)
        self._set_last_active_world(None)
        logger.info("Restored pre-AP save snapshot to %s", userdata_save)

    def list_worlds(self) -> list[WorldMeta]:
        """Enumerate all `backups/<world_key>/` directories with a meta.json.

        Skips `pre_ap` and `orphan_*` (they aren't AP-managed worlds).
        Sorted by `last_synced_at` descending so the most-recently-active
        world appears first.
        """
        self.ensure_setup()
        out: list[WorldMeta] = []
        if not self.paths.backups.exists():
            return out
        for child in self.paths.backups.iterdir():
            if not child.is_dir():
                continue
            if child.name == PRE_AP_DIRNAME or child.name.startswith("orphan_"):
                continue
            meta = self._read_meta(child.name)
            if meta is not None:
                out.append(meta)
        out.sort(key=lambda m: m.last_synced_at, reverse=True)
        return out

    def get_last_active_world(self) -> Optional[str]:
        cfg = self._read_config()
        if not cfg:
            return None
        return cfg.get("last_active_world")

    # ----------------------------------------------------------------- internals

    def _resolve_steam_paths(self) -> SteamPaths:
        if self._steam is not None:
            return self._steam
        # Try config.json first; fall back to discovery and persist.
        config = self._read_config()
        steam_root: Optional[Path] = None
        if config and config.get("steam_root"):
            candidate = Path(config["steam_root"])
            if candidate.exists():
                steam_root = candidate
        if steam_root is None:
            steam_root = _find_steam_path()
        self._steam = _find_userdata_save_path(steam_root)
        return self._steam

    def _read_config(self) -> Optional[dict]:
        if not self.paths.config.exists():
            return None
        try:
            return json.loads(self.paths.config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read %s (%s); treating as missing", self.paths.config, exc)
            return None

    def _write_config(self) -> None:
        steam = self.steam
        existing = self._read_config() or {}
        payload = {
            "steam_root": str(steam.steam_root),
            "userdata_save": str(steam.userdata_save),
            "last_active_world": existing.get("last_active_world"),
            "updated_at": _now_iso(),
            "schema_version": 1,
        }
        _write_json_atomic(self.paths.config, payload)

    def _set_last_active_world(self, world_key: Optional[str]) -> None:
        existing = self._read_config() or {}
        existing["last_active_world"] = world_key
        existing["updated_at"] = _now_iso()
        # Make sure the persisted config still has steam_root / userdata_save
        # filled in even if we somehow ended up here before _write_config ran.
        existing.setdefault("steam_root", str(self.steam.steam_root))
        existing.setdefault("userdata_save", str(self.steam.userdata_save))
        existing.setdefault("schema_version", 1)
        _write_json_atomic(self.paths.config, existing)

    def _read_meta(self, world_key: str) -> Optional[WorldMeta]:
        path = self.world_meta_path(world_key)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read %s (%s); treating as missing", path, exc)
            return None
        return WorldMeta.from_dict(raw)

    def _write_meta(self, meta: WorldMeta) -> None:
        self.world_dir(meta.world_key).mkdir(parents=True, exist_ok=True)
        _write_json_atomic(self.world_meta_path(meta.world_key), meta.to_dict())

    def _capture_current_userdata(self, userdata_save: Path) -> None:
        """Stash whatever's in `userdata_save` into the matching backup
        directory if its hash matches one we track, otherwise into a
        timestamped `orphan_<>/`.

        Always called before swap/restore so no live save is ever clobbered
        without a copy on disk.
        """
        try:
            current_hash = _hash_file(userdata_save)
        except OSError as exc:
            logger.warning(
                "Could not hash current userdata save %s (%s); skipping capture",
                userdata_save, exc,
            )
            return

        # Look for an existing backup with the same hash.
        for meta in self.list_worlds():
            backup = self.world_save(meta.world_key)
            if not backup.exists():
                continue
            try:
                if _hash_file(backup) == current_hash:
                    # Already captured here. No-op — copying would just churn mtime.
                    logger.info(
                        "Current userdata save matches existing backup %s; no capture needed",
                        meta.world_key,
                    )
                    return
            except OSError:
                continue

        # No match — orphan it so it's recoverable.
        ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        orphan_dir = self.paths.backups / f"orphan_{ts}"
        orphan_dir.mkdir(parents=True, exist_ok=True)
        _copy_atomic(userdata_save, orphan_dir / SAVE_FILENAME)
        logger.info(
            "Captured unmatched userdata save to %s before swap", orphan_dir / SAVE_FILENAME,
        )


# ----------------------------------------------------------------- module helpers

def _write_json_atomic(path: Path, payload: dict) -> None:
    """Write `payload` as pretty JSON to `path` via tmp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".json_", suffix=".tmp", dir=str(path.parent),
    )
    os.close(fd)
    tmp = Path(tmp_path)
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
