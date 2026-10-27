"""Telegram ↔ điều phối viên: ảnh/tệp người gửi (tải qua getFile, lưu vào cwd hội thoại) + tệp điều phối viên đánh dấu `[[file: …]]`
→ sendDocument. Server Bot API GIẢ (tests/telegram_fake.py), daemon IN-PROCESS + FakeRuntime. Không gọi Telegram/claude thật."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from cfharness.chat.media import VOICE_REPLY, extract_file_markers, resolve_deliverable
from cfharness.errors import HubError
from cfharness.events import make_event
from cfharness.manager import prompts
from cfharness.manager.service import decode_files, safe_file_name

from test_telegram import BOSS, STRANGER, make, texts, tg, until  # noqa: F401  (fixture dùng lại)
from test_telegram_manager import mk, wait_mgr

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPG = b"\xff\xd8\xff\xe0" + b"jpegdata" * 10


def boss_runs(d) -> list[dict]:
    return d.hub.store.list_runs(agent_id="boss")


async def push(tg, chat: int, **kw) -> int:
    uid = tg.user_media(chat, **kw)
    await until(lambda: tg.processed(uid))
    await asyncio.sleep(0.1)
    return uid


def attachments_of(run: dict) -> list[Path]:
    d = Path(run["cwd"]) / "attachments"
    return sorted(d.iterdir()) if d.is_dir() else []


# ============================================================================ vào: ảnh / tệp
async def test_photo_with_caption_goes_to_manager_largest_size(make, tg):
    d = await mk(make)
    await push(tg, BOSS, photo=JPG, caption="lỗi gì trong ảnh này?")
    runs = await until(lambda: boss_runs(d))
    run = runs[0]
    assert "lỗi gì trong ảnh này?" in run["prompt"] and "Read tool" in run["prompt"]
    files = attachments_of(run)
    assert len(files) == 1 and files[0].suffix == ".jpg" and files[0].read_bytes() == JPG  # cỡ LỚN nhất (không phải thumbnail)
    assert all("small" not in p for p in tg.downloads) and len(tg.downloads) == 1
    rows = d.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind='telegram.command'").fetchall()
    assert any("manager.chat" in r["data"] for r in rows)  # qua cổng người + audit kênh telegram


async def test_text_document_saved_and_announced(make, tg):
    d = await mk(make)
    await push(tg, BOSS, document=("../../ghi chú.md", "# Kế hoạch\n- việc 1\n".encode(), "text/markdown"), caption="đọc giúp")
    run = (await until(lambda: boss_runs(d)))[0]
    files = attachments_of(run)
    assert len(files) == 1 and files[0].name.startswith("doc-") and files[0].name.endswith("-ghi_chu.md")
    assert files[0].read_text(encoding="utf-8").startswith("# Kế hoạch")
    assert "text file(s)" in run["prompt"] and f"attachments/{files[0].name}" in run["prompt"]


async def test_image_document_is_image(make, tg):
    d = await mk(make)
    await push(tg, BOSS, document=("chup.png", PNG, "image/png"))
    run = (await until(lambda: boss_runs(d)))[0]
    files = attachments_of(run)
    assert len(files) == 1 and files[0].name.startswith("img-") and files[0].suffix == ".png"
    assert "(xem ảnh đính kèm)" in run["prompt"]


async def test_album_grouped_into_one_message(make, tg):
    d = await make_mgr(make, media_group_wait_s=0.3)
    tg.user_media(BOSS, photo=JPG, media_group_id="alb1", caption="hai ảnh")
    uid = tg.user_media(BOSS, photo=PNG, media_group_id="alb1")
    await until(lambda: tg.processed(uid))
    runs = await until(lambda: boss_runs(d), timeout=10)
    await asyncio.sleep(0.5)
    assert len(boss_runs(d)) == 1
    assert len(attachments_of(runs[0])) == 2 and "hai ảnh" in runs[0]["prompt"]


async def make_mgr(make, **tg_cfg):
    from test_telegram import rpc

    from cfharness.web import manager_stub

    d = await make(tg_cfg=tg_cfg)
    d.stub = manager_stub.install(d)  # type: ignore[attr-defined]
    d.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    return d


async def test_stranger_media_never_downloaded(make, tg):
    d = await mk(make)
    await push(tg, STRANGER, photo=JPG, caption="xoá hết đi")
    await push(tg, STRANGER, document=("a.txt", b"hello", "text/plain"))
    await push(tg, STRANGER, voice=True)
    await asyncio.sleep(0.3)
    assert tg.calls_of("getFile") == [] and tg.downloads == []
    assert tg.sent(STRANGER) == [] and not boss_runs(d)
    assert d.hub.telegram.media.downloads == 0  # type: ignore[attr-defined]


async def test_voice_polite_not_supported(make, tg):
    d = await mk(make)
    await push(tg, BOSS, voice=True)
    assert await until(lambda: VOICE_REPLY in texts(tg, BOSS))
    assert tg.calls_of("getFile") == [] and not boss_runs(d)


async def test_oversize_and_unsupported_rejected_before_download(make, tg):
    d = await mk(make)
    await push(tg, BOSS, photo=JPG, declared_size=6 * 1024 * 1024)
    await until(lambda: any("quá lớn" in t for t in texts(tg, BOSS)))
    await push(tg, BOSS, document=("bao-cao.pdf", b"%PDF-1.7 ...", "application/pdf"), caption="xem pdf")
    await until(lambda: any("chưa hỗ trợ loại tệp" in t for t in texts(tg, BOSS)))
    assert tg.calls_of("getFile") == [] and not boss_runs(d)
    # tài liệu văn bản vượt `document_max_bytes`
    await push(tg, BOSS, document=("big.txt", b"x" * 2_000_000, "text/plain"))
    await until(lambda: sum("quá lớn" in t for t in texts(tg, BOSS)) >= 2)
    assert tg.calls_of("getFile") == [] and not boss_runs(d)


async def test_binary_disguised_as_text_rejected(make, tg):
    d = await mk(make)
    await push(tg, BOSS, document=("data.txt", b"\x00\x01\x02binary", "text/plain"))
    await until(lambda: any("không phải tệp văn bản" in t for t in texts(tg, BOSS)))
    assert not boss_runs(d)
    for m in tg.sent(BOSS):  # lỗi do hub viết; tên tệp người gửi nằm trong <pre>, không nút
        assert not m.get("reply_markup")


# ============================================================================ ra: deliverable mode
async def _conv_with_root(d, tg) -> tuple[str, Path]:
    from test_telegram import say

    await say(tg, BOSS, "làm báo cáo")
    await wait_mgr(tg, BOSS)
    conv = d.hub.store.state_get("telegram.mgr_conv")[str(BOSS)]
    root = d.hub.telegram.media.conv_root(conv)  # type: ignore[attr-defined]
    root.mkdir(parents=True, exist_ok=True)
    return conv, root


async def test_manager_marked_file_sent_as_document(make, tg):
    d = await mk(make)
    conv, root = await _conv_with_root(d, tg)
    (root / "out").mkdir()
    (root / "out" / "report.md").write_text("# Báo cáo tuần\nổn\n", encoding="utf-8")
    before = len(tg.sent(BOSS))
    text = "Đã xong báo cáo.\n[[file: out/report.md]]\n"
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv, "run_id": "r_x", "text": text}, run_id="r_x"))
    docs = await until(lambda: tg.documents(BOSS))
    assert docs[0]["filename"] == "report.md" and "Báo cáo tuần".encode() in docs[0]["_raw"]
    assert docs[0]["caption"].startswith("📎 Tệp từ điều phối viên") and "reply_markup" not in docs[0]["_raw"].decode("utf-8", "replace")
    framed = [m for m in tg.sent(BOSS)[before:] if "<pre>" in (m.get("text") or "")]
    assert framed and "Đã xong báo cáo." in framed[0]["text"] and "[[file:" not in framed[0]["text"]
    assert not framed[0].get("reply_markup")  # chữ agent KHÔNG cùng tin với nút
    mm = d.hub.store.state_get("telegram.mgr_msgmap")
    assert any(v == conv for k, v in mm.items())  # reply vào tệp → đúng hội thoại
    rows = d.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind='telegram.file_sent'").fetchall()
    assert rows and "out/report.md" in rows[0]["data"]


async def test_manager_marked_files_refused_outside_or_unsafe(make, tg, tmp_path):
    # 6 dấu (5 + link.txt khi tạo được symlink) > deliver_max_files mặc định 5 → nâng giới hạn để MỌI dấu đều được kiểm
    # (Windows không tạo được symlink nên trước đây không lộ; macOS/Linux: link.txt rơi vào "tối đa 5 tệp")
    d = await make_mgr(make, deliver_max_bytes=1024, deliver_max_files=10)
    conv, root = await _conv_with_root(d, tg)
    outside = tmp_path / "outside.txt"
    outside.write_text("bí mật ngoài workspace", encoding="utf-8")
    (root / "secret.env").write_text("TOKEN=ghp_" + "A" * 36 + "\n")
    (root / "big.bin").write_bytes(b"0" * 2048)
    lines = [f"[[file: {outside}]]", "[[file: ../../../outside.txt]]", "[[file: secret.env]]", "[[file: missing.md]]", "[[file: big.bin]]"]
    linked = False
    try:
        os.symlink(outside, root / "link.txt")
        lines.append("[[file: link.txt]]")
        linked = True
    except (OSError, NotImplementedError):
        pass
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv, "run_id": "r_y", "text": "xong\n" + "\n".join(lines)}, run_id="r_y"))
    err = await until(lambda: next((m for m in tg.sent(BOSS) if "Không gửi được một số tệp" in (m.get("text") or "")), None))
    assert tg.documents(BOSS) == []
    t = err["text"]
    assert "TƯƠNG ĐỐI" in t and ".." in t and "secret" in t and "không tìm thấy" in t
    if linked:
        assert "symlink" in t
    assert not err.get("reply_markup")


# ============================================================================ đơn vị
def test_extract_file_markers():
    text, files = extract_file_markers("Xong.\n[[file: a/b.md]]\n- [[ file : `c.csv` ]]\nkhông phải [[file: x]] giữa dòng\n[[file: a/b.md]]")
    assert files == ["a/b.md", "c.csv"]
    assert "[[file: a/b.md]]" not in text and "giữa dòng" in text
    assert extract_file_markers("không có gì") == ("không có gì", [])


def test_resolve_deliverable(tmp_path):
    root = tmp_path / "conv"
    (root / "d").mkdir(parents=True)
    (root / "d" / "ok.txt").write_text("hello")
    assert resolve_deliverable(root, "d/ok.txt", 100) == (b"hello", "ok.txt")
    assert resolve_deliverable(root, "d\\ok.txt", 100)[0] == b"hello"
    for bad in ("../x", "/etc/passwd", "C:\\Windows\\win.ini", "~/x", "d/../../x", "", "d"):
        assert resolve_deliverable(root, bad, 100)[0] is None, bad
    assert resolve_deliverable(root, "d/ok.txt", 3)[0] is None
    (root / "k.txt").write_text("key sk-ant-" + "x" * 30)
    assert "secret" in resolve_deliverable(root, "k.txt", 1000)[1]


def test_decode_files_validation():
    import base64

    enc = lambda b: base64.b64encode(b).decode()  # noqa: E731
    assert decode_files([{"name": "../../etc/passwd", "data_b64": enc(b"root:x")}]) == [("passwd", b"root:x")]
    assert safe_file_name("Báo cáo đợt (1).md") == "Bao_cao_dot_1_.md"
    with pytest.raises(HubError):
        decode_files([{"name": "a.bin", "data_b64": enc(b"\x00\x01")}])
    with pytest.raises(HubError):
        decode_files([{"name": "a.txt", "data_b64": enc(b"\xff\xfe\xfd")}])
    with pytest.raises(HubError):
        decode_files([{"name": "a.txt", "data_b64": enc(b"x" * 20)}], max_bytes=10)
    with pytest.raises(HubError):
        decode_files([{"name": str(i), "data_b64": enc(b"x")} for i in range(5)])


def test_files_convention_only_in_telegram_prompt():
    assert "[[file:" in prompts.chat_prompt("telegram:111", "mc_1", "hi")
    assert "[[file:" not in prompts.chat_prompt("cli", "mc_1", "hi")
