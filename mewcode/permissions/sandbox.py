"""Layer 2: filesystem path sandbox (ch06)."""

from __future__ import annotations

import tempfile
from pathlib import Path


class PathSandbox:
    """Confines file tools to project root + temp dir (+ extra allowed roots)."""

    def __init__(
        self, project_root: str, extra_allowed: list[str] | None = None
    ) -> None:
        self._project_root = Path(project_root).resolve()
        roots = [self._project_root, Path(tempfile.gettempdir()).resolve()]
        for extra in extra_allowed or []:
            roots.append(Path(extra).resolve())
        self._allowed_roots = roots

    def check(self, path: str) -> tuple[bool, str]:
        """Return (ok, reason). ok=True means inside the sandbox."""
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self._project_root / p
        try:
            resolved = p.resolve(strict=True)
        except (FileNotFoundError, OSError):
            # New file: resolve the parent (which must exist) then re-attach name.
            try:
                resolved = p.parent.resolve(strict=True) / p.name
            except (FileNotFoundError, OSError):
                resolved = p

        for root in self._allowed_roots:
            try:
                resolved.relative_to(root)
                return True, ""
            except ValueError:
                continue
        return False, f"路径 {path} 超出沙箱范围"
