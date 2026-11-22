"""Luồng D workspace song song — tự cô lập & tích hợp (docs/dev-notes/workspace-parallel.md §D).

Task con của một task gốc tách việc (`kind=root`) có thể được hub tách vào git worktree riêng lúc DISPATCH (không phải lúc tạo):
quyết định dựa trên trạng thái khoá THẬT ngay trước khi run chạy, và worktree được tạo đồng bộ trước `runs.submit`.

- Chế độ (`mode`): `workspace.isolate` (never|auto|always, mặc định auto) ← `policies.isolate` của team ghi đè. Task: `workspace.isolate`
  = true (lead yêu cầu qua kế hoạch / hub-self `task_create`) → cô lập khi chế độ ≠ never; = false → không cô lập task đó.
  `auto`: cô lập khi task yêu cầu, khi khoá repo đang bị giữ (`_lock_block`), khi có task mở KHÁC (không phải hậu duệ) cùng khoá
  (`TaskService._same_lock_tasks`), hoặc khi task gốc đã có nhánh tích hợp trên repo đó (giữ mọi phần việc về một chỗ).
  Không cô lập: task đã từng chạy (thay đổi dở nằm ở chỗ cũ), agent chỉ đọc, không phải repo git, task đã có worktree, task con
  có tổ tiên đã chạy thẳng trên checkout chính (cần thấy thay đổi chưa commit của nó).
- Nhánh: tích hợp `cfh/<root id>/integration` (worktree riêng, tạo lười từ nhánh hiện tại của repo ở lần cô lập đầu), task con
  `cfh/<root id>/<task id>` tách từ nhánh tích hợp. Vướng ref (`refs/heads/cfh/<root id>` của đường B cũ…) → tiền tố khác
  (`cfh/<root id>-int`, `cfharness-int/<root id>`).
- Task con `done` → ghép (giữ khoá cả worktree tích hợp lẫn worktree con): auto-commit thay đổi chưa commit của task con,
  `merge --no-ff` (rerere bật qua `-c`, không hook). Xung đột chỉ ở lockfile → lấy bản nhánh tích hợp + cờ `needs_lockfile_refresh`;
  thử lại `-Xignore-space-change` (chỉ khoảng trắng); còn lại → `merge --abort` + task `merge` cho assignee của task con (workspace =
  worktree tích hợp). Task merge xong mà nhánh chưa được ghép / bị blocked → task gốc `blocked` kèm lý do rõ.
- Task gốc xong → để nguyên nhánh tích hợp (không push; fast-forward base ở checkout chính chỉ khi
  `workspace.integrate.fast_forward` + checkout chính sạch + đang ở base), báo trong kết quả, gỡ worktree con (`remove_worktree`).

Trạng thái ở `tasks.workspace.integration` của task gốc: `{repo, branch, prefix, base, base_commit, path, worktree, children,
merged, conflicts, pending, needs_lockfile_refresh, finalized}`. Task con cô lập: `workspace.isolation = {root, reason,
origin_path, at}`; task merge: `workspace.merge_for = <task con>`. Event `task.isolated`, `task.integrated`,
`task.integration_conflict`.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Callable

from ..errors import HubError
from ..store import now_iso

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.dispatch.integrate")

ISOLATE_MODES = ("never", "auto", "always")
# lockfile: xung đột chỉ ở các file này → lấy bản của nhánh tích hợp, cờ needs_lockfile_refresh (sinh lại lockfile ngoài phạm vi)
MERGE_LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json", "bun.lockb", "Cargo.lock", "poetry.lock",
                   "uv.lock", "Pipfile.lock", "Gemfile.lock", "composer.lock", "go.sum")
INTERNAL_WS_KEYS = ("integration", "isolation", "merge_for")  # chỉ hub ghi — tasks.create bỏ khi nhận từ ngoài
_GIT_ID = ("-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false")
_DEFAULT_ID = ("-c", "user.name=cfharness", "-c", "user.email=cfharness@localhost")


class IntegrationError(HubError):
    """Lỗi git khi ghép (không phải xung đột nội dung) — task gốc bị chặn kèm lý do."""


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def is_lockfile(rel: str) -> bool:
    return rel.replace("\\", "/").rsplit("/", 1)[-1] in MERGE_LOCKFILES


class Integrator:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub

    @property
    def tasks(self):
        return self.hub.tasks

    @property
    def store(self):
        return self.hub.store

    # ------------------------------------------------------------------ chế độ
    def mode(self, t: dict[str, Any]) -> str:
        m = None
        if t.get("team_id"):
            try:
                m = self.hub.teams.policies(t["team_id"]).isolate
            except HubError:
                m = None
        if m is None:
            try:
                m = self.hub.config.get("workspace.isolate")
            except KeyError:
                m = None
        return m if m in ISOLATE_MODES else "auto"

    # ------------------------------------------------------------------ trạng thái trên task gốc
    @staticmethod
    def _ws(t: dict[str, Any] | None) -> dict[str, Any]:
        ws = (t or {}).get("workspace")
        return ws if isinstance(ws, dict) else {}

    def state(self, root: dict[str, Any] | None) -> dict[str, Any] | None:
        integ = self._ws(root).get("integration")
        return integ if isinstance(integ, dict) else None

    def _save(self, root_id: str, integ: dict[str, Any], base_ws: dict[str, Any] | None = None) -> None:
        root = self.tasks.row(root_id)
        ws = dict(self._ws(root) or base_ws or {})
        ws["integration"] = integ
        self.tasks.update(root_id, {"workspace": ws})

    def _root_of(self, t: dict[str, Any]) -> dict[str, Any] | None:
        rid = t.get("origin_task_id")
        root = self.tasks.maybe(rid) if rid else None
        return root if root is not None and root.get("kind") == "root" else None

    # ------------------------------------------------------------------ quyết định cô lập (lúc dispatch)
    def _ancestors(self, tid: str) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        stack = list(self.tasks.parents(tid))
        while stack:
            p = stack.pop()
            if p in seen:
                continue
            seen.add(p)
            row = self.tasks.maybe(p)
            if row is not None:
                out.append(row)
                stack += self.tasks.parents(p)
        return out

    def wait_reason(self, t: dict[str, Any]) -> str | None:
        """Task con mà một tổ tiên (cô lập) chưa được ghép xong vào nhánh tích hợp → chờ (`integration_pending`): nhánh của nó tách
        từ nhánh tích hợp nên phải có việc của cha trước."""
        root = self._root_of(t)
        integ = self.state(root)
        if not integ:
            return None
        merged = {m.get("task") for m in integ.get("merged") or []}
        for a in self._ancestors(t["id"]):
            iso = self._ws(a).get("isolation")
            if isinstance(iso, dict) and iso.get("root") == (root or {}).get("id") and a["id"] not in merged:
                return "integration_pending"
        return None

    def isolation_reason(self, t: dict[str, Any], agent_id: str, *, lock_blocked: Callable[[], bool]) -> str | None:
        from ..runs import _agent_read_only
        from ..workspace import git_root

        ws = self._ws(t)
        if t.get("kind") != "task" or ws.get("kind") != "dir" or not ws.get("path"):
            return None
        if ws.get("worktree") is not None or ws.get("merge_for") or ws.get("isolation"):
            return None
        root = self._root_of(t)
        if root is None:
            return None  # task đứng riêng: không có chỗ ghép lại → giữ hành vi cũ (chờ khoá / người tạo task kèm `worktree`)
        if int(t.get("attempts") or 0) > 0 or self.tasks.last_work_run(t["id"]) is not None:
            return None  # đã chạy ở chỗ cũ: thay đổi dở nằm đó, resume phải chạy cùng chỗ
        ag = self.store.get_agent(agent_id)
        if ag is None or _agent_read_only(ag):
            return None
        mode, want = self.mode(t), ws.get("isolate")
        if mode == "never" or want is False:
            return None
        repo = git_root(str(ws["path"]))
        if repo is None:
            return None
        for a in self._ancestors(t["id"]):  # tổ tiên đã ghi thẳng checkout chính của repo này → phải chạy cùng chỗ để thấy việc đó
            aws = self._ws(a)
            if aws.get("kind") == "dir" and aws.get("path") and not aws.get("worktree") and (git_root(str(aws["path"])) or "") == repo \
                    and self.tasks.last_work_run(a["id"]) is not None:
                return None
        if mode == "always":
            return "always"
        if want is True:
            return "requested"
        integ = self.state(root)
        if integ and _same(str(integ.get("repo") or ""), repo):
            return "integration"
        if lock_blocked():
            return "conflict"
        exclude = {t["id"], root["id"], *self.tasks.descendants(t["id"])}
        if self.tasks._same_lock_tasks(str(ws["path"]), exclude):
            return "conflict"
        return None

    def maybe_isolate(self, t: dict[str, Any], agent_id: str, *, lock_blocked: Callable[[], bool]) -> dict[str, Any] | None:
        """Cô lập `t` nếu cần (trả task đã cập nhật) — None = chạy như cũ. Lỗi git → log + chạy như cũ (chờ khoá)."""
        reason = self.isolation_reason(t, agent_id, lock_blocked=lock_blocked)
        if reason is None:
            return None
        try:
            return self.isolate(t, reason)
        except HubError as e:
            log.warning("cô lập task %s thất bại (%s) — chạy như cũ", t["id"], e.message, exc_info=True)
            self.tasks.comment(t["id"], f"hub không tạo được worktree riêng cho task này ({e.message[:300]}) — chạy trên workspace cũ",
                               author="hub", kind="system")
            return None

    def _prefix(self, repo: str, root_id: str) -> str:
        from ..workspace import ref_collision

        for p in (f"cfh/{root_id}", f"cfh/{root_id}-int", f"cfharness-int/{root_id}"):
            if ref_collision(repo, f"{p}/integration") is None:
                return p
        raise HubError(f"không chọn được tiền tố nhánh cho task gốc {root_id}: mọi tên `cfh/{root_id}…` đều vướng ref có sẵn")

    def ensure_integration(self, root: dict[str, Any], repo: str, child_ws: dict[str, Any]) -> dict[str, Any]:
        """Worktree + nhánh tích hợp của task gốc (tạo lười). Base = nhánh hiện tại của repo lúc tạo."""
        from ..workspace import _git, _head_name, ensure_worktree_meta

        integ = self.state(root)
        if integ:
            if not _same(str(integ.get("repo") or ""), repo):
                raise HubError(f"task gốc {root['id']} đã có nhánh tích hợp trên repo khác ({integ.get('repo')}) — chỉ ghép một repo mỗi task gốc")
            if not os.path.isdir(str(integ.get("path") or "")):  # thư mục bị xoá tay → dựng lại trên nhánh có sẵn
                meta = ensure_worktree_meta(self.hub, repo, integ["branch"], integ.get("base"))
                integ.update(path=meta["path"], worktree=meta)
                self._save(root["id"], integ)
            return integ
        base = _head_name(repo)
        head = _git(repo, "rev-parse", "--verify", "-q", "HEAD").stdout.strip()
        if not base or not head:
            raise HubError(f"repo {repo} chưa có commit nào — không tách nhánh tích hợp được")
        prefix = self._prefix(repo, root["id"])
        branch = f"{prefix}/integration"
        meta = ensure_worktree_meta(self.hub, repo, branch, base)
        integ = {"repo": repo, "branch": branch, "prefix": prefix, "base": meta.get("base") or base, "base_commit": head,
                 "path": meta["path"], "worktree": meta, "created_at": now_iso(), "children": {}, "merged": [], "conflicts": [],
                 "pending": [], "needs_lockfile_refresh": []}
        base_ws = self._ws(root) if self._ws(root).get("kind") else {k: v for k, v in child_ws.items() if k not in ("isolate", *INTERNAL_WS_KEYS)}
        self._save(root["id"], integ, base_ws)
        self.tasks.comment(root["id"], f"hub mở nhánh tích hợp `{branch}` (worktree {meta['path']}, tách từ `{integ['base']}`) để các phần "
                                       "việc chạy song song trên worktree riêng rồi ghép lại", author="hub", kind="system")
        return integ

    def isolate(self, t: dict[str, Any], reason: str) -> dict[str, Any]:
        from ..workspace import ensure_worktree_meta, git_root, ref_collision

        ws = self._ws(t)
        path = str(ws["path"])
        repo = git_root(path)
        root = self._root_of(t)
        if repo is None or root is None:
            raise HubError("không cô lập được: không phải repo git / không có task gốc")
        integ = self.ensure_integration(root, repo, ws)
        branch = f"{integ['prefix']}/{t['id']}"
        clash = ref_collision(repo, branch)
        if clash is not None:
            raise HubError(f"nhánh {branch} vướng ref {clash}")
        meta = ensure_worktree_meta(self.hub, repo, branch, integ["branch"])
        rel = os.path.relpath(os.path.realpath(path), repo)
        new_path = meta["path"] if rel in (".", "") or rel.startswith("..") else os.path.join(meta["path"], rel)
        if not os.path.isdir(new_path):
            new_path = meta["path"]
        new_ws = {**ws, "kind": "dir", "path": new_path, "worktree": meta,
                  "isolation": {"root": root["id"], "reason": reason, "origin_path": path, "at": now_iso()}}
        self.tasks.update(t["id"], {"workspace": new_ws})
        integ = self.state(self.tasks.row(root["id"])) or integ
        integ.setdefault("children", {})[t["id"]] = {"branch": branch, "path": meta["path"]}
        self._save(root["id"], integ)
        t2 = self.tasks.row(t["id"])
        self.tasks.publish("task.isolated", t2, {"root_id": root["id"], "branch": branch, "path": meta["path"], "base": integ["branch"],
                                                 "reason": reason})
        return t2

    # ------------------------------------------------------------------ ghép
    def on_done(self, t: dict[str, Any]) -> None:
        """Task vừa `done` (gọi từ `TaskDispatcher._after_done`, TRƯỚC khi xét task gốc/thăng task con)."""
        t = self.tasks.maybe(t["id"]) or t
        ws = self._ws(t)
        if ws.get("merge_for"):
            self._on_merge_task_done(t)
        elif isinstance(ws.get("isolation"), dict):
            self.integrate_child(t)

    def _open_conflicts(self, integ: dict[str, Any]) -> list[dict[str, Any]]:
        return [c for c in integ.get("conflicts") or [] if c.get("status") in ("open", "failed")]

    def integrate_child(self, t: dict[str, Any]) -> str:
        """Ghép nhánh của task con vào nhánh tích hợp. Trả `merged|pending|conflict|blocked|skip`."""
        iso = self._ws(t).get("isolation") or {}
        root = self.tasks.maybe(iso.get("root")) if iso.get("root") else None
        integ = self.state(root)
        if root is None or integ is None or t.get("status") != "done":
            return "skip"
        if t["id"] in {m.get("task") for m in integ.get("merged") or []}:
            return "merged"
        if any(c.get("task") == t["id"] for c in self._open_conflicts(integ)):
            return "conflict"
        pending = list(integ.get("pending") or [])
        if self._open_conflicts(integ):  # đang có task merge làm trên worktree tích hợp → xếp hàng, ghép sau khi nó xong
            if t["id"] not in pending:
                integ["pending"] = pending + [t["id"]]
                self._save(root["id"], integ)
            return "pending"
        meta = self._ws(t).get("worktree") or {}
        locks = self.hub.runs.locks
        keys = [os.path.realpath(str(integ["path"])), os.path.realpath(str(meta.get("path") or integ["path"]))]
        sess = f"integrate:{root['id']}"
        if not locks.try_acquire_many(keys, sess, None):
            if t["id"] not in pending:
                integ["pending"] = pending + [t["id"]]
                self._save(root["id"], integ)
            return "pending"
        try:
            res = self._merge(root, integ, t)
        except IntegrationError as e:
            res = {"result": "error", "reason": e.message}
        finally:
            locks.release_many(keys, sess, reason="integrated")
        integ = self.state(self.tasks.row(root["id"])) or integ
        integ["pending"] = [x for x in integ.get("pending") or [] if x != t["id"]]
        branch = (meta.get("branch") or (integ.get("children") or {}).get(t["id"], {}).get("branch"))
        if res["result"] == "merged":
            rec = {"task": t["id"], "branch": branch, "commit": res.get("commit"), "how": res["how"], "at": now_iso()}
            if res.get("lockfiles"):
                rec["lockfiles"] = res["lockfiles"]
                integ["needs_lockfile_refresh"] = sorted(set(integ.get("needs_lockfile_refresh") or []) | set(res["lockfiles"]))
            integ.setdefault("merged", []).append(rec)
            self._save(root["id"], integ)
            self.tasks.publish("task.integrated", self.tasks.row(t["id"]),
                               {"root_id": root["id"], "branch": branch, "into": integ["branch"], "commit": res.get("commit"), "how": res["how"],
                                "needs_lockfile_refresh": res.get("lockfiles") or []})
            return "merged"
        if res["result"] == "conflict":
            return self._open_merge_task(root, integ, t, branch, res["files"])
        reason = (f"ghép nhánh {branch} vào {integ['branch']} lỗi: {res.get('reason')} — xử lý tay trong {integ['path']} (ghép nhánh đó) rồi "
                  f"`cfharness task unblock {root['id']}`")
        integ.setdefault("conflicts", []).append({"task": t["id"], "branch": branch, "files": [], "merge_task": None, "status": "failed",
                                                  "reason": reason[:500], "at": now_iso()})
        self._save(root["id"], integ)
        self._block_root(root["id"], reason)
        return "blocked"

    def _open_merge_task(self, root: dict[str, Any], integ: dict[str, Any], t: dict[str, Any], branch: str, files: list[str]) -> str:
        ib, ip = integ["branch"], integ["path"]
        shown = files[:50]
        body = "\n".join([
            f"cfharness merged the work of task {t['id']} (\"{t.get('title')}\") into the integration branch of task {root['id']} and git "
            "reported conflicts it cannot resolve automatically. The hub aborted that merge; redo it here and resolve it.",
            "",
            f"- Working directory: the integration worktree `{ip}` (branch `{ib}`).",
            f"- Branch to merge: `{branch}` (the finished subtask). Integration branch: `{ib}`.",
            "- Conflicting files:", *[f"  - {f}" for f in shown], *(["  - …"] if len(files) > len(shown) else []),
            "",
            "Steps:",
            f"1. `git merge --no-ff {branch}` in the working directory.",
            "2. Resolve every conflict keeping the intent of BOTH sides (read the subtask's goal above and the integration branch's code).",
            "3. Build/test what the conflicting files touch if the project has a quick check.",
            "4. Commit the merge (`git commit --no-edit`). Do not push, do not switch branches, do not touch other worktrees.",
            "If you cannot resolve it safely, call task_block with what a human must decide.",
        ])
        team = root.get("team_id") or t.get("team_id")
        try:
            mt = self.tasks.create({"team": team, "assignee": t.get("assignee_agent_id"), "title": f"Merge: {t.get('title')}"[:200], "body": body,
                                    "workspace": {"kind": "dir", "path": ip, "worktree": integ.get("worktree")}, "origin_task_id": root["id"],
                                    "overrides": root.get("overrides"), "priority": t.get("priority"), "decompose": False}, created_by="hub")
        except HubError as e:
            reason = (f"ghép {branch} vào {ib} xung đột ở {len(files)} file ({', '.join(files[:5])}) và không giao được task merge: {e.message} — "
                      f"ghép tay trong {ip} rồi `cfharness task unblock {root['id']}`")
            integ.setdefault("conflicts", []).append({"task": t["id"], "branch": branch, "files": files[:200], "merge_task": None,
                                                      "status": "failed", "reason": reason[:500], "at": now_iso()})
            self._save(root["id"], integ)
            self._block_root(root["id"], reason)
            return "blocked"
        mws = {**self._ws(self.tasks.row(mt["id"])), "merge_for": t["id"]}
        self.tasks.update(mt["id"], {"workspace": mws})
        integ.setdefault("conflicts", []).append({"task": t["id"], "branch": branch, "files": files[:200], "merge_task": mt["id"],
                                                  "status": "open", "at": now_iso()})
        self._save(root["id"], integ)
        self.tasks.publish("task.integration_conflict", self.tasks.row(t["id"]),
                           {"root_id": root["id"], "branch": branch, "into": ib, "files": files[:50], "merge_task_id": mt["id"]})
        self.tasks.comment(root["id"], f"ghép `{branch}` vào `{ib}` xung đột ở {len(files)} file ({', '.join(files[:5])}"
                                       f"{'…' if len(files) > 5 else ''}) — đã giao task {mt['id']} cho {t.get('assignee_agent_id')}",
                           author="hub", kind="system")
        return "conflict"

    def _on_merge_task_done(self, mt: dict[str, Any]) -> None:
        from ..workspace import _git

        root = self.tasks.maybe(mt.get("origin_task_id")) if mt.get("origin_task_id") else None
        integ = self.state(root)
        if root is None or integ is None:
            return
        child_id = self._ws(mt).get("merge_for")
        entry = next((c for c in integ.get("conflicts") or [] if c.get("task") == child_id and c.get("status") in ("open", "failed")), None)
        if entry is None:
            return
        ok, why = self._verify_merged(integ, entry["branch"])
        if not ok:
            reason = (f"task merge {mt['id']} đã xong nhưng nhánh {entry['branch']} chưa được ghép vào {integ['branch']} ({why}) — "
                      f"ghép tay trong {integ['path']} rồi `cfharness task unblock {root['id']}`")
            entry.update(status="failed", reason=reason)
            self._save(root["id"], integ)
            self._block_root(root["id"], reason)
            return
        self._resolved(root, integ, entry, how="merge_task",
                       commit=_git(integ["path"], "rev-parse", "HEAD").stdout.strip() or None)
        self.process_pending(root["id"])

    def _resolved(self, root: dict[str, Any], integ: dict[str, Any], entry: dict[str, Any], *, how: str, commit: str | None) -> None:
        entry.update(status="resolved", resolved_at=now_iso())
        integ.setdefault("merged", []).append({"task": entry["task"], "branch": entry["branch"], "commit": commit, "how": how,
                                               "merge_task": entry.get("merge_task"), "at": now_iso()})
        self._save(root["id"], integ)
        child = self.tasks.maybe(entry["task"])
        if child is not None:
            self.tasks.publish("task.integrated", child, {"root_id": root["id"], "branch": entry["branch"], "into": integ["branch"],
                                                          "commit": commit, "how": how, "merge_task_id": entry.get("merge_task"),
                                                          "needs_lockfile_refresh": []})

    def _verify_merged(self, integ: dict[str, Any], branch: str) -> tuple[bool, str]:
        """Nhánh `branch` đã nằm trong nhánh tích hợp? Merge đã giải hết xung đột mà chưa commit → hub commit giúp (không hook)."""
        from ..workspace import _git

        ip = str(integ["path"])
        if not os.path.isdir(ip):
            return False, f"worktree tích hợp {ip} không còn"
        if _git(ip, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
            if self._unmerged(ip):
                return False, "còn file xung đột chưa giải"
            r = self._commit(ip, "--no-edit")
            if r.returncode != 0:
                return False, f"không commit được merge: {(r.stderr or r.stdout).strip()[:200]}"
        if _git(ip, "rev-parse", "-q", "--verify", f"refs/heads/{branch}").returncode != 0:
            return False, f"nhánh {branch} không còn"
        if _git(ip, "merge-base", "--is-ancestor", f"refs/heads/{branch}", "HEAD").returncode != 0:
            return False, f"nhánh {branch} chưa nằm trong {integ['branch']}"
        return True, ""

    def process_pending(self, root_id: str) -> None:
        root = self.tasks.maybe(root_id)
        integ = self.state(root)
        if integ is None:
            return
        for cid in list(integ.get("pending") or []):
            child = self.tasks.maybe(cid)
            if child is None or child.get("status") != "done":
                continue
            if self.integrate_child(child) in ("conflict", "blocked", "pending"):
                break
        disp = getattr(self.hub, "dispatcher", None)
        if disp is not None:
            disp.kick()

    def reconcile(self) -> None:
        """Mỗi tick: task gốc còn việc ghép dở (`pending` / xung đột `failed` người có thể đã ghép tay) → thử lại, rồi xét task gốc."""
        rows = self.store.conn.execute("SELECT id FROM tasks WHERE kind='root' AND status IN ('running','blocked') AND deleted_at IS NULL "
                                       "AND workspace LIKE '%\"integration\"%'").fetchall()
        disp = getattr(self.hub, "dispatcher", None)
        for (rid,) in rows:
            root = self.tasks.maybe(rid)
            integ = self.state(root)
            if root is None or integ is None or integ.get("finalized"):
                continue
            changed = False
            for c in list(integ.get("conflicts") or []):
                if c.get("status") != "failed":
                    continue
                ok, _why = self._verify_merged(integ, c["branch"]) if c.get("branch") else (False, "")
                if ok:
                    from ..workspace import _git

                    self._resolved(root, integ, c, how="manual", commit=_git(integ["path"], "rev-parse", "HEAD").stdout.strip() or None)
                    changed = True
            if integ.get("pending") and not self._open_conflicts(integ):
                self.process_pending(rid)
                changed = True
            if changed and disp is not None:
                disp._refresh_root({"origin_task_id": rid})

    # ------------------------------------------------------------------ git
    def _ident(self, cwd: str) -> tuple[str, ...]:
        from ..workspace import _git

        have = _git(cwd, "config", "user.email").stdout.strip() and _git(cwd, "config", "user.name").stdout.strip()
        return _GIT_ID + (() if have else _DEFAULT_ID)

    def _commit(self, cwd: str, *args: str) -> Any:
        from ..workspace import _git

        return _git(cwd, *self._ident(cwd), "commit", "--no-verify", *args)

    @staticmethod
    def _unmerged(cwd: str) -> list[str]:
        from ..workspace import _names_z

        return _names_z(cwd, "diff", "--name-only", "-z", "--diff-filter=U")

    def _do_merge(self, ip: str, branch: str, msg: str, *extra: str) -> tuple[bool, list[str]]:
        from ..workspace import _git

        r = _git(ip, *self._ident(ip), "-c", "rerere.enabled=true", "-c", "rerere.autoUpdate=true", "merge", "--no-ff", "--no-verify",
                 "--no-edit", *extra, "-m", msg, f"refs/heads/{branch}")
        if r.returncode == 0:
            return True, []
        in_merge = _git(ip, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0
        if not in_merge:
            raise IntegrationError(f"git merge thất bại: {(r.stderr or r.stdout).strip()[:300]}")
        un = self._unmerged(ip)
        if not un:  # rerere đã giải hết (autoUpdate đã stage) → commit
            c = self._commit(ip, "--no-edit")
            if c.returncode != 0:
                raise IntegrationError(f"không commit được merge sau rerere: {(c.stderr or c.stdout).strip()[:300]}")
            return True, []
        return False, un

    def _take_ours(self, ip: str, files: list[str]) -> None:
        from ..workspace import _git

        for f in files:
            if _git(ip, "checkout", "--ours", "--", f).returncode == 0:
                _git(ip, "add", "--", f)
            else:  # phía nhánh tích hợp đã xoá file → giữ việc xoá
                _git(ip, "rm", "-q", "--ignore-unmatch", "--", f)
        if self._unmerged(ip):
            raise IntegrationError("không giải được xung đột lockfile")
        c = self._commit(ip, "--no-edit")
        if c.returncode != 0:
            raise IntegrationError(f"không commit được merge (lockfile): {(c.stderr or c.stdout).strip()[:300]}")

    def _abort(self, ip: str) -> None:
        from ..workspace import _git

        _git(ip, "merge", "--abort")

    def _merge(self, root: dict[str, Any], integ: dict[str, Any], t: dict[str, Any]) -> dict[str, Any]:
        from ..workspace import _git, _status_paths

        meta = self._ws(t).get("worktree") or {}
        wt, branch = str(meta.get("path") or ""), str(meta.get("branch") or "")
        ip = str(integ["path"])
        if not branch or _git(ip, "rev-parse", "-q", "--verify", f"refs/heads/{branch}").returncode != 0:
            raise IntegrationError(f"nhánh của task con không còn ({branch or '?'})")
        if not os.path.isdir(ip):
            raise IntegrationError(f"worktree tích hợp {ip} không còn")
        # 1. auto-commit việc chưa commit của task con (liên kết node_modules đã bị info/exclude bỏ qua)
        if os.path.isdir(wt):
            try:
                with os.scandir(wt) as it:
                    links = {x.name for x in it if x.is_junction() or x.is_symlink()}
                dirty = [p for p in _status_paths(wt) if p.rstrip("/") not in links]
            except (OSError, HubError) as e:
                raise IntegrationError(f"không đọc được trạng thái worktree {wt}: {e}") from e
            if dirty:
                _git(wt, "add", "-A")
                c = self._commit(wt, "-m", f"cfh: task {t['id']} — {str(t.get('title') or '')[:150]}")
                if c.returncode != 0:
                    raise IntegrationError(f"không auto-commit được thay đổi của task {t['id']}: {(c.stderr or c.stdout).strip()[:300]}")
        # 2. worktree tích hợp phải sạch
        if _git(ip, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0 or _status_paths(ip):
            raise IntegrationError(f"worktree tích hợp {ip} còn thay đổi/merge dở — dọn trước khi ghép tiếp")
        msg = f"cfh: ghép task {t['id']} — {str(t.get('title') or '')[:150]}"
        # 3. merge thường (rerere) → lockfile → thử lại bỏ qua khoảng trắng → lockfile → xung đột thật
        ok, un = self._do_merge(ip, branch, msg)
        how, lockfiles = "clean", []
        if not ok:
            if all(is_lockfile(f) for f in un):
                self._take_ours(ip, un)
                ok, how, lockfiles = True, "lockfile", un
            else:
                self._abort(ip)
                ok, un2 = self._do_merge(ip, branch, msg, "-Xignore-space-change")
                if ok:
                    how = "whitespace"
                elif all(is_lockfile(f) for f in un2):
                    self._take_ours(ip, un2)
                    ok, how, lockfiles = True, "whitespace+lockfile", un2
                else:
                    self._abort(ip)
                    return {"result": "conflict", "files": sorted(set(un) | set(un2))}
        return {"result": "merged", "how": how, "lockfiles": lockfiles, "commit": _git(ip, "rev-parse", "HEAD").stdout.strip() or None}

    # ------------------------------------------------------------------ task gốc
    def _block_root(self, root_id: str, reason: str) -> None:
        root = self.tasks.maybe(root_id)
        if root is None or root["status"] in ("done", "archived"):
            return
        if root["status"] != "blocked" or root.get("block_reason") != reason:
            self.tasks.set_status(root_id, "blocked", reason=reason)

    def gate(self, root: dict[str, Any], subs: list[dict[str, Any]]) -> tuple[str, str] | None:
        """Mọi task con đã `done`: task gốc chỉ xong khi mọi task con cô lập đã được ghép. Trả None (xong được), ("wait", lý do) hoặc
        ("blocked", lý do)."""
        integ = self.state(root)
        if integ is None:
            return None
        failed = [c for c in integ.get("conflicts") or [] if c.get("status") == "failed"]
        if failed:
            return "blocked", str(failed[0].get("reason") or f"ghép nhánh {failed[0].get('branch')} thất bại")
        merged = {m.get("task") for m in integ.get("merged") or []}
        todo = [s for s in subs if (self._ws(s).get("isolation") or {}).get("root") == root["id"] and s["id"] not in merged]
        for s in todo:  # ghép chưa chạy (vd daemon dừng giữa chừng) → thử ngay
            if self.integrate_child(s) in ("conflict", "blocked"):
                break
        integ = self.state(self.tasks.row(root["id"])) or integ
        failed = [c for c in integ.get("conflicts") or [] if c.get("status") == "failed"]
        if failed:
            return "blocked", str(failed[0].get("reason"))
        merged = {m.get("task") for m in integ.get("merged") or []}
        left = [s["id"] for s in todo if s["id"] not in merged]
        if left:
            return "wait", f"chờ ghép {', '.join(left[:5])} vào {integ['branch']}"
        return None

    def finalize(self, root: dict[str, Any]) -> dict[str, Any] | None:
        """Task gốc sắp `done`: fast-forward (nếu được phép), gỡ worktree con đã ghép, trả `{result, open_extra, need_extra}`."""
        from ..workspace import _git, _head, _status_paths, git_root, remove_worktree

        root = self.tasks.row(root["id"])
        integ = self.state(root)
        if integ is None:
            return None
        ib, ip, base = integ["branch"], integ["path"], integ.get("base")
        cleaned, kept = [], []
        for m in integ.get("merged") or []:
            child = self.tasks.maybe(m.get("task"))
            meta = self._ws(child).get("worktree") if child else None
            if not isinstance(meta, dict) or not meta.get("owned"):
                continue
            try:
                remove_worktree(meta, locks=self.hub.runs.locks)
                cleaned.append(meta.get("path"))
            except HubError as e:
                log.warning("không gỡ được worktree %s của task %s: %s", meta.get("path"), m.get("task"), e.message)
                kept.append({"path": meta.get("path"), "reason": e.message[:300]})
        ff: dict[str, Any] = {"done": False}
        try:
            want_ff = bool(self.hub.config.get("workspace.integrate.fast_forward"))
        except KeyError:
            want_ff = False
        repo = git_root(str(integ["repo"])) if integ.get("repo") else None
        if want_ff and repo:
            _h, cur = _head(repo)
            if cur != base:
                ff["reason"] = f"checkout chính đang ở {cur or 'HEAD tách rời'}, không phải {base}"
            elif _status_paths(repo):
                ff["reason"] = "checkout chính còn thay đổi chưa commit"
            else:
                r = _git(repo, "merge", "--ff-only", "--no-verify", f"refs/heads/{ib}")
                ff = {"done": r.returncode == 0} if r.returncode == 0 else {"done": False, "reason": (r.stderr or r.stdout).strip()[:300]}
        integ.update(finalized=now_iso(), cleaned=cleaned, cleanup_kept=kept, fast_forward=ff)
        self._save(root["id"], integ)
        n = len(integ.get("merged") or [])
        if ff.get("done"):
            need = [f"Đã fast-forward `{base}` ở checkout chính tới nhánh tích hợp `{ib}` ({n} phần việc làm song song) — chưa push."]
        else:
            need = [f"Xem nhánh tích hợp `{ib}` (worktree {ip}, tách từ `{base}`): hub đã ghép {n} phần việc làm song song vào đó, "
                    f"KHÔNG push và KHÔNG đụng `{base}` — tự merge/push khi thấy ổn."]
            if want_ff and ff.get("reason"):
                need.append(f"Không fast-forward `{base}`: {ff['reason']}.")
        open_extra = []
        if integ.get("needs_lockfile_refresh"):
            open_extra.append(f"Lockfile {', '.join(integ['needs_lockfile_refresh'][:5])} lấy bản của nhánh tích hợp khi ghép — chạy lại trình "
                              "quản lý gói (vd `npm install`) để cập nhật trước khi merge.")
        if kept:
            open_extra.append(f"{len(kept)} worktree con chưa gỡ được — `cfharness worktree list`.")
        result = {"branch": ib, "path": ip, "base": base, "merged": [m.get("task") for m in integ.get("merged") or []],
                  "needs_lockfile_refresh": integ.get("needs_lockfile_refresh") or [], "fast_forward": ff, "cleaned": cleaned}
        return {"result": result, "open_extra": open_extra, "need_extra": need}


def strip_internal(ws: Any) -> Any:
    """Bỏ khoá trạng thái chỉ hub ghi (`integration`/`isolation`/`merge_for`) khỏi workspace nhận từ ngoài (task con kế thừa workspace
    task gốc, người/agent tạo task)."""
    if isinstance(ws, dict) and any(k in ws for k in INTERNAL_WS_KEYS):
        return {k: v for k, v in ws.items() if k not in INTERNAL_WS_KEYS}
    return ws
