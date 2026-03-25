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

            # Handle tool calls (up to 10 rounds, supports parallel calls)
            for _ in range(10):
                if (
                    not response.candidates
                    or not response.candidates[0].content
                    or not response.candidates[0].content.parts
                ):
                    break

                # Collect ALL function calls from this response (parallel tool calling)
                function_calls = []
                for part in response.candidates[0].content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        function_calls.append(part)

                if not function_calls:
                    break

                # Build model response with all function call parts
                model_parts = []
                response_parts = []

                for part in function_calls:
                    fc = part.function_call
                    fn_name = fc.name
                    fn_args = dict(fc.args) if fc.args else {}

                    # Emit tool call event
                    yield f"data: {json.dumps({'type': 'tool_call', 'name': fn_name, 'args': fn_args})}\n\n"

                    # Execute via MCP proxy
                    tool_result = await _call_mcp_tool(fn_name, fn_args)

                    # Emit tool result event
                    yield f"data: {json.dumps({'type': 'tool_result', 'name': fn_name, 'result': tool_result[:8000]})}\n\n"

                    model_parts.append(
                        types.Part(
                            function_call=types.FunctionCall(
                                name=fn_name, args=fn_args
                            )
                        )
                    )
                    response_parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=fn_name,
                                response={"result": tool_result[:8000]},
                            )
                        )
                    )

                # Append model's function calls and our responses
                contents.append(
                    types.Content(role="model", parts=model_parts)
                )
                contents.append(
                    types.Content(role="user", parts=response_parts)
                )

                # Continue with tools available for chaining
                response = client.models.generate_content(
                    model=config.GEMINI_MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        tools=gemini_tools,
                        temperature=0.3,
                    ),
                )

            # Stream the final text response
            if (
                response.candidates
                and response.candidates[0].content
                and response.candidates[0].content.parts
            ):
                # Collect all text parts (there may be multiple after parallel calls)
                text = ""
                for part in response.candidates[0].content.parts:
                    if hasattr(part, "text") and part.text:
                        text += part.text
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


# ── Quick Actions ────────────────────────────────────────────────────────

SCRIPTS_DIR = os.path.expanduser(
    "~/.gemini/antigravity/scratch/usc_gmail_mcp"
)


def _run_script(script: str, args: list = None, timeout: int = 15) -> str:
    """Run a PeterOS script and return stdout."""
    import subprocess

    cmd = [
        sys.executable, os.path.join(SCRIPTS_DIR, script)
    ] + (args or [])
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=SCRIPTS_DIR,
        )
        return proc.stdout.strip()
    except Exception as e:
        return f"Error: {e}"


@app.get("/api/status")
async def quick_status(request: Request):
    """One-call unified status: CRS, priorities, infra, commitments."""
    _check_auth(request)

    status = {
        "timestamp": datetime.now().isoformat(),
        "day": datetime.now().strftime("%A, %b %d"),
    }

    # CRS
    try:
        sys.path.insert(0, SCRIPTS_DIR)
        from weekly_scheduler import _compute_crs
        crs = _compute_crs()
        status["crs"] = {
            "total": round(crs.get("total", 0)),
            "sleep": round(crs.get("sleep", 0)),
            "recovery": round(crs.get("recovery", 0)),
            "workload": round(crs.get("workload", 0)),
            "wrr_label": crs.get("wrr_label", "unknown"),
        }
    except Exception as e:
        status["crs"] = {"total": 0, "error": str(e)}

    # Top priorities
    try:
        from morning_dashboard import _get_top_priorities
        status["priorities"] = _get_top_priorities(3)
    except Exception:
        status["priorities"] = []

    # Infrastructure health
    try:
        infra_file = os.path.expanduser("~/.antigravity/infra_status.json")
        if os.path.exists(infra_file):
            with open(infra_file) as f:
                infra = json.load(f)
            status["infra"] = {
                "running": infra.get("running", 0),
                "crashed": infra.get("crashed", 0),
                "restarted": len(infra.get("restarted", [])),
                "escalated": infra.get("escalated", []),
            }
    except Exception:
        status["infra"] = {"running": 0, "crashed": 0}

    # Commitments due
    try:
        from commitment_extractor import get_commitments_summary
        status["commitments"] = get_commitments_summary()
    except Exception:
        status["commitments"] = {"active_count": 0, "due_today": 0}

    # Unread email count
    try:
        result = await _call_mcp_tool(
            "search_emails", {"query": "is:unread", "max_results": 1}
        )
        # Just get count hint from result
        if "results" in result.lower():
            import re
            nums = re.findall(r"(\d+)\s+results?", result.lower())
            status["unread_emails"] = int(nums[0]) if nums else 0
        else:
            status["unread_emails"] = 0
    except Exception:
        status["unread_emails"] = 0

    return status


