"""Dữ liệu NẶNG cho demo web (`python -m cfharness.web.demo --heavy`): thử UI khi hub có nhiều agent, luồng, hội thoại, task, run,
thao tác chờ duyệt và câu hỏi — tên/tiêu đề dài, hội thoại 80+ tin. Chỉ dùng trong demo (FakeRuntime, không gọi `claude`)."""

from __future__ import annotations

import asyncio
import random
import traceback
from datetime import datetime, timedelta, timezone

AGENTS = [("phan-tich-du-lieu", "researcher", "Phân tích dữ liệu"), ("frontend", "coder", "Frontend"), ("backend", "coder", "Backend"),
          ("qa-tu-dong", "reviewer", "QA tự động"), ("bao-mat", "reviewer", "Bảo mật"), ("devops", "ops", "DevOps"),
          ("viet-tai-lieu", "researcher", "Viết tài liệu"), ("ho-tro-khach-hang", "researcher", "Hỗ trợ khách hàng"),
          ("toi-uu-chi-phi", "ops", "Tối ưu chi phí"), ("dich-thuat", "researcher", "Dịch thuật"),
          ("crawler-tin-tuc-thi-truong-chau-a-thai-binh-duong", "researcher", "Crawler tin tức thị trường châu Á – Thái Bình Dương (tên rất dài)")]
WS = [("WEB-101", "Thiết kế lại trang Điều phối"), ("API-7", "Chuyển API sang v2"), ("OPS", "Vận hành hằng ngày"),
      ("SEC-3", "Rà soát bảo mật quý 4"), ("DOC", "Tài liệu người dùng mới"), ("DATA-22", "Báo cáo doanh thu theo vùng"),
      ("MOB-5", "Ứng dụng di động: thông báo đẩy"), ("LONG-1", "Luồng có tiêu đề rất rất dài để thử cắt chữ trên thanh bên và danh sách hội thoại")]
TOPICS = ["Tóm tắt các PR đã merge tuần này", "Vì sao CI trên main đỏ?", "Lên kế hoạch release v0.3", "Kiểm tra chi phí agent tháng này",
          "Viết lại README phần cài đặt", "Tìm nguyên nhân daemon chậm khi khởi động", "Rà quyền MCP github", "So sánh 3 phương án cache",
          "Soạn email báo khách về sự cố", "Dọn task cũ quá 30 ngày", "Thêm test cho module lịch chạy", "Tạo agent mới cho team dữ liệu",
          "Phân tích log lỗi 500 hôm qua", "Dịch tài liệu onboarding sang tiếng Anh", "Báo cáo tiến độ sprint 216",
          "Hỏi nhanh: agent nào đang rảnh?", "Rà soát bảng giá đối thủ", "Chuẩn bị demo cho buổi họp thứ Sáu",
          "Sửa lỗi hiển thị trên Safari iOS", "Kiểm tra backup đêm qua", "Tối ưu truy vấn báo cáo doanh thu", "Viết spec cho tính năng xuất PDF",
          "Gom feedback người dùng tuần này", "Xử lý ticket hỗ trợ #4821", "Đánh giá lại ngân sách team docs",
          "Một câu hỏi rất dài về việc có nên chuyển toàn bộ hạ tầng sang kiến trúc hướng sự kiện hay không, kèm phân tích chi phí, rủi ro và lộ trình chuyển đổi trong 6 tháng",
          "Lập lịch chạy báo cáo mỗi sáng thứ Hai", "Kiểm tra link hỏng toàn site", "Rà soát hợp đồng nhà cung cấp", "Tóm tắt cuộc họp sáng nay"]
LONG_REPLY = ("Tôi đã xem xong. Tóm tắt:\n\n## Kết quả\n- **3 PR** đã merge: #201, #203, #204\n- CI đỏ do `test_lane_service` (không liên quan)\n"
              "- Chi phí hôm nay: $1.27, trong ngân sách\n\n## Việc tiếp theo\n1. Giao `qa-tu-dong` chạy lại bộ test hồi quy\n"
              "2. Nhờ `bao-mat` rà quyền MCP github\n3. Báo lại anh trước 17h\n\n```bash\nuv run pytest -q tests/test_lane_service.py\n```\n"
              "Nếu anh muốn tôi ưu tiên khác thì nhắn thêm nhé.")
CMDS = ["rm -rf /var/log/app/archive-2025", "git push --force origin main", "DROP TABLE staging_orders;", "kubectl delete ns preview-42",
        "aws s3 rm s3://cf-backups/old --recursive", "chmod -R 777 /srv/shared"]
