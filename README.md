# Antigravity Mobile

Mobile PWA for the Antigravity AI IDE — full Gemini chat with 98 MCP tools, conversation history, and continuation from your phone.

## Setup

The frontend is hosted on GitHub Pages. The backend runs on your Mac and is accessible via a Cloudflare tunnel.

### 1. Start the API server

```bash
cd antigravity-mobile
./setup.sh
```

### 2. Start a Cloudflare tunnel

```bash
cloudflared tunnel --url http://localhost:8766
```

### 3. Connect from your phone

Open `https://czhou732.github.io/antigravity-mobile/` on your phone, enter the tunnel URL and your access token.

## Architecture

- **Frontend**: Static PWA (HTML/CSS/JS) hosted on GitHub Pages
- **Backend**: FastAPI server running locally on your Mac
- **AI**: Gemini 2.5 Flash via Google GenAI SDK
- **Tools**: 98 MCP tools via proxy
- **Tunnel**: Cloudflare for HTTPS access
