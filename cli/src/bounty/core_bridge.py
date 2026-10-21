"""Locate and drive the bounty-core engine.

Lookup order: an explicit path, ``BOUNTY_CORE_BIN``, the checkout's ``bin/``
directory, ``PATH``, and finally a one-off build with the Go toolchain. The
engine stays a separate process, so its JSON contract is the only coupling.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class BridgeError(RuntimeError):
    """The engine could not be found, or it exited with an error."""

    def __init__(self, message: str, returncode: int = 1) -> None:
        super().__init__(message)
        self.returncode = returncode


def binary_name() -> str:
    return "bounty-core.exe" if os.name == "nt" else "bounty-core"


def repo_root() -> Path | None:
    """Return the checkout root when running from a source tree."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "core" / "go.mod").is_file():
            return parent
    return None


def _usable(path: Path) -> bool:
    return path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))


def find_core(explicit: str | None = None) -> Path:
    """Return a usable engine binary, building one if that is possible."""
    if explicit:
        path = Path(explicit).expanduser()
        if not _usable(path):
            raise BridgeError(f"no engine at {path} (from --core)")
        return path

    override = os.environ.get("BOUNTY_CORE_BIN")
    if override:
        path = Path(override).expanduser()
        if not _usable(path):
            raise BridgeError(f"BOUNTY_CORE_BIN points at {path}, which is not a usable binary")
        return path

    root = repo_root()

    if root is not None:
        candidate = root / "bin" / binary_name()
        if _usable(candidate):
            return candidate

    on_path = shutil.which("bounty-core")
    if on_path:
        return Path(on_path)

    if root is not None:
        go = shutil.which("go")
        if go:
            return _build(root, go)

    raise BridgeError(
        "could not find the bounty-core engine.\n"
        "  Build it once:  cd core && go build -o ../bin/bounty-core .\n"
        "  Or put it on PATH, or set BOUNTY_CORE_BIN to its location."
    )


def _build(root: Path, go: str) -> Path:
    target = root / "bin" / binary_name()
    target.parent.mkdir(parents=True, exist_ok=True)
    print("bounty: building the bounty-core engine (first run only)…", file=sys.stderr)
    try:
        subprocess.run([go, "build", "-o", str(target), "."], cwd=root / "core", check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BridgeError(f"building the engine failed: {exc}") from exc
    return target


@dataclass(frozen=True)
class ScanOutput:
    """A finished scan: the engine's JSON plus whatever it said on stderr."""

    stdout: bytes
    stderr: str = ""


def run_scan(core: Path, args: list[str]) -> bytes:
    """Run ``bounty-core scan`` and return its stdout bytes.

    stderr is inherited, so rate-limit warnings appear while the scan is still
    running rather than arriving at the end.
    """
    return _scan(core, args, capture_stderr=False).stdout


def run_scan_quiet(core: Path, args: list[str]) -> ScanOutput:
    """Run a scan and collect stderr instead of inheriting it.

    A full-screen dashboard cannot let the engine print over the display, so
    the TUI takes the warnings and shows them itself.
    """
    return _scan(core, args, capture_stderr=True)


def _scan(core: Path, args: list[str], *, capture_stderr: bool) -> ScanOutput:
    try:
        proc = subprocess.run(
            [str(core), "scan", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture_stderr else None,
        )
    except OSError as exc:
        raise BridgeError(f"could not run {core}: {exc}") from exc

    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        code = proc.returncode if proc.returncode in (1, 2) else 1
        message = f"scan failed (bounty-core exited {proc.returncode})"
        # With stderr inherited the user already saw the cause; when it was
        # captured, the last line is the only clue they would otherwise lose.
        cause = stderr.strip().splitlines()[-1:] if stderr.strip() else []
        if cause:
            message += f": {cause[0]}"
        raise BridgeError(message, code)
    return ScanOutput(stdout=proc.stdout, stderr=stderr)