QS = [("Nên đặt tên bảng mới là revenue_by_region hay region_revenue?", ["revenue_by_region", "region_revenue"]),
      ("Có 340 ticket cũ chưa đóng. Đóng hàng loạt hay để từng người xử lý?", ["Đóng hàng loạt", "Để từng người xử lý", "Chỉ đóng ticket > 90 ngày"]),
      ("Bản dịch dùng 'workspace' hay 'không gian làm việc'?", ["workspace", "không gian làm việc"]),
      ("Deploy lên preview trước hay thẳng production? Thay đổi có migration DB.", ["Preview trước", "Thẳng production"]),
      ("Font chữ trên app mobile: giữ Inter hay đổi sang hệ thống?", ["Giữ Inter", "Font hệ thống"]),
      ("Báo cáo gửi cho ai: chỉ ban giám đốc hay cả team?", ["Chỉ ban giám đốc", "Cả team", "Ban giám đốc + trưởng nhóm"])]


def step(name):
    def deco(fn):
        async def run(*a, **k):
            try:
                r = fn(*a, **k)
                if asyncio.iscoroutine(r):
                    r = await r
                print(f"HEAVY {name}: {r}", flush=True)
                return r
            except Exception:  # best-effort: hub demo nạp dữ liệu, lỗi một mục không dừng cả lượt
                print(f"HEAVY {name} FAILED\n{traceback.format_exc()}", flush=True)
        return run
    return deco


