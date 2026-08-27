"""Replay protection for write endpoints.

A client that loses its connection mid-request cannot tell "never arrived" from
"arrived and the reply was lost". With an `Idempotency-Key` it does not have to:
the key is reserved before the work starts, so the retry either replays the
stored response or is told the original is still running — but it never creates
a second message or a second attachment.

Only completed successes and 4xx failures that are safe to replay are stored. A
5xx or an unexpected crash releases the key, because the right move for the
client then really is to try again.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Callable

from .. import ids
from ..db import Database
from ..errors import ConflictError, IdempotencyConflict, ServiceError
from ..repositories import idempotency as repo

# status_code 0 marks a reservation: the request is running right now.
IN_PROGRESS = 0


def fingerprint(method: str, path: str, body: Any) -> str:
    """Stable hash of what the request asks for, so a reused key is detectable."""
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256()
    digest.update(method.upper().encode())
    digest.update(b"\0")
    digest.update(path.encode())
    digest.update(b"\0")
    digest.update(canonical.encode())
    return digest.hexdigest()


class IdempotencyService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def run(
        self,
        *,
        user_id: str,
        request_key: str | None,
        request_fingerprint: str,
        produce: Callable[[], Any],
    ) -> Any:
        if not request_key:
            return produce()

        state = self.db.write(
            lambda conn: _reserve(conn, user_id, request_key, request_fingerprint),
            label="idempotency_reserve",
        )
        if state["outcome"] == "replay":
            return json.loads(state["response_json"])
        if state["outcome"] == "mismatch":
            raise IdempotencyConflict(
                "同一個 Idempotency-Key 被用在不同的請求上"
            )
        if state["outcome"] == "in_progress":
            raise ConflictError("同一個請求正在處理中，請稍候再確認結果")

        try:
            result = produce()
        except ServiceError as exc:
            if 400 <= exc.status < 500:
                # A deterministic rejection: replaying it beats re-running it.
                self._store(user_id, request_key, request_fingerprint, exc.status, exc.payload())
            else:
                self._release(user_id, request_key)
            raise
        except BaseException:
            self._release(user_id, request_key)
            raise

        self._store(user_id, request_key, request_fingerprint, 200, result)
        return result

    def _store(
        self, user_id: str, request_key: str, request_fingerprint: str, status: int, payload: Any
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str)
        self.db.write(
            lambda conn: repo.remember(
                conn,
                user_id=user_id,
                request_key=request_key,
                fingerprint=request_fingerprint,
                status_code=status,
                response_json=body,
                now_ms=ids.now_ms(),
            ),
            label="idempotency_store",
        )

    def _release(self, user_id: str, request_key: str) -> None:
        self.db.write(
            lambda conn: conn.execute(
                "DELETE FROM idempotency_keys WHERE user_id = ? AND request_key = ?"
                " AND status_code = ?",
                (user_id, request_key, IN_PROGRESS),
            ),
            label="idempotency_release",
        )


def _reserve(
    conn: sqlite3.Connection, user_id: str, request_key: str, request_fingerprint: str
) -> dict[str, Any]:
    now = ids.now_ms()
    existing = repo.lookup(conn, user_id, request_key, now)
    if existing is not None:
        if existing["request_fingerprint"] != request_fingerprint:
            return {"outcome": "mismatch"}
        if existing["status_code"] == IN_PROGRESS:
            return {"outcome": "in_progress"}
        return {"outcome": "replay", "response_json": existing["response_json"]}
    repo.remember(
        conn,
        user_id=user_id,
        request_key=request_key,
        fingerprint=request_fingerprint,
        status_code=IN_PROGRESS,
        response_json="null",
        now_ms=now,
    )
    return {"outcome": "reserved"}
