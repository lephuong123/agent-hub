"""Graph memory (docs/dev-notes/plan-graph-memory.md): thực thể + fact có thời gian trên SQLite (hub.db), kiểu Graphiti.

Ghi DUY NHẤT qua `learning.pipeline.propose(target="graph")` → `Ledger.apply(Change(kind="graph"))`. Module này chỉ đọc + lập kế hoạch.
"""

from .store import ENTRY, KEY_PREFIX, SNAPSHOT_FILE, GraphStore, graph_key, is_graph_key, owner_key
from .text import canon, folded, nfc, norm_relation

__all__ = ["ENTRY", "KEY_PREFIX", "SNAPSHOT_FILE", "GraphStore", "graph_key", "is_graph_key", "owner_key", "canon", "folded", "nfc", "norm_relation"]
