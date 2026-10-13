"""Give each script/stylesheet its content identity across browser/edge caches."""

from functools import lru_cache
from hashlib import sha256
from pathlib import Path
import re

ASSET = re.compile(
    r"(?P<attribute>\b(?:src|href))=(?P<quote>[\"\'])"
    r"(?P<path>/static/[^\"\'?#]+\.(?:js|css))(?P=quote)"
)


@lru_cache(maxsize=4)
def versioned_shell(index_path: Path, static_root: Path) -> str:
    """Release files are immutable for the life of the application process."""
    static_root = static_root.resolve()

    def replace(match):
        relative = match["path"].removeprefix("/static/")
        asset = (static_root / relative).resolve()
        if not asset.is_relative_to(static_root) or not asset.is_file():
            raise ValueError("Shell references an invalid static asset")
        revision = sha256(asset.read_bytes()).hexdigest()[:24]
        quote = match["quote"]
        return f"{match['attribute']}={quote}{match['path']}?v={revision}{quote}"

    return ASSET.sub(replace, index_path.read_text(encoding="utf-8"))
