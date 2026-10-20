import hashlib
import os
import tempfile
from pathlib import Path

from .rik_client import RikClient


def fetch_pdf(client: RikClient, url: str, cache_dir: Path) -> Path:
    path = cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".pdf")
    if path.exists():
        return path
    data = client.download(url)
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