async def seed_heavy(d) -> None:
    from .. import templates
    from ..events import iso

    random.seed(7)  # dữ liệu giống nhau mỗi lần chạy
    hub = d.hub
    now = datetime.now(timezone.utc)

    async def call(method, params):
        resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp["result"]

    @step("agents")
    def agents():
        for aid, tpl, name in AGENTS:
            cfg, soul = templates.render(tpl, aid, {"name": name})
            hub.agents.create(cfg, soul)
        for tid, name, mem in (("du-lieu", "Nhóm dữ liệu", ["phan-tich-du-lieu", "backend"]), ("san-pham", "Nhóm sản phẩm", ["frontend", "backend", "qa-tu-dong"]),
                               ("van-hanh", "Nhóm vận hành", ["devops", "bao-mat", "toi-uu-chi-phi"])):
            hub.teams.create({"id": tid, "name": name, "charter": f"{name}: phụ trách mảng của mình."})
            for m in mem:
                hub.teams.member_set(tid, m, "worker", 1)
        return len(AGENTS)
    await agents()

    ws_ids = []

    @step("workstreams")
    def workstreams():
        for key, title in WS:
            ws_ids.append(hub.manager.ws.create(title, key=key, by="user")["id"])
        return len(ws_ids)
    await workstreams()

    @step("tasks")
    def tasks():
        names = [a[0] for a in AGENTS] + ["nghien-cuu", "lap-trinh", "review", "van-hanh"]
        st = ["triage"] * 4 + ["todo"] * 10 + ["ready"] * 6 + ["review"] * 6 + ["done"] * 25 + ["blocked"] * 5 + ["running"] * 4
        n = 0
        for i, s in enumerate(st):
            title = random.choice(TOPICS) if i % 7 else TOPICS[25]
            data = {"title": f"{title} ({i + 1})", "body": "Chi tiết việc cần làm, tiêu chí xong, link tài liệu liên quan.", "assignee": random.choice(names)}
            if ws_ids and i % 3 == 0:
                data["workstream"] = ws_ids[i % len(ws_ids)]
            try:
                t = hub.tasks.create(data)
            except Exception:  # best-effort: hub demo nạp dữ liệu, lỗi một mục không dừng cả lượt
                data.pop("workstream", None)
                t = hub.tasks.create(data)
            try:
                if s == "blocked":
                    hub.tasks.block(t["id"], "Chờ quyền truy cập kho dữ liệu production")
                elif s != t.get("status"):
                    hub.tasks.set_status(t["id"], s)
            except Exception as e:  # best-effort: hub demo nạp dữ liệu, lỗi một mục không dừng cả lượt
                print("task status", s, e, flush=True)
            n += 1
        return n
    await tasks()

    @step("runs-done")
    async def runs_done():
        names = [a[0] for a in AGENTS]
        rs = []
        for i in range(90):
            p = random.choice(TOPICS)
            rs.append(hub.runs.submit(random.choice(names), p))
        for r in rs:
            try:
                await hub.runs.wait(r["id"], 60)
            except Exception:  # best-effort: hub demo nạp dữ liệu, lỗi một mục không dừng cả lượt
                pass
        return len(rs)
    await runs_done()

    held = []

    @step("runs-live")
    async def runs_live():
        for aid in ("frontend", "backend", "devops", "bao-mat", "phan-tich-du-lieu", "qa-tu-dong", "viet-tai-lieu", "toi-uu-chi-phi",
                    "crawler-tin-tuc-thi-truong-chau-a-thai-binh-duong", "dich-thuat"):
            held.append(hub.runs.submit(aid, "[hold] " + random.choice(TOPICS)))
        for _ in range(400):
            if all((hub.store.get_run(x["id"]) or {}).get("status") == "running" for x in held):
                break
            await asyncio.sleep(0.05)
        return [(hub.store.get_run(x["id"]) or {}).get("status") for x in held]
    await runs_live()

    @step("approvals")
    def approvals():
        n = 0
        for i, cmd in enumerate(CMDS):
            run = hub.store.get_run(held[i]["id"])
            if not run or run.get("status") != "running":
                continue
            ap = hub.approvals.open(run, tool_name="Bash", tool_input={"command": cmd}, tool_use_id=f"toolu_heavy{i}", fingerprint=f"fp-heavy{i}",
                                    verdict={"klass": "fs_delete", "rule": "destructive_cmd", "verdict": "destructive", "reason": "thao tác phá huỷ khó hoàn tác"},
                                    channel="hook")
            hub.approvals.to_waiting_human(ap["id"])
            n += 1
        return n
    await approvals()

    @step("questions")
    def questions():
        n = 0
        for i, (text, opts) in enumerate(QS):
            run = hub.store.get_run(held[6 + i % 4]["id"]) if i < 4 else hub.store.get_run(held[i]["id"])
            if run:
                hub.questions.open(dict(run), text, opts)
                n += 1
        return n
    await questions()

    @step("conversations")
    async def conversations():
        conn = hub.store.conn
        made = []
        for i, topic in enumerate(TOPICS):
            r = await call("manager.chat", {"channel": "web", "text": topic})
            await hub.runs.wait(r["run_id"], 30)
            cid = r["conversation_id"]
            made.append(cid)
            if i % 4 == 0:
                r2 = await call("manager.chat", {"channel": "web", "conversation_id": cid, "text": "Cụ thể hơn được không? Liệt kê từng bước."})
                await hub.runs.wait(r2["run_id"], 30)
            ids = [x[0] for x in conn.execute("SELECT id, role FROM manager_messages WHERE conversation_id=? ORDER BY id", (cid,))]
            age = timedelta(hours=i * 7 + random.randint(0, 5), minutes=random.randint(0, 59))
            for k, mid in enumerate(ids):
                role = conn.execute("SELECT role FROM manager_messages WHERE id=?", (mid,)).fetchone()[0]
                conn.execute("UPDATE manager_messages SET ts=? WHERE id=?", (iso(now - age + timedelta(minutes=k)), mid))
                if role != "user":
                    conn.execute("UPDATE manager_messages SET text=? WHERE id=?", (LONG_REPLY if k % 2 else "Đã nhận, tôi đang xử lý.", mid))
            conn.execute("UPDATE manager_conversations SET title=?, updated_at=?, created_at=? WHERE id=?",
                         (topic, iso(now - age + timedelta(minutes=len(ids))), iso(now - age), cid))
            if ws_ids and i % 3 == 0:
                try:
                    hub.manager.ws.attach_conversation(cid, ws_ids[(i // 3) % len(ws_ids)], by="user")
                except Exception as e:  # best-effort: hub demo nạp dữ liệu, lỗi một mục không dừng cả lượt
                    print("attach", e, flush=True)
        # một hội thoại rất dài (80 tin)
        cid = made[1]
        for k in range(80):
            role = "user" if k % 2 == 0 else "assistant"
            text = f"Tin số {k + 1}: " + ("Anh kiểm tra giúp phần này nhé." if role == "user" else LONG_REPLY)
            conn.execute("INSERT INTO manager_messages(conversation_id, role, text, ts) VALUES (?,?,?,?)",
                         (cid, role, text, iso(now - timedelta(minutes=200 - k * 2))))
        conn.execute("UPDATE manager_conversations SET updated_at=? WHERE id=?", (iso(now), cid))
        conn.commit()
        return len(made)
    await conversations()
