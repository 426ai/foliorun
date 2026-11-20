import os, tempfile
from pathlib import Path
import pytest
PACK_ENV = "TENDER_HARNESS_TEST_PACK"
def pack_path():
    v = os.environ.get(PACK_ENV)
    return Path(v) if v else None
_cache = {}
def offline_fixture():
    """One deterministic offline three-stage run of the external pack into a fresh temp dir per test session."""
    if "root" not in _cache:
        from tender_harness.offline import run_offline
        from tender_harness.pack import load_project_pack
        p = pack_path()
        if p is None:
            pytest.skip(PACK_ENV + " not set", allow_module_level=True)
        pack = load_project_pack(p); root = Path(tempfile.mkdtemp(prefix="th-test-")) / "root"
        run_offline(pack, root); _cache.update(root=root, pack=pack)
    return _cache["pack"], _cache["root"]
