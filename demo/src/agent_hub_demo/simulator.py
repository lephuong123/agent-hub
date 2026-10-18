"""Hub GIẢ LẬP: 5 agent trong 2 team, không gọi `claude` thật.

Để chứng minh đường ống thật, kịch bản sinh ra các dòng KIỂU stream-json của `claude -p
--output-format stream-json --verbose --include-partial-messages` (system/init, stream_event,
assistant, user/tool_result, result) rồi đưa qua `ClaudeStreamAdapter` → `EventBus`.
Chỉ những gì CLI không phát (duyệt lệnh từ hook/policy, đề xuất học, steer) mới được phát thẳng
dưới dạng event hub — đúng như hub thật sẽ làm.

`FakeHub` cũng là bản nháp API điều khiển của hub thật: approve/deny/steer/stop, CRUD agent,
duyệt/từ chối/rollback learning.
"""

from __future__ import annotations

import asyncio
import itertools
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Generator, Iterator

from .bus import Aggregator, AgentState, EventBus
from .claude_adapter import PRICES, ClaudeStreamAdapter
from .events import HubEvent, make_event

TEAMS = ["Nghiên cứu", "Vận hành"]
MODELS = ["opus", "sonnet", "haiku"]
MODEL_IDS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
MCP_CATALOG = [
    "hthong-tlkc-don-vi",
    "tools-cchc",
    "cds-100-ngay",
    "viec-nhom-2",
    "theo-doi-cchc",
    "sang-kien-cchc",
    "serena",
]
BASE_TOOLS = ["Bash", "Read", "Edit", "Write", "Grep", "Glob", "Task", "WebSearch", "WebFetch"]


# ============================================================================ bước kịch bản
@dataclass
class Line:
    """Một dòng stream-json (dict) — đi qua ClaudeStreamAdapter."""

    obj: dict


@dataclass
class Hub:
    """Event hub phát trực tiếp (hook/policy/learning) — không có trong stdout của claude."""

    type: str
    payload: dict


@dataclass
class Begin:
    """Bắt đầu một lượt chạy mới (= spawn một tiến trình `claude -p`)."""

    sj: "StreamJson"


@dataclass
class Sleep:
    s: float


@dataclass
class Live:
    """Mốc: phần trước mốc này được 'chạy sẵn' khi khởi động (lịch sử), phần sau chạy realtime."""


@dataclass
class AwaitApproval:
    request_id: str


Step = Line | Hub | Begin | Sleep | Live | AwaitApproval
Script = Generator[Step, Any, None]


