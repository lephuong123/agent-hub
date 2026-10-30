"""Phần CHÍNH SÁCH AN TOÀN của vòng học (FR-SG-05, FR-LN-15, FR-LN-18, FR-LN-19): `HUB_HOME/policy/learning.yaml`.

Gồm công tắc học (`enabled`, `auto_apply` toàn cục / theo team / theo agent — cấp hẹp hơn chỉ được CHẶT hơn),
`config_bounds` (Q-20) và `rate` (Q-19). Chỉ NGƯỜI sửa qua CLI `cfharness learn policy …` (RPC `learn.policy_set`,
có audit + bản sao `policy/history/`); agent không ghi được `policy/` (guard runtime M1) và vòng học không có đường
nào sửa file này (guard học chặn mọi proposal chạm nó → `guard.blocked`).
Tham số không thuộc an toàn (ngân sách review, ngưỡng trigger, hạn mức ký tự…) ở `config.yaml` khoá `learning.*`.
"""

from __future__ import annotations

import copy
import shutil
import time
from pathlib import Path
from typing import Any

import yaml

from ..config import deep_merge, get_path, set_path
from ..errors import HubError

EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")

POLICY_DEFAULTS: dict[str, Any] = {
    "enabled": True,  # FR-LN-15: tắt → không review
    "auto_apply": True,  # tắt → proposal chỉ ghi `suggested`
    # {agent_id: {enabled: bool, auto_apply: bool, shared_memory_write: bool}} — shared_memory_write: tool `memory` của agent được ghi
    # tầng team/global (mặc định chỉ lead ghi tầng team của mình, điều phối viên ghi mọi tầng)
    "agents": {},
    # Q-20 — biên cấu hình vòng học được tự đặt (FR-LN-19)
    "config_bounds": {
        "models": ["opus", "sonnet", "haiku"],
        "max_budget_usd_per_run": 5.0,
        "budget_usd_per_day": 20.0,
        "max_concurrent_runs": 3,
        "max_effort": "high",
    },
    # Q-19 — giới hạn tần suất (FR-LN-18)
    "rate": {
        "max_changes_per_agent_per_day": 5,
        "max_config_changes_per_agent_per_week": 2,
        "max_team_global_changes_per_day": 3,
        "cooldown_per_target_h": 24,
        # tool `memory` của agent (kiểu Hermes: không giới hạn số lần ghi; trần ký tự kho là thứ hãm). 0 = không giới hạn.
        # Không dùng chung suất với review (max_changes_per_agent_per_day / max_team_global_changes_per_day).
        "agent_tool_max_per_day": 0,
    },
}


class LearningPolicy:
    def __init__(self, hub_home: Path) -> None:
        self.dir = hub_home / "policy"
        self.path = self.dir / "learning.yaml"
        self._cache: tuple[float | None, dict[str, Any]] = (None, {})

    def _mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    @property
    def data(self) -> dict[str, Any]:
        m = self._mtime()
        if m != self._cache[0]:
            over: dict[str, Any] = {}
            if self.path.exists():
                loaded = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
                if not isinstance(loaded, dict):
                    raise HubError(f"{self.path}: phải là mapping YAML")
                over = loaded
            self._cache = (m, over)
        return deep_merge(POLICY_DEFAULTS, self._cache[1])

    def get(self, key: str | None = None) -> Any:
        d = self.data
        return d if not key else get_path(d, key)

    def set(self, key: str, value: Any) -> dict[str, Any]:
        """CHỈ người gọi (qua RPC có audit). Ghi bản sao lịch sử trước khi sửa."""
        _validate(key, value)
        self.dir.mkdir(parents=True, exist_ok=True)
        over: dict[str, Any] = {}
        if self.path.exists():
            over = copy.deepcopy(yaml.safe_load(self.path.read_text(encoding="utf-8")) or {})
            hist = self.dir / "history"
            hist.mkdir(exist_ok=True)
            shutil.copy2(self.path, hist / f"learning-{time.strftime('%Y%m%dT%H%M%S')}-{time.time_ns() % 1_000_000:06d}.yaml")
        set_path(over, key, value)
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(over, allow_unicode=True, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        self._cache = (None, {})
        return self.data

    # ------------------------------------------------------------------ công tắc hiệu lực (cấp hẹp chỉ chặt hơn)
    def switches(self, agent_id: str | None, team_policies: list[dict[str, Any]] | None = None) -> dict[str, bool]:
        d = self.data
        enabled, auto = bool(d.get("enabled", True)), bool(d.get("auto_apply", True))
        for tp in team_policies or []:
            lt = (tp or {}).get("learning") or {}
            enabled = enabled and bool(lt.get("enabled", True))
            auto = auto and bool(lt.get("auto_apply", True))
        if agent_id:
            a = (d.get("agents") or {}).get(agent_id) or {}
            enabled = enabled and bool(a.get("enabled", True))
            auto = auto and bool(a.get("auto_apply", True))
        return {"enabled": enabled, "auto_apply": auto and enabled}

    def shared_memory_write(self, agent_id: str) -> bool:
        return bool(((self.data.get("agents") or {}).get(agent_id) or {}).get("shared_memory_write"))


def _validate(key: str, value: Any) -> None:
    top = key.split(".")[0]
    if top not in POLICY_DEFAULTS:
        raise HubError(f"khoá chính sách học không hợp lệ: {key} (có: {', '.join(POLICY_DEFAULTS)})")
    if key in ("enabled", "auto_apply") or key.endswith((".enabled", ".auto_apply", ".shared_memory_write")):
        if not isinstance(value, bool):
            raise HubError(f"{key} phải là true/false")
    if key == "config_bounds.max_effort" and value not in EFFORT_ORDER:
        raise HubError(f"max_effort phải là một trong {EFFORT_ORDER}")
    if key.startswith("rate.") and (not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0):
        raise HubError(f"{key} phải là số ≥ 0")
