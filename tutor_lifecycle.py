"""Process-local exclusion for generated-question lifecycle operations.

The persisted study state intentionally contains no transient request flags.  A
single plugin instance still has one ``current_question``, so generation and
evaluation must reserve that in-memory slot before either operation awaits an
LLM.  The lifecycle lock is always acquired before a caller reads or writes
``_lock``-protected question state; it is never held across an LLM await.
"""

from __future__ import annotations

import asyncio


_LOCK_ATTR = "_tutor_question_lifecycle_lock"
_OPERATION_ATTR = "_tutor_question_lifecycle_operation"


def _lifecycle_lock(owner: object) -> asyncio.Lock:
    """Return the lazily created per-plugin lifecycle lock.

    Mixins are also exercised directly in lightweight tests, so the lock is
    created on first use instead of requiring every standalone subject to
    initialize it.  Attribute creation contains no await and is atomic on the
    owning event loop.
    """

    lock = getattr(owner, _LOCK_ATTR, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(owner, _LOCK_ATTR, lock)
    return lock


async def reserve_question_lifecycle(owner: object, operation: str) -> str:
    """Reserve the shared question lifecycle, returning a conflicting operation."""

    lock = _lifecycle_lock(owner)
    async with lock:
        active = str(getattr(owner, _OPERATION_ATTR, "") or "").strip()
        if active:
            return active
        setattr(owner, _OPERATION_ATTR, operation)
    return ""


async def release_question_lifecycle(owner: object, operation: str) -> None:
    """Release a reservation owned by *operation*, including cancellation paths."""

    lock = _lifecycle_lock(owner)
    async with lock:
        if getattr(owner, _OPERATION_ATTR, "") == operation:
            setattr(owner, _OPERATION_ATTR, "")
