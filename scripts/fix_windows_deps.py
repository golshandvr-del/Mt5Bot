"""
Fix Windows 7 / Python 3.8 dependency problems (DLL + NumPy version mismatch).

WHY THIS EXISTS
---------------
Two very common, related failures on the Windows 7 (64-bit) + Python 3.8 live
box are:

  1.  A missing WinRT DLL such as
          api-ms-win-core-winrt-string-l1-1-0.dll is missing ...
      This appears when a package (most often NumPy >= 2.0, but also new SciPy /
      pandas / scikit-learn / LightGBM builds) was installed at a version whose
      wheels are compiled against a newer Windows runtime that DOES NOT EXIST on
      Windows 7. Those "api-ms-win-core-winrt-*.dll" files are Windows 8+ only.

  2.  When trying to load a trained model:
          load error: No module named 'numpy._core'
      NumPy 2.x renamed its internals from ``numpy.core`` to ``numpy._core``.
      A model pickled on a NumPy-2 machine cannot be unpickled on a NumPy-1
      machine (and vice-versa). The bot now quarantines such a model file
      automatically, but the ROOT cause is still that the wrong NumPy is
      installed.

Both are cured by forcing the scientific stack back to the exact
Windows-7-friendly, cp38 wheels pinned in requirements.txt.

WHAT THIS SCRIPT DOES
---------------------
  * Prints the currently-installed version of every scientific package.
  * For each package whose installed version does not match the Win7 pin, it
    uninstalls it and reinstalls the pinned version using ONLY pre-built binary
    wheels (--only-binary=:all:) so pip can never try to compile against a
    newer runtime.
  * Verifies that ``import numpy`` works afterwards and prints a clear result.

It is SAFE to run repeatedly. It only touches the packages listed below; it
never edits your config or data. Standard ASCII English only.

USAGE
-----
    python scripts/fix_windows_deps.py            # fix mismatched packages
    python scripts/fix_windows_deps.py --check     # report only, change nothing
    python scripts/fix_windows_deps.py --all       # force-reinstall every pin
"""

from __future__ import annotations

import argparse
import subprocess
import sys


# The exact Windows-7 / Python-3.8 friendly pins. Keep in sync with
# requirements.txt. import_name -> (pip_name, pinned_version, required?)
WIN7_PINS = [
    ("numpy",   "numpy",        "1.24.4", True),
    ("pandas",  "pandas",       "1.5.3",  False),
    ("scipy",   "scipy",        "1.10.1", False),
    ("sklearn", "scikit-learn", "1.3.2",  False),
    ("lightgbm", "lightgbm",    "3.3.5",  False),
]


def _installed_version(import_name):
    """Return the installed version string, or None if not importable."""
    try:
        mod = __import__(import_name)
        return getattr(mod, "__version__", "unknown")
    except Exception:
        return None


def _pip(args):
    """Run a pip command; return True on success."""
    cmd = [sys.executable, "-m", "pip"] + list(args)
    print("    > " + " ".join(cmd))
    try:
        return subprocess.call(cmd) == 0
    except Exception as exc:
        print("    pip error: %s" % exc)
        return False


def _reinstall_pinned(pip_name, version):
    """
    Force the exact pinned version using only pre-built binary wheels so pip
    cannot compile a wheel that links against a Windows 8+ runtime DLL.
    """
    # Uninstall first so a broken/newer build is fully removed.
    _pip(["uninstall", "-y", pip_name])
    spec = "%s==%s" % (pip_name, version)
    ok = _pip([
        "install", "--only-binary=:all:", "--prefer-binary",
        "--force-reinstall", spec,
    ])
    if not ok:
        # Retry without --only-binary as a last resort (source build); this may
        # still succeed on some machines and is better than leaving it broken.
        print("    binary-only install failed; retrying allowing source build")
        ok = _pip(["install", "--force-reinstall", spec])
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Repair Windows 7 / Python 3.8 scientific-stack deps."
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Only report installed versions; change nothing.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Force-reinstall every pinned package even if it already matches.",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    print("=" * 78)
    print("Windows 7 dependency repair")
    print("Python: %s" % sys.version.replace("\n", " "))
    print("Executable: %s" % sys.executable)
    if not sys.platform.startswith("win"):
        print("NOTE: this machine is not Windows. The script still works but the "
              "WinRT-DLL problem only occurs on Windows 7. Proceeding anyway.")
    print("=" * 78)

    mismatched = []
    for import_name, pip_name, version, required in WIN7_PINS:
        have = _installed_version(import_name)
        tag = "REQUIRED" if required else "optional"
        if have is None:
            print("  %-12s (%s): NOT installed  -> want %s"
                  % (import_name, tag, version))
            # Only auto-install required ones; optional stay optional unless --all.
            if required or args.all:
                mismatched.append((import_name, pip_name, version))
        elif have != version:
            print("  %-12s (%s): %s  MISMATCH -> want %s"
                  % (import_name, tag, have, version))
            mismatched.append((import_name, pip_name, version))
        else:
            print("  %-12s (%s): %s  OK" % (import_name, tag, have))
            if args.all:
                mismatched.append((import_name, pip_name, version))

    if args.check:
        print("-" * 78)
        print("--check mode: no changes made.")
        return 0

    if not mismatched:
        print("-" * 78)
        print("Nothing to fix - all pinned packages already match. If you still "
              "see the WinRT DLL error, run with --all to force a clean "
              "reinstall of the whole scientific stack.")
        return 0

    print("-" * 78)
    print("Reinstalling %d package(s) at their Windows-7 pins..." % len(mismatched))
    failures = []
    for import_name, pip_name, version in mismatched:
        print("\n[%s] -> %s==%s" % (import_name, pip_name, version))
        if not _reinstall_pinned(pip_name, version):
            failures.append(pip_name)

    print("=" * 78)
    # Final verification: numpy MUST import cleanly (it is the root of the WinRT
    # DLL / numpy._core problems).
    numpy_ver = _installed_version("numpy")
    if numpy_ver is None:
        print("RESULT: FAILED - numpy still does not import. If you saw a missing "
              "'api-ms-win-core-winrt-*.dll' error, install the Microsoft "
              "Visual C++ 2015-2019 x64 redistributable (see "
              "installer/install_vcredist.ps1) and re-run this script.")
        return 1
    print("numpy imports OK, version %s" % numpy_ver)
    if failures:
        print("WARNING: these packages could not be reinstalled: %s"
              % ", ".join(failures))
        print("The bot still runs (it degrades gracefully), but reinstall them "
              "manually for full ML performance.")
    print("RESULT: done. Now delete any stale model and retrain:")
    print("    python main.py --mode train")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
