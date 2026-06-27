"""FileBrowser REST client.

Creates a permanent public share for a project folder so users can browse
and download all pipeline outputs via a single link — no login required.

The share is created once at project-creation time (using the project_id as
the path inside /srv).  The returned hash is stored on the Project row and
included in every analyze / finalize response as ``files_url``.

If TCP_FILEBROWSER_BASE_URL is not set the module is a no-op.
"""
import json
import urllib.error
import urllib.request

from app.core.settings import settings


def filebrowser_enabled() -> bool:
    return bool((settings.filebrowser_base_url or "").strip())


def _get_token() -> str:
    """Login and return the JWT token string."""
    base = settings.filebrowser_base_url.rstrip("/")
    body = json.dumps({
        "username": settings.filebrowser_username or "admin",
        "password": settings.filebrowser_password or "",
    }).encode()
    req = urllib.request.Request(
        f"{base}/api/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode().strip()


def create_project_share(project_id: str) -> str:
    """Create a permanent public share for the project folder.

    Returns the hash string (e.g. 'fNqIKDS3').
    Raises RuntimeError on any failure so the caller can decide whether to
    swallow or surface the error.
    """
    base = settings.filebrowser_base_url.rstrip("/")
    try:
        token = _get_token()
    except Exception as e:
        raise RuntimeError(f"FileBrowser login failed: {e}") from e

    body = json.dumps({}).encode()
    req = urllib.request.Request(
        f"{base}/api/share/{project_id}",
        data=body,
        headers={"Content-Type": "application/json", "X-Auth": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return data["hash"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"FileBrowser share API returned {e.code}: {detail}") from e
    except Exception as e:
        raise RuntimeError(f"FileBrowser share failed: {e}") from e


def share_url(share_hash: str) -> str:
    """Build the user-facing share URL from a stored hash.

    Uses filebrowser_public_url if set (browser-accessible), otherwise
    falls back to filebrowser_base_url.
    """
    base = (settings.filebrowser_public_url or settings.filebrowser_base_url).rstrip("/")
    return f"{base}/share/{share_hash}"
