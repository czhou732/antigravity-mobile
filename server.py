#!/usr/bin/env python3
"""
Antigravity Mobile — FastAPI server that replicates the desktop AI IDE on phone.

Connects to:
  - Gemini 2.5 Flash for chat (with function calling)
  - MCP Proxy at :8765 for all tool execution
  - Local filesystem for file browsing
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
import keyring
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import config

# ── Logging ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [mobile] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("antigravity-mobile")

# ── App Setup ────────────────────────────────────────────────────────────

app = FastAPI(title="Antigravity Mobile", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth ─────────────────────────────────────────────────────────────────

_AUTH_TOKEN: Optional[str] = None


def _get_auth_token() -> str:
    global _AUTH_TOKEN
    if _AUTH_TOKEN is None:
        _AUTH_TOKEN = keyring.get_password(
            config.KEYCHAIN_SERVICE, config.KEYCHAIN_USER
        )
        if not _AUTH_TOKEN:
            raise RuntimeError(
                "No auth token in Keychain. Run setup.sh first."
            )
    return _AUTH_TOKEN


def _check_auth(request: Request):
    auth = request.headers.get("Authorization", "")
    token = request.query_params.get("token", "")
    expected = _get_auth_token()

    if auth == f"Bearer {expected}":
        return
    if token == expected:
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


# ── Gemini Client ────────────────────────────────────────────────────────

_GEMINI_KEY: Optional[str] = None
_GEMINI_CLIENT = None


def _get_gemini_client():
    global _GEMINI_KEY, _GEMINI_CLIENT
    if _GEMINI_CLIENT is None:
        _GEMINI_KEY = keyring.get_password(
            config.GEMINI_KEYCHAIN_SERVICE, config.GEMINI_KEYCHAIN_USER
        )
        if not _GEMINI_KEY:
            _GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
        if not _GEMINI_KEY:
            raise RuntimeError("No Gemini API key found")
        from google import genai

        _GEMINI_CLIENT = genai.Client(api_key=_GEMINI_KEY)
    return _GEMINI_CLIENT


# ── MCP Proxy Client ────────────────────────────────────────────────────

_MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}

_MCP_HTTP = httpx.AsyncClient(
    base_url=config.MCP_PROXY_URL, timeout=httpx.Timeout(130.0)
)

_MCP_INITIALIZED = False
_MCP_REQUEST_ID = 0
_TOOL_CACHE: list[dict] = []
_TOOL_CACHE_TIME: float = 0


def _next_id() -> int:
    global _MCP_REQUEST_ID
    _MCP_REQUEST_ID += 1
    return _MCP_REQUEST_ID


async def _mcp_request(method: str, params: dict = None) -> dict:
    """Send a JSON-RPC request to the MCP proxy with proper headers."""
    resp = await _MCP_HTTP.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": _next_id(),
            "method": method,
            "params": params or {},
        },
        headers=_MCP_HEADERS,
    )
    data = resp.json()
    # Streamable HTTP may return a list of responses
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "result" in item:
                return item
        return data[0] if data else {}
    return data


async def _ensure_mcp_initialized():
    """Perform the MCP initialize handshake if not already done."""
    global _MCP_INITIALIZED
    if _MCP_INITIALIZED:
        return

    try:
        result = await _mcp_request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "antigravity-mobile", "version": "1.0"},
        })
        if "result" in result:
            _MCP_INITIALIZED = True
            log.info(
                f"MCP initialized: {result['result'].get('serverInfo', {})}"
            )
            # Send initialized notification
            await _MCP_HTTP.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                },
                headers=_MCP_HEADERS,
            )
        else:
            log.warning(f"MCP init response: {result}")
    except Exception as e:
        log.error(f"MCP initialization failed: {e}")


async def _fetch_tools(force: bool = False) -> list[dict]:
    """Fetch tool definitions from the MCP proxy."""
    global _TOOL_CACHE, _TOOL_CACHE_TIME

    if not force and _TOOL_CACHE and (time.time() - _TOOL_CACHE_TIME) < 300:
        return _TOOL_CACHE

    await _ensure_mcp_initialized()

    try:
        data = await _mcp_request("tools/list")
        tools = data.get("result", {}).get("tools", [])
        _TOOL_CACHE = tools
        _TOOL_CACHE_TIME = time.time()
        log.info(f"Fetched {len(tools)} tools from MCP proxy")
        return tools
    except Exception as e:
        log.error(f"Failed to fetch tools: {e}")
        return _TOOL_CACHE or []


async def _call_mcp_tool(name: str, arguments: dict) -> str:
    """Execute a tool call via the MCP proxy."""
    await _ensure_mcp_initialized()

    try:
        data = await _mcp_request("tools/call", {
            "name": name,
            "arguments": arguments,
        })

        if "error" in data:
            return f"Error: {data['error'].get('message', str(data['error']))}"

        result = data.get("result", {})
        content_parts = result.get("content", [])
        texts = []
        for part in content_parts:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(part["text"])
        return "\n".join(texts) if texts else json.dumps(result)
    except Exception as e:
        return f"Error calling tool {name}: {e}"


# ── System Prompt Builder ────────────────────────────────────────────────


def _load_system_prompt(user_text: str = "") -> str:
    """Build the system prompt from GEMINI.md + context."""
    now = datetime.now()
    hour = now.hour

    base_parts = []

    # Load GEMINI.md
    try:
        with open(config.GEMINI_MD_PATH) as f:
            gemini_md = f.read().strip()
        base_parts.append(gemini_md)
    except FileNotFoundError:
        pass

    # Core identity
    base_parts.append(
        "\n--- MOBILE CONTEXT ---\n"
        "You are running on Peter's phone via Antigravity Mobile. "
        "You have access to all MCP tools (Gmail, Calendar, Drive, Memory, Scholar, "
        "Notes, Health, Linear, Brightspace, Slack, OSF, iSTAR, web scraping). "
        "When the user asks something, decide which tool(s) to call. "
        "Keep responses concise — this is mobile. Use markdown formatting.\n"
        f"Current time: {now.strftime('%A, %B %d, %Y at %I:%M %p')}."
    )

    # Time-of-day routing
    if hour >= 23 or hour < 5:
        base_parts.append(
            "\n⚠️ It is late at night. Suppress Marcus (no ROI pushes). "
            "If the user is doing non-urgent work, gently suggest rest."
        )

    # Memory context injection
    if user_text:
        try:
            sys.path.insert(
                0,
                os.path.expanduser(
                    "~/.gemini/antigravity/scratch/usc_gmail_mcp"
                ),
            )
            from memory_utils import ranked_search

            stop = {
                "the", "a", "an", "is", "are", "was", "were", "do", "does",
                "did", "can", "could", "will", "would", "should", "have",
                "has", "had", "my", "me", "i", "what", "when", "where",
                "how", "who", "about", "with", "from", "for", "to", "in",
                "on", "at", "and", "or", "but",
            }
            words = [
                w
                for w in user_text.lower().split()
                if w not in stop and len(w) > 2
            ]
            if words:
                results = []
                for word in words[:3]:
                    for r in ranked_search(word, limit=2):
                        if r["name"] not in [x["name"] for x in results]:
                            results.append(r)
                if results:
                    ctx_lines = [
                        "\n--- MEMORY CONTEXT ---"
                    ]
                    for r in results[:4]:
                        obs = "; ".join(r.get("observations", [])[-3:])
                        if obs:
                            ctx_lines.append(
                                f"• {r['name']} ({r.get('entityType','?')}): "
                                f"{obs[:250]}"
                            )
                    base_parts.append("\n".join(ctx_lines))
        except Exception:
            pass

    return "\n\n".join(base_parts)


# ── Gemini Tool Definitions ─────────────────────────────────────────────


def _mcp_tools_to_gemini(mcp_tools: list[dict]) -> list:
    """Convert MCP tool schemas to Gemini function declarations."""
    from google.genai import types

    declarations = []
    for tool in mcp_tools:
        name = tool.get("name", "")
        desc = tool.get("description", "")[:500]
        schema = tool.get("inputSchema", {})
        props = schema.get("properties", {})
        required = schema.get("required", [])

        # Clean properties for Gemini (remove unsupported fields)
        clean_props = {}
        for pname, pschema in props.items():
            clean = {"type": pschema.get("type", "string")}
            if "description" in pschema:
                clean["description"] = pschema["description"][:200]
            if "enum" in pschema:
                clean["enum"] = pschema["enum"]
            if "default" in pschema:
                clean["default"] = pschema["default"]
            # Handle items for arrays
            if clean["type"] == "array" and "items" in pschema:
                items = pschema["items"]
                clean["items"] = {"type": items.get("type", "string")}
            clean_props[pname] = clean

        # Remove the waitForPreviousTools param (IDE-only)
        clean_props.pop("waitForPreviousTools", None)

        params = {
            "type": "object",
            "properties": clean_props,
        }
        if required:
            req = [r for r in required if r != "waitForPreviousTools"]
            if req:
                params["required"] = req

        try:
            declarations.append(
                types.FunctionDeclaration(
                    name=name,
                    description=desc,
                    parameters=params if clean_props else None,
                )
            )
        except Exception as e:
            log.warning(f"Skipping tool {name}: {e}")

    return declarations


# ── Chat Endpoint ────────────────────────────────────────────────────────


@app.post("/api/chat")
async def chat(request: Request):
    """Stream a Gemini response with MCP tool calling."""
    _check_auth(request)

    body = await request.json()
    messages = body.get("messages", [])
    continuation_context = body.get("continuation_context", "")
    if not messages:
        raise HTTPException(400, "No messages provided")

    user_text = messages[-1].get("content", "") if messages else ""

    async def stream():
        try:
            from google.genai import types

            client = _get_gemini_client()
            mcp_tools = await _fetch_tools()
            declarations = _mcp_tools_to_gemini(mcp_tools)
            system_prompt = _load_system_prompt(user_text)

            # Inject continuation context from a previous conversation
            if continuation_context:
                system_prompt += (
                    "\n\n--- CONTINUATION FROM DESKTOP CONVERSATION ---\n"
                    "The user is continuing a conversation that was started on the desktop IDE. "
                    "Treat the following context as the full history of that conversation. "
                    "Continue the work seamlessly — reference prior decisions, artifacts, "
                    "and progress. Do NOT re-introduce yourself.\n\n"
                    + continuation_context[:12000]
                )

            # Build conversation history
            contents = []
            for msg in messages:
                role = "user" if msg["role"] == "user" else "model"
                contents.append(
                    types.Content(
                        role=role,
                        parts=[types.Part(text=msg["content"])],
                    )
                )

            gemini_tools = [types.Tool(function_declarations=declarations)] if declarations else None

            # First call
            response = client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    tools=gemini_tools,
                    temperature=0.3,
                ),
            )

            # Handle tool calls (up to 5 rounds)
            for _ in range(5):
                if (
                    not response.candidates
                    or not response.candidates[0].content
                    or not response.candidates[0].content.parts
                ):
                    break

                part = response.candidates[0].content.parts[0]

                if not hasattr(part, "function_call") or not part.function_call:
                    break

                fc = part.function_call
                fn_name = fc.name
                fn_args = dict(fc.args) if fc.args else {}

                # Emit tool call event
                yield f"data: {json.dumps({'type': 'tool_call', 'name': fn_name, 'args': fn_args})}\n\n"

                # Execute via MCP proxy
                tool_result = await _call_mcp_tool(fn_name, fn_args)

                # Emit tool result event
                yield f"data: {json.dumps({'type': 'tool_result', 'name': fn_name, 'result': tool_result[:2000]})}\n\n"

                # Append to conversation and continue
                contents.append(
                    types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                function_call=types.FunctionCall(
                                    name=fn_name, args=fn_args
                                )
                            )
                        ],
                    )
                )
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    name=fn_name,
                                    response={"result": tool_result[:4000]},
                                )
                            )
                        ],
                    )
                )

                response = client.models.generate_content(
                    model=config.GEMINI_MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.3,
                    ),
                )

            # Stream the final text response
            if (
                response.candidates
                and response.candidates[0].content
                and response.candidates[0].content.parts
            ):
                text = response.candidates[0].content.parts[0].text or ""
                # Stream in chunks for a nice typing effect
                chunk_size = 40
                for i in range(0, len(text), chunk_size):
                    chunk = text[i : i + chunk_size]
                    yield f"data: {json.dumps({'type': 'text', 'content': chunk})}\n\n"
                    await asyncio.sleep(0.02)

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            log.error(f"Chat error: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ── Tool Endpoints ───────────────────────────────────────────────────────


@app.get("/api/tools")
async def list_tools(request: Request):
    _check_auth(request)
    tools = await _fetch_tools(force=True)
    return {"tools": tools, "count": len(tools)}


@app.post("/api/tool")
async def call_tool(request: Request):
    _check_auth(request)
    body = await request.json()
    name = body.get("name")
    args = body.get("arguments", {})
    if not name:
        raise HTTPException(400, "name required")
    result = await _call_mcp_tool(name, args)
    return {"result": result}


# ── File Browser Endpoints ───────────────────────────────────────────────


def _validate_path(path_str: str) -> Path:
    """Ensure the path is within allowed roots."""
    p = Path(path_str).resolve()
    for root in config.ALLOWED_ROOTS:
        if str(p).startswith(root):
            return p
    raise HTTPException(403, f"Path not in allowed roots")


@app.get("/api/files")
async def list_files(request: Request, path: str = "~/Research"):
    _check_auth(request)
    expanded = os.path.expanduser(path)
    p = _validate_path(expanded)

    if not p.exists():
        raise HTTPException(404, "Path not found")
    if not p.is_dir():
        raise HTTPException(400, "Not a directory")

    entries = []
    try:
        for item in sorted(p.iterdir()):
            if item.name.startswith("."):
                continue
            entry = {
                "name": item.name,
                "path": str(item),
                "is_dir": item.is_dir(),
            }
            if item.is_file():
                entry["size"] = item.stat().st_size
                entry["ext"] = item.suffix
            elif item.is_dir():
                try:
                    entry["children"] = len(list(item.iterdir()))
                except PermissionError:
                    entry["children"] = 0
            entries.append(entry)
    except PermissionError:
        raise HTTPException(403, "Permission denied")

    return {"path": str(p), "entries": entries}


@app.get("/api/file")
async def read_file(request: Request, path: str = ""):
    _check_auth(request)
    if not path:
        raise HTTPException(400, "path required")
    expanded = os.path.expanduser(path)
    p = _validate_path(expanded)

    if not p.exists():
        raise HTTPException(404, "File not found")
    if not p.is_file():
        raise HTTPException(400, "Not a file")
    if p.stat().st_size > 500_000:
        raise HTTPException(413, "File too large (>500KB)")

    try:
        content = p.read_text(errors="replace")
    except Exception as e:
        raise HTTPException(500, f"Read error: {e}")

    return {"path": str(p), "content": content, "size": p.stat().st_size}


@app.put("/api/file")
async def write_file(request: Request):
    _check_auth(request)
    body = await request.json()
    path_str = body.get("path", "")
    content = body.get("content", "")
    if not path_str:
        raise HTTPException(400, "path required")

    expanded = os.path.expanduser(path_str)
    p = _validate_path(expanded)

    try:
        p.write_text(content)
    except Exception as e:
        raise HTTPException(500, f"Write error: {e}")

    return {"path": str(p), "size": len(content), "status": "saved"}


# ── Conversation History ─────────────────────────────────────────────────

BRAIN_DIR = os.path.expanduser("~/.gemini/antigravity/brain")


def _parse_digest(conv_id: str) -> dict:
    """Parse a conversation's digest.md for title, UUID, timestamps."""
    digest_path = os.path.join(BRAIN_DIR, conv_id, "digest.md")
    info = {"id": conv_id, "title": conv_id[:8], "last_active": "", "summary": ""}

    if not os.path.exists(digest_path):
        # Fall back to directory mtime
        conv_dir = os.path.join(BRAIN_DIR, conv_id)
        if os.path.isdir(conv_dir):
            mtime = os.path.getmtime(conv_dir)
            info["last_active"] = datetime.fromtimestamp(mtime).isoformat()
        return info

    try:
        with open(digest_path) as f:
            content = f.read()

        lines = content.split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("# ") and info["title"] == conv_id[:8]:
                info["title"] = line[2:].strip()
            elif "**Last active:**" in line:
                ts = line.split("**Last active:**")[1].strip().strip("`").strip()
                info["last_active"] = ts
            elif "**Started:**" in line:
                if not info.get("started"):
                    ts = line.split("**Started:**")[1].strip().strip("`").strip()
                    info["started"] = ts
            elif line.startswith("- **") and "**" in line[4:]:
                # Artifact entry
                if not info["summary"]:
                    info["summary"] = line

        # Get directory mtime as fallback
        if not info["last_active"]:
            conv_dir = os.path.join(BRAIN_DIR, conv_id)
            mtime = os.path.getmtime(conv_dir)
            info["last_active"] = datetime.fromtimestamp(mtime).isoformat()

    except Exception:
        pass

    return info


