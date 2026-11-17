"""Cổng cfharness — thư mục chung Không gian số KHÔNG qua đồng bộ (`filehub.mode = lane`), docs/dev-notes/plan-filehub-lane.md.

| Tệp | Vai trò |
|---|---|
| `client.py` | `LaneClient` HTTP+SSE theo hợp đồng §C (`lane/v1`), `LaneError` (mã C.7), `norm_rel` (C.4 phía hub), `check_url` (chỉ loopback) |
| `service.py` | `LaneService` (`hub.filehub.lane`): thiết lập khoá (secret store), trạng thái, luồng sự kiện + cursor, steer, vòng đời cây theo run |
| `workdir.py` | `Workdir`: cây làm việc theo run — dựng, quét/đẩy (quét secret TRƯỚC khi đẩy), kéo thay đổi của người, xung đột, flush, mồ côi |
| `tools.py` | `fsself.call` (tool hub-self `fs_*`) — ràng run, phạm vi workspace, chỉ đọc khi run chỉ đọc |
"""
