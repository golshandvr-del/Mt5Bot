"""
MT5 Smart Trading Bot - installation helper (Windows 7, Python 3.8.x).

This script is invoked by install.bat AFTER a suitable Python interpreter has
been located/installed. It performs the robust, retrying part of the install:

  1. Verifies the Python version is compatible (warns loudly if not 3.8.x on
     Windows 7, but continues so advanced users on newer OS can still use it).
  2. Ensures pip exists and is reasonably up to date (best-effort, offline-safe).
  3. Installs project dependencies from requirements.txt with:
        - a global retry loop,
        - per-package fallback so one failing OPTIONAL wheel never aborts the
          whole install,
        - clear, ASCII-only progress and error messages.
  4. Verifies the install by importing the core modules and printing a report.
  5. Generates synthetic sample data (if none exists) so the bot can be run
     immediately for a first smoke test even without MetaTrader5.

Design goals
------------
- NEVER crash with a raw traceback in the user's face; every failure is caught
  and reported as a readable message with a suggested fix.
- Distinguish REQUIRED packages from OPTIONAL ones. Only REQUIRED failures make
  the final status "incomplete"; optional failures are reported as warnings.
- Standard ASCII English only, everywhere.

Exit codes
----------
  0  : install verified OK (all REQUIRED deps import).
  1  : install finished but one or more REQUIRED deps are missing.
  2  : a fatal environment problem (e.g. no pip and cannot bootstrap).
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import time


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
INSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(INSTALLER_DIR)
REQUIREMENTS = os.path.join(PROJECT_ROOT, "requirements.txt")


# --------------------------------------------------------------------------- #
# Package classification for verification and fallback behavior.
# Each entry maps the import name -> (pip name, required?).
# --------------------------------------------------------------------------- #
CORE_IMPORTS = [
    # import_name,      pip_name,          required
    ("yaml",            "PyYAML",          False),   # loader has a fallback
    ("numpy",           "numpy",           True),
    ("pandas",          "pandas",          False),
    ("requests",        "requests",        False),   # news has urllib fallback
]

# On Windows we also want the MetaTrader5 bridge (required there for live use).
if sys.platform.startswith("win"):
    CORE_IMPORTS.insert(0, ("MetaTrader5", "MetaTrader5", True))

OPTIONAL_IMPORTS = [
    ("sklearn",         "scikit-learn",    False),
    ("lightgbm",        "lightgbm",        False),
    ("vaderSentiment",  "vaderSentiment",  False),
    ("feedparser",      "feedparser",      False),
]


# --------------------------------------------------------------------------- #
# Small console helpers (ASCII only).
# --------------------------------------------------------------------------- #
def line(char: str = "-", width: int = 78) -> None:
    print(char * width)


def info(msg: str) -> None:
    print("[INFO]  " + msg)


def warn(msg: str) -> None:
    print("[WARN]  " + msg)


def err(msg: str) -> None:
    print("[ERROR] " + msg)


def ok(msg: str) -> None:
    print("[ OK ]  " + msg)


# --------------------------------------------------------------------------- #
# Step 1: Python version check.
# --------------------------------------------------------------------------- #
def check_python() -> None:
    line("=")
    info("Python interpreter: %s" % sys.executable)
    info("Python version    : %s" % sys.version.replace("\n", " "))
    major, minor = sys.version_info[:2]
    if sys.platform.startswith("win"):
        if (major, minor) != (3, 8):
            warn(
                "You are on Windows but not running Python 3.8.x. Python 3.8 is "
                "the recommended (and last Windows-7-supported) line. Pinned "
                "wheels in requirements.txt target cp38; other versions may "
                "fail to find matching wheels."
            )
        else:
            ok("Python 3.8.x detected - matches the pinned Windows 7 wheels.")
    else:
        info("Non-Windows platform detected (development mode). "
             "MetaTrader5 is skipped; the bot runs offline from CSV data.")
    line("=")


# --------------------------------------------------------------------------- #
# Step 2: ensure pip.
# --------------------------------------------------------------------------- #
def ensure_pip() -> bool:
    """Return True if pip is usable (bootstrapping it via ensurepip if needed)."""
    try:
        import pip  # noqa: F401
        ok("pip is available.")
        return True
    except Exception:
        warn("pip not found; attempting to bootstrap with ensurepip...")
    try:
        subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
        ok("pip bootstrapped via ensurepip.")
        return True
    except Exception as exc:
        err("Could not bootstrap pip automatically: %s" % exc)
        err("Fix: reinstall Python 3.8.x with the 'pip' option checked, "
            "or run 'python -m ensurepip' manually, then re-run install.bat.")
        return False


def upgrade_pip_tools() -> None:
    """Best-effort upgrade of pip/setuptools/wheel. Never fatal."""
    info("Upgrading pip, setuptools, wheel (best-effort)...")
    try:
        subprocess.call([
            sys.executable, "-m", "pip", "install", "--upgrade",
            "pip", "setuptools", "wheel",
        ])
    except Exception as exc:
        warn("pip self-upgrade skipped: %s" % exc)


# --------------------------------------------------------------------------- #
# Step 3: install dependencies with retries and per-package fallback.
# --------------------------------------------------------------------------- #
def _pip_install(args, retries: int = 3, sleep_s: int = 4) -> bool:
    """Run 'pip install <args>' with a retry loop. Returns True on success."""
    cmd = [sys.executable, "-m", "pip", "install", "--prefer-binary"] + list(args)
    for attempt in range(1, retries + 1):
        info("pip install attempt %d/%d: %s" % (attempt, retries, " ".join(args)))
        try:
            rc = subprocess.call(cmd)
            if rc == 0:
                return True
            warn("pip returned exit code %d." % rc)
        except Exception as exc:
            warn("pip invocation error: %s" % exc)
        if attempt < retries:
            info("Retrying in %d seconds..." % sleep_s)
            time.sleep(sleep_s)
    return False


def install_requirements() -> None:
    """
    Try the fast path (install the whole requirements.txt at once). If that
    fails (usually one optional wheel), fall back to installing packages one by
    one so a single optional failure does not abort everything.
    """
    line("=")
    info("Installing project dependencies from requirements.txt ...")
    if not os.path.exists(REQUIREMENTS):
        err("requirements.txt not found at %s" % REQUIREMENTS)
        return

    # Fast path: whole file at once.
    if _pip_install(["-r", REQUIREMENTS], retries=2):
        ok("All dependencies installed via requirements.txt.")
        return

    warn("Bulk install failed; switching to per-package install so optional "
         "wheels cannot block the required ones.")

    # Per-package fallback. Required first, then optional.
    required = [(imp, pip, req) for (imp, pip, req) in CORE_IMPORTS if req]
    core_optional = [(imp, pip, req) for (imp, pip, req) in CORE_IMPORTS if not req]
    for imp, pip_name, _ in required:
        if not _pip_install([pip_name], retries=3):
            err("REQUIRED package '%s' failed to install. The bot may not run "
                "fully until this is resolved." % pip_name)
    for imp, pip_name, _ in core_optional + OPTIONAL_IMPORTS:
        if not _pip_install([pip_name], retries=2):
            warn("Optional package '%s' failed to install; the bot will use a "
                 "built-in fallback and keep running." % pip_name)


# --------------------------------------------------------------------------- #
# Step 4: verify the install by importing modules.
# --------------------------------------------------------------------------- #
def verify_imports() -> bool:
    """Import each dependency and the bot's own core. Returns True if all
    REQUIRED imports succeed."""
    line("=")
    info("Verifying installation by importing modules...")
    all_required_ok = True

    def _check(import_name, pip_name, required):
        nonlocal all_required_ok
        try:
            importlib.import_module(import_name)
            ok("import %-14s -> present" % import_name)
        except Exception as exc:
            if required:
                all_required_ok = False
                err("import %-14s -> MISSING (required). pip name: %s. %s"
                    % (import_name, pip_name, exc))
            else:
                warn("import %-14s -> missing (optional). pip name: %s"
                     % (import_name, pip_name))

    for imp, pip_name, req in CORE_IMPORTS:
        _check(imp, pip_name, req)
    for imp, pip_name, req in OPTIONAL_IMPORTS:
        _check(imp, pip_name, req)

    # Now verify the bot's own package imports cleanly.
    info("Importing the bot's own modules (config + core)...")
    sys.path.insert(0, PROJECT_ROOT)
    try:
        from config.loader import load_config  # noqa: F401
        import core.indicators  # noqa: F401  (registers all indicators)
        from core.indicators.registry import list_indicators
        from app.context import BotContext  # noqa: F401
        names = list_indicators()
        ok("Bot package imports cleanly. %d indicators registered." % len(names))
        info("Indicators: %s" % ", ".join(names))
    except Exception as exc:
        all_required_ok = False
        err("Bot package failed to import: %s" % exc)

    return all_required_ok


# --------------------------------------------------------------------------- #
# Step 5: generate sample data for an immediate first run.
# --------------------------------------------------------------------------- #
def ensure_sample_data() -> None:
    """Create synthetic CSV history if the history folder is empty."""
    line("=")
    hist_dir = os.path.join(PROJECT_ROOT, "data_store", "history")
    has_csv = os.path.isdir(hist_dir) and any(
        f.lower().endswith(".csv") for f in os.listdir(hist_dir)
    ) if os.path.isdir(hist_dir) else False
    if has_csv:
        ok("Sample/history CSV data already present; skipping generation.")
        return
    info("No history CSV found; generating synthetic sample data for a first "
         "offline run...")
    gen = os.path.join(PROJECT_ROOT, "examples", "generate_sample_data.py")
    try:
        rc = subprocess.call([sys.executable, gen])
        if rc == 0:
            ok("Synthetic sample data generated in data_store/history/.")
        else:
            warn("Sample-data generator returned code %d." % rc)
    except Exception as exc:
        warn("Could not generate sample data: %s" % exc)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    line("=")
    print("MT5 Smart Trading Bot - Installer Helper")
    print("Project root: %s" % PROJECT_ROOT)
    line("=")

    check_python()

    if not ensure_pip():
        err("Cannot continue without pip.")
        return 2

    upgrade_pip_tools()
    install_requirements()
    ensure_sample_data()
    all_ok = verify_imports()

    line("=")
    if all_ok:
        ok("INSTALL VERIFIED. You can now run the bot, e.g.:")
        print("      python main.py --mode search     (build strategy memory)")
        print("      python main.py --mode backtest    (report best strategy)")
        print("      python main.py --mode paper       (one paper decision pass)")
        print("   or double-click scripts\\run_bot.bat")
        line("=")
        return 0
    else:
        err("INSTALL INCOMPLETE: one or more REQUIRED packages are missing.")
        err("Re-run install.bat (it retries), check your internet connection, "
            "and confirm you are using Python 3.8.x on Windows 7.")
        line("=")
        return 1


if __name__ == "__main__":
    sys.exit(main())