@app.post("/api/quick-action")
async def quick_action(request: Request):
    """Execute a pre-built quick action without Gemini roundtrip."""
    _check_auth(request)
    body = await request.json()
    action = body.get("action", "")

    if action == "log_commitment":
        text = body.get("text", "")
        deadline = body.get("deadline", "")
        person = body.get("person", "")
        if not text:
            raise HTTPException(400, "text required")

        try:
            sys.path.insert(0, SCRIPTS_DIR)
            from commitment_extractor import _load_commitments, _save_commitments
            registry = _load_commitments()
            registry["commitments"].append({
                "text": text,
                "deadline": deadline,
                "person": person,
                "source": "mobile_quick_action",
                "detected_at": datetime.now().isoformat(),
                "status": "active",
            })
            _save_commitments(registry)
            return {"status": "ok", "message": f"Logged: {text[:50]}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    elif action == "mark_done":
        index = body.get("index", -1)
        try:
            sys.path.insert(0, SCRIPTS_DIR)
            from commitment_extractor import _load_commitments, _save_commitments
            registry = _load_commitments()
            active = [
                c for c in registry["commitments"]
                if c.get("status") == "active"
            ]
            if 0 <= index < len(active):
                active[index]["status"] = "done"
                active[index]["completed_at"] = datetime.now().isoformat()
                _save_commitments(registry)
                return {
                    "status": "ok",
                    "message": f"Done: {active[index]['text'][:50]}"
                }
            return {"status": "error", "message": "Invalid index"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    elif action == "log_caffeine":
        dose = body.get("dose_mg", 100)
        try:
            state_path = os.path.join(SCRIPTS_DIR, "advisor_state.json")
            with open(state_path) as f:
                state = json.load(f)
            state.setdefault("nutrition", {})
            state["nutrition"]["last_caffeine"] = (
                datetime.now().strftime("%H:%M")
            )
            state["nutrition"]["caffeine_dose_mg"] = dose
            with open(state_path, "w") as f:
                json.dump(state, f, indent=2)
            return {
                "status": "ok",
                "message": f"Logged {dose}mg caffeine at "
                           f"{datetime.now().strftime('%H:%M')}"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    elif action == "log_gym":
        try:
            state_path = os.path.join(SCRIPTS_DIR, "advisor_state.json")
            with open(state_path) as f:
                state = json.load(f)
            state.setdefault("recovery", {})
            state["recovery"]["days_since_rest"] = 0
            state["recovery"]["last_workout"] = (
                datetime.now().strftime("%Y-%m-%d %H:%M")
            )
            with open(state_path, "w") as f:
                json.dump(state, f, indent=2)
            return {
                "status": "ok",
                "message": f"Gym logged at {datetime.now().strftime('%H:%M')}"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    elif action == "restart_agent":
        label = body.get("label", "")
        if not label:
            raise HTTPException(400, "label required")
        try:
            sys.path.insert(0, SCRIPTS_DIR)
            from resurrection_daemon import restart_agent
            result = restart_agent(label, reason="mobile_quick_action")
            return result
        except Exception as e:
            return {"status": "error", "message": str(e)}

    elif action == "infra_scan":
        try:
            sys.path.insert(0, SCRIPTS_DIR)
            from resurrection_daemon import scan_and_heal
            report = scan_and_heal()
            return {
                "running": report["running"],
                "crashed": report["crashed"],
                "restarted": report["restarted"],
                "escalated": report["escalated"],
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    else:
        raise HTTPException(
            400,
            f"Unknown action: {action}. "
            f"Available: log_commitment, mark_done, log_caffeine, "
            f"log_gym, restart_agent, infra_scan"
        )


@app.get("/api/infra")
async def infra_status(request: Request):
    """Get infrastructure health from the resurrection daemon."""
    _check_auth(request)
    infra_file = os.path.expanduser("~/.antigravity/infra_status.json")
    if os.path.exists(infra_file):
        with open(infra_file) as f:
            return json.load(f)
    return {"error": "No infra status available. Run resurrection_daemon.py --status first."}


# ── iOS Shortcuts API ────────────────────────────────────────────────────
# Compact, single-purpose endpoints for iOS Shortcuts "Get Contents of URL"
# Each returns {display: str, ...data} where display is optimized for
# Shortcuts' "Show Result" action (notification banner).


@app.get("/api/shortcuts/energy")
async def shortcut_energy(request: Request):
    """Compact energy check for iOS Shortcut."""
    _check_auth(request)
    try:
        sys.path.insert(0, SCRIPTS_DIR)
        from energy_router import evaluate_energy
        r = evaluate_energy()
        recs = "\n".join(f"→ {x}" for x in r.get("recommendations", [])[:3])
        mods = "\n".join(f"📅 {x}" for x in r.get("schedule_modifications", [])[:2])
        display = (
            f"{r['icon']} Energy: {r['energy_score']}/100 ({r['level']})\n"
            f"Sleep: {r['inputs']['last_sleep_hours']}h | "
            f"Recovery: {r['inputs']['recovery_zone']}\n"
            f"{recs}"
        )
        if mods:
            display += f"\n{mods}"
        return {
            "display": display,
            "score": r["energy_score"],
            "level": r["level"],
            "block_commitments": r.get("block_commitments", False),
            "recommendations": r.get("recommendations", []),
        }
    except Exception as e:
        return {"display": f"⚠️ Energy check failed: {e}", "error": str(e)}


@app.get("/api/shortcuts/ali")
async def shortcut_ali(request: Request):
    """Compact ALI check for iOS Shortcut."""
    _check_auth(request)
    try:
        sys.path.insert(0, SCRIPTS_DIR)
        from allostatic_monitor import compute_ali
        r = compute_ali()
        domains = " | ".join(
            f"{d['domain'][:4]}:{d['score']}" for d in r["domains"]
        )
        display = (
            f"🏋️ ALI: {r['ali_score']}/10 {r['zone']}\n"
            f"{domains}\n"
            f"{r['recommendation'][:120]}"
        )
        if r.get("advisor_activation"):
            display += f"\n👤 Advisor: {r['advisor_activation']}"
        return {
            "display": display,
            "ali_score": r["ali_score"],
            "zone": r["zone"],
            "block_commitments": r.get("block_new_commitments", False),
        }
    except Exception as e:
        return {"display": f"⚠️ ALI check failed: {e}", "error": str(e)}


@app.post("/api/shortcuts/focus-start")
async def shortcut_focus_start(request: Request):
    """Start a 90-min ultradian focus block from iOS."""
    _check_auth(request)
    try:
        sys.path.insert(0, SCRIPTS_DIR)
        from ultradian_engine import start_block
        r = start_block()
        return {"display": r["message"], **r}
    except Exception as e:
        return {"display": f"⚠️ Focus start failed: {e}", "error": str(e)}


@app.post("/api/shortcuts/focus-end")
async def shortcut_focus_end(request: Request):
    """End current focus block. Body: {quality: 1-10}"""
    _check_auth(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    quality = body.get("quality", 7)
    try:
        sys.path.insert(0, SCRIPTS_DIR)
        from ultradian_engine import end_block
        r = end_block(quality=quality)
        return {"display": r["message"], **r}
    except Exception as e:
        return {"display": f"⚠️ Focus end failed: {e}", "error": str(e)}


@app.get("/api/shortcuts/circadian")
async def shortcut_circadian(request: Request):
    """Current circadian window + optimal task type."""
    _check_auth(request)
    try:
        sys.path.insert(0, SCRIPTS_DIR)
        from circadian_router import get_todays_windows
        r = get_todays_windows()
        current = [w for w in r.get("schedule", []) if w.get("is_current")]
        if current:
            w = current[0]
            tasks = "\n".join(
                f"→ {t['milestone'][:40]}" for t in w.get("tasks", [])[:2]
            )
            display = (
                f"🕐 {w['label']}\n"
                f"Window: {w['hours']}\n"
                f"{w['mechanism'][:80]}"
            )
            if tasks:
                display += f"\n\nTasks:\n{tasks}"
            return {"display": display, "window": w["window"], "label": w["label"]}
        else:
            return {"display": "🕐 Outside cognitive windows — admin or rest", "window": "none"}
    except Exception as e:
        # Fallback: compute from time of day
        hour = datetime.now().hour
        if 7 <= hour < 12:
            window = "🌅 Morning Peak — analytical tasks"
        elif 12 <= hour < 14:
            window = "🌤️ Midday — lighter tasks, post-lunch dip"
        elif 14 <= hour < 18:
            window = "🌇 Afternoon — creative / insight tasks"
        elif 18 <= hour < 22:
            window = "🌙 Evening — admin, review, light work"
        else:
            window = "🌑 Night — wind down, no deep work"
        return {"display": window, "window": window, "source": "fallback"}


@app.post("/api/shortcuts/add-paper")
async def shortcut_add_paper(request: Request):
    """Add a paper to the knowledge pipeline via dictation."""
    _check_auth(request)
    body = await request.json()
    paper = body.get("paper", "")
    findings = body.get("findings", [])
    if isinstance(findings, str):
        findings = [f.strip() for f in findings.split(";") if f.strip()]
    if not paper:
        return {"display": "⚠️ Paper title required", "error": "missing paper"}
    try:
        sys.path.insert(0, SCRIPTS_DIR)
        from knowledge_pipeline import add_paper
        r = add_paper(paper, findings)
        display = (
            f"📚 Added: {paper[:50]}\n"
            f"Findings: {len(findings)}\n"
            f"First review: {r.get('next_review', 'tomorrow')}"
        )
        return {"display": display, **r}
    except Exception as e:
        return {"display": f"⚠️ Add failed: {e}", "error": str(e)}


@app.get("/api/shortcuts/meeting-prep")
async def shortcut_meeting_prep(request: Request):
    """Quick meeting prep for a PI. Query param: ?pi=itti"""
    _check_auth(request)
    pi_name = request.query_params.get("pi", "").lower()
    if not pi_name:
        return {"display": "⚠️ Specify PI: ?pi=itti or ?pi=read", "error": "missing pi"}
    try:
        sys.path.insert(0, SCRIPTS_DIR)
        # Pull commitments for this PI
        from commitment_extractor import get_commitments_summary
        commitments = get_commitments_summary()
        pi_commitments = [
            c for c in commitments.get("active", [])
            if pi_name in c.get("person", "").lower()
        ]

        # Pull goal milestones relevant to PI
        import json as _json
        goal_file = os.path.join(SCRIPTS_DIR, "goal_registry.json")
        with open(goal_file) as f:
            goals = _json.load(f).get("goals", [])

        pi_tags = {"itti": ["Itti-lab", "IRB", "NSG", "review-paper", "preprint"],
                   "read": ["Read-lab", "NSG", "preprint"],
                   "kvarta": ["NIH", "career"],
                   "kumar": ["NIH", "career"]}
        tags = pi_tags.get(pi_name, [])

        relevant_milestones = []
        for g in goals:
            if g.get("status") != "active":
                continue
            if any(t in g.get("tags", []) for t in tags):
                for ms in g.get("milestones", []):
                    if ms.get("status") in ("in_progress", "not_started"):
                        relevant_milestones.append(
                            f"• {ms['text'][:50]} (due {ms.get('deadline', '?')})"
                        )

        display = f"📋 Prep for {pi_name.title()}\n\n"
        if relevant_milestones:
            display += "Milestones:\n" + "\n".join(relevant_milestones[:5]) + "\n\n"
        if pi_commitments:
            display += "Open commitments:\n"
            for c in pi_commitments[:3]:
                display += f"• {c.get('text', '')[:50]}\n"
        else:
            display += "No open commitments ✅"

        return {
            "display": display,
            "pi": pi_name,
            "milestones": relevant_milestones[:5],
            "commitments": pi_commitments[:3],
        }
    except Exception as e:
        return {"display": f"⚠️ Prep failed: {e}", "error": str(e)}


@app.get("/api/shortcuts/goals")
async def shortcut_goals(request: Request):
    """Compact goal status for iOS Shortcut."""
    _check_auth(request)
    try:
        import json as _json
        goal_file = os.path.join(SCRIPTS_DIR, "goal_registry.json")
        with open(goal_file) as f:
            data = _json.load(f)

        lines = []
        for g in data.get("goals", []):
            if g.get("status") != "active":
                continue
            milestones = g.get("milestones", [])
            done = sum(1 for m in milestones if m.get("status") == "done")
            total = len(milestones)
            pct = round(done / total * 100) if total else 0
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            deadline = g.get("deadline", "?")
            lines.append(
                f"{'🔴' if g.get('priority', 5) <= 1 else '🟡' if g.get('priority', 5) <= 2 else '⚪'} "
                f"{g['title'][:30]}\n"
                f"   [{bar}] {pct}% | Due {deadline}"
            )

        display = "🎯 Goals\n\n" + "\n\n".join(lines)
        return {"display": display, "goal_count": len(lines)}
    except Exception as e:
        return {"display": f"⚠️ Goals failed: {e}", "error": str(e)}


@app.get("/api/shortcuts/bethesda")
async def shortcut_bethesda(request: Request):
    """NIH SIP countdown + pending logistics."""
    _check_auth(request)
    from datetime import date
    start_date = date(2026, 5, 27)
    today = date.today()
    days_left = (start_date - today).days

    # Check if transition file exists
    tasks_file = os.path.expanduser("~/.antigravity/bethesda_transition.json")
    pending = []
    try:
        import json as _json
        if os.path.exists(tasks_file):
            with open(tasks_file) as f:
                data = _json.load(f)
            pending = [
                t for t in data.get("tasks", [])
                if t.get("status") != "done"
            ]
    except Exception:
        pass

    if not pending:
        # Default logistics checklist
        pending = [
            {"task": "Secure Bethesda housing", "priority": "high"},
            {"task": "Arrange car storage", "priority": "high"},
            {"task": "Pre-arrival paperwork", "priority": "medium"},
            {"task": "Pack for 10-week stay", "priority": "low"},
        ]

    task_lines = "\n".join(
        f"{'🔴' if t.get('priority') == 'high' else '🟡'} {t.get('task', t.get('text', ''))[:40]}"
        for t in pending[:5]
    )

    if days_left > 0:
        urgency = "🟢" if days_left > 30 else "🟡" if days_left > 14 else "🔴"
        display = (
            f"🏛️ Bethesda in {days_left} days {urgency}\n"
            f"Start: May 27, 2026\n\n"
            f"Pending ({len(pending)}):\n{task_lines}"
        )
    else:
        display = "🏛️ You're at NIH! 🎉"

    return {
        "display": display,
        "days_left": days_left,
        "pending_count": len(pending),
    }


@app.get("/api/shortcuts/commitments")
async def shortcut_commitments(request: Request):
    """Active commitments list for iOS Shortcut."""
    _check_auth(request)
    try:
        sys.path.insert(0, SCRIPTS_DIR)
        from commitment_extractor import get_commitments_summary
        summary = get_commitments_summary()
        active = summary.get("active", [])

        if not active:
            return {"display": "✅ No active commitments", "count": 0}

        lines = []
        for c in active[:8]:
            person = c.get("person", "")[:12]
            text = c.get("text", "")[:40]
            deadline = c.get("deadline", "")
            prefix = f"@{person} " if person else ""
            suffix = f" (due {deadline})" if deadline else ""
            lines.append(f"• {prefix}{text}{suffix}")

        display = f"📋 Commitments ({len(active)})\n\n" + "\n".join(lines)
        return {"display": display, "count": len(active), "items": active[:8]}
    except Exception as e:
        return {"display": f"⚠️ Failed: {e}", "error": str(e)}


@app.get("/api/shortcuts/dashboard")
async def shortcut_dashboard(request: Request):
    """Compact full dashboard for iOS Shortcut — one-screen overview."""
    _check_auth(request)
    parts = []

    # Energy
    try:
        sys.path.insert(0, SCRIPTS_DIR)
        from energy_router import get_energy_for_dashboard
        e = get_energy_for_dashboard()
        parts.append(f"{e.get('icon', '?')} Energy: {e.get('energy_score', '?')}/100")
    except Exception:
        parts.append("⚡ Energy: ?")

    # ALI
    try:
        ali_file = os.path.expanduser("~/.antigravity/allostatic_load.json")
        if os.path.exists(ali_file):
            import json as _json
            with open(ali_file) as f:
                ali = _json.load(f)
            parts.append(f"🏋️ ALI: {ali.get('ali_score', '?')}/10 {ali.get('zone', '?')}")
    except Exception:
        pass

    # Ultradian
    try:
        from ultradian_engine import get_cycle_status
        u = get_cycle_status()
        parts.append(
            f"🧠 Blocks: {u['blocks_completed']}/{u['max_blocks']} "
            f"({u['total_deep_minutes']}min deep)"
        )
        if u.get("active_block"):
            ab = u["active_block"]
            parts.append(f"   ▶ {ab['task'][:30]} ({ab['remaining_min']}min left)")
    except Exception:
        pass

    # Bethesda countdown
    from datetime import date
    days_left = (date(2026, 5, 27) - date.today()).days
    if days_left > 0:
        parts.append(f"🏛️ Bethesda: {days_left}d")

    # Commitments
    try:
        from commitment_extractor import get_commitments_summary
        cs = get_commitments_summary()
        active = len(cs.get("active", []))
        due = cs.get("due_today", 0)
        if active > 0:
            parts.append(f"📋 Commits: {active} active" + (f", {due} due today" if due else ""))
    except Exception:
        pass

    # Circadian window
    hour = datetime.now().hour
    if 7 <= hour < 12:
        parts.append("🕐 Window: analytical peak")
    elif 14 <= hour < 18:
        parts.append("🕐 Window: creative/insight")
    elif hour >= 22:
        parts.append("🌙 Wind down — no deep work")

    display = "\n".join(parts)
    return {"display": display, "sections": len(parts)}


# ── Linear iOS Shortcuts ─────────────────────────────────────────────────
# One-tap status checks for Linear PeterOS dashboard


@app.get("/api/shortcuts/linear")
async def shortcut_linear_dashboard(request: Request):
    """One-tap Linear dashboard: urgent issues, due this week, blocked."""
    _check_auth(request)
    try:
        # Fetch all PeterOS issues via MCP
        result = await _call_mcp_tool("linear_search_issues", {"search_query": "POS"})

        # Parse issues from result
        lines = result.split("\n")
        urgent = []
        due_soon = []
        blocked = []
        in_progress = []

        for line in lines:
            line = line.strip()
            if not line or line.startswith("Found") or line.startswith("---"):
                continue
            if "Urgent" in line or "priority: 1" in line.lower():
                urgent.append(line[:60])
            if "blocked" in line.lower():
                blocked.append(line[:60])
            if "In Progress" in line:
                in_progress.append(line[:60])

        parts = []
        if urgent:
            parts.append("🔴 URGENT\n" + "\n".join(f"  {u}" for u in urgent[:5]))
        if in_progress:
            parts.append("▶️ IN PROGRESS\n" + "\n".join(f"  {i}" for i in in_progress[:5]))
        if blocked:
            parts.append("🚫 BLOCKED\n" + "\n".join(f"  {b}" for b in blocked[:3]))

        display = "📊 Linear Dashboard\n\n" + "\n\n".join(parts) if parts else "📊 Linear — all clear ✅"
        return {"display": display, "urgent": len(urgent), "blocked": len(blocked), "in_progress": len(in_progress)}
    except Exception as e:
        return {"display": f"⚠️ Linear check failed: {e}", "error": str(e)}


@app.get("/api/shortcuts/linear/critical")
async def shortcut_linear_critical(request: Request):
    """Critical path issues — narrative:core label, PhD-essential."""
    _check_auth(request)
    try:
        import subprocess as _sp

        api_key = keyring.get_password("mcp-linear", "api-key")
        if not api_key:
            return {"display": "⚠️ No Linear API key", "error": "missing key"}

        resp = httpx.post(
            "https://api.linear.app/graphql",
            json={"query": """{
                issueLabels(filter: {name: {eq: "narrative:core"}}) {
                    nodes {
                        issues {
                            nodes { identifier title state { name } dueDate priority }
                        }
                    }
                }
            }"""},
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            timeout=10.0,
        )
        data = resp.json()
        label_nodes = data.get("data", {}).get("issueLabels", {}).get("nodes", [])

        issues = []
        for ln in label_nodes:
            for iss in ln.get("issues", {}).get("nodes", []):
                if iss.get("state", {}).get("name") not in ("Done", "Canceled", "Cancelled"):
                    issues.append(iss)

        # Sort by priority (1=urgent first), then by due date
        issues.sort(key=lambda x: (x.get("priority", 99), x.get("dueDate") or "9999"))

        lines = []
        prio_icons = {1: "🔴", 2: "🟠", 3: "🟡", 4: "⚪"}
        for iss in issues[:10]:
            icon = prio_icons.get(iss.get("priority", 4), "⚪")
            due = iss.get("dueDate", "")
            due_str = f" (due {due})" if due else ""
            state = iss.get("state", {}).get("name", "")
            state_str = f" [{state}]" if state else ""
            lines.append(f"{icon} {iss['identifier']}: {iss['title'][:40]}{due_str}{state_str}")

        display = "🔥 Critical Path\n\n" + "\n".join(lines) if lines else "🔥 Critical Path — all clear ✅"
        return {"display": display, "count": len(issues), "issues": [i["identifier"] for i in issues[:10]]}
    except Exception as e:
        return {"display": f"⚠️ Critical path failed: {e}", "error": str(e)}


@app.post("/api/shortcuts/linear/update")
async def shortcut_linear_update(request: Request):
    """Quick-update an issue status. Body: {identifier: "POS-5", state: "Done"}"""
    _check_auth(request)
    body = await request.json()
    identifier = body.get("identifier", "")
    new_state = body.get("state", "Done")

    if not identifier:
        return {"display": "⚠️ identifier required (e.g. POS-5)", "error": "missing identifier"}

    try:
        # Search for the issue
        result = await _call_mcp_tool("linear_search_issues", {"search_query": identifier})
        # Extract issue ID from search
        import re
        id_match = re.search(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', result)

        if not id_match:
            return {"display": f"⚠️ Couldn't find {identifier}", "error": "not found"}

        issue_id = id_match.group()
        update_result = await _call_mcp_tool("linear_update_issue", {
            "issue_id": issue_id,
            "state_name": new_state,
        })
        return {"display": f"✅ {identifier} → {new_state}", "result": update_result[:200]}
    except Exception as e:
        return {"display": f"⚠️ Update failed: {e}", "error": str(e)}


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
