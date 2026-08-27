"""Domain errors shared by repositories, services and the HTTP layer.

Every error carries the HTTP status the API should report, so services can stay
free of FastAPI imports and the API layer needs one exception handler instead of
a translation table that drifts.
"""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """Base class. `status` is the HTTP status; `detail` reaches the client."""

    status = 500
    code = "internal_error"

    def __init__(self, detail: str = "", **extra: Any) -> None:
        super().__init__(detail or self.__class__.__name__)
        self.detail = detail or self.code
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        return {"error": self.code, "detail": self.detail, **self.extra}


class ValidationError(ServiceError):
    status, code = 400, "invalid_request"


class AuthenticationError(ServiceError):
    status, code = 401, "unauthenticated"


class PermissionDenied(ServiceError):
    """Also raised for objects the caller may not even know exist.

    A viewer probing ids must not be able to tell "no such project" apart from
    "not yours", so `NotFound` is only raised once read access is established.
    """

    status, code = 403, "forbidden"


class NotFound(ServiceError):
    status, code = 404, "not_found"


class ConflictError(ServiceError):
    """Optimistic-concurrency and name-collision failures.

    `current_revision` is included when the caller sent a stale revision, so a
    client can refetch and retry without a second round trip.
    """

    status, code = 409, "conflict"


class RevisionConflict(ConflictError):
    code = "revision_conflict"


class NameConflict(ConflictError):
    code = "name_conflict"


class IdempotencyConflict(ConflictError):
    code = "idempotency_key_reused"


class PayloadTooLarge(ServiceError):
    status, code = 413, "payload_too_large"


class StorageBusy(ServiceError):
    """The writer queue could not get the write lock within its retry budget."""

    status, code = 503, "storage_busy"
