"""Gộp thực thể khi GHI (PR write): tên mới trỏ về thực thể đã có theo tên chuẩn (`canon`), theo alias, rồi (tuỳ chọn) theo embedding.

`set_embed_matcher(fn)` là chỗ cắm cho PR retrieval (bge-m3, `cfharness[vector]`): `fn(store, scope, owner, name) -> dict | None`
trả thực thể gần nhất đủ ngưỡng. Chưa cắm → chỉ khớp tất định (canon + alias). Gộp HAI thực thể đã có (LLM xác nhận) là việc
của consolidation (PR quality) — module này chỉ tránh đẻ thực thể trùng mới.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from .text import canon

log = logging.getLogger("cfharness.graph")

EmbedMatcher = Callable[[Any, str, str, str], "dict[str, Any] | None"]
_embed_matcher: EmbedMatcher | None = None


def set_embed_matcher(fn: EmbedMatcher | None) -> None:
    """Cắm (hoặc gỡ với None) bộ khớp thực thể bằng embedding. Lỗi của bộ khớp không bao giờ chặn ghi."""
    global _embed_matcher
    _embed_matcher = fn


def embed_matcher() -> EmbedMatcher | None:
    return _embed_matcher


def resolve_entity(store: Any, scope: str, owner: str | None, name: str, aliases: Iterable[str] = ()) -> dict[str, Any] | None:
    """Thực thể CÒN DÙNG trong đúng tầng mà `name` (hoặc một alias của nó) chỉ tới; None = thực thể mới."""
    e = store.find_entity(scope, owner, name)
    if e is not None:
        return e
    for a in aliases:
        if canon(a) and canon(a) != canon(name):
            e = store.find_entity(scope, owner, a)
            if e is not None:
                return e
    fn = _embed_matcher
    if fn is not None:
        try:
            return fn(store, scope, str(owner or ""), name)
        except Exception:  # pragma: no cover - bộ khớp ngoài không được làm hỏng ghi
            log.exception("embed matcher lỗi với %r", name)
    return None
