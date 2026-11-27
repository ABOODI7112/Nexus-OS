# NEXUS upgrade: setup

## 1. Drop the files in
Extract everything into your `API key` folder, overwriting `main.py`, `test_client.py`, `nexus_memory.json`.
Keep your existing `api_key.txt` (NEXUS now uses it as the real key). Your old memory is saved as `nexus_memory.backup.json`.

No new pip packages are needed (fastapi, openai, psutil, uvicorn, python-multipart are already in your venv).

## 2. Models (Ollama)
```
ollama pull llama3.1            # brain (tool calling)
ollama pull nomic-embed-text    # needed for "learning" (library search)
ollama pull qwen2.5vl:7b        # optional: lets you attach a picture and ask about it
```

## 3. Image generation
Ollama's own image generation is macOS-only for now, so NEXUS talks to a local Stable Diffusion server instead.
1. Install **Stable Diffusion WebUI Forge** or **AUTOMATIC1111** (any server with the `/sdapi/v1/txt2img` API works). Keep it in its own folder/Python, don't install it into this venv.
2. In its `webui-user.bat`, set `set COMMANDLINE_ARGS=--api`
3. Put a checkpoint in `models/Stable-diffusion`. Pick one your GPU can handle: SDXL-class needs about 8 GB+ VRAM, SD 1.5-class runs on less.
4. Start the WebUI first, then NEXUS. Say: "draw a neon city at night in the rain".

The header chips show LLM / IMAGES / LIBRARY status, and a yellow bar tells you exactly what's missing.

## 4. Run
Double-click `start_nexus.bat` (or `python main.py`), then open http://127.0.0.1:8000
Self-check without Ollama: `python test_nexus.py`

## 5. Teaching it things
| You want | Do this |
|---|---|
| A short fact or preference | "Remember that I like short answers" |
| A web page | Paste the link: "learn this https://..." (only links you type are learned) |
| A document | Click 📚 LEARN and pick a .txt / .md / .csv / code file |
| Current facts | Just ask, it searches the web and can read pages |

Learned material is searched by meaning on every question and the best matches are shown to the model (sources are listed under its reply).
`GET /knowledge` lists what it knows; `DELETE /knowledge?source=...` removes something.

## Settings (environment variables, optional)
`NEXUS_MODEL` (default llama3.1) · `OLLAMA_VISION_MODEL` · `NEXUS_EMBED_MODEL` · `SD_API_URL` (default http://127.0.0.1:7860) · `SD_SIZE`, `SD_STEPS`, `SD_CFG` · `NEXUS_RAG_MIN_SCORE` (lower = recalls more, default 0.5)

## Safety changes
- Commands: read-only ones (Get-Date, ipconfig, ...) run instantly. Anything else waits for you to click **Approve** in the chat.
- The server only listens on 127.0.0.1. Don't start it with `--host 0.0.0.0`: that would expose a PC-controlling API to your network.
- `open_browser` no longer goes through PowerShell (the old version could be tricked into running commands by a crafted URL).
- Web fetching refuses localhost/home-network addresses, and page text is treated as untrusted data.
