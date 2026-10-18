"""Prompt của vòng học. `hermes_review.py` / `hermes_curator.py` là văn bản NGUYÊN VĂN của hermes-agent (MIT,
Copyright (c) 2025 Nous Research — xem `NOTICE` ở gốc repo); `render.py` chỉ đổi tên tool và nối phần của hub."""

from .render import TOOL_RENAMES, render_curator_prompt, render_review_prompt, rename_tools

__all__ = ["TOOL_RENAMES", "render_curator_prompt", "render_review_prompt", "rename_tools"]
