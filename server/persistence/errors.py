"""Persistence-layer exceptions (transport-agnostic, zero FastAPI dependency)."""

from __future__ import annotations


class PersistenceError(Exception):
    """Base error raised by the persistence layer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class NotFoundError(PersistenceError):
    """Raised when a requested entity does not exist."""


class ConflictError(PersistenceError):
    """Raised when a state transition or idempotency constraint conflicts."""


class UnauthorizedError(PersistenceError):
    """Raised when a token or credential check fails."""


class ValidationError(PersistenceError):
    """Raised when persisted payload data violates domain invariants."""