# ============================================================================ builder stream-json
class StreamJson:
    """Sinh các dòng giống hệt định dạng `claude -p --output-format stream-json` (rút gọn trường)."""

    def __init__(self, model: str, task: str, mcp: list[str], cwd: str = "/srv/agent-hub/ws", ctx0: int = 21_900):
        self.alias = model
        self.model = MODEL_IDS.get(model, model)
        self.task = task
        self.mcp = mcp
        self.cwd = cwd
        self.session_id = str(uuid.uuid4())
        # ngữ cảnh ban đầu khác nhau một chút giữa các lượt cho giống thật
        self.ctx = ctx0 + (sum(map(ord, task)) * 97) % 38_000
        self.total_in = 0
        self.total_out = 0
        self.n_msgs = 0
        self.t0 = time.monotonic()
        self._msg_id: str | None = None
        self._idx = 0
        self.done = False

    def _l(self, **kw: Any) -> dict:
        kw.setdefault("session_id", self.session_id)
        kw.setdefault("uuid", str(uuid.uuid4()))
        return kw

    def _se(self, event: dict) -> dict:
        return self._l(type="stream_event", event=event, parent_tool_use_id=None, api_message_id=self._msg_id)

    def init(self) -> dict:
        tools = BASE_TOOLS + [f"mcp__{s}__*" for s in self.mcp]
        return self._l(
            type="system",
            subtype="init",
            cwd=self.cwd,
            tools=tools,
            mcp_servers=[{"name": s, "status": "connected"} for s in self.mcp],
            model=self.model,
            permissionMode="default",
            claude_code_version="2.1.302",
        )

    def msg_start(self) -> dict:
        self._msg_id = f"msg_{uuid.uuid4().hex[:20]}"
        self._idx = 0
        self.n_msgs += 1
        self.total_in += self.ctx
        return self._se(
            {
                "type": "message_start",
                "message": {
                    "model": self.model,
                    "id": self._msg_id,
                    "role": "assistant",
                    "content": [],
                    "usage": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 400,
                        "cache_read_input_tokens": max(0, self.ctx - 402),
                        "output_tokens": 4,
                    },
                },
            }
        )

    def block_start(self, kind: str, **extra: Any) -> dict:
        return self._se({"type": "content_block_start", "index": self._idx, "content_block": {"type": kind, **extra}})

    def block_stop(self) -> dict:
        ev = self._se({"type": "content_block_stop", "index": self._idx})
        self._idx += 1
        return ev

    def text_delta(self, text: str) -> dict:
        return self._se({"type": "content_block_delta", "index": self._idx, "delta": {"type": "text_delta", "text": text}})

    def thinking_delta(self, text: str) -> dict:
        return self._se(
            {"type": "content_block_delta", "index": self._idx, "delta": {"type": "thinking_delta", "thinking": text}}
        )

    def assistant(self, block: dict) -> dict:
        return self._l(
            type="assistant",
            message={"model": self.model, "id": self._msg_id, "role": "assistant", "content": [block]},
            parent_tool_use_id=None,
        )

    def msg_delta(self, out_tokens: int, stop_reason: str) -> dict:
        self.total_out += out_tokens
        self.ctx += out_tokens
        return self._se(
            {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": out_tokens}}
        )

    def msg_stop(self) -> dict:
        return self._se({"type": "message_stop"})

    def tool_result(self, tid: str, content: str, is_error: bool = False) -> dict:
        self.ctx += len(content) // 3 + 60
        return self._l(
            type="user",
            message={"role": "user", "content": [{"tool_use_id": tid, "type": "tool_result", "content": content, "is_error": is_error}]},
            parent_tool_use_id=None,
        )

    def cost(self) -> float:
        in_p, out_p = PRICES[self.alias] if self.alias in PRICES else PRICES["sonnet"]
        return round((self.total_in * in_p * 0.3 + self.total_out * out_p) / 1e6, 6)

    def result(self, text: str, *, is_error: bool = False, subtype: str = "success", duration_ms: int = 0) -> dict:
        self.done = True
        return self._l(
            type="result",
            subtype=subtype,
            is_error=is_error,
            result=text,
            num_turns=self.n_msgs,
            duration_ms=duration_ms,
            total_cost_usd=self.cost(),
            usage={
                "input_tokens": self.n_msgs * 2,
                "cache_creation_input_tokens": self.n_msgs * 400,
                "cache_read_input_tokens": self.total_in - self.n_msgs * 402,
                "output_tokens": self.total_out,
            },
            modelUsage={self.model: {"contextWindow": 200_000, "costUSD": self.cost()}},
            permission_denials=[],
        )


def _chunks(text: str, n: int) -> Iterator[str]:
    """Cắt chữ thành mẩu ~n ký tự theo ranh giới từ (giống delta token)."""
    words = text.split(" ")
    buf = ""
    for i, w in enumerate(words):
        piece = w if i == 0 else " " + w
        buf += piece
        if len(buf) >= n:
            yield buf
            buf = ""
    if buf:
        yield buf


# ============================================================================ khối kịch bản
def think(sj: StreamJson, text: str, delay: float = 0.03) -> Script:
    yield Line(sj.block_start("thinking", thinking=""))
    for c in _chunks(text, 18):
        yield Line(sj.thinking_delta(c))
        yield Sleep(delay)
    yield Line(sj.assistant({"type": "thinking", "thinking": text}))
    yield Line(sj.block_stop())


def write_text(sj: StreamJson, text: str, delay: float = 0.04, live_after: int | None = None) -> Script:
    yield Line(sj.block_start("text", text=""))
    for i, c in enumerate(_chunks(text, 10)):
        yield Line(sj.text_delta(c))
        yield Sleep(delay)
        if live_after is not None and i == live_after:
            yield Live()
    yield Line(sj.assistant({"type": "text", "text": text}))
    yield Line(sj.block_stop())


def tool_call(
    sj: StreamJson,
    name: str,
    inp: dict,
    result: str,
    dur: float = 0.8,
    *,
    thinking: str | None = None,
    text: str | None = None,
    is_error: bool = False,
    live_while_running: bool = False,
) -> Script:
    """Một message assistant: [thinking] [text] tool_use → chạy tool → tool_result."""
    yield Line(sj.msg_start())
    yield Sleep(0.3)
    if thinking:
        yield from think(sj, thinking)
    if text:
        yield from write_text(sj, text)
    tid = f"toolu_{uuid.uuid4().hex[:20]}"
    yield Line(sj.block_start("tool_use", id=tid, name=name, input={}))
    yield Line(sj.assistant({"type": "tool_use", "id": tid, "name": name, "input": inp}))
    yield Line(sj.block_stop())
    yield Line(sj.msg_delta(len(str(inp)) // 3 + len(thinking or "") // 3 + len(text or "") // 3 + 30, "tool_use"))
    yield Line(sj.msg_stop())
    if live_while_running:
        yield Live()
    yield Sleep(dur)
    yield Line(sj.tool_result(tid, result, is_error))


def final_answer(sj: StreamJson, text: str, *, thinking: str | None = None, live_after: int | None = None) -> Script:
    yield Line(sj.msg_start())
    yield Sleep(0.3)
    if thinking:
        yield from think(sj, thinking)
    yield from write_text(sj, text, live_after=live_after)
    yield Line(sj.msg_delta(len(text) // 3 + 20, "end_turn"))
    yield Line(sj.msg_stop())
    yield Line(sj.result(text[:200], duration_ms=int((sj.n_msgs * 4.2) * 1000)))


def learning(agent: str, scope: str, kind: str, title: str, diff: str, *, auto: bool) -> Script:
    pid = f"L{uuid.uuid4().hex[:5]}"
    yield Hub("learning.proposal", {"proposal_id": pid, "scope": scope, "kind": kind, "title": title, "diff": diff, "auto": auto})
    if auto:
        yield Sleep(0.5)
        yield Hub("learning.applied", {"proposal_id": pid, "action": "applied", "by": "auto (eval 12/12 đạt)"})


# ============================================================================ kịch bản 5 agent
def life_researcher(a: AgentState) -> Script:
    tasks = itertools.cycle(
        [
            "Tra cứu văn bản hướng dẫn chuyển đổi số cấp phường năm 2026",
            "Tóm tắt các văn bản mới về cải cách hành chính tháng 10",
        ]
    )
    first = True
    while True:
        task = next(tasks)
        sj = StreamJson(a.model, task, a.mcp_servers)
        yield Begin(sj)
        yield Line(sj.init())
        yield from tool_call(
            sj,
            "mcp__hthong-tlkc-don-vi__vb_tu_dien_truong",
            {"loai": "Văn bản hành chính"},
            "17 cột: SoKyHieu, TrichYeu, CoQuan, MaDonVi, NgayBanHanh, LoaiVanBan, LinhVuc, …",
            0.6,
            thinking="Người dùng cần các văn bản hướng dẫn CĐS cấp phường. Theo hướng dẫn của cổng TLKC, phải gọi "
            "vb_tu_dien_truong trước để biết tên cột thật — lọc sai tên cột sẽ bị bỏ qua im lặng.",
        )
        yield from tool_call(
            sj,
            "mcp__hthong-tlkc-don-vi__vb_tim",
            {"tu_khoa": "chuyển đổi số", "nam": 2026, "linh_vuc": "CĐS"},
            "Tìm thấy 14 văn bản (pham_vi: 1 cơ quan)\n"
            "1. KH 215/KH-UBND — Kế hoạch CĐS năm 2026\n2. CV 1180/STTTT — Hướng dẫn chỉ số DTI cấp xã\n…",
            1.4,
            text="Đã có từ điển trường. Tôi lọc theo lĩnh vực và năm ban hành.",
            live_while_running=first,
        )
        yield from tool_call(
            sj,
            "Task",
            {"subagent_type": "general-purpose", "description": "Đọc và tóm tắt 5 văn bản chính", "prompt": "…"},
            "Đã tóm tắt 5 văn bản: KH 215, CV 1180, QĐ 2034, HD 07, TB 512.",
            2.5,
            text="Phạm vi tín chỉ chỉ thấy 1 cơ quan — kết quả có thể chưa đủ toàn hệ. Giao subagent đọc song song 5 văn bản chính.",
        )
        yield from tool_call(
            sj,
            "mcp__hthong-tlkc-don-vi__vb_doc",
            {"so_ky_hieu": "215/KH-UBND"},
            "KẾ HOẠCH Chuyển đổi số năm 2026 … mục tiêu 100% hồ sơ TTHC được số hoá …",
            0.9,
        )
        yield from final_answer(
            sj,
            "## Kết quả tra cứu\n"
            "Có **5 văn bản** trực tiếp hướng dẫn CĐS cấp phường năm 2026:\n"
            "- KH 215/KH-UBND: kế hoạch năm, 100% hồ sơ TTHC số hoá trước 31/12.\n"
            "- CV 1180/STTTT: cách tính chỉ số DTI cấp xã, phường.\n"
            "- QĐ 2034/QĐ-UBND: danh mục dịch vụ công trực tuyến toàn trình.\n"
            "Lưu ý: tín chỉ hiện chỉ thấy kho của 1 cơ quan, danh sách có thể chưa đầy đủ.",
        )
        if first:
            yield from learning(
                a.id, "agent", "memory", "Luôn gọi vb_tu_dien_truong trước lượt lọc đầu",
                "+ Với cổng hthong-tlkc-don-vi: gọi vb_tu_dien_truong trước khi lọc;\n"
                "+ cột sai tên bị bỏ qua IM LẶNG → kết quả nhiều hơn thực tế.",
                auto=True,
            )
        first = False
        yield Hub("agent.status", {"status": "idle"})
        yield Live()
        yield Sleep(10)


def life_writer(a: AgentState) -> Script:
    first = True
    while True:
        sj = StreamJson(a.model, "Soạn báo cáo tuần — đợt 100 ngày CĐS phường Thượng Cát", a.mcp_servers)
        yield Begin(sj)
        yield Line(sj.init())
        yield from tool_call(sj, "mcp__cds-100-ngay__boi_canh", {}, "Đợt 100 ngày: ngày 96/100 · kết thúc 22/10/2026", 0.5)
        yield from tool_call(
            sj,
            "mcp__cds-100-ngay__so_lieu_tong_hop",
            {},
            "Nhiệm vụ cấp phường: 48 · hoàn thành 39 (81%) · đang làm 6 · quá hạn 3",
            1.1,
            thinking="Mẫu số phải là nhiệm vụ cấp PHƯỜNG, lấy số chính thức từ so_lieu_tong_hop, không tự cộng các tầng.",
        )
        yield from tool_call(
            sj,
            "mcp__cds-100-ngay__canh_bao",
            {},
            "3 nhiệm vụ quá hạn: NV-12 Số hoá hồ sơ đất đai; NV-27 Cập nhật CSDL dân cư; NV-31 Tập huấn chữ ký số",
            0.8,
        )
        yield from final_answer(
            sj,
            "# BÁO CÁO TUẦN — Đợt 100 ngày Chuyển đổi số\n"
            "Tính đến ngày 96/100 (đợt kết thúc 22/10/2026), phường có 48 nhiệm vụ; đã hoàn thành 39 (81%), "
            "đang thực hiện 6, quá hạn 3. Các nhiệm vụ quá hạn cần ưu tiên trong 4 ngày còn lại: số hoá hồ sơ đất đai, "
            "cập nhật CSDL dân cư và tập huấn chữ ký số. Đề xuất: giao đầu mối từng nhiệm vụ, báo cáo hằng ngày đến hết đợt.",
            live_after=22 if first else None,
        )
        if first:
            yield from learning(
                a.id, "team", "skill", "Skill mới: bao-cao-tuan-cds (khung báo cáo tuần)",
                "+ skills/bao-cao-tuan-cds/SKILL.md\n+ # Khung báo cáo tuần đợt 100 ngày\n"
                "+ 1. Gọi boi_canh → ngày thứ mấy\n+ 2. Số liệu: so_lieu_tong_hop (mẫu số = cấp phường)\n+ 3. canh_bao → quá hạn",
                auto=False,
            )
        first = False
        yield Hub("agent.status", {"status": "idle"})
        yield Live()
        yield Sleep(25)


def life_coder(a: AgentState) -> Script:
    while True:
        sj = StreamJson(a.model, "Sửa lỗi lọc nhiệm vụ quá hạn (viec-nhom-2)", a.mcp_servers)
        yield Begin(sj)
        yield Line(sj.init())
        yield from tool_call(sj, "Grep", {"pattern": "qua_han", "path": "src/"}, "src/loc_nhiem_vu.py:42\nsrc/so_lieu.py:88", 0.4)
        yield from tool_call(
            sj, "Read", {"file_path": "src/loc_nhiem_vu.py"},
            "def loc_qua_han(ds, hom_nay):\n    return [nv for nv in ds if nv.han < hom_nay]  # bỏ sót nv.han == None",
            0.3,
            thinking="Hàm lọc so sánh `nv.han < hom_nay` nhưng không loại nhiệm vụ đã hoàn thành và không xử lý han=None.",
        )
        yield from tool_call(
            sj, "Edit", {"file_path": "src/loc_nhiem_vu.py"},
            "Đã sửa 1 chỗ: thêm điều kiện nv.han is not None and not nv.hoan_thanh", 0.5,
            text="Sửa điều kiện lọc: bỏ việc đã hoàn thành và việc chưa có hạn.",
        )
        yield from tool_call(sj, "Bash", {"command": "uv run pytest -q"}, "2 failed, 41 passed — ImportError: build/lib/loc_nhiem_vu.py (bản cũ)", 2.1)
        # --- lệnh nguy hiểm → policy (hook PreToolUse) chặn lại, hỏi người duyệt
        yield Line(sj.msg_start())
        yield from write_text(sj, "Thư mục build/ chứa bản cũ khiến pytest import nhầm. Cần xoá build/ và dist/.")
        tid = f"toolu_{uuid.uuid4().hex[:20]}"
        cmd = "rm -rf build/ dist/ .pytest_cache"
        yield Line(sj.assistant({"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}))
        yield Line(sj.msg_delta(60, "tool_use"))
        rid = f"R{uuid.uuid4().hex[:5]}"
        yield Hub(
            "request.open",
            {"request_id": rid, "kind": "approval", "tool": "Bash", "command": cmd, "tool_use_id": tid,
             "reason": "Lệnh xoá đệ quy (rm -rf) — chính sách yêu cầu người duyệt"},
        )
        yield Hub("agent.status", {"status": "waiting", "detail": f"duyệt: {cmd}"})
        decision = yield AwaitApproval(rid)
        yield Hub("agent.status", {"status": "working"})
        if decision == "approved":
            yield Sleep(0.4)
            yield Line(sj.tool_result(tid, "(không có output)"))
            yield from tool_call(sj, "Bash", {"command": "uv run pytest -q"}, "43 passed in 3.2s", 2.0)
            yield from final_answer(sj, "Đã sửa `loc_qua_han`: bỏ việc đã hoàn thành và việc chưa có hạn. Xoá build/ cũ; 43/43 test đạt.")
        else:
            yield Line(sj.tool_result(tid, "Người dùng từ chối lệnh này.", is_error=True))
            yield from tool_call(
                sj, "Bash", {"command": "uv run pytest -q -p no:cacheprovider --import-mode=importlib"},
                "43 passed in 3.4s", 2.0,
                text="Hiểu. Không xoá thư mục; chạy pytest với --import-mode=importlib để bỏ qua build/ cũ.",
            )
            yield from final_answer(sj, "Đã sửa `loc_qua_han`; không xoá gì, dùng import-mode=importlib — 43/43 test đạt.")
        yield from learning(
            a.id, "agent", "skill", "Xoá skill cũ `chay-test-legacy` (đã lỗi thời)",
            "- skills/chay-test-legacy/SKILL.md\n- # Chạy test bằng nose (cũ)\n(xoá skill → luôn cần người duyệt)",
            auto=False,
        )
        yield Hub("agent.status", {"status": "idle"})
        yield Live()
        yield Sleep(30)


def life_reviewer(a: AgentState) -> Script:
    while True:
        sj = StreamJson(a.model, "Rà soát PR #42: thêm bộ lọc nhiệm vụ theo nhóm", a.mcp_servers)
        yield Begin(sj)
        yield Line(sj.init())
        yield from tool_call(sj, "Bash", {"command": "git diff main...pr-42 --stat"}, "src/loc_nhiem_vu.py | 24 ++++--\ntests/test_loc.py | 31 +++++", 0.6)
        yield from tool_call(sj, "Read", {"file_path": "tests/test_loc.py"}, "def test_loc_theo_nhom(): …", 0.3)
        yield from final_answer(
            sj,
            "PR #42 ổn: logic lọc theo nhóm đúng, có test. Góp ý nhỏ: thiếu test cho nhóm rỗng.",
            thinking="Kiểm tra diff và test; rủi ro chính là nhóm rỗng trả về toàn bộ danh sách.",
        )
        yield from learning(
            a.id, "agent", "memory", "Nhắc kiểm tra trường hợp nhóm rỗng khi review bộ lọc",
            "+ Khi review bộ lọc danh sách: luôn hỏi 'đầu vào rỗng thì sao?'", auto=True,
        )
        yield Hub("agent.status", {"status": "idle"})
        yield Live()
        yield Sleep(45)


def life_ops(a: AgentState) -> Script:
    attempt = 0
    while True:
        attempt += 1
        sj = StreamJson(a.model, "Kiểm tra sức khoẻ các cổng MCP nội bộ", a.mcp_servers)
        yield Begin(sj)
        yield Line(sj.init())
        yield from tool_call(sj, "mcp__cds-100-ngay__boi_canh", {}, "OK · 112ms", 0.5)
        if attempt == 1:
            err = "MCP error -32000: connect ECONNREFUSED 10.0.4.12:8443"
            yield from tool_call(sj, "mcp__theo-doi-cchc__boi_canh", {}, err, 1.5, is_error=True)
            yield from tool_call(sj, "mcp__theo-doi-cchc__boi_canh", {}, err, 1.5, is_error=True, text="Thử lại một lần.")
            yield Line(sj.msg_start())
            yield from write_text(sj, "Không kết nối được cổng theo-doi-cchc sau 2 lần thử. Dừng và báo lỗi.")
            yield Line(sj.msg_delta(40, "end_turn"))
            yield Line(sj.result("Không kết nối được MCP theo-doi-cchc (ECONNREFUSED) sau 2 lần thử",
                                 is_error=True, subtype="error_during_execution", duration_ms=5200))
            yield Live()
            yield Sleep(60)
        else:
            yield from tool_call(sj, "mcp__theo-doi-cchc__boi_canh", {}, "OK · 240ms", 0.7)
            yield from final_answer(sj, "Tất cả cổng MCP hoạt động bình thường.")
            yield Hub("agent.status", {"status": "idle"})
            yield Live()
            yield Sleep(60)


def life_new_agent(a: AgentState) -> Script:
    sj = StreamJson(a.model, "Khởi tạo: nạp persona và kiểm tra MCP", a.mcp_servers)
    yield Begin(sj)
    yield Line(sj.init())
    yield from final_answer(
        sj,
        f"Xin chào, tôi là **{a.name}** ({a.role or 'chưa có vai trò'}) thuộc team {a.team}. "
        f"MCP: {', '.join(a.mcp_servers) or 'không có'}. Sẵn sàng nhận việc.",
    )
    yield Hub("agent.status", {"status": "idle"})
    yield Live()


def steer_ack(sj: StreamJson, text: str) -> Script:
    yield Line(sj.msg_start())
    yield from write_text(sj, f"Đã nhận chỉ đạo: “{text}”. Tôi sẽ điều chỉnh ở bước tiếp theo.")
    yield Line(sj.msg_delta(30, "end_turn"))


LEARNING_POOL = [
    ("agent", "memory", "Ghi nhớ: báo cáo dùng định dạng ngày dd/mm/yyyy",
     "+ Ngày tháng trong báo cáo: dd/mm/yyyy (vd 22/10/2026)."),
    ("team", "memory", "Team: mẫu số mọi tỷ lệ là nhiệm vụ cấp phường",
     "+ Mọi tỷ lệ % đợt 100 ngày: mẫu số = NhiemVuPhuong (cấp 2)."),
    ("agent", "skill", "Cập nhật skill tra-cuu-van-ban: thêm bước kiểm tra pham_vi",
     "  ## Bước 3\n- Trả kết quả\n+ Đọc trường pham_vi trước khi kết luận 'không có'\n+ Trả kết quả kèm phạm vi"),
    ("team", "skill", "Skill team: kiem-tra-mcp (ping boi_canh từng cổng)",
     "+ skills/kiem-tra-mcp/SKILL.md\n+ Gọi boi_canh của từng MCP; >2s hoặc lỗi → báo ops"),
]


def _no_more() -> Script:
    return
    yield  # pragma: no cover


# ============================================================================ runtime 1 agent
class SimAgent:
    def __init__(self, hub: "FakeHub", state: AgentState, life: Callable[[AgentState], Script], started_ago: float) -> None:
        self.hub = hub
        self.state = state
        self.gen = life(state)
        self.vclock = 0.0
        self.base = time.time() - started_ago
        self.adapter: ClaudeStreamAdapter | None = None
        self.sj: StreamJson | None = None
        self._send: Any = None
        self.pending: AwaitApproval | None = None
        self.decision: asyncio.Future | None = None
        self.steers: list[str] = []
        self.task: asyncio.Task | None = None
        self.finished = False

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.base + self.vclock, timezone.utc)

    def _next(self) -> Step | None:
        try:
            v, self._send = self._send, None
            return self.gen.send(v)
        except StopIteration:
            self.finished = True
            return None

    def emit(self, step: Step) -> None:
        aid = self.state.id
        if isinstance(step, Begin):
            self.sj = step.sj
            self.adapter = ClaudeStreamAdapter(
                aid, run_id=f"run-{uuid.uuid4().hex[:6]}", clock=lambda: self.vclock, now=self.now, task=step.sj.task
            )
        elif isinstance(step, Line):
            if self.adapter is None:
                return
            self.hub.bus.publish_many(self.adapter.feed(step.obj))
        elif isinstance(step, Hub):
            self.hub.bus.publish(
                make_event(step.type, aid, step.payload,  # type: ignore[arg-type]
                           session_id=self.sj.session_id if self.sj else None,
                           run_id=self.adapter.run_id if self.adapter else None, ts=self.now())
            )

    # -- chạy đồng bộ (lịch sử lúc khởi động / chế độ không realtime)
    def drive_sync(self, max_steps: int = 5000) -> None:
        for _ in range(max_steps):
            if self.pending is not None:
                return
            if self.steers:
                self._ack_steers_sync()
            step = self._next()
            if step is None or isinstance(step, Live):
                return
            if isinstance(step, Sleep):
                self.vclock += step.s
            elif isinstance(step, AwaitApproval):
                self.pending = step
                return
            else:
                self.emit(step)

    def _steer_steps(self) -> list[Step]:
        steps: list[Step] = []
        while self.steers:
            text = self.steers.pop(0)
            if self.sj is not None and not self.sj.done:
                steps.extend(steer_ack(self.sj, text))
            else:  # đang rảnh → mở một lượt ngắn (resume phiên) để phản hồi
                sj = StreamJson(self.state.model, f"Phản hồi chỉ đạo: {text[:40]}", self.state.mcp_servers)
                steps.append(Begin(sj))
                steps.append(Line(sj.init()))
                steps.extend(steer_ack(sj, text))
                steps.append(Line(sj.result("Đã nhận chỉ đạo")))
                steps.append(Hub("agent.status", {"status": "idle"}))
        return steps

    def _ack_steers_sync(self) -> None:
        for s in self._steer_steps():
            if isinstance(s, Sleep):
                self.vclock += s.s
            elif not isinstance(s, (Live, AwaitApproval)):
                self.emit(s)

    # -- chạy realtime
    async def run_live(self, speed: float) -> None:
        self.base = time.time() - self.vclock
        while not self.finished:
            if self.pending is not None:
                if self.decision is None:
                    self.decision = asyncio.get_running_loop().create_future()
                self._send = await self.decision
                self.pending, self.decision = None, None
            if self.steers:
                for s in self._steer_steps():
                    await self._live_step(s, speed)
            step = self._next()
            if step is None:
                break
            await self._live_step(step, speed)

    async def _live_step(self, step: Step, speed: float) -> None:
        if isinstance(step, Sleep):
            self.vclock += step.s
            await asyncio.sleep(step.s * speed)
        elif isinstance(step, AwaitApproval):
            self.pending = step
        elif isinstance(step, Live):
            return
        else:
            self.emit(step)


# ============================================================================ hub giả
@dataclass
class AgentSpec:
    id: str
    name: str
    team: str
    role: str
    model: str
    mcp: list[str]
    life: Callable[[AgentState], Script]
    started_ago: float
    base_tokens: int = 0
    base_cost: float = 0.0


DEFAULT_AGENTS = [
    AgentSpec("a1", "researcher", "Nghiên cứu", "Tra cứu văn bản, tổng hợp nguồn", "sonnet",
              ["hthong-tlkc-don-vi", "tools-cchc"], life_researcher, 250, 184_000, 0.52),
    AgentSpec("a2", "writer", "Nghiên cứu", "Soạn báo cáo, văn bản", "opus",
              ["cds-100-ngay"], life_writer, 430, 96_000, 0.71),
    AgentSpec("a3", "coder", "Vận hành", "Sửa lỗi, viết code cho hệ nội bộ", "sonnet",
              ["viec-nhom-2", "serena"], life_coder, 560, 88_000, 0.20),
    AgentSpec("a4", "reviewer", "Vận hành", "Rà soát thay đổi, kiểm thử", "haiku",
              ["tools-cchc"], life_reviewer, 1500, 12_000, 0.03),
    AgentSpec("a5", "ops", "Vận hành", "Giám sát sức khoẻ hệ thống, MCP", "haiku",
              ["theo-doi-cchc", "cds-100-ngay"], life_ops, 130, 21_000, 0.02),
]


class FakeHub:
    def __init__(self, *, live: bool = True, speed: float = 1.0, learning_every: float = 30.0, seed: int = 7) -> None:
        self.bus = EventBus()
        self.agg = Aggregator()
        self.bus.add_listener(self.agg.apply)
        self.live = live
        self.speed = speed
        self.learning_every = learning_every
        self.rng = random.Random(seed)
        self.agents: dict[str, SimAgent] = {}
        self._ids = itertools.count(len(DEFAULT_AGENTS) + 1)
        self._bg: list[asyncio.Task] = []
        self.started = False
        for spec in DEFAULT_AGENTS:
            self._add(spec)
        # chạy sẵn phần "lịch sử" của mọi agent
        for sa in self.agents.values():
            sa.drive_sync()
        # vài đề xuất học có sẵn từ trước
        for aid, idx in (("a1", 2), ("a2", 1), ("a5", 3)):
            self.propose(aid, *LEARNING_POOL[idx])
        # từ đây đồng hồ ảo của mỗi agent khớp giờ thật
        for sa in self.agents.values():
            sa.base = time.time() - sa.vclock

    def _add(self, spec: AgentSpec) -> SimAgent:
        st = AgentState(
            id=spec.id, name=spec.name, team=spec.team, role=spec.role, model=spec.model,
            mcp_servers=list(spec.mcp), base_tokens=spec.base_tokens, base_cost=spec.base_cost,
        )
        self.agg.register(st)
        sa = SimAgent(self, st, spec.life, spec.started_ago)
        self.agents[spec.id] = sa
        return sa

    # -- vòng đời
    async def start(self) -> None:
        if self.started or not self.live:
            return
        self.started = True
        for sa in self.agents.values():
            self._spawn(sa)
        self._bg.append(asyncio.create_task(self._learning_ticker()))

    def _spawn(self, sa: SimAgent) -> None:
        if self.live and self.started and (sa.task is None or sa.task.done()):
            sa.task = asyncio.create_task(sa.run_live(self.speed))

    async def shutdown(self) -> None:
        tasks = [sa.task for sa in self.agents.values() if sa.task] + self._bg
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _learning_ticker(self) -> None:
        while True:
            await asyncio.sleep(self.learning_every * self.speed)
            if not self.agents:
                continue
            aid = self.rng.choice(list(self.agents))
            scope, kind, title, diff = self.rng.choice(LEARNING_POOL)
            self.propose(aid, scope, kind, title, diff)

    # -- learnings
    def propose(self, agent_id: str, scope: str, kind: str, title: str, diff: str) -> str:
        """Chính sách (BRIEF quyết định 2): chỉ thay đổi tầng AGENT, không phải xoá skill → tự áp dụng."""
        auto = scope == "agent" and not diff.lstrip().startswith("- skills/")
        pid = f"L{uuid.uuid4().hex[:5]}"
        self.bus.publish(make_event("learning.proposal", agent_id, {
            "proposal_id": pid, "scope": scope, "kind": kind, "title": title, "diff": diff, "auto": auto}))
        if auto:
            self.bus.publish(make_event("learning.applied", agent_id, {
                "proposal_id": pid, "action": "applied", "by": "auto (eval 12/12 đạt)"}))
        return pid

    def _learning_action(self, pid: str, action: str, allowed_from: set[str]) -> bool:
        p = self.agg.proposals.get(pid)
        if p is None or p.status not in allowed_from:
            return False
        self.bus.publish(make_event("learning.applied", p.agent_id, {"proposal_id": pid, "action": action, "by": "người dùng"}))
        return True

    def approve_learning(self, pid: str) -> bool:
        return self._learning_action(pid, "applied", {"pending", "rejected"})

    def reject_learning(self, pid: str) -> bool:
        return self._learning_action(pid, "rejected", {"pending"})

    def rollback_learning(self, pid: str) -> bool:
        return self._learning_action(pid, "rolled_back", {"applied"})

    # -- điều khiển agent
    def _decide(self, agent_id: str, decision: str) -> bool:
        sa = self.agents.get(agent_id)
        if sa is None or sa.pending is None:
            return False
        rid = sa.pending.request_id
        self.bus.publish(make_event("request.cancel", agent_id,
                                    {"request_id": rid, "decision": decision, "by": "người dùng"}, ts=sa.now()))
        if sa.decision is not None and not sa.decision.done():
            sa.decision.set_result(decision)
        else:
            sa._send = decision
            sa.pending = None
            if not (self.live and self.started):
                sa.drive_sync()
        return True

    def approve(self, agent_id: str) -> bool:
        return self._decide(agent_id, "approved")

    def deny(self, agent_id: str) -> bool:
        return self._decide(agent_id, "denied")

    def steer(self, agent_id: str, text: str) -> bool:
        sa = self.agents.get(agent_id)
        if sa is None or not text.strip():
            return False
        self.bus.publish(make_event("user.message", agent_id, {"text": text, "by": "người dùng"}, ts=sa.now()))
        sa.steers.append(text)
        if self.live and self.started:
            if sa.task is None or sa.task.done():  # agent đã dừng → chạy lượt phản hồi
                sa.finished = False
                sa.gen = _no_more()
                self._spawn(sa)
        else:
            sa._ack_steers_sync()
        return True

    def stop(self, agent_id: str) -> bool:
        sa = self.agents.get(agent_id)
        if sa is None:
            return False
        if sa.task and not sa.task.done():
            sa.task.cancel()
        st = sa.state
        if sa.pending is not None:
            self.bus.publish(make_event("request.cancel", agent_id, {
                "request_id": sa.pending.request_id, "decision": "cancelled", "by": "dừng agent"}))
            sa.pending = None
        if sa.sj is not None and not sa.sj.done:
            sa.sj.done = True
            self.bus.publish(make_event("turn.complete", agent_id, {
                "status": "interrupted", "cost_usd": st.run_cost, "total_tokens": st.run_tokens}))
        self.bus.publish(make_event("agent.status", agent_id, {"status": "stopped", "detail": "dừng bởi người dùng"}))
        sa.finished = True
        return True

    # -- CRUD agent (chỉ dữ liệu giả)
    def create_agent(self, name: str, role: str, team: str, model: str, mcp: list[str]) -> str:
        aid = f"a{next(self._ids)}"
        while aid in self.agents:
            aid = f"a{next(self._ids)}"
        sa = self._add(AgentSpec(aid, name, team, role, model, list(mcp), life_new_agent, 0))
        self.bus.publish(make_event("agent.status", aid, {"status": "idle", "detail": "vừa tạo"}))
        if self.live and self.started:
            self._spawn(sa)
        else:
            sa.drive_sync()
        return aid

    def update_agent(self, agent_id: str, **fields: Any) -> bool:
        st = self.agg.agents.get(agent_id)
        if st is None:
            return False
        for k in ("name", "role", "team", "model"):
            if fields.get(k):
                setattr(st, k, fields[k])
        if "mcp" in fields and fields["mcp"] is not None:
            st.mcp_servers = list(fields["mcp"])
        return True

    def delete_agent(self, agent_id: str) -> bool:
        sa = self.agents.pop(agent_id, None)
        if sa is None:
            return False
        if sa.task and not sa.task.done():
            sa.task.cancel()
        self.agg.unregister(agent_id)
        self.bus.forget(agent_id)
        return True
