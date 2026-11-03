"""Activate optional native plugins installed inside this Python environment."""

import os
from pathlib import Path
import sys


def activate():
    directories = sorted((Path(sys.prefix) / "native/usr/lib").glob("*/gstreamer-1.0"))
    if directories:
        existing = os.environ.get("GST_PLUGIN_PATH", "")
        entries = list(dict.fromkeys([*(str(path) for path in directories), *filter(None, existing.split(":"))]))
        os.environ["GST_PLUGIN_PATH"] = ":".join(entries)
    return directories