@app.get("/api/conversations")
async def list_conversations(request: Request):
    """List all conversations sorted by recency."""
    _check_auth(request)

    conversations = []
    try:
        for entry in os.listdir(BRAIN_DIR):
            full = os.path.join(BRAIN_DIR, entry)
            if not os.path.isdir(full):
                continue
            # Skip non-UUID directories
            if len(entry) < 20 or entry in ("tempmediaStorage",):
                continue

            info = _parse_digest(entry)
            conversations.append(info)
    except Exception as e:
        raise HTTPException(500, f"Error scanning conversations: {e}")

    # Sort by last_active descending
    conversations.sort(key=lambda c: c.get("last_active", ""), reverse=True)

    return {"conversations": conversations, "count": len(conversations)}


@app.get("/api/conversation/{conv_id}")
async def get_conversation(request: Request, conv_id: str):
    """Get full conversation details including digest and artifacts."""
    _check_auth(request)

    conv_dir = os.path.join(BRAIN_DIR, conv_id)
    if not os.path.isdir(conv_dir):
        raise HTTPException(404, "Conversation not found")

    # Read digest
    digest_content = ""
    digest_path = os.path.join(conv_dir, "digest.md")
    if os.path.exists(digest_path):
        try:
            digest_content = Path(digest_path).read_text(errors="replace")
        except Exception:
            pass

    # List artifacts (non-hidden files)
    artifacts = []
    for item in sorted(Path(conv_dir).iterdir()):
        if item.name.startswith("."):
            continue
        if item.is_file():
            ext = item.suffix
            size = item.stat().st_size
            artifacts.append({
                "name": item.name,
                "path": str(item),
                "ext": ext,
                "size": size,
                "is_readable": ext in (
                    ".md", ".txt", ".json", ".py", ".js", ".css",
                    ".html", ".yaml", ".yml", ".sh", ".tex",
                ),
            })

    info = _parse_digest(conv_id)

    return {
        "id": conv_id,
        "title": info["title"],
        "last_active": info["last_active"],
        "digest": digest_content,
        "artifacts": artifacts,
    }


