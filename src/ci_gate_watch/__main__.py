"""Allow ``python -m ci_gate_watch`` alongside the installed console script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
