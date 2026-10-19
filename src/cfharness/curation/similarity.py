"""So khớp skill TẤT ĐỊNH (FR-LN-12 "độ trùng từ vựng trước, embedding sau") — hàm THUẦN, không I/O.

Điểm tương tự hai skill (0..1) = 0.25 · Jaccard token TÊN + 0.75 · cosine TF của token NỘI DUNG (mô tả + thân SKILL.md,
bỏ stopword, bỏ markdown). Gom cụm bằng union-find trên các cặp của HAI AGENT KHÁC NHAU có điểm ≥ ngưỡng; cụm hợp lệ khi
có ≥ `min_agents` agent khác nhau. Gộp tất định: chọn skill "gốc" (dùng nhiều nhất, rồi thân dài nhất), nối thêm các dòng
quy tắc riêng của skill còn lại vào mục "Additional rules" (bỏ dòng trùng sau chuẩn hoá) — không mất nội dung hấp thụ.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Any

STOPWORDS = frozenset(
    "a an the and or of to in on for with by from at as is are be been was were it its this that these those you your "
    "we our they their he she i me my do does did done not no yes if then else when while use using used via into than "
    "so such can could should would will may might must each every any all some one two per etc e g ie also only just "
    "md skill skills step steps".split()
)
_TOKEN = re.compile(r"[a-z0-9]+")
_MD = re.compile(r"`{1,3}|[#*_>|\[\]()]")


def tokens(text: str) -> list[str]:
    t = _MD.sub(" ", (text or "").lower())
    return [w for w in _TOKEN.findall(t) if w not in STOPWORDS and len(w) > 1]


def name_tokens(name: str) -> set[str]:
    return {w for w in re.split(r"[-_]+", (name or "").lower()) if w and w not in STOPWORDS}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def skill_similarity(a: dict[str, Any], b: dict[str, Any]) -> dict[str, float]:
    """a/b: {name, description, body}. Trả {score, name, content}."""
    nj = jaccard(name_tokens(a.get("name", "")), name_tokens(b.get("name", "")))
    ca = Counter(tokens(f"{a.get('description', '')} {a.get('body', '')}"))
    cb = Counter(tokens(f"{b.get('description', '')} {b.get('body', '')}"))
    cs = cosine(ca, cb)
    return {"score": round(0.25 * nj + 0.75 * cs, 4), "name": round(nj, 4), "content": round(cs, 4)}


def find_clusters(skills: list[dict[str, Any]], threshold: float, min_agents: int = 2) -> list[dict[str, Any]]:
    """skills: [{agent_id, name, description, body, use_count?}]. Cụm: {members, pairs, score(min cặp nối), agents}.
    Chỉ nối cặp của HAI agent khác nhau (trùng trong một agent là việc của curator pha 2)."""
    n = len(skills)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    pairs: list[tuple[int, int, dict[str, float]]] = []
    for i in range(n):
        for j in range(i + 1, n):
            if skills[i]["agent_id"] == skills[j]["agent_id"]:
                continue
            s = skill_similarity(skills[i], skills[j])
            if s["score"] >= threshold:
                pairs.append((i, j, s))
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out = []
    for idx in groups.values():
        if len(idx) < 2:
            continue
        agents = sorted({skills[i]["agent_id"] for i in idx})
        if len(agents) < min_agents:
            continue
        ps = [(skills[i], skills[j], s) for i, j, s in pairs if i in idx and j in idx]
        out.append({
            "members": [skills[i] for i in sorted(idx, key=lambda k: (skills[k]["agent_id"], skills[k]["name"]))],
            "agents": agents,
            "pairs": [{"a": f"{x['agent_id']}/{x['name']}", "b": f"{y['agent_id']}/{y['name']}", **s} for x, y, s in ps],
            "score": round(max(s["score"] for _, _, s in ps), 4),
        })
    out.sort(key=lambda c: -c["score"])
    return out


def cluster_key(team_id: str, members: list[dict[str, Any]]) -> str:
    """Dấu vân tay cụm (để không đề xuất lặp): team + tập (agent/skill) + băm nội dung."""
    h = hashlib.sha256()
    h.update(team_id.encode())
    for m in sorted(members, key=lambda x: (x["agent_id"], x["name"])):
        h.update(f"|{m['agent_id']}/{m['name']}|".encode())
        h.update(hashlib.sha256((m.get("body") or "").encode()).digest())
    return h.hexdigest()[:16]


def _norm_line(s: str) -> str:
    return " ".join(tokens(s))


def merge_deterministic(members: list[dict[str, Any]], max_desc: int = 60) -> dict[str, str]:
    """Gộp tất định: {name, description, body}. Không bịa nội dung — chỉ chọn + nối dòng riêng."""
    base = sorted(members, key=lambda m: (-(int(m.get("use_count") or 0)), -len(m.get("body") or ""), m["agent_id"], m["name"]))[0]
    names = Counter(m["name"] for m in members)
    top = max(names.values())
    name = base["name"] if names[base["name"]] == top else sorted(k for k, v in names.items() if v == top)[0]
    desc = " ".join((base.get("description") or "").split())[:max_desc]
    body = (base.get("body") or "").rstrip()
    seen = {_norm_line(x) for x in body.splitlines() if _norm_line(x)}
    extra: list[str] = []
    for m in members:
        if m is base:
            continue
        for line in (m.get("body") or "").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            k = _norm_line(s)
            if k and k not in seen:
                seen.add(k)
                extra.append(s if s.startswith(("-", "*", "1", "2", "3", "4", "5", "6", "7", "8", "9")) else f"- {s}")
    if extra:
        body += "\n\n## Additional rules (merged from other agents)\n" + "\n".join(extra)
    return {"name": name, "description": desc, "body": body + "\n"}
