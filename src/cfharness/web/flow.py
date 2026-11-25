"""Luồng việc ngay trong chat (`docs/dev-notes/ux-flow-in-chat.md`): `GET /api/manager/flow` — một ảnh chụp gọn cho island Điều phối:

- `need`: khay "Cần bạn" ghim trên ô nhập — thao tác phá huỷ chờ duyệt + câu hỏi chờ BẠN của MỌI agent, đếm bằng đúng định nghĩa của huy hiệu
  menu (`present.need_count`); câu hỏi còn ở điều phối viên chỉ là số `with_manager`. Mục cùng dạng với `actions` của ảnh chụp v2
  (`manager_feed.approval_json` / `question_json`) → island vẽ bằng CÙNG thẻ duyệt/câu hỏi, duyệt qua CÙNG `/api/manager/act/*` (`plan_action`,
  bước xác nhận, bước 2 → trang Duyệt).
- `team`: "Đội đang làm gì" (panel phải) — agent đang làm / chờ / hỏi, trạng thái qua `present.display_state` (dùng chung mọi bề mặt).
- `work`: thẻ việc sống trong hội thoại — `?work=t_…,r_…` (đích task/run của thẻ hành động điều phối viên) → trạng thái, agent, bước hiện tại.

Chỉ RPC ĐỌC (`tui.snapshot`, `question.list`, `manager.get`, `task.get`, `run.get`). Mọi chữ là chữ THÔ (island gán textContent).
"""

from __future__ import annotations

import re
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..rpc import DaemonUnavailable, RpcError
from . import present as P

WORK_MAX = 12  # số thẻ việc tối đa mỗi lần hỏi
_WORK_ID = re.compile(r"^[tr]_[A-Za-z0-9_\-]{1,64}$")
# thứ tự "Đội đang làm gì": cần bạn trước, rồi đang làm, xếp hàng, chờ điều phối viên
_TEAM_ORDER = {"waiting_human": 0, "waiting": 1, "asking": 2, "working": 3, "with_manager": 4}


