"""Điều phối task (FR-TK-04..13, FR-RT-16): dispatcher (claim nguyên tử, lease/heartbeat, retry có `--resume`, circuit
breaker, timeout), định tuyến theo vai trò/mô tả + tải, lead tách việc bằng `--json-schema`, bước review, tổng hợp kết quả.

- `dispatcher.py` : `TaskDispatcher` (tick + xử lý kết quả run)
- `routing.py`    : chọn worker theo role/description + tải
- `plan.py`       : JSON Schema kế hoạch của lead / phán quyết review + kiểm hợp lệ
- `rpc.py`        : RPC `team.*`, `task.*`, `taskself.*` (hub-self MCP), `workspace.*`, `report.*`, `run.revert`, `agent.clone|export|import`
- `cli.py`        : `agent-hub team …`, `task …`, `run revert`, `report`, `cost`, `agent clone|export|import`
"""

from .dispatcher import TaskDispatcher

__all__ = ["TaskDispatcher"]