@app.get("/api/conversation/{conv_id}/context")
async def get_conversation_context(request: Request, conv_id: str):
    """Build a rich context bundle for continuing a conversation from mobile."""
    _check_auth(request)

    conv_dir = os.path.join(BRAIN_DIR, conv_id)
    if not os.path.isdir(conv_dir):
        raise HTTPException(404, "Conversation not found")

    context_parts = []

    # 1. Load digest
    digest_path = os.path.join(conv_dir, "digest.md")
    if os.path.exists(digest_path):
        digest = Path(digest_path).read_text(errors="replace").strip()
        context_parts.append(f"--- PREVIOUS CONVERSATION DIGEST ---\n{digest}")

    # 2. Load key artifacts (task, implementation plan, walkthrough)
    priority_files = [
        "task.md", "implementation_plan.md", "walkthrough.md",
    ]
    for fname in priority_files:
        fpath = os.path.join(conv_dir, fname)
        if os.path.exists(fpath):
            content = Path(fpath).read_text(errors="replace").strip()
            if content and len(content) > 20:
                context_parts.append(
                    f"--- ARTIFACT: {fname} ---\n{content[:3000]}"
                )

    # 3. Load any other .md artifacts (truncated)
    for item in sorted(Path(conv_dir).iterdir()):
        if (
            item.suffix == ".md"
            and item.name not in priority_files
            and item.name != "digest.md"
            and not item.name.endswith(".resolved")
            and not item.name.endswith(".metadata.json")
            and item.is_file()
        ):
            content = item.read_text(errors="replace").strip()
            if content and len(content) > 50:
                context_parts.append(
                    f"--- ARTIFACT: {item.name} ---\n{content[:1500]}"
                )

    info = _parse_digest(conv_id)
    context_text = "\n\n".join(context_parts)

    return {
        "id": conv_id,
        "title": info["title"],
        "context": context_text,
        "context_length": len(context_text),
    }


# ── Health ───────────────────────────────────────────────────────────────


@app.get("/api/health")
async def health(request: Request):
    # Health is public (no auth required) for monitoring
    mcp_ok = False
    mcp_info = {}
    try:
        resp = await _MCP_HTTP.get("/health", timeout=5.0)
        if resp.status_code == 200:
            mcp_ok = True
            mcp_info = resp.json()
    except Exception:
        pass

    return {
        "status": "ok",
        "mcp_proxy": "connected" if mcp_ok else "unreachable",
        "mcp_info": mcp_info,
        "server_time": datetime.now().isoformat(),
    }


# ── Serve PWA Frontend ──────────────────────────────────────────────────

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(STATIC_DIR, "index.html")
    with open(index_path) as f:
        return HTMLResponse(f.read())


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Entry Point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    log.info(f"🚀 Starting Antigravity Mobile on http://{config.HOST}:{config.PORT}")
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        log_level="info",
    )
