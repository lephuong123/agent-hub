"""Câu hỏi có lựa chọn của agent (UX): `hub.questions` — service.py, RPC `question.*` (rpc.py), CLI `questions`/`answer` (cli.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .service import FINAL, PENDING, QuestionService

if TYPE_CHECKING:
    from ..hub import Hub

__all__ = ["FINAL", "PENDING", "QuestionService", "attach"]


def attach(hub: "Hub") -> None:
    hub.questions = QuestionService(hub)  # type: ignore[attr-defined]
