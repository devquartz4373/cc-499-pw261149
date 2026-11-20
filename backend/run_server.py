#!/usr/bin/env python3 -I
"""Standalone launcher: works under Python isolated mode (-I) by adding the
backend directory explicitly before importing the package.

Usage:  python3 -I run_server.py --db ../data/analytics.db --port 8080
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from delivery_analytics.server import main  # noqa: E402

if __name__ == "__main__":
    main()
