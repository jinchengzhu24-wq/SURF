"""Request identity rules for ordinary messages and their persisted retries."""

import hashlib

from cocreation.persistence.database import dump_json


def message_request_signature(
    content: str,
    *,
    base_version_id: str,
    request_proposal: bool,
    action: str,
    source_turn_id: str | None,
) -> str:
    """Return the signature used by persisted message-understanding failures."""
    # Field order and compact JSON are part of the stored signature format.
    binding = {
        "content": content,
        "baseVersionId": base_version_id,
        "requestProposal": request_proposal,
        "action": action,
        "sourceTurnId": source_turn_id,
    }
    return hashlib.sha256(dump_json(binding).encode("utf-8")).hexdigest()


def conflicts_with_saved_user_turn(saved_turn, content: str, base_version_id: str) -> bool:
    """Check the content and Stage of a user turn looked up by session and key."""
    return saved_turn is not None and (
        saved_turn["content"] != content or saved_turn["version_id"] != base_version_id
    )
