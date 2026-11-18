"""Race-resistant local path traversal for captures and explicit downloads (POSIX)."""

import os
from contextlib import contextmanager
from pathlib import Path

from .contracts import RuntimeFault


@contextmanager
def parent_directory(path: Path, *, create=False):
    path = Path(path).absolute()
    if ".." in path.parts or not path.name or path.name in {".", ".."}:
        raise RuntimeFault("unsafe_local_path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError:
                raise RuntimeFault("unsafe_local_path") from None
            os.close(fd)
            fd = child
        yield fd, path.name
    finally:
        os.close(fd)


def open_regular(path: Path):
    with parent_directory(path) as (parent, leaf):
        try:
            return os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except OSError:
            raise RuntimeFault("unsafe_local_path") from None
