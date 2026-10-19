import hashlib
from pathlib import Path

from .rik_client import RikClient


def fetch_pdf(client: RikClient, url: str, cache_dir: Path) -> Path:
    path = cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".pdf")
    if path.exists():
        return path
    data = client.download(url)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path