def _line(v: Any, n: int) -> str:
    s = " ".join(str(v or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _state(kind: str, key: str) -> dict[str, str]:
    if kind == "agent":
        ic, label, tone = P.agent_status(key)
        return {"state": key, "label": label, "tone": tone, "icon": ic}
    label, tone = P.run_status(key)
    return {"state": key, "label": label, "tone": tone}


def need_json(approvals: Any, qs: Any) -> dict[str, Any]:
    """Khay "Cần bạn": duyệt trước (chờ quá hạn → trước), rồi câu hỏi chờ bạn (cũ trước). `count` = `present.need_count` (= huy hiệu menu)."""
    from .manager_feed import approval_json, question_json

    aps = sorted([a for a in approvals or [] if isinstance(a, dict) and a.get("status", "open") in P.AP_PENDING],
                 key=lambda a: (a.get("status") != "waiting_human", str(a.get("opened_at") or "")))
    qopen = sorted([q for q in qs or [] if isinstance(q, dict) and q.get("status") == "open"], key=lambda q: str(q.get("created_at") or ""))
    items = [approval_json(a) for a in aps] + [{"kind": "question", **question_json(q)} for q in qopen]
    return {"count": P.need_count(approvals, qs), "approvals": len(aps), "questions": len(qopen),
            "with_manager": sum(1 for q in qs or [] if isinstance(q, dict) and q.get("status") == "with_manager"), "items": items}


def team_json(rows: Any, idx: dict[str, dict[str, str]], manager_id: str | None) -> list[dict[str, Any]]:
    """Agent KHÔNG rảnh (trừ điều phối viên — nó là người đang chat), cần bạn trước. Việc = tiêu đề task / nhãn thân thiện của prompt."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or not r.get("id") or r.get("id") == manager_id:
            continue
        st = P.agent_state(r, idx)
        if st not in _TEAM_ORDER:
            continue
        task = r.get("task")
        out.append({"id": r["id"], "name": r.get("name") or r["id"], "avatar": P.avatar_tone(r.get("name") or r["id"]), **_state("agent", st),
                    "task": P.run_title({"prompt": task}, n=90) if task else "", "tool": P.short_tool(r["last_tool"]) if r.get("last_tool") else "",
                    "run_id": r.get("current_run_id") or r.get("waiting_human") or r.get("last_run_id"), "started_at": r.get("run_started_at")})
    out.sort(key=lambda x: (_TEAM_ORDER.get(x["state"], 9), x["id"]))
    return out


def _step(run_id: str | None, rows: Any) -> str:
    """Bước hiện tại của một run đang chạy = tool cuối của agent đang giữ run đó (`tui.snapshot.last_tool`)."""
    for r in rows or []:
        if isinstance(r, dict) and run_id and r.get("current_run_id") == run_id and r.get("last_tool"):
            return "Đang dùng " + P.short_tool(r["last_tool"])
    return ""


def work_task(t: dict[str, Any], idx: dict[str, dict[str, str]], rows: Any) -> dict[str, Any]:
    """Thẻ việc của một task: trạng thái = run gần nhất còn sống (thống nhất với /runs) hoặc trạng thái task."""
    runs = [r for r in t.get("runs") or [] if isinstance(r, dict)]
    live = next((r for r in reversed(runs) if P.is_live_run(r)), None)
    last = live or (runs[-1] if runs else None)
    if live and t.get("status") not in ("done", "archived"):
        s = _state("run", P.run_state(live, idx))
    else:
        label, tone = P.TASK_STATUS.get(str(t.get("status") or ""), (str(t.get("status") or "—"), "muted"))
        s = {"state": str(t.get("status") or ""), "label": label, "tone": tone}
    done = t.get("status") == "done"
    return {"kind": "task", "id": t.get("id"), "title": _line(t.get("title") or t.get("id"), 120), "agent": (last or {}).get("agent_id") or t.get("assignee_agent_id"),
            **s, "live": bool(live), "end": done or t.get("status") in ("archived",), "step": _step(live["id"] if live else None, rows),
            "run_id": (last or {}).get("id"), "href": f"/tasks/{t.get('id')}", "result": _line(t.get("result_summary"), 200) if done else ""}


def work_run(r: dict[str, Any], idx: dict[str, dict[str, str]], rows: Any, titles: dict[str, str] | None = None) -> dict[str, Any]:
    if r.get("kind") == "manager":  # lượt của chính điều phối viên = chính cuộc trò chuyện này — không thành thẻ việc
        return {"kind": "run", "id": r.get("id"), "skip": True}
    live = P.is_live_run(r)
    return {"kind": "run", "id": r.get("id"), "title": P.run_title(r, titles, n=120), "agent": r.get("agent_id"), **_state("run", P.run_state(r, idx)),
            "live": live, "end": not live, "step": _step(r.get("id"), rows) if live else "", "run_id": r.get("id"), "href": f"/runs/{r.get('id')}",
            "result": _line(r.get("summary"), 200) if r.get("status") == "completed" else ""}


async def flow_data(request: Request, work: list[str]) -> dict[str, Any]:
    from .app import gather_ordered, questions, rpc

    async def _mgr() -> str | None:
        try:
            return (await rpc(request, "manager.get") or {}).get("agent_id")
        except RpcError:
            return None

    snap, qs, mid = await gather_ordered(rpc(request, "tui.snapshot"), questions(request, "pending"), _mgr())
    snap = snap or {}
    rows, aps = snap.get("agents") or [], snap.get("approvals") or []
    idx = P.need_index(aps, qs)

    async def _one(wid: str) -> tuple[str, dict[str, Any] | None]:
        try:
            if wid.startswith("t_"):
                return wid, work_task(await rpc(request, "task.get", {"id": wid}) or {}, idx, rows)
            r = await rpc(request, "run.get", {"id": wid}) or {}
            titles: dict[str, str] = {}
            if r.get("task_id"):
                try:
                    titles = {r["task_id"]: (await rpc(request, "task.get", {"id": r["task_id"]}) or {}).get("title") or ""}
                except RpcError:
                    titles = {}
            return wid, work_run(r, idx, rows, titles)
        except RpcError:
            return wid, None

    got = await gather_ordered(*(_one(w) for w in work)) if work else []
    h = snap.get("hub") or {}
    return {"need": need_json(aps, qs), "team": team_json(rows, idx, mid), "idle": sum(1 for r in rows if P.agent_state(r, idx) == "idle"),
            "running": int(h.get("running") or 0), "queued": int(h.get("queued") or 0), "cost_today": P.fmt_cost(h.get("cost_today_usd") or 0),
            "work": {k: v for k, v in got if v}}


def work_ids(raw: str | None) -> list[str]:
    out: list[str] = []
    for x in str(raw or "").split(","):
        x = x.strip()
        if _WORK_ID.match(x) and x not in out:
            out.append(x)
    return out[:WORK_MAX]


async def api_flow(request: Request) -> Response:
    """`GET /api/manager/flow?work=t_…,r_…` → `{need, team, idle, running, queued, cost_today, work}` (no-store; hết phiên → 401 JSON)."""
    try:
        data = await flow_data(request, work_ids(request.query_params.get("work")))
    except (RpcError, DaemonUnavailable, OSError) as e:
        return JSONResponse({"error": str(e)}, status_code=503, headers={"Cache-Control": "no-store"})
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


def routes() -> list[Any]:
    from starlette.routing import Route

    return [Route("/api/manager/flow", api_flow)]
