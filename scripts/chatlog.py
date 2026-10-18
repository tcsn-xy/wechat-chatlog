#!/usr/bin/env python3
"""Public read-only CLI entry point; independent of current working directory."""
from chatlog_core.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
