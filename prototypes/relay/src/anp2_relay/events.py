"""Pydantic event models (ANP v0.1)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from .crypto import compute_event_id, verify_signature


class Event(BaseModel):
    id: str = Field(..., min_length=64, max_length=64)
    agent_id: str = Field(..., min_length=64, max_length=64)
    created_at: int = Field(..., ge=0)
    kind: int = Field(..., ge=0)
    tags: list[list[str]] = Field(default_factory=list)
    content: str = ""
    sig: str = Field(..., min_length=128, max_length=128)

    @field_validator("id", "agent_id", "sig")
    @classmethod
    def must_be_lower_hex(cls, v: str) -> str:
        v = v.lower()
        int(v, 16)
        return v

    def is_valid(self) -> tuple[bool, str | None]:
        expected_id = compute_event_id(
            self.agent_id, self.created_at, self.kind, self.tags, self.content
        )
        if expected_id != self.id:
            return False, "event id mismatch"
        if not verify_signature(self.id, self.sig, self.agent_id):
            return False, "bad signature"
        return True, None
