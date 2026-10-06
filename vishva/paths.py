import os
import sys

def _detect_base() -> str:
    env = os.environ.get("VISHVA_HOME", "").strip()
    if env:
        return os.path.abspath(env)
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(pkg_dir)
BASE_DIR = _detect_base()
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
def p(*rel: str) -> str:
    if not rel:
        return BASE_DIR
    if os.path.isabs(rel[0]):
        return os.path.join(*rel)
    return os.path.join(BASE_DIR, *rel)
def cfg_path(config: dict, key: str, default: str) -> str:
    return p(str((config or {}).get(key, default) or default))
