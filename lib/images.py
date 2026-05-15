"""Image extraction helpers — extracted from hooks/log_event.py (task-508 Phase 0 Spike B).

Verbatim transplant of:
  - ALLOWED_IMAGE_TYPES (module constant)
  - get_images_dir() (helper)
  - extract_images_from_prompt() (UserPromptSubmit content-block extraction)
  - extract_images_from_transcript() (Stop-hook transcript scan)

Byte-identity contract: outputs of these functions MUST match the inline
versions in hooks/log_event.py for all inputs. Verified by
tests/test_spike_b_images_byte_identity.py.

Dependency injection: log_error is taken as a callable parameter (default
no-op) to avoid circular import. log_event.py wires its own log_error.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
from base64 import b64decode
from pathlib import Path
from typing import Any, Callable

# Module constant — verbatim from log_event.py line 69
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp"}


def _noop_log_error(exc: Exception, event_type: str) -> None:
    """Default error sink — no-op. log_event.py wires its own."""
    pass


def get_images_dir(storage_path: Path, session_id: str) -> Path:
    """Get the images directory for a session.

    Verbatim from log_event.py:156-160.
    """
    images_dir = storage_path / "images" / session_id
    images_dir.mkdir(parents=True, exist_ok=True)
    return images_dir


def extract_images_from_prompt(
    prompt: Any,
    storage_path: Path,
    session_id: str,
    event_id: str,
    log_error: Callable[[Exception, str], None] = _noop_log_error,
) -> tuple[str, list[dict[str, Any]]]:
    """
    Extract images from prompt content blocks and save to files.

    When users paste images into Claude Code, the prompt becomes an array
    of content blocks (text + image) rather than a simple string.

    Verbatim from log_event.py:163-269 (only signature change: log_error
    parameterized for dependency injection; default no-op).

    Args:
        prompt: The prompt field - either a string or list of content blocks
        storage_path: Base logging storage directory
        session_id: Current session ID
        event_id: Current event ID for filename uniqueness
        log_error: Callable(Exception, str) for failure reporting (no-op default)

    Returns:
        Tuple of (combined_text, image_references)
        - combined_text: All text blocks concatenated
        - image_references: List of {"type", "path", "media_type", "size"} dicts
    """
    # If prompt is a simple string, return as-is with no images
    if isinstance(prompt, str):
        return prompt, []

    # If not a list, convert to string
    if not isinstance(prompt, list):
        return str(prompt), []

    text_parts = []
    image_refs = []
    images_dir = get_images_dir(storage_path, session_id)

    for idx, block in enumerate(prompt):
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue

        block_type = block.get("type", "")

        if block_type == "text":
            text_parts.append(block.get("text", ""))

        elif block_type == "image":
            source = block.get("source", {})

            # Currently Claude Code uses base64 encoding
            if source.get("type") == "base64":
                # Validate and normalize media type
                media_type = source.get("media_type", "image/jpeg")
                if media_type not in ALLOWED_IMAGE_TYPES:
                    media_type = "image/jpeg"  # Default to jpeg for unknown types

                data = source.get("data", "")

                if data:
                    try:
                        # Decode base64 image data
                        image_bytes = b64decode(data)

                        # Generate content hash for deduplication/identification
                        content_hash = hashlib.sha256(image_bytes).hexdigest()[:12]

                        # Determine file extension from media type
                        ext = mimetypes.guess_extension(media_type) or ".jpg"
                        if ext == ".jpe":  # mimetypes returns .jpe for jpeg
                            ext = ".jpg"

                        # Create filename: hash_eventId_index.ext
                        filename = f"{content_hash}_{event_id}_{idx}{ext}"
                        filepath = images_dir / filename

                        # Save image (skip if already exists - deduplication)
                        if not filepath.exists():
                            filepath.write_bytes(image_bytes)

                        # Add reference to list
                        image_refs.append(
                            {
                                "type": "image",
                                "path": f"images/{session_id}/{filename}",
                                "media_type": media_type,
                                "size": len(image_bytes),
                                "index": idx,
                            }
                        )

                    except Exception as e:
                        # Log error but don't fail - continue processing
                        log_error(e, "ImageExtraction")

            elif source.get("type") == "url":
                # URL-based images - just store the reference
                url = source.get("url", "")
                if url:
                    image_refs.append(
                        {
                            "type": "image",
                            "url": url,
                            "media_type": source.get("media_type", "image/jpeg"),
                            "index": idx,
                        }
                    )

    combined_text = "\n".join(text_parts) if text_parts else ""
    return combined_text, image_refs


def extract_images_from_transcript(
    transcript_path: str,
    storage_path: Path,
    session_id: str,
    log_error: Callable[[Exception, str], None] = _noop_log_error,
) -> dict[int, list[dict[str, Any]]]:
    """
    Extract images from all user messages in Claude's transcript.

    Claude Code doesn't pass image data to hooks, but the transcript contains
    the full message content including images. We extract them here during
    the Stop hook when the transcript is complete.

    Verbatim from log_event.py:459-588 (only signature change: log_error
    parameterized for dependency injection; default no-op).

    Args:
        transcript_path: Path to Claude's transcript JSONL file
        storage_path: Base logging storage directory
        session_id: Current session ID
        log_error: Callable(Exception, str) for failure reporting (no-op default)

    Returns:
        Dictionary mapping user message index (0-based) to list of image references.
        E.g., {0: [{"type": "image", "path": "...", ...}], 2: [...]}
    """
    image_refs_by_msg: dict[int, list[dict[str, Any]]] = {}

    try:
        transcript = Path(transcript_path)
        if not transcript.exists():
            return {}

        lines = transcript.read_text(encoding="utf-8").strip().split("\n")
        user_msg_idx = 0

        for line in lines:
            if not line.strip():
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Only process user messages
            if entry.get("type") != "user":
                continue

            content = entry.get("message", {}).get("content", [])

            # Skip if content isn't a list (no content blocks)
            if not isinstance(content, list):
                user_msg_idx += 1
                continue

            # Look for image blocks in this user message
            images_in_msg = []
            images_dir = get_images_dir(storage_path, session_id)

            for block_idx, block in enumerate(content):
                if not isinstance(block, dict):
                    continue

                if block.get("type") != "image":
                    continue

                source = block.get("source", {})

                # Handle base64 images
                if source.get("type") == "base64":
                    media_type = source.get("media_type", "image/png")
                    data = source.get("data", "")

                    if not data:
                        continue

                    if media_type not in ALLOWED_IMAGE_TYPES:
                        media_type = "image/png"

                    try:
                        # Decode image
                        image_bytes = b64decode(data)

                        # Generate content hash for deduplication
                        content_hash = hashlib.sha256(image_bytes).hexdigest()[:12]

                        # Determine file extension
                        ext = mimetypes.guess_extension(media_type) or ".png"
                        if ext == ".jpe":
                            ext = ".jpg"

                        # Filename includes user message position for correlation
                        filename = f"user{user_msg_idx}_{content_hash}_{block_idx}{ext}"
                        filepath = images_dir / filename

                        # Save image (skip if exists - deduplication)
                        if not filepath.exists():
                            filepath.write_bytes(image_bytes)

                        # Record reference
                        images_in_msg.append(
                            {
                                "type": "image",
                                "path": f"images/{session_id}/{filename}",
                                "media_type": media_type,
                                "size": len(image_bytes),
                                "index": block_idx,
                            }
                        )

                    except Exception as e:
                        log_error(e, "TranscriptImageExtraction")

                # Handle URL-based images
                elif source.get("type") == "url":
                    url = source.get("url", "")
                    if url:
                        images_in_msg.append(
                            {
                                "type": "image",
                                "url": url,
                                "media_type": source.get("media_type", "image/jpeg"),
                                "index": block_idx,
                            }
                        )

            # Store references for this user message if any images found
            if images_in_msg:
                image_refs_by_msg[user_msg_idx] = images_in_msg

            user_msg_idx += 1

    except Exception as e:
        log_error(e, "TranscriptImageExtraction")

    return image_refs_by_msg
