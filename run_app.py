from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


# Packages whose import name differs from the pip name
PACKAGE_IMPORT_MAP = {
    "python-dotenv": "dotenv",
}


def _iter_required_packages(root: Path) -> list[str]:
    """Read requirements.txt and return a list of package names (no versions)."""
    req_path = root / "requirements.txt"
    if not req_path.is_file():
        return []

    pkgs: list[str] = []
    for line in req_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Strip env markers and version specifiers
        base = (
            line.split(";", 1)[0]
            .split("==", 1)[0]
            .split(">=", 1)[0]
            .split("~=", 1)[0]
            .strip()
        )
        if base:
            pkgs.append(base)

    return pkgs


def _find_missing_packages(root: Path) -> list[str]:
    """Return a list of packages from requirements.txt that cannot be imported."""
    missing: list[str] = []
    for pkg in _iter_required_packages(root):
        import_name = PACKAGE_IMPORT_MAP.get(pkg, pkg.replace("-", "_"))
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pkg)
    return missing


def _ensure_dependencies(root: Path) -> None:
    """
    Ensure all packages in requirements.txt are installed.

    In a frozen EXE (PyInstaller), we skip this and assume everything was
    bundled correctly.
    """
    # If we're running as a frozen executable, skip pip stuff
    if getattr(sys, "frozen", False):
        return

    missing = _find_missing_packages(root)
    if not missing:
        return

    print("The following Python packages are missing:")
    for pkg in missing:
        print(f"  - {pkg}")
    print("\nAttempting to install them with:")
    print("  pip install -r requirements.txt\n")

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
        cwd=root,
    )

    if result.returncode != 0:
        raise SystemExit(
            "Automatic installation failed. Please run:\n"
            "  pip install -r requirements.txt\n"
            "and then re-run:\n"
            "  python run_app.py"
        )

    still_missing = _find_missing_packages(root)
    if still_missing:
        msg_lines = [
            "Some packages are still missing after installation:",
            *[f"  - {pkg}" for pkg in still_missing],
            "",
            "Please check your environment and try:",
            "  pip install -r requirements.txt",
        ]
        raise SystemExit("\n".join(msg_lines))


def _get_project_root() -> Path:
    """
    Return the directory we want to treat as the project root.

    - In normal Python: directory containing this file.
    - In a PyInstaller EXE: directory containing the .exe.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main() -> None:
    root = _get_project_root()
    os.chdir(root)

    # In dev, make sure deps exist. In exe, this is a no-op.
    _ensure_dependencies(root)

    # Run the Streamlit UI
    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", "ui/app.py"],
        cwd=root,
    )


if __name__ == "__main__":
    main()
