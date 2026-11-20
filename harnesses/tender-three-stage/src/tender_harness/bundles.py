"""Deterministic content-addressed zip bundles consumed by the sandbox engine."""
import hashlib, json, zipfile
from pathlib import Path
_EPOCH = (1980, 1, 1, 0, 0, 0)
def _data(v):
    return v if isinstance(v, bytes) else Path(v).read_bytes()
def bundle(files, target):
    """files: {member name: Path | bytes}. Refuses to overwrite; fixed timestamps for stable hashes."""
    target = Path(target); target.parent.mkdir(parents=True, exist_ok=True)
    data = {n: _data(v) for n, v in sorted(files.items())}
    for n in data:
        if n.startswith(("/", ".")) or ".." in Path(n).parts or ".env" in n or "\\" in n:
            raise ValueError("unsafe_bundle_member")
    manifest = {"files": {n: hashlib.sha256(b).hexdigest() for n, b in data.items()}}
    with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("bundle-manifest.json", _EPOCH), json.dumps(manifest, sort_keys=True))
        for n, b in data.items():
            z.writestr(zipfile.ZipInfo(n, _EPOCH), b, zipfile.ZIP_DEFLATED)
    return target
def bundle_manifest(path):
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read("bundle-manifest.json"))
