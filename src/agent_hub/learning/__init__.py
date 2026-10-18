"""Lõi học M4-A (FR-LN-01..07, 11, 15, 18, 19; FR-SG-05/06 phần học; FR-RT-10/17; FR-OB-04).

| Module | Vai trò |
|---|---|
| `service.py`   | `LearningService` (`hub.learning`), `attach(hub)`, listener bus, vòng nền, `learnself_call` (MCP hub-self) |
| `signals.py`   | Signal có cấu trúc + bộ phát hiện (steer phủ định, stop, revert, sửa output, lỗi lặp, chuỗi tool lặp) |
| `review.py`    | Trigger + hàng đợi review (gộp newest-wins) + review fork `claude --resume --fork-session` |
| `pipeline.py`  | Proposal + tự áp dụng L1/L2/L3 (không hàng chờ duyệt), rate limit, eval gate (hợp đồng B) |
| `guard.py`     | Guard học + bất biến chính sách an toàn (policy_touch / self_escalation / bounds / guard_rule) |
| `ledger.py`    | Hàm áp dụng DUY NHẤT: snapshot content-addressed → ghi → LedgerEntry; rollback từng entry/chuỗi |
| `policy.py`    | `policy/learning.yaml`: công tắc học, config_bounds (Q-20), rate (Q-19) — chỉ người sửa |
| `runtime.py`   | Lắp system prompt (memory + skill index, frozen theo session) + MCP cho run review/knowledge |
| `prompts/`     | Prompt Hermes NGUYÊN VĂN (MIT, NOTICE) + đổi tên tool |
| `threats.py`   | Quét injection/exfil/Unicode ẩn (phỏng Hermes) + mẫu chạm chính sách hub |
| `cache.py`     | FR-OB-04 cache hit + cảnh báo hash prompt đổi nhiều |
"""

from .service import LearningService, attach

__all__ = ["LearningService", "attach"]
