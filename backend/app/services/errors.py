"""Shared error types for transient infrastructure failures."""

from __future__ import annotations


class InfrastructureError(RuntimeError):
    def __init__(self, code: str, message: str, *, stage: str) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage


def infrastructure_failure_code(error: BaseException) -> str | None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "infrastructure_code", None)
        if isinstance(code, str) and code:
            return code
        if isinstance(current, InfrastructureError):
            return current.code
        current = current.__cause__
    return None
