"""Trang web bổ sung (web/extra_pages.py): Lịch chạy, Tìm kiếm hội thoại, Thay đổi file/hoàn tác của run, thẻ Sức khoẻ.

Cùng khung với tests/test_web.py: daemon IN-PROCESS trên socket thật + FakeRuntime, app Starlette qua ASGITransport. Không gọi `claude`.
"""

from __future__ import annotations

import subprocess

import pytest

from cfharness.web.extra_pages import diff_view, schedule_params, snippet_parts, split_patch
from cfharness.workspace import compute_diff, take_checkpoint
from test_web import ISOLATED, _start, _stop, audit_rows, until  # noqa: F401 — dùng lại khung test web

from cfharness.runtime import FakeRuntime


@pytest.fixture
async def env(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e
    await _stop(e)


async def done_run(env, agent: str = "ag0", prompt: str = "việc mẫu") -> dict:
    if agent not in [a["id"] for a in env.hub.agents.list()]:
        env.hub.agents.create({"id": agent, "name": agent, "model": "haiku"})
    run = env.hub.runs.submit(agent, prompt)
    assert await until(lambda: (env.hub.store.get_run(run["id"]) or {}).get("status") in ("completed", "failed"))
    return env.hub.store.get_run(run["id"])


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    git(r, "config", "core.autocrlf", "false")  # máy dev Windows có thể bật autocrlf toàn cục — giữ byte cố định cho phép so sánh
    (r / "a.txt").write_bytes(("a1\n-- giữ dòng này\n").encode())
    (r / "b.txt").write_bytes(("b1\n").encode())
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


def attach_diff(env, run_id: str, repo, edits: dict) -> dict:
    """Giả lập run đã sửa repo: checkpoint → ghi file → diff, gắn vào hàng `runs` (như RunManager làm khi run kết thúc)."""
    store = env.hub.runs.checkpoint_dir(run_id)
    cp = take_checkpoint(repo, store)
    for rel, text in edits.items():
        p = repo / rel
        if text is None:
            p.unlink()
        else:
            p.write_bytes((text).encode())
    diff = compute_diff(cp, store)
    env.hub.store.update_run(run_id, {"diff": diff})
    return diff


# =========================================================================== hàm thuần
def test_split_patch_counts_hunks_and_ignores_content_like_headers():
    patch = ("--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n a1\n--- giữ dòng này\n+++ dòng mới\n"  # dòng nội dung giống header
             "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hello\n\\ No newline at end of file\nBinary file img.png differs\n")
    files = split_patch(patch)
    assert [f["path"] for f in files] == ["a.txt", "new.txt", "img.png"]
    a = files[0]
    assert (a["adds"], a["dels"]) == (1, 1) and ("del", "--- giữ dòng này") in a["lines"] and ("add", "+++ dòng mới") in a["lines"]
    assert files[1]["adds"] == 1 and files[1]["lines"][-1][0] == "meta" and files[2]["binary"]


def test_diff_view_caps_and_revert_gates():
    big = "--- a/x.txt\n+++ b/x.txt\n@@ -0,0 +1,900 @@\n" + "".join(f"+l{i}\n" for i in range(900))
    res = {"diff": {"kind": "git", "root": "/r", "files": [{"path": "x.txt", "status": "added", "revertible": True}], "revertible": True,
                    "stat": {"files": 1, "insertions": 900, "deletions": 0}}, "patch": big, "modified_since": []}
    dv = diff_view(res, {"status": "completed"})
    f = dv["files"][0]
    assert len(f["lines"]) == 400 and f["more"] == 501 and dv["can_revert"]
    assert not diff_view({**res, "modified_since": ["x.txt"]}, {"status": "completed"})["can_revert"]
    done = diff_view({**res, "modified_since": ["x.txt"], "reverted_at": "2026-10-26T00:00:00Z"}, {"status": "completed"})
    assert done["modified"] == [] and not done["can_revert"] and "Đã hoàn tác" in done["why_not"]
    assert "đang chạy" in diff_view(res, {"status": "running"})["why_not"]
    assert "git" in diff_view({**res, "diff": {**res["diff"], "kind": "dir"}}, {"status": "completed"})["why_not"]
    assert diff_view({}, {"status": "completed"})["has"] is False


def test_snippet_parts_and_schedule_params():
    assert snippet_parts("…a «b» c «d»") == [(False, "…a "), (True, "b"), (False, " c "), (True, "d")]
    p = schedule_params({"kind": "agent", "expr": "daily 08:00", "prompt": "x", "target": "ag0", "budget_usd": "0,5"})
    assert p == {"kind": "agent", "expr": "daily 08:00", "prompt": "x", "target": "ag0", "budget_usd": 0.5}
    with pytest.raises(ValueError):
        schedule_params({"kind": "task", "expr": "daily 08:00", "prompt": "x"})
    with pytest.raises(ValueError):
        schedule_params({"kind": "rm -rf", "expr": "x", "prompt": "x"})


# =========================================================================== xác thực
async def test_extra_pages_require_login_and_csrf(env):
    c = env.client
    assert (await c.get("/schedules")).status_code == 303
    assert (await c.get("/search?q=x")).status_code == 303
    assert (await c.get("/partials/run/x/diff")).status_code == 401
    assert (await c.get("/partials/health")).status_code == 401
    assert (await c.post("/schedules/create", data={"expr": "daily 08:00", "prompt": "x"})).status_code == 401
    await env.login()
    # thiếu CSRF / thiếu khoá phiên → 403, không tạo gì
    r = await c.post("/schedules/create", data={"kind": "manager", "expr": "daily 08:00", "prompt": "x", "confirm": "yes", "k": env.key})
    assert r.status_code == 403
    r = await c.post("/schedules/create", data={"kind": "manager", "expr": "daily 08:00", "prompt": "x", "confirm": "yes", "csrf": env.csrf})
    assert r.status_code == 403
    r = await c.post("/do/run_revert", data={"id": "x", "confirm": "yes", "csrf": "sai", "k": env.key})
    assert r.status_code == 403
    assert env.hub.schedules.list() == []


# =========================================================================== lịch chạy
async def test_schedules_page_and_nav(env):
    await env.login()
    env.hub.agents.create({"id": "ag0", "name": "ag0", "model": "haiku"})
    r = await env.client.get("/schedules")
    assert r.status_code == 200 and "Lịch chạy" in r.text and "Chưa có lịch nào" in r.text
    assert 'href="/schedules"' in r.text and 'href="/search"' in r.text and "#i-calendar" in r.text and 'class="side-search"' in r.text
    assert '<option value="ag0"' in r.text
    r = await env.client.get("/schedules?preview=weekdays%2008:30")
    assert r.status_code == 200 and "Các lượt tới" in r.text
    r = await env.client.get("/schedules?preview=khong%20phai%20lich")
    assert r.status_code == 200 and "không hợp lệ" in r.text


async def test_schedule_create_confirm_validate_and_actions(env):
    await env.login()
    env.hub.agents.create({"id": "ag0", "name": "ag0", "model": "haiku"})
    form = {"kind": "agent", "target": "ag0", "expr": "daily 08:00", "prompt": "Tóm tắt góp ý mới", "name": "Tóm tắt sáng", "budget_usd": "0.2"}
    # lần 1: trang xác nhận (biểu thức đã kiểm, mốc chạy tới), chưa tạo
    r = await env.post("/schedules/create", form)
    assert r.status_code == 200 and "Tạo lịch chạy tự động?" in r.text and 'name="confirm" value="yes"' in r.text and "agent ag0" in r.text
    assert env.hub.schedules.list() == []
    # biểu thức sai → 400, giữ giá trị đã nhập, không tạo
    r = await env.post("/schedules/create", {**form, "expr": "mỗi khi trời mưa"})
    assert r.status_code == 400 and "không hợp lệ" in r.text and "Tóm tắt góp ý mới" in r.text
    r = await env.post("/schedules/create", {**form, "expr": "mỗi khi trời mưa", "confirm": "yes"})
    assert r.status_code == 400 and env.hub.schedules.list() == []
    # agent không tồn tại → lỗi từ service
    r = await env.post("/schedules/create", {**form, "target": "khong-co", "confirm": "yes"})
    assert r.status_code == 400 and env.hub.schedules.list() == []
    # xác nhận → tạo, audit kênh web
    r = await env.post("/schedules/create", {**form, "confirm": "yes"})
    assert r.status_code == 303 and r.headers["location"] == "/schedules"
    [s] = env.hub.schedules.list()
    assert s["name"] == "Tóm tắt sáng" and s["target"] == "ag0" and s["budget_usd"] == 0.2 and s["created_by"] == "user"
    assert audit_rows(env, "schedule.create")[-1]["channel"] == "web"
    r = await env.client.get("/schedules")
    assert "Đã tạo lịch" in r.text and "Tóm tắt sáng" in r.text and "Tạm dừng" in r.text and f'id="sc-{s["id"]}"' in r.text
    # tạm dừng: chưa xác nhận → trang xác nhận; xác nhận → paused
    r = await env.post("/do/schedule_pause", {"id": s["id"], "name": s["name"], "next": "/schedules"})
    assert r.status_code == 200 and "Tạm dừng lịch" in r.text and env.hub.schedules.get(s["id"])["status"] == "active"
    r = await env.post("/do/schedule_pause", {"id": s["id"], "name": s["name"], "next": "/schedules", "confirm": "yes"})
    assert r.status_code == 303 and env.hub.schedules.get(s["id"])["status"] == "paused"
    assert audit_rows(env, "schedule.pause")[-1]["channel"] == "web"
    assert "Bật lại" in (await env.client.get("/schedules?status=paused")).text
    r = await env.post("/do/schedule_resume", {"id": s["id"], "next": "/schedules", "confirm": "yes"})
    assert r.status_code == 303 and env.hub.schedules.get(s["id"])["status"] == "active"
    # chạy ngay → run thật (FakeRuntime), chuyển tới trang run
    r = await env.post("/do/schedule_run_now", {"id": s["id"], "next": "/schedules", "confirm": "yes"})
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/")
    rid = r.headers["location"].rsplit("/", 1)[1]
    assert env.hub.store.get_run(rid)["kind"] == "schedule"
    assert audit_rows(env, "schedule.run_now")[-1]["channel"] == "web"
    # xoá: hộp xác nhận nguy hiểm, xoá mềm
    r = await env.post("/do/schedule_rm", {"id": s["id"], "name": s["name"], "next": "/schedules"})
    assert r.status_code == 200 and "Xoá mềm" in r.text and "btn danger" in r.text
    r = await env.post("/do/schedule_rm", {"id": s["id"], "next": "/schedules", "confirm": "yes"})
    assert r.status_code == 303 and env.hub.schedules.list() == []
    assert env.hub.store.conn.execute("SELECT deleted_at FROM schedules WHERE id=?", (s["id"],)).fetchone()[0]
    assert audit_rows(env, "schedule.rm")[-1]["channel"] == "web"


# =========================================================================== tìm kiếm hội thoại
async def test_search_page_highlights_and_links_runs(env):
    await env.login()
    r = await env.client.get("/search")
    assert r.status_code == 200 and "Gõ từ khoá để bắt đầu" in r.text
    run = await done_run(env, prompt="Kiểm tra cấu hình sandbox <script>alert(1)</script> giúp tôi")
    r = await env.client.get("/search", params={"q": "cau hinh sandbox"})  # không dấu vẫn khớp
    assert r.status_code == 200, r.text
    assert f'href="/runs/{run["id"]}"' in r.text and "<mark>cấu</mark> <mark>hình</mark>" in r.text
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;" in r.text  # chữ agent/người luôn được escape
    r = await env.client.get("/search", params={"q": "cau hinh", "agent": "ag0", "since": "7d", "role": "user"})
    assert f'/runs/{run["id"]}' in r.text
    r = await env.client.get("/search", params={"q": "cau hinh", "agent": "khac"})
    assert "Không có kết quả" in r.text
    r = await env.client.get("/search", params={"q": "cau hinh", "since": "bậy"})  # bộ lọc lạ bị bỏ qua, không lỗi
    assert r.status_code == 200 and f'/runs/{run["id"]}' in r.text


# =========================================================================== diff / hoàn tác của run
async def test_run_diff_section_and_revert_with_conflict(env, repo):
    await env.login()
    run = await done_run(env)
    rid = run["id"]
    # run không ghi file → khối rỗng (biến mất khỏi trang run)
    r = await env.client.get(f"/partials/run/{rid}/diff")
    assert r.status_code == 200 and r.text.strip() == ""
    page = await env.client.get(f"/runs/{rid}")
    assert f'hx-get="/partials/run/{rid}/diff"' in page.text
    attach_diff(env, rid, repo, {"a.txt": "a1\n<b>agent</b>\n", "new.txt": "hello\n"})
    r = await env.client.get(f"/partials/run/{rid}/diff")
    assert r.status_code == 200 and "Thay đổi file" in r.text and "a.txt" in r.text and "new.txt" in r.text
    assert 'class="dl add"' in r.text and 'class="dl del"' in r.text and "&lt;b&gt;agent&lt;/b&gt;" in r.text
    assert "Hoàn tác thay đổi" in r.text and "CHƯA bị sửa tiếp" in r.text and 'action="/do/run_revert"' in r.text
    full = await env.client.get(f"/runs/{rid}/diff")
    assert full.status_code == 200 and "Thay đổi file" in full.text and 'href="/runs/' in full.text
    # kiểm tra trước (dry run): không ghi gì
    r = await env.post("/do/run_revert_check", {"id": rid, "next": f"/runs/{rid}"})
    assert r.status_code == 303 and (repo / "new.txt").exists()
    assert "Kiểm tra: hoàn tác được 2 file" in (await env.client.get(f"/runs/{rid}")).text
    # người sửa tiếp file sau run → trang báo xung đột, không có nút hoàn tác; ép gọi → lỗi xung đột rõ ràng, không ghi đè
    (repo / "a.txt").write_bytes(("a1\n<b>agent</b>\nngười sửa sau\n").encode())
    r = await env.client.get(f"/partials/run/{rid}/diff")
    assert "đã bị sửa tiếp" in r.text and 'action="/do/run_revert"' not in r.text
    r = await env.post("/do/run_revert", {"id": rid, "confirm": "yes", "next": f"/runs/{rid}"})
    assert r.status_code == 303
    txt = (await env.client.get(f"/runs/{rid}")).text
    assert "flash error" in txt and "xung đột" in txt and "a.txt" in txt
    assert "người sửa sau" in (repo / "a.txt").read_bytes().decode() and (repo / "new.txt").exists()
    # gỡ xung đột → hoàn tác thật (hộp xác nhận nguy hiểm trước)
    (repo / "a.txt").write_bytes(("a1\n<b>agent</b>\n").encode())
    r = await env.post("/do/run_revert", {"id": rid, "files": "2", "next": f"/runs/{rid}"})
    assert r.status_code == 200 and "Hoàn tác thay đổi file" in r.text and "CHƯA bị sửa tiếp" in r.text and "btn danger" in r.text
    r = await env.post("/do/run_revert", {"id": rid, "confirm": "yes", "next": f"/runs/{rid}"})
    assert r.status_code == 303 and r.headers["location"] == f"/runs/{rid}#diff"
    assert (repo / "a.txt").read_bytes().decode() == "a1\n-- giữ dòng này\n" and not (repo / "new.txt").exists()
    assert audit_rows(env, "run.revert")[-1]["channel"] == "web"
    r = await env.client.get(f"/partials/run/{rid}/diff")
    assert "Đã hoàn tác lúc" in r.text and 'action="/do/run_revert"' not in r.text


# =========================================================================== sức khoẻ
async def test_health_card_on_settings(env):
    await env.login()
    page = await env.client.get("/settings")
    assert page.status_code == 200 and 'hx-get="/partials/health"' in page.text
    r = await env.client.get("/partials/health")
    assert r.status_code == 200 and "Sức khoẻ" in r.text and "Daemon" in r.text and "Toàn vẹn chính sách" in r.text and "cfharness doctor" in r.text


async def test_health_card_shows_hang_stats(env):
    """report.hangs: run timed_out/interrupted 14 ngày theo agent/loại/tool dở/thời lượng (auto_resume.hang_stats)."""
    import uuid

    from cfharness.store import now_iso

    env.hub.agents.create({"id": "slowpoke", "name": "slowpoke", "model": "haiku"})
    for oc in ("timed_out", "timed_out", "interrupted"):
        env.hub.store.insert_run({"id": "r_" + uuid.uuid4().hex[:10], "agent_id": "slowpoke", "kind": "adhoc", "prompt": "p", "session_id": str(uuid.uuid4()),
                                  "status": oc, "outcome": oc, "queued_at": now_iso(), "started_at": now_iso(), "ended_at": now_iso()})
    await env.login()
    r = await env.client.get("/partials/health")
    assert r.status_code == 200 and "Run treo / quá giờ (14 ngày)" in r.text and "2 timed_out" in r.text
    assert "Run hay treo / quá giờ — 3 run" in r.text and "slowpoke" in r.text and "Tool đang chạy dở" in r.text
