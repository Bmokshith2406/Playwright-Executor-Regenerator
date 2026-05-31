from typing import Optional
import logging
from app.core.utils import extract_quoted

logger = logging.getLogger("base_extractor")


class BaseExtractor:
    """
    Common base class for all action extractors.
    Provides common validation and extraction logic.
    """

    def __init__(self) -> None:
        self._last_step_intent = ""
        self._last_original_code = ""
        self._last_dom_snapshot = ""

    def _extract_quoted(self, text: str) -> Optional[str]:
        """
        Extracts the first quoted string.
        """
        return extract_quoted(text)

    def _literal_exists_in_sources(self, value: str) -> bool:
        """
        Ensures the extracted visible text exists in step intent,
        original code, or the pruned DOM snapshot.
        Case-sensitive match.
        """
        sources = [
            getattr(self, "_last_step_intent", "") or "",
            getattr(self, "_last_original_code", "") or "",
            getattr(self, "_last_dom_snapshot", "") or "",
        ]

        for src in sources:
            if value in src:
                return True

        return False
