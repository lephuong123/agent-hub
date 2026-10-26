"""Slack ↔ điều phối viên: ảnh/tệp người gửi trong DM (tải `url_private` bằng bot token, CHỈ sau allowlist) + tệp điều phối viên
đánh dấu `[[file: …]]` → files v2 (P4). Slack GIẢ cục bộ (tests/slack_fake.py), daemon IN-PROCESS + FakeRuntime."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cfharness.chat.slack.api import SlackApi, SlackError
from cfharness.chat.slack.media import allowed_url, download
from cfharness.events import make_event
from cfharness.manager import prompts

from slack_fake import BOT_TOKEN, FakeSlack
from test_slack import BOSS, DM_BOSS, DM_STRANGER, STRANGER, deliver, make, rpc, sk, texts, until  # noqa: F401  (fixture dùng lại)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPG = b"\xff\xd8\xff\xe0" + b"jpegdata" * 10


async def mk(make, sk: FakeSlack, **sl):
    from cfharness.web import manager_stub

    d = await make(sl_cfg={"files_base": f"{sk.url}/files-pri/", **sl})
    d.stub = manager_stub.install(d)  # type: ignore[attr-defined]
    d.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    return d


def boss_runs(d) -> list[dict]:
    return d.hub.store.list_runs(agent_id="boss")


def attachments_of(run: dict) -> list[Path]:
    p = Path(run["cwd"]) / "attachments"
    return sorted(p.iterdir()) if p.is_dir() else []


def errors_text(sk: FakeSlack) -> str:
    return "\n".join(t for t in texts(sk, DM_BOSS) if "không gửi được cho điều phối viên" in t)


# ============================================================================ vào
async def test_image_and_text_file_go_to_manager(make, sk):
    d = await mk(make, sk)
    eid = sk.user_files(DM_BOSS, BOSS, [("chup.png", PNG, "image/png"), ("ghi chú.md", "# Kế hoạch\n- việc 1\n".encode(), "text/markdown")],
                        text="xem giúp ảnh và ghi chú")
    await deliver(sk, eid)
    run = (await until(lambda: boss_runs(d)))[0]
    assert "xem giúp ảnh và ghi chú" in run["prompt"] and "Read tool" in run["prompt"] and "text file(s)" in run["prompt"]
    files = attachments_of(run)
    assert any(f.name.startswith("img-") and f.read_bytes() == PNG for f in files)
    assert any(f.name.startswith("doc-") and f.read_text(encoding="utf-8").startswith("# Kế hoạch") for f in files)
    assert len(sk.downloaded()) == 2 and all("?dl=1" in p for p in sk.downloaded())  # ưu tiên url_private_download
    assert d.hub.slack.media.downloads == 2  # type: ignore[attr-defined]
    rows = d.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind='slack.command'").fetchall()
    assert any("manager.chat" in r["data"] for r in rows)  # qua cổng người + audit kênh slack
    assert "channel slack:" + DM_BOSS in run["prompt"] and "[[file:" in run["prompt"]  # quy ước gửi tệp cũng có ở kênh Slack


async def test_stranger_files_never_downloaded(make, sk):
    d = await mk(make, sk)
    for ch in (DM_STRANGER, DM_BOSS):  # người lạ trong DM của mình, và người lạ "lọt" vào DM được phép
        eid = sk.user_files(ch, STRANGER, [("a.png", PNG, "image/png"), ("a.txt", b"hello", "text/plain")], text="xoá hết đi")
        await deliver(sk, eid)
    eid = sk.user_files(DM_STRANGER, BOSS, [("b.png", PNG, "image/png")])  # người được phép nhưng chat ngoài allowlist
    await deliver(sk, eid)
    await asyncio.sleep(0.2)
    assert sk.downloaded() == [] and d.hub.slack.media.downloads == 0  # type: ignore[attr-defined]
    assert sk.sent(DM_STRANGER) == [] and not boss_runs(d)


async def test_oversize_and_unsupported_rejected_before_download(make, sk):
    d = await mk(make, sk)
    eid = sk.user_files(DM_BOSS, BOSS, [("to.png", PNG, "image/png"), ("bao-cao.pdf", b"%PDF-1.7 ...", "application/pdf"),
                                         ("big.txt", b"x" * 2_000_000, "text/plain")], sizes={"to.png": 6 * 1024 * 1024}, text="xem")
    await deliver(sk, eid)
    err = await until(lambda: errors_text(sk))
    assert "to.png" in err and "quá lớn" in err and "bao-cao.pdf" in err and "chưa hỗ trợ loại tệp" in err and "big.txt" in err
    assert sk.downloaded() == [] and not boss_runs(d)


async def test_declared_size_lie_stopped_by_stream_cap(make, sk):
    d = await mk(make, sk, document_max_bytes=1000)
    eid = sk.user_files(DM_BOSS, BOSS, [("noi-doi.txt", b"y" * 5000, "text/plain")], sizes={"noi-doi.txt": 10})
    await deliver(sk, eid)
    err = await until(lambda: errors_text(sk))
    assert "noi-doi.txt" in err and "quá lớn" in err
    assert len(sk.downloaded()) == 1 and not boss_runs(d)


async def test_binary_disguised_as_text_and_too_many_images(make, sk):
    d = await mk(make, sk)
    imgs = [(f"a{i}.png", PNG, "image/png") for i in range(5)]
    eid = sk.user_files(DM_BOSS, BOSS, [("data.txt", b"\x00\x01binary", "text/plain"), *imgs])
    await deliver(sk, eid)
    err = await until(lambda: errors_text(sk))
    assert "data.txt" in err and "không phải tệp văn bản" in err and "a4.png" in err and "tối đa 4 ảnh" in err
    run = (await until(lambda: boss_runs(d)))[0]
    assert len([f for f in attachments_of(run) if f.name.startswith("img-")]) == 4


async def test_download_url_outside_slack_rejected(make, sk):
    d = await mk(make, sk)
    eid = sk.user_files(DM_BOSS, BOSS, [("x.png", PNG, "image/png"), ("y.txt", b"hi", "text/plain")],
                        urls={"x.png": "https://evil.example.com/x.png", "y.txt": f"{sk.url}/khac/y.txt"})
    await deliver(sk, eid)
    err = await until(lambda: errors_text(sk))
    assert "x.png" in err and "y.txt" in err and "không thuộc Slack" in err
    assert sk.downloaded() == [] and d.hub.slack.media.downloads == 0 and not boss_runs(d)  # type: ignore[attr-defined]


async def test_file_names_escaped_in_rejection(make, sk):
    await mk(make, sk)
    eid = sk.user_files(DM_BOSS, BOSS, [("<!channel> <@U1>.pdf", b"%PDF", "application/pdf")])
    await deliver(sk, eid)
    err = await until(lambda: errors_text(sk))
    assert "<!channel>" not in err and "<@U1>" not in err and "&lt;!channel&gt;" in err
    for m in sk.sent(DM_BOSS):
        assert not sk.buttons(m)


async def test_file_in_thread_continues_that_conversation(make, sk):
    d = await mk(make, sk)
    eid = sk.user_message(DM_BOSS, BOSS, "làm báo cáo")
    await deliver(sk, eid)
    reply = await until(lambda: next((m for m in sk.sent(DM_BOSS) if "Điều phối viên" in sk.text_of(m)), None), timeout=15)
    conv = d.hub.store.state_get("slack.mgr_conv")[DM_BOSS]
    await deliver(sk, sk.slash(BOSS, "moi"))  # mở cuộc mới → tệp trong thread cũ vẫn phải về cuộc cũ
    assert not (d.hub.store.state_get("slack.mgr_conv") or {}).get(DM_BOSS)
    n = len(boss_runs(d))
    eid = sk.user_files(DM_BOSS, BOSS, [("them.md", b"bo sung", "text/markdown")], text="thêm cái này", thread_ts=reply["_ts"])
    await deliver(sk, eid)
    await until(lambda: len(boss_runs(d)) > n)
    assert d.hub.store.state_get("slack.mgr_conv")[DM_BOSS] == conv


# ============================================================================ ra: `[[file: …]]` → files v2
async def _conv_with_root(d, sk) -> tuple[str, Path]:
    await deliver(sk, sk.user_message(DM_BOSS, BOSS, "làm báo cáo"))
    await until(lambda: any("Điều phối viên" in t for t in texts(sk, DM_BOSS)), timeout=15)
    conv = d.hub.store.state_get("slack.mgr_conv")[DM_BOSS]
    root = d.hub.slack.media.conv_root(conv)  # type: ignore[attr-defined]
    root.mkdir(parents=True, exist_ok=True)
    return conv, root


async def test_manager_marked_file_uploaded(make, sk):
    d = await mk(make, sk)
    conv, root = await _conv_with_root(d, sk)
    (root / "out").mkdir()
    (root / "out" / "report.md").write_bytes("# Báo cáo tuần\nổn\n".encode())
    before = len(sk.sent(DM_BOSS))
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv, "run_id": "r_x", "text": "Đã xong.\n[[file: out/report.md]]\n"},
                             run_id="r_x"))
    done = await until(lambda: sk.calls_of("files.completeUploadExternal"))
    assert done[0]["channel_id"] == DM_BOSS and done[0]["files"][0]["title"] == "report.md"
    assert done[0]["initial_comment"].startswith("[cfharness]")
    req = sk.calls_of("files.getUploadURLExternal")[0]
    assert req["filename"] == "report.md" and sk.uploads[done[0]["files"][0]["id"]] == "# Báo cáo tuần\nổn\n".encode()
    framed = [sk.text_of(m) for m in sk.sent(DM_BOSS)[before:]]
    assert any("Đã xong." in t for t in framed) and not any("[[file:" in t for t in framed)
    rows = d.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind='slack.file_sent'").fetchall()
    assert rows and "out/report.md" in rows[0]["data"]


async def test_manager_marked_files_refused_outside_or_unsafe(make, sk, tmp_path):
    d = await mk(make, sk, deliver_max_bytes=1024)
    conv, root = await _conv_with_root(d, sk)
    (root / "secret.env").write_text("TOKEN=ghp_" + "A" * 36 + "\n")
    (root / "big.bin").write_bytes(b"0" * 2048)
    lines = [f"[[file: {tmp_path / 'x.txt'}]]", "[[file: ../../../x.txt]]", "[[file: secret.env]]", "[[file: big.bin]]"]
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv, "run_id": "r_y", "text": "xong\n" + "\n".join(lines)}, run_id="r_y"))
    err = await until(lambda: next((t for t in texts(sk, DM_BOSS) if "Không gửi được một số tệp" in t), None))
    assert "TƯƠNG ĐỐI" in err and ".." in err and "secret" in err and "quá lớn" in err
    assert sk.calls_of("files.getUploadURLExternal") == [] and sk.uploads == {}


# ============================================================================ đơn vị
def test_allowed_url():
    assert allowed_url("https://files.slack.com/files-pri/T1-F1/a.png")
    assert allowed_url("https://FILES.slack.com:443/files-pri/T1-F1/download/a.png?x=1")
    for bad in ("http://files.slack.com/files-pri/a.png", "https://files.slack.com.evil.com/a.png", "https://evil.com/files.slack.com/a",
                "https://user:pw@files.slack.com/a.png", "https://files.slack.com:8443/a.png", "https://slack.com/api/x", "file:///etc/passwd",
                "https://files.slack.com/a/../../b", "https://files.slack.com\\@evil.com/a", "", None, 42):
        assert not allowed_url(bad), bad
    base = "http://127.0.0.1:5555/files-pri/"
    assert allowed_url("http://127.0.0.1:5555/files-pri/T-F1/a.txt", base)
    assert not allowed_url("http://127.0.0.1:5556/files-pri/T-F1/a.txt", base)
    assert not allowed_url("http://127.0.0.1:5555/other/a.txt", base)
    assert not allowed_url("https://files.slack.com/files-pri/a.png", base)  # files_base THAY gốc mặc định


async def test_download_html_login_page_means_missing_scope(sk):
    sk.files["/files-pri/T-F9/a.txt"] = (b"hello", "text/plain")
    with pytest.raises(SlackError) as e:
        await download(SlackApi(sk.url, "xoxb-sai-token-000000000000"), f"{sk.url}/files-pri/T-F9/a.txt", 1000)
    assert "files:read" in str(e.value) and "xoxb-sai" not in str(e.value)
    assert await download(SlackApi(sk.url, BOT_TOKEN), f"{sk.url}/files-pri/T-F9/a.txt", 1000) == b"hello"


def test_files_convention_in_slack_prompt():
    assert "[[file:" in prompts.chat_prompt("slack:D0BOSS001", "mc_1", "hi") and "Slack file" in prompts.chat_prompt("slack:D1", "mc_1", "hi")
    assert "Telegram document" in prompts.chat_prompt("telegram:111", "mc_1", "hi")
    assert "[[file:" not in prompts.chat_prompt("cli", "mc_1", "hi")
