"""Vận hành học (M4-B): theo dõi hồi quy + tự rollback (FR-LN-17), curator pha 1 (FR-LN-08), digest (FR-LN-13),
lịch chạy nền (digest 08:00, eval L1 theo lô, curator khi rảnh). Gắn vào hub bằng `wiring.attach(hub)`.
"""

from .wiring import LearningOps, attach

__all__ = ["LearningOps", "attach"]
