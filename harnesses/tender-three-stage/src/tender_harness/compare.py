"""Read-only comparison of two stage output directories at OOXML member level (zip container metadata ignored)."""
import hashlib, json, zipfile
from pathlib import Path
def members(p):
    with zipfile.ZipFile(p) as z:
        return {n: hashlib.sha256(z.read(n)).hexdigest() for n in z.namelist()}
def compare_docx(a, b):
    ma, mb = members(a), members(b)
    return {"same_members": set(ma) == set(mb), "differing_members": sorted(n for n in set(ma) | set(mb) if ma.get(n) != mb.get(n))}
def compare_dirs(old, new, rename=None):
    rename = rename or {}; out = {}
    for p in sorted(Path(new).iterdir()):
        q = Path(old) / rename.get(p.name, p.name)
        if not q.exists():
            out[p.name] = {"missing_in_baseline": True}
        elif p.suffix == ".docx":
            out[p.name] = compare_docx(q, p)
        else:
            out[p.name] = {"byte_equal": q.read_bytes() == p.read_bytes()}
    return out
