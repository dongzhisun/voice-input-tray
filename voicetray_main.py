"""PyInstaller entry point (top-level script, not a package module)."""
from voicetray.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
