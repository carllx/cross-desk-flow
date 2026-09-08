"""Windowless launcher entrypoint for desk-audio-bridge Windows controller.

This launcher ensures that the repository root is placed on sys.path and set as current
working directory before invoking the controller host service, guaranteeing reliable execution
even when Windows Task Scheduler invokes the process from System32 or another arbitrary cwd.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

try:
    os.chdir(REPO_ROOT)
except Exception:
    pass

# Ensure stdout and stderr do not fail when run under windowless pythonw
if sys.stdout is None:
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass

if sys.stderr is None:
    try:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass

from windows.cli import run_host_service

if __name__ == "__main__":
    run_host_service()
