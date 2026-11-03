"""One literal KEY=VALUE grammar for launch, bootstrap, fleet and monitoring."""

from functools import lru_cache
import argparse
import logging
from pathlib import Path
import re
import shlex
import sys


@lru_cache(maxsize=32)
def _read(path, modified, size):
    values = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if match is None:
            logging.getLogger(__name__).warning("Ignoring non-assignment in %s line %s", path, number)
            continue
        key, value = match.groups()
        value = value.strip()
        if value.startswith(("'", '"')):
            lexer = shlex.shlex(value, posix=True)
            lexer.whitespace = ""
            lexer.commenters = ""
            try:
                value = "".join(lexer)
            except ValueError as exc:
                raise ValueError(f"Invalid quoting in {path} line {number}") from exc
        if "\0" in value:
            raise ValueError(f"NUL byte in {path} line {number}")
        values[key] = value
    return values


def load_env_file(path):
    if not path or not Path(path).is_file():
        return {}
    target = Path(path).resolve()
    stat = target.stat()
    return dict(_read(str(target), stat.st_mtime_ns, stat.st_size))


def quote(value):
    value = str(value)
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("environment values must occupy one line")
    return shlex.quote(value)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    args = parser.parse_args()
    for key, value in load_env_file(args.path).items():
        sys.stdout.buffer.write(key.encode() + b"\0" + value.encode() + b"\0")
