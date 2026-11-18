"""Caller-owned storage injection, executable offline with a synthetic S3-shaped client."""

import io
from pathlib import Path
from tempfile import TemporaryDirectory

from foliorun import LocalSessionRepository, S3BlobStore, Storage


class SyntheticObjectClient:
    """Demo only. Replace with your already configured, conditional-write-capable client."""

    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch):
        assert IfNoneMatch == "*"
        key = (Bucket, Key)
        if key in self.objects:
            error = RuntimeError("synthetic precondition failure")
            error.response = {"Error": {"Code": "PreconditionFailed"}}
            raise error
        self.objects[key] = bytes(Body)

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def main():
    client = SyntheticObjectClient()
    with TemporaryDirectory(prefix="foliorun-custom-storage-") as temporary:
        store = Storage(
            blobs=S3BlobStore(
                client=client,
                bucket="fictional-bucket",
                prefix="caller/chosen-prefix",
                scope="fictional-tenant",
            ),
            sessions=LocalSessionRepository(Path(temporary), "fictional-tenant"),
        )
        ref = store.put(b"Entirely fictional custom-storage example")
        assert store.put(store.read(ref)) == ref
        store.cas("example/root", 0, {"blob": ref.model_dump()})
        assert store.gc() == ()
        print("custom storage: injected client, bucket/prefix, CAS and read-back passed")


if __name__ == "__main__":
    main()
