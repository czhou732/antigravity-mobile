"""
Antigravity Mobile — Configuration
"""
import os

# Server
HOST = "127.0.0.1"
PORT = 8766

# MCP Proxy (existing infrastructure)
MCP_PROXY_URL = "http://127.0.0.1:8765"

# Gemini
GEMINI_MODEL = "gemini-2.5-flash"

# Paths
GEMINI_MD_PATH = os.path.expanduser("~/.gemini/GEMINI.md")
KNOWLEDGE_GRAPH_PATH = os.path.expanduser(
    "~/.gemini/antigravity/memory/knowledge_graph.json"
)
KEYCHAIN_UTILS_PATH = os.path.expanduser(
    "~/.gemini/antigravity/scratch/keychain_utils.py"
)

# Allowed workspace roots (file browser)
ALLOWED_ROOTS = [
    os.path.expanduser("~/Research"),
    os.path.expanduser("~/Projects"),
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/.gemini/antigravity"),
]

# Auth
KEYCHAIN_SERVICE = "antigravity-mobile-token"
KEYCHAIN_USER = "peterzhou"

# Gemini API key
GEMINI_KEYCHAIN_SERVICE = "google-gemini-api"
GEMINI_KEYCHAIN_USER = "peterzhou"
