"""NEXUS server. Start with:  python main.py   (then open http://127.0.0.1:8000)"""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import urllib.request
from pathlib import Path

import openai
from fastapi import FastAPI, File, Form, HTTPException, Request, Security, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

import nexus_agent as agent
import nexus_knowledge as kb
import nexus_tools as tools

BASE_DIR = Path(__file__).resolve().parent
OLLAMA_URL = os.getenv("NEXUS_OLLAMA_URL", "http://localhost:11434").rstrip("/")
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DOC_BYTES = 2 * 1024 * 1024

client = OpenAI(base_url=f"{OLLAMA_URL}/v1", api_key="ollama", timeout=300)
app = FastAPI(title="NEXUS")
app.mount("/images", StaticFiles(directory=str(tools.IMG_DIR)), name="images")


# --- API key: env var > api_key.txt > generated (written to api_key.txt) --------
def load_api_key() -> str:
    env = os.getenv("NEXUS_API_KEY", "").strip()
    if env:
        return env
    path = BASE_DIR / "api_key.txt"
    if path.exists():
        key = path.read_text(encoding="utf-8-sig").strip()
        if key:
            return key
    key = secrets.token_urlsafe(24)
    path.write_text(key, encoding="utf-8")
    return key


API_KEY = load_api_key()
api_key_header = APIKeyHeader(name="X-API-Key")


def verify_api_key(api_key: str = Security(api_key_header)) -> str:
    if secrets.compare_digest(api_key.encode(), API_KEY.encode()):
        return api_key
    raise HTTPException(status_code=403, detail="Not authenticated")


def _is_loopback(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


# --- Agent ----------------------------------------------------------------------
@app.post("/ai-agent")
def run_ai_agent(prompt: str = Form(...), session_id: str = Form("default"),
                 file: UploadFile | None = File(None), api_key: str = Security(verify_api_key)):
    image_bytes = image_mime = None
    if file is not None and file.filename:
        image_mime = file.content_type or ""
        if not image_mime.startswith("image/"):
            raise HTTPException(400, "Attach an image file (png, jpg, webp).")
        image_bytes = file.file.read(MAX_IMAGE_BYTES + 1)
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "Image is too large (10 MB max).")
    try:
        result = agent.run_agent(client, prompt, session_id[:64], image_bytes, image_mime)
    except openai.APIConnectionError:
        raise HTTPException(503, f"Can't reach Ollama at {OLLAMA_URL}. Is it running?")
    except openai.NotFoundError:
        model = agent.VISION_MODEL if image_bytes else agent.TEXT_MODEL
        raise HTTPException(404, f"Model '{model}' isn't installed. Run: ollama pull {model}")
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"status": "success", **result}


@app.post("/new-chat")
def new_chat(session_id: str = Form("default"), api_key: str = Security(verify_api_key)):
    agent.clear_history(session_id[:64])
    return {"status": "cleared"}


# --- Approvals for risky actions ------------------------------------------------
@app.post("/approve/{pid}")
def approve(pid: str, api_key: str = Security(verify_api_key)):
    return {"result": tools.resolve_approval(pid, True)}


@app.post("/deny/{pid}")
def deny(pid: str, api_key: str = Security(verify_api_key)):
    return {"result": tools.resolve_approval(pid, False)}


# --- Knowledge library ----------------------------------------------------------
@app.get("/knowledge")
def knowledge_list(api_key: str = Security(verify_api_key)):
    return {"sources": kb.list_sources(), **kb.stats()}


@app.post("/knowledge")
def knowledge_upload(file: UploadFile = File(...), api_key: str = Security(verify_api_key)):
    raw = file.file.read(MAX_DOC_BYTES + 1)
    if len(raw) > MAX_DOC_BYTES:
        raise HTTPException(413, "File too large (2 MB max). Split it up.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "Only plain-text files (.txt, .md, .py, .csv, .json...) for now.")
    try:
        n = kb.add_document(client, f"file: {file.filename}", text)
    except Exception as e:
        raise HTTPException(500, f"Couldn't index it ({e}). Run: ollama pull {kb.EMBED_MODEL}")
    return {"status": "learned", "source": f"file: {file.filename}", "chunks": n}


@app.delete("/knowledge")
def knowledge_forget(source: str, api_key: str = Security(verify_api_key)):
    return {"removed_chunks": kb.forget_source(source)}


# --- Status for the HUD ---------------------------------------------------------
def _get_json(url: str, timeout: float = 2.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


@app.get("/status")
def status(api_key: str = Security(verify_api_key)):
    out = {"llm": False, "image": False, "missing_models": [], **kb.stats()}
    try:
        names = [m["name"] for m in _get_json(f"{OLLAMA_URL}/api/tags").get("models", [])]
        out["llm"] = True
        have = lambda want: any(n == want or n.split(":")[0] == want.split(":")[0] and ":" not in want
                                for n in names)
        out["missing_models"] = [m for m in (agent.TEXT_MODEL, kb.EMBED_MODEL) if not have(m)]
    except Exception:
        pass
    try:
        _get_json(f"{tools.SD_API_URL}/sdapi/v1/sd-models")
        out["image"] = True
    except Exception:
        pass
    return out


# --- HUD ------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def get_chat_ui(request: Request):
    # The page contains the API key, so only hand it to this PC itself.
    if not _is_loopback(request.client.host if request.client else None):
        raise HTTPException(403, "The NEXUS HUD is only available on the machine it runs on.")
    page = (BASE_DIR / "ui.html").read_text(encoding="utf-8")
    return page.replace('"__API_KEY__"', json.dumps(API_KEY))


if __name__ == "__main__":
    import uvicorn
    print(f"NEXUS starting on http://127.0.0.1:8000  (model: {agent.TEXT_MODEL})")
    uvicorn.run(app, host="127.0.0.1", port=8000)
