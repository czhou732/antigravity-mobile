#!/bin/bash
# ── Antigravity Mobile — Setup Script ────────────────────────────────────
# Run once: creates venv, generates auth token, sets up Cloudflare Tunnel.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "⚡ Setting up Antigravity Mobile..."

# ── 1. Python venv ───────────────────────────────────────────────────────
echo ""
echo "📦 Creating Python virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install -q -r requirements.txt
echo "   ✅ Dependencies installed"

# ── 2. Generate auth token ───────────────────────────────────────────────
echo ""
echo "🔑 Generating auth token..."
TOKEN=$(python3 -c "
import keyring, secrets
existing = keyring.get_password('antigravity-mobile-token', 'peterzhou')
if existing:
    print(existing)
else:
    token = secrets.token_urlsafe(48)
    keyring.set_password('antigravity-mobile-token', 'peterzhou', token)
    print(token)
")
echo "   ✅ Auth token (save this for your phone):"
echo ""
echo "   ╔══════════════════════════════════════════════════════════════╗"
echo "   ║  $TOKEN"
echo "   ╚══════════════════════════════════════════════════════════════╝"
echo ""

# ── 3. Generate PWA icons ────────────────────────────────────────────────
echo "🎨 Generating PWA icons..."
if [ -f "$SCRIPT_DIR/static/icon-source.png" ] || [ -f "$SCRIPT_DIR/static/icon-192.png" ]; then
    echo "   ✅ Icons already exist"
else
    # Create minimal SVG icons as fallback
    python3 -c "
import struct, zlib

def create_png(size, path):
    # Create a simple purple gradient icon
    pixels = []
    for y in range(size):
        row = [0]  # filter byte
        for x in range(size):
            # Purple gradient
            r = int(108 + (x/size) * 50)
            g = int(92 + (x/size) * 50)
            b = int(231 - (y/size) * 30)
            a = 255
            # Round corners
            cx, cy = size/2, size/2
            radius = size * 0.42
            corner_r = size * 0.15
            dx = abs(x - cx) - (radius - corner_r)
            dy = abs(y - cy) - (radius - corner_r)
            if dx > 0 and dy > 0:
                if (dx*dx + dy*dy) > corner_r*corner_r:
                    a = 0
            elif abs(x - cx) > radius or abs(y - cy) > radius:
                a = 0
            row.extend([r, g, b, a])
        pixels.append(bytes(row))

    raw = b''.join(pixels)
    def chunk(ctype, data):
        c = ctype + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

    header = b'\\x89PNG\\r\\n\\x1a\\n'
    ihdr = chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
    idat = chunk(b'IDAT', zlib.compress(raw))
    iend = chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(header + ihdr + idat + iend)

create_png(192, '$SCRIPT_DIR/static/icon-192.png')
create_png(512, '$SCRIPT_DIR/static/icon-512.png')
    "
    echo "   ✅ Icons generated"
fi

# ── 4. Cloudflare Tunnel ─────────────────────────────────────────────────
echo ""
echo "🌐 Setting up Cloudflare Tunnel..."
if command -v cloudflared &> /dev/null; then
    echo "   cloudflared found at $(which cloudflared)"
    echo ""
    echo "   To create a quick tunnel (no domain needed), run:"
    echo "   cloudflared tunnel --url http://localhost:8766"
    echo ""
    echo "   For a persistent named tunnel:"
    echo "   cloudflared tunnel create antigravity-mobile"
    echo "   Then configure ~/.cloudflared/config.yml"
else
    echo "   ⚠️  cloudflared not found. Install with:"
    echo "   brew install cloudflared"
fi

# ── 5. LaunchAgent ───────────────────────────────────────────────────────
echo ""
echo "🔧 Installing LaunchAgent..."
PLIST_PATH="$HOME/Library/LaunchAgents/com.peterzhou.antigravity-mobile.plist"
cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.peterzhou.antigravity-mobile</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SCRIPT_DIR/venv/bin/python</string>
        <string>$SCRIPT_DIR/server.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/logs/server.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/logs/server_err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
PLIST
echo "   ✅ LaunchAgent installed at $PLIST_PATH"

# ── 6. Start ─────────────────────────────────────────────────────────────
echo ""
echo "🚀 Starting Antigravity Mobile..."
launchctl bootout gui/$(id -u) "$PLIST_PATH" 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$PLIST_PATH"
sleep 2

# Health check
if curl -s http://localhost:8766/api/health | grep -q "ok"; then
    echo "   ✅ Server is running on http://localhost:8766"
else
    echo "   ⚠️  Server may still be starting. Check logs:"
    echo "   tail -f $SCRIPT_DIR/logs/server_err.log"
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo " ⚡ Antigravity Mobile is ready!"
echo ""
echo " Local:   http://localhost:8766"
echo " Token:   $TOKEN"
echo ""
echo " To access from your phone:"
echo "   cloudflared tunnel --url http://localhost:8766"
echo " Then open the URL on your phone and paste the token."
echo "═══════════════════════════════════════════════════════════════"
