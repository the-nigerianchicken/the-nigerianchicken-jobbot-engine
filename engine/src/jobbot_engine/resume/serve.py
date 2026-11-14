"""The resume loop, run by the resume workflow on GitHub when the app asks for one.

It takes each resume the app has queued, oldest first, makes it in a folder of its own, and answers done or
failed in plain words. One resume going wrong never stops the rest. It ends when none are left.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jobbot_engine.api_client.client import ApiClient, ApiError, ContractError

log = logging.getLogger(__name__)

Make = Callable[[dict[str, Any], Callable[[str], dict[str, Any]], Path], dict[str, Any]]


def serve(api: ApiClient, ask: Callable[[str], dict[str, Any]], make: Make | None = None) -> int:
    """Make every queued resume; the number answered or tried."""
    if make is None:
        # Imported here: it needs the resume extra (pyyaml), which the engine's runs don't install.
        from jobbot_engine.resume.make import make as make_one

        make = make_one
    tried = 0
    while (work := api.next_resume()) is not None:
        tried += 1
        with tempfile.TemporaryDirectory(prefix="resume-") as folder:
            try:
                answer = make(work, ask, Path(folder))
            except Exception as error:
                log.exception("Resume %s went wrong", work["id"])
                answer = {
                    "status": "failed",
                    "file_name": None,
                    "pdf": None,
                    "notes": None,
                    "error": f"Making it went wrong ({type(error).__name__})",
                }
        try:
            api.answer_resume(work["id"], answer)
        except (ApiError, ContractError) as error:
            log.error("Resume %s wasn't answered: %s", work["id"], error)
    return tried
