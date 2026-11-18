"""Core-only synthetic VFS/blob round trip; no model, sandbox or scheduler service."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from foliorun import Limits, LocalArtifactStore, MemoryFS, WorkerBindings, __version__
from foliorun.vfs import LazyFiles, freeze_vfs


async def main():
    data = b"Entirely fictional Foliorun specimen\x00\xff\n"
    with TemporaryDirectory(prefix="foliorun-offline-") as directory:
        store = LocalArtifactStore(Path(directory) / "store", "synthetic-example")
        bindings = WorkerBindings()
        snapshot = await freeze_vfs(
            MemoryFS({"/documents/synthetic.bin": data}), store, Limits(), 3600, bindings=bindings
        )
        file = snapshot.files[0]
        lazy = LazyFiles(store, bindings, {file.path: file}, Limits())
        assert await lazy.read(file.path) == data
        assert store.read(file.blob) == data
        destination = Path(directory) / "synthetic.bin"
        await store.download_to(file, destination, expires_at=9999999999)
        assert destination.read_bytes() == data
        print(f"foliorun {__version__}: synthetic VFS/blob round trip passed ({len(data)} bytes)")


if __name__ == "__main__":
    asyncio.run(main())
