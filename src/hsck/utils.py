def get_page_dir(vod_id: int) -> str:
    return str((vod_id + 99) // 100)


def is_absolute_url(url: str) -> bool:
    return url.startswith(("http://", "https://"))


def normalize_thumb_url(raw: str, fallback_host: str = "") -> str:
    """Keep the page's full cover URL; only join a host for relative paths."""
    url = (raw or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if is_absolute_url(url):
        return url
    if not fallback_host:
        return url
    path = url if url.startswith("/") else f"/{url}"
    return fallback_host.rstrip("/") + path


def export_thumb_url(thumd: str, image_host: str) -> str:
    """Prefer a stored absolute cover URL over a global image_host prefix."""
    url = (thumd or "").strip()
    if url.startswith("//"):
        return "https:" + url
    if is_absolute_url(url):
        return url
    host = (image_host or "").rstrip("/")
    if not url:
        return host
    path = url if url.startswith("/") else f"/{url}"
    return host + path
