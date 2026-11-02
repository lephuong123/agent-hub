"""Kênh vector của graph memory (tuỳ chọn — extra `cfharness[vector]`).

- `Embedder` (giao diện): `model`, `version`, `dim`, `ready` (model đã nạp vào RAM), `embed(texts) -> list[list[float]]` (chuẩn hoá L2).
- `BgeM3Embedder`: **BAAI/bge-m3** dense 1024-d (CLS pooling + chuẩn hoá — đúng `1_Pooling/config.json` của repo HF) chạy bằng
  **fastembed** (onnxruntime, không cần torch → nhẹ, chạy được trên Windows). fastembed KHÔNG có bge-m3 trong danh sách dense dựng sẵn
  (đã kiểm `fastembed/text/onnx_embedding.py` 2026-11-02) → đăng ký bằng `TextEmbedding.add_custom_model` trỏ tới `onnx/model.onnx`
  (+ `onnx/model.onnx_data`) có sẵn trong repo `BAAI/bge-m3`. Import LƯỜI; lần `embed` đầu nạp (và có thể TẢI ~2.3 GB) model —
  chỉ xảy ra trong vòng nền `run_vector_loop`, không bao giờ trên đường chạy run (truy vấn chỉ dùng vector khi `ready`).
- sqlite-vec (`vec_distance_cosine`, nạp vào kết nối hub.db nếu có gói `sqlite-vec` và Python cho nạp extension) tính khoảng cách
  trong SQLite trên cột BLOB float32 của `mem_vectors` (cùng định dạng); không có → cosine bằng Python trên ≤ `vector.max_scan` vector.
- Không có embedder (chưa cài extra / `graph_memory.vector.enabled=false` / env `CFHARNESS_GRAPH_VECTOR=off`) → chỉ FTS5.

`mem_vectors` là chỉ mục DẪN XUẤT từ văn bản fact (không phải tri thức): vòng nền ghi thẳng bằng `GraphStore.put_vector`, không qua
ledger (rollback fact = `retracted_by`, đọc luôn lọc theo fact → vector mồ côi vô hại). Đổi model/phiên bản → `needs_embedding` trả lại
mọi fact → chỉ mục lại.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sqlite3
import struct
from typing import Any, Protocol, Sequence

log = logging.getLogger("cfharness.graph.vector")

BGE_M3 = "BAAI/bge-m3"
BGE_M3_DIM = 1024
ENV_SWITCH = "CFHARNESS_GRAPH_VECTOR"  # "off" → tắt hẳn kênh vector (test đặt biến này để KHÔNG BAO GIỜ tải model)


class Embedder(Protocol):
    model: str
    version: str
    dim: int

    @property
    def ready(self) -> bool: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def pack(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes, dim: int | None = None) -> list[float]:
    n = dim if dim is not None else len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def normalize(v: Sequence[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class BgeM3Embedder:
    """bge-m3 dense qua fastembed (ONNX). `cache_dir` mặc định `HUB_HOME/models` (không rải vào thư mục người dùng)."""

    model = BGE_M3
    dim = BGE_M3_DIM

    def __init__(self, cache_dir: str | None = None, threads: int | None = None, max_length: int = 512) -> None:
        import fastembed  # noqa: F401 — kiểm có extra; ImportError → get_embedder trả None

        self.version = f"onnx-cls-l2/fastembed-{getattr(fastembed, '__version__', '?')}"
        self.cache_dir = cache_dir
        self.threads = threads
        self.max_length = max_length
        self._m: Any = None

    @property
    def ready(self) -> bool:
        return self._m is not None

    def _load(self) -> Any:
        if self._m is None:
            import inspect

            from fastembed import TextEmbedding
            from fastembed.common.model_description import ModelSource, PoolingType

            known = {str(d.get("model") if isinstance(d, dict) else getattr(d, "model", "")).lower() for d in TextEmbedding.list_supported_models()}
            if BGE_M3.lower() not in known:
                kw: dict[str, Any] = {"model": BGE_M3, "pooling": PoolingType.CLS, "normalization": True, "sources": ModelSource(hf=BGE_M3),
                                      "dim": BGE_M3_DIM, "model_file": "onnx/model.onnx"}
                if "additional_files" in inspect.signature(TextEmbedding.add_custom_model).parameters:
                    kw["additional_files"] = ["onnx/model.onnx_data"]  # trọng số ngoài file (> 2 GB)
                TextEmbedding.add_custom_model(**kw)
            args: dict[str, Any] = {"model_name": BGE_M3}
            if self.cache_dir:
                args["cache_dir"] = self.cache_dir
            if self.threads:
                args["threads"] = self.threads
            self._m = TextEmbedding(**args)
        return self._m

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        m = self._load()
        return [normalize([float(x) for x in v]) for v in m.embed(list(texts), batch_size=16)]


def _cfg(hub: Any, key: str, default: Any) -> Any:
    try:
        v = hub.config.get(f"graph_memory.{key}")
    except Exception:
        return default
    return default if v is None else v


def get_embedder(hub: Any) -> Embedder | None:
    """Embedder của hub (dựng một lần, lười). Test gán `hub.graph_embedder = <giả>` trước. None = chỉ FTS5."""
    emb = getattr(hub, "graph_embedder", None)
    if emb is not None:
        return emb or None
    if str(os.environ.get(ENV_SWITCH, "")).lower() in ("off", "0", "false", "no"):
        return None
    mode = _cfg(hub, "vector.enabled", "auto")
    if mode is False or str(mode).lower() in ("false", "off", "0", "no"):
        return None
    if str(_cfg(hub, "vector.model", BGE_M3)) != BGE_M3:
        log.warning("graph_memory.vector.model=%s chưa hỗ trợ (chỉ %s) — tắt kênh vector", _cfg(hub, "vector.model", BGE_M3), BGE_M3)
        hub.graph_embedder = False
        return None
    try:
        cache = str(hub.paths.home / "models")
    except Exception:
        cache = None
    try:
        emb = BgeM3Embedder(cache_dir=cache, threads=_cfg(hub, "vector.threads", None))
    except ImportError:
        if str(mode).lower() == "true" or mode is True:
            log.warning("graph_memory.vector.enabled=true nhưng chưa cài extra: uv pip install 'cfharness[vector]' — chỉ FTS5")
        hub.graph_embedder = False
        return None
    hub.graph_embedder = emb
    return emb


# ============================================================================ sqlite-vec
_VEC_IMPORT_FAILED = False


def load_vec(conn: sqlite3.Connection) -> bool:
    """Nạp sqlite-vec vào `conn` (một lần; idempotent). False khi không có gói hoặc Python không cho nạp extension."""
    global _VEC_IMPORT_FAILED
    try:
        conn.execute("SELECT vec_version()").fetchone()
        return True
    except sqlite3.OperationalError:
        pass
    if _VEC_IMPORT_FAILED or str(os.environ.get(ENV_SWITCH, "")).lower() in ("off", "0", "false", "no"):
        return False
    try:
        import sqlite_vec  # type: ignore[import-not-found]
    except ImportError:
        _VEC_IMPORT_FAILED = True
        return False
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
        conn.execute("SELECT vec_version()").fetchone()
        return True
    except (AttributeError, sqlite3.Error) as e:  # Python build không có enable_load_extension / DLL hỏng
        log.info("không nạp được sqlite-vec (%s) — cosine bằng Python", e)
        _VEC_IMPORT_FAILED = True
        return False


# ============================================================================ backfill nền (như session_search.run_backfill)
def pending(hub: Any, emb: Embedder, batch: int) -> list[tuple[str, str]]:
    from .store import GraphStore

    ids = GraphStore(hub.store.conn).needs_embedding(emb.model, emb.version, limit=batch)
    if not ids:
        return []
    rows = hub.store.conn.execute(f"SELECT id, fact FROM mem_facts WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return [(r[0], r[1]) for r in rows]


def pending_entities(hub: Any, emb: Embedder, batch: int) -> list[tuple[str, str]]:
    """Thực thể còn dùng chưa có vector tên (model, phiên bản) — cho bộ khớp thực thể khi ghi (`entity_matcher`)."""
    return [(r[0], r[1]) for r in hub.store.conn.execute(
        "SELECT e.id, e.name FROM mem_entities e LEFT JOIN mem_vectors v ON v.item_kind='entity' AND v.item_id=e.id AND v.model=? AND v.model_version=? "
        "WHERE e.retracted_by IS NULL AND e.invalidated_at IS NULL AND v.item_id IS NULL ORDER BY e.rowid LIMIT ?", (emb.model, emb.version, int(batch)))]


def write_vectors(hub: Any, emb: Embedder, items: list[tuple[str, str]], vecs: list[list[float]], kind: str = "fact") -> int:
    from .store import GraphStore

    gs = GraphStore(hub.store.conn)
    n = 0
    with hub.store.tx():
        for (fid, _), v in zip(items, vecs):
            if len(v) != emb.dim:
                raise ValueError(f"embedder trả vector {len(v)} chiều, cần {emb.dim}")
            gs.put_vector(kind, fid, emb.model, emb.version, v)
            n += 1
    return n


def backfill_entities(hub: Any, emb: Embedder, batch: int = 64) -> int:
    total = 0
    while True:
        items = pending_entities(hub, emb, batch)
        if not items:
            return total
        total += write_vectors(hub, emb, items, emb.embed([t for _, t in items]), kind="entity")


def entity_matcher(hub: Any, threshold: float = 0.92, max_scan: int = 5000) -> Any:
    """Bộ khớp thực thể bằng embedding cho `resolve.set_embed_matcher` (PR write): tên mới ↔ vector tên thực thể đã có cùng tầng,
    cosine ≥ `threshold` → dùng thực thể đó (tên mới thành alias). CHỈ khi model đã nạp (`ready`) — không bao giờ chờ nạp model khi ghi."""
    from .store import owner_key

    def fn(store: Any, scope: str, owner: str, name: str) -> dict[str, Any] | None:
        if getattr(store, "conn", None) is not hub.store.conn:  # matcher là toàn cục — chỉ phục vụ DB của hub đã cắm nó
            return None
        emb = get_embedder(hub)
        if emb is None or not getattr(emb, "ready", False) or not str(name or "").strip():
            return None
        q = emb.embed([str(name)[:200]])[0]
        rows = store.conn.execute(
            "SELECT e.*, v.vec AS _v, v.dim AS _dim FROM mem_vectors v JOIN mem_entities e ON e.id=v.item_id WHERE v.item_kind='entity' AND v.model=? "
            "AND v.model_version=? AND e.scope=? AND e.owner_id=? AND e.retracted_by IS NULL AND e.invalidated_at IS NULL ORDER BY e.rowid DESC LIMIT ?",
            (emb.model, emb.version, scope, owner_key(scope, owner or None), int(max_scan))).fetchall()
        best, best_s = None, float(threshold)
        for r in rows:
            s = cosine(q, unpack(r["_v"], r["_dim"]))
            if s >= best_s:
                best, best_s = r, s
        return None if best is None else {k: best[k] for k in best.keys() if k not in ("_v", "_dim")}

    return fn


def backfill(hub: Any, emb: Embedder, batch: int = 64) -> int:
    """Đồng bộ (test/công cụ): chỉ mục mọi fact chưa có vector của (model, phiên bản)."""
    total = 0
    while True:
        items = pending(hub, emb, batch)
        if not items:
            return total
        total += write_vectors(hub, emb, items, emb.embed([t for _, t in items]))


async def run_vector_loop(hub: Any) -> None:
    """Vòng nền trong daemon: mỗi lô embed trong THREAD (CPU, có thể nạp/tải model lần đầu), ghi trong một giao dịch ngắn trên event
    loop; hết việc → ngủ `vector.interval_s` rồi quét fact mới (ghi graph qua ledger không phải chờ embedding)."""
    batch = int(_cfg(hub, "vector.batch", 32) or 32)
    interval = float(_cfg(hub, "vector.interval_s", 60) or 60)
    failures = 0
    while True:
        emb = get_embedder(hub)
        if emb is None:
            return
        try:
            items = pending(hub, emb, batch)
            kind = "fact"
            if not items:
                items, kind = pending_entities(hub, emb, batch), "entity"
            if items:
                vecs = await asyncio.to_thread(emb.embed, [t for _, t in items])
                write_vectors(hub, emb, items, vecs, kind=kind)
                failures = 0
                await asyncio.sleep(0.05)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            log.exception("embedding graph memory lỗi (lần %d) — thử lại sau", failures)
            if failures >= 5:
                log.warning("tắt kênh vector graph memory sau %d lỗi liên tiếp (chỉ FTS5 tới khi daemon khởi động lại)", failures)
                hub.graph_embedder = False
                return
            await asyncio.sleep(min(interval * failures, 600))
            continue
        await asyncio.sleep(interval)


def start_vector_loop(hub: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if get_embedder(hub) is None:
        return
    try:
        load_vec(hub.store.conn)
    except Exception:  # pragma: no cover
        pass
    from .resolve import set_embed_matcher  # bộ khớp thực thể khi GHI (PR write) — dùng vector tên do vòng nền này chỉ mục

    set_embed_matcher(entity_matcher(hub, float(_cfg(hub, "vector.entity_match", 0.92))))
    hub._bg.append(loop.create_task(run_vector_loop(hub)))
