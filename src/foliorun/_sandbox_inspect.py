"""Stdlib-only bounded inspection, also sent as trusted source to the run's sandbox.

The command entrypoint fixes the root to /output. No model-selected executable,
root, environment access or filesystem service download is used.
"""

import errno
import hashlib
import json
import os
import stat
import sys


class InspectionFault(Exception):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def text_page(body, offset, length, budget):
    try:
        text = body.decode("utf-8")
    except UnicodeError:
        raise InspectionFault("file_not_utf8") from None
    if any((ord(c) < 32 and c not in "\t\n\r") or 127 <= ord(c) < 160 for c in text):
        raise InspectionFault("file_not_text")

    def result(count):
        value = text[offset : offset + count]
        return {
            "kind": "json",
            "value": {
                "text": value,
                "next_offset": offset + len(value),
                "eof": offset + len(value) >= len(text),
            },
        }

    # Budget includes the JSON envelope and escaping, not just visible characters.
    low, high = 0, min(length, max(0, len(text) - offset))
    while low < high:
        middle = (low + high + 1) // 2
        if len(encoded(result(middle))) <= budget:
            low = middle
        else:
            high = middle - 1
    page = result(low)
    if len(encoded(page)) > budget or (low == 0 and offset < len(text)):
        raise InspectionFault("file_page_budget")
    return page


def inspect_output(root, path, limit, *, offset=0, length=1, budget=32768, read=False):
    # Host also validates paths. Repeat here before touching any filesystem entry.
    parts = path.split("/")
    if (
        len(parts) < 3 or parts[:2] != ["", "output"]
        or any(p in {"", ".", ".."} for p in parts[2:])
        or "\\" in path or any(ord(c) < 32 for c in path)
        or len(path) > 1024 or len(parts) > 20
    ):
        raise InspectionFault("invalid_output_path")
    fd = None
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        # Open every root/parent component without following links, then anchor
        # the leaf read to that directory descriptor (no check/open race).
        for part in [*root.split("/")[1:], *parts[2:-1]]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(leaf, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise InspectionFault("output_unsafe_file")
            if before.st_size > limit:
                raise InspectionFault("file_too_large")
            body = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if len(body) > limit:
                raise InspectionFault("file_too_large")
            fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
            if len(body) != before.st_size or any(
                getattr(before, f) != getattr(after, f) for f in fields
            ):
                raise InspectionFault("output_changed")
        if read:
            return text_page(body, offset, length, budget)
        return {"kind": "json", "value": {
            "path": path, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
        }}
    except OSError as exc:
        code = "output_missing" if exc.errno == errno.ENOENT else "output_unsafe_file"
        raise InspectionFault(code) from None
    finally:
        if fd is not None:
            os.close(fd)


def main():
    try:
        args = json.loads(sys.argv[1])
        result = inspect_output("/output", **args)
    except InspectionFault as exc:
        result = {"error": str(exc)}
    except Exception:
        result = {"error": "file_inspection_failed"}
    print(encoded(result).decode())


if __name__ == "__main__":
    main()
