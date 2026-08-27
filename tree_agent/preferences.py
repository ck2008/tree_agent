"""Small, non-secret local preferences for the Tree Agent desktop."""

from __future__ import annotations

import json
import os
from pathlib import Path


class DesktopPreferences:
    """Persist local UI choices separately from the encrypted login session."""

    def __init__(self, home: str) -> None:
        self.path = Path(home) / "desktop-preferences.json"

    def _read(self) -> dict[str, object]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def email_verification_prompt_suppressed(self) -> bool:
        return bool(self._read().get("suppress_email_verification_prompt", False))

    def set_email_verification_prompt_suppressed(self, suppressed: bool) -> None:
        data = self._read()
        data["suppress_email_verification_prompt"] = bool(suppressed)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
