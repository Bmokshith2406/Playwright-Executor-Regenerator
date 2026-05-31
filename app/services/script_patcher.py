from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Tuple
from datetime import datetime

logger = logging.getLogger("script.patcher")


class ScriptPatcher:
    """
    Deterministic, spacing-safe patcher for async step functions.

    HARD GUARANTEES:
    - Patches EXACTLY one function body
    - Preserves vertical spacing perfectly
    - Preserves indentation depth
    - Never trims surrounding whitespace
    - Idempotent across repeated patches
    - Creates immutable backups
    """

    # ==================================================
    # PUBLIC API
    # ==================================================

    def patch_step(
        self,
        *,
        script_path: str,
        step_function_name: str,
        new_step_body: str,
        backup: bool = True,
    ) -> None:
        script_path = Path(script_path)

        logger.info(
            "SCRIPT PATCH START | file=%s | step=%s | backup=%s",
            script_path,
            step_function_name,
            backup,
        )

        if not script_path.exists():
            raise FileNotFoundError(script_path)

        source = script_path.read_text(encoding="utf-8")

        resolved_name = self._resolve_step_name(source, step_function_name)
        start, end, indent, body_indent = self._locate_step_body(
            source, resolved_name
        )

        original_body = source[start:end]
        original_hash = self._hash(original_body)
        new_hash = self._hash(new_step_body)

        logger.info(
            "SCRIPT PATCH TARGET LOCATED | step=%s | old_hash=%s | new_hash=%s",
            resolved_name,
            original_hash,
            new_hash,
        )

        # --------------------------------------------------
        # IDEMPOTENCY GUARANTEE
        # --------------------------------------------------
        if original_hash == new_hash:
            logger.info(
                "SCRIPT PATCH NO-OP | step=%s | reason=identical_body",
                resolved_name,
            )
            return

        # --------------------------------------------------
        # SAFETY FALLBACK: EMPTY REGENERATED CODE → NO-OP
        # --------------------------------------------------
        if not new_step_body or not new_step_body.strip():
            logger.warning(
                "EMPTY REGENERATED CODE | step=%s | keeping original body",
                resolved_name,
            )
            return

        logger.info(
            "REGENERATED STEP BODY (PRE-PATCH) | step=%s\n"
            "----- BEGIN REGENERATED CODE -----\n%s\n"
            "----- END REGENERATED CODE -----",
            resolved_name,
            new_step_body,
        )

        patched_body = self._format_body(
            new_step_body,
            indent=indent,
            body_indent=body_indent,
            original_body=original_body,
        )

        if backup:
            backup_path = self._write_backup(script_path, source)
            logger.info("SCRIPT BACKUP CREATED | path=%s", backup_path)

        script_path.write_text(
            source[:start] + patched_body + source[end:],
            encoding="utf-8",
        )

        logger.info(
            "SCRIPT PATCH SUCCESS | file=%s | step=%s",
            script_path,
            resolved_name,
        )

    # ==================================================
    # NAME RESOLUTION
    # ==================================================

    def _resolve_step_name(self, source: str, step_name: str) -> str:
        if self._step_exists(source, step_name):
            return step_name

        underscored = f"_{step_name}"
        if self._step_exists(source, underscored):
            logger.warning(
                "SCRIPT PATCH NAME RESOLVED | original=%s | resolved=%s",
                step_name,
                underscored,
            )
            return underscored

        raise RuntimeError(
            f"Step function not found: {step_name} (or {underscored})"
        )

    @staticmethod
    def _step_exists(source: str, step_name: str) -> bool:
        return f"async def {step_name}(" in source

    # ==================================================
    # CORE LOCATION LOGIC (INDENT-SAFE)
    # ==================================================

    def _locate_step_body(
        self,
        source: str,
        step_name: str,
    ) -> Tuple[int, int, str, str]:
        lines = source.splitlines(keepends=True)

        def_idx = None
        for i, line in enumerate(lines):
            if line.lstrip().startswith(f"async def {step_name}("):
                def_idx = i
                break

        if def_idx is None:
            raise RuntimeError(f"Step function not found: {step_name}")

        def_indent = lines[def_idx][: len(lines[def_idx]) - len(lines[def_idx].lstrip())]

        body_start = def_idx + 1
        body_end = body_start

        body_indent = None

        while body_end < len(lines):
            line = lines[body_end]

            if not line.strip():
                body_end += 1
                continue

            current_indent = line[: len(line) - len(line.lstrip())]

            # First real body line defines body indentation
            if body_indent is None:
                body_indent = current_indent
                body_end += 1
                continue

            # Block ends when indentation drops back to def level or less
            if len(current_indent) <= len(def_indent):
                break

            body_end += 1

        start_idx = sum(len(l) for l in lines[:body_start])
        end_idx = sum(len(l) for l in lines[:body_end])

        return start_idx, end_idx, def_indent, body_indent or (def_indent + "    ")

    # ==================================================
    # FORMATTING (SPACING-SAFE)
    # ==================================================

    def _format_body(
        self,
        new_body: str,
        *,
        indent: str,
        body_indent: str,
        original_body: str,
    ) -> str:
        """
        Replace body content WITHOUT altering vertical spacing.

        Strategy:
        - Preserve leading/trailing blank lines
        - Preserve detected body indentation
        """

        original_lines = original_body.splitlines(keepends=True)
        new_lines = new_body.splitlines()

        leading_blanks = self._count_leading_blank_lines(original_lines)
        trailing_blanks = self._count_trailing_blank_lines(original_lines)

        formatted = []

        formatted.extend(original_lines[:leading_blanks])

        for line in new_lines:
            if line.strip():
                formatted.append(f"{body_indent}{line.rstrip()}\n")
            else:
                formatted.append("\n")

        formatted.extend(
            original_lines[len(original_lines) - trailing_blanks :]
        )

        return "".join(formatted)

    @staticmethod
    def _count_leading_blank_lines(lines):
        count = 0
        for l in lines:
            if l.strip():
                break
            count += 1
        return count

    @staticmethod
    def _count_trailing_blank_lines(lines):
        count = 0
        for l in reversed(lines):
            if l.strip():
                break
            count += 1
        return count

    # ==================================================
    # UTILS
    # ==================================================

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:12]

    @staticmethod
    def _write_backup(script_path: Path, content: str) -> Path:
        digest = hashlib.sha256(content.encode()).hexdigest()[:8]
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        backup_path = script_path.with_suffix(
            script_path.suffix + f".bak.{digest}.{timestamp}"
        )
        backup_path.write_text(content, encoding="utf-8")
        return backup_path
