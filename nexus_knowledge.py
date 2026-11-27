"""NEXUS memory + local knowledge base.

Two kinds of "learning":
  1. Rules/preferences  -> nexus_memory.json   (short facts the user asks it to remember)
  2. Knowledge library  -> nexus_knowledge.json (documents/web pages, searched by meaning)

The model's weights never change. "Learning" = storing things and pulling the
relevant ones back into the prompt at question time (RAG).
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
MEMORY_FILE = BASE_DIR / "nexus_memory.json"
KNOWLEDGE_FILE = BASE_DIR / "nexus_knowledge.json"

EMBED_MODEL = os.getenv("NEXUS_EMBED_MODEL", "nomic-embed-text")
RAG_MIN_SCORE = float(os.getenv("NEXUS_RAG_MIN_SCORE", "0.5"))

MAX_RULES = 100
MAX_HISTORY = 50
_lock = threading.RLock()


def _atomic_write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Rules / preferences memory
# --------------------------------------------------------------------------
def load_memory() -> dict:
    with _lock:
        data = {}
        if MEMORY_FILE.exists():
            try:
                data = json.loads(MEMORY_FILE.read_text(encoding="utf-8-sig"))
            except Exception:
                data = {}
        data.setdefault("learned_rules", [])
        data.setdefault("command_history", [])
        return data


def save_memory(data: dict) -> None:
    with _lock:
        data["command_history"] = data.get("command_history", [])[-MAX_HISTORY:]
        try:
            _atomic_write(MEMORY_FILE, data)
        except Exception as e:  # never crash the assistant over a save
            print(f"Memory save error: {e}")


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s.lower()).strip()


def validate_rule(rule: str, existing: list[str]) -> tuple[bool, str]:
    rule = (rule or "").strip()
    if len(rule) < 8:
        return False, "That's too short to be a useful rule."
    if len(rule) > 300:
        return False, "That's too long for a rule. Use a document upload for long text."
    if re.search(r"<[^>\n]{1,40}>", rule):
        return False, "That looks like a template placeholder, not a real rule."
    if sum(c.isalnum() for c in rule) < len(rule) * 0.5:
        return False, "That doesn't look like a sentence."
    if _norm(rule) in {_norm(r) for r in existing}:
        return False, "Already learned that."
    return True, ""


def store_learned_rule(rule: str) -> str:
    with _lock:
        memory = load_memory()
        ok, why = validate_rule(rule, memory["learned_rules"])
        if not ok:
            return why
        if len(memory["learned_rules"]) >= MAX_RULES:
            return f"Memory is full ({MAX_RULES} rules). Ask me to forget some first."
        memory["learned_rules"].append(rule.strip())
        save_memory(memory)
        return f"Locked in new knowledge: '{rule.strip()}'"


def record_command(command: str) -> None:
    with _lock:
        memory = load_memory()
        memory["command_history"].append(command)
        save_memory(memory)


def rules_for_prompt() -> str:
    rules = load_memory()["learned_rules"]
    return "\n".join(f"- {r}" for r in rules) if rules else "None yet."


# --------------------------------------------------------------------------
# Knowledge library (RAG)
# --------------------------------------------------------------------------
def chunk_text(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    if not text:
        return []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        while len(p) > size:  # hard-split oversized paragraphs, preferring sentence ends
            cut = p.rfind(". ", 0, size)
            cut = cut + 1 if cut > size // 2 else size
            piece, p = p[:cut].strip(), p[cut:].strip()
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(piece)
        if not p:
            continue
        if len(cur) + len(p) + 2 <= size:
            cur = f"{cur}\n\n{p}" if cur else p
        else:
            if cur:
                chunks.append(cur)
            cur = p
    if cur:
        chunks.append(cur)
    if overlap and len(chunks) > 1:
        out = [chunks[0]]
        for prev, c in zip(chunks, chunks[1:]):
            tail = prev[-overlap:]
            tail = tail[tail.find(" ") + 1:] if " " in tail else ""
            out.append(f"{tail} {c}".strip())
        return out
    return chunks


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def _prefixed(text: str, kind: str) -> str:
    # nomic-embed-text was trained with these task prefixes; they improve retrieval.
    if EMBED_MODEL.startswith("nomic"):
        return f"{'search_query' if kind == 'query' else 'search_document'}: {text}"
    return text


def embed_texts(client, texts: list[str], kind: str = "document") -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), 16):
        batch = [_prefixed(t, kind) for t in texts[i:i + 16]]
        resp = client.embeddings.create(model=EMBED_MODEL, input=batch)
        out.extend(_normalize(list(d.embedding)) for d in resp.data)
    return out


_kb_cache: list[dict] | None = None


def _load_kb() -> list[dict]:
    global _kb_cache
    with _lock:
        if _kb_cache is None:
            try:
                _kb_cache = json.loads(KNOWLEDGE_FILE.read_text(encoding="utf-8"))
            except Exception:
                _kb_cache = []
        return _kb_cache


def _save_kb(kb: list[dict]) -> None:
    global _kb_cache
    with _lock:
        _kb_cache = kb
        _atomic_write(KNOWLEDGE_FILE, kb)


def add_document(client, source: str, text: str) -> int:
    """Index text under `source`. Re-learning the same source replaces the old copy."""
    chunks = chunk_text(text)
    if not chunks:
        return 0
    vectors = embed_texts(client, chunks, "document")  # may raise if embed model missing
    with _lock:
        kb = [c for c in _load_kb() if c["source"] != source]
        kb.extend({"source": source, "text": t, "embedding": v} for t, v in zip(chunks, vectors))
        _save_kb(kb)
    return len(chunks)


def search(client, query: str, k: int = 4, min_score: float | None = None) -> list[dict]:
    kb = _load_kb()
    if not kb or not query.strip():
        return []
    threshold = RAG_MIN_SCORE if min_score is None else min_score
    q = embed_texts(client, [query], "query")[0]
    scored = []
    for c in kb:
        score = sum(a * b for a, b in zip(q, c["embedding"]))
        if score >= threshold:
            scored.append({"source": c["source"], "text": c["text"], "score": round(score, 3)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:k]


def list_sources() -> list[dict]:
    counts: dict[str, int] = {}
    for c in _load_kb():
        counts[c["source"]] = counts.get(c["source"], 0) + 1
    return [{"source": s, "chunks": n} for s, n in sorted(counts.items())]


def forget_source(source: str) -> int:
    with _lock:
        kb = _load_kb()
        kept = [c for c in kb if c["source"] != source]
        removed = len(kb) - len(kept)
        if removed:
            _save_kb(kept)
        return removed


def stats() -> dict:
    kb = _load_kb()
    return {"chunks": len(kb), "sources": len({c["source"] for c in kb}),
            "rules": len(load_memory()["learned_rules"])}
