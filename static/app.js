/* ── Antigravity Mobile — Client-Side Logic ────────────────────────────── */

let API_BASE = localStorage.getItem('ag_server') || '';
let TOKEN = localStorage.getItem('ag_token') || '';
let conversationHistory = [];
let isStreaming = false;
let currentFilePath = '';
let currentFileEditable = false;

// ── Auth ─────────────────────────────────────────────────────────────────

function authenticate() {
    const serverInput = document.getElementById('server-input');
    const tokenInput = document.getElementById('token-input');
    const server = serverInput.value.trim().replace(/\/+$/, '');
    const token = tokenInput.value.trim();
    if (!server || !token) {
        document.getElementById('auth-error').textContent = 'Both server URL and token are required.';
        return;
    }

    fetch(`${server}/api/health?token=${token}`)
        .then(r => {
            if (r.status === 401) throw new Error('Invalid token');
            return r.json();
        })
        .then(data => {
            API_BASE = server;
            TOKEN = token;
            localStorage.setItem('ag_server', server);
            localStorage.setItem('ag_token', token);
            showApp();
            checkHealth();
        })
        .catch(e => {
            document.getElementById('auth-error').textContent = 'Connection failed. Check URL and token.';
        });
}

function showApp() {
    document.getElementById('auth-screen').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');
}

// Check if we have stored credentials
if (TOKEN && API_BASE) {
    // Pre-fill the inputs
    document.getElementById('server-input').value = API_BASE;
    document.getElementById('token-input').value = '••••••••';

    fetch(`${API_BASE}/api/health?token=${TOKEN}`)
        .then(r => {
            if (r.ok) {
                showApp();
                checkHealth();
            } else {
                TOKEN = '';
                localStorage.removeItem('ag_token');
            }
        })
        .catch(() => {
            // Server might be down, show auth screen
            document.getElementById('auth-error').textContent = 'Server unreachable. Enter new tunnel URL.';
        });
}

// Enter key on inputs
document.getElementById('token-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') authenticate();
});
document.getElementById('server-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('token-input').focus();
});

// ── Health Check ────────────────────────────────────────────────────────

function checkHealth() {
    const dot = document.getElementById('status-dot');
    fetch(`${API_BASE}/api/health?token=${TOKEN}`)
        .then(r => r.json())
        .then(data => {
            dot.className = 'status-dot ' + (data.mcp_proxy === 'connected' ? 'connected' : 'error');
        })
        .catch(() => {
            dot.className = 'status-dot error';
        });
}

setInterval(checkHealth, 30000);

// ── Chat ────────────────────────────────────────────────────────────────

function autoResize(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

function handleKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
}

function quickSend(text) {
    document.getElementById('input').value = text;
    sendMessage();
}

function newConversation() {
    conversationHistory = [];
    continuationContext = '';
    continuationTitle = '';
    document.getElementById('messages').innerHTML = '';
    document.getElementById('welcome').classList.remove('hidden');
    document.getElementById('input').value = '';
    document.getElementById('input').style.height = 'auto';
    document.getElementById('input').placeholder = 'Message Antigravity...';
}

async function sendMessage() {
    const input = document.getElementById('input');
    const text = input.value.trim();
    if (!text || isStreaming) return;

    // Hide welcome, show messages
    document.getElementById('welcome').classList.add('hidden');

    // Add user message
    conversationHistory.push({ role: 'user', content: text });
    appendMessage('user', text);

    // Clear input
    input.value = '';
    input.style.height = 'auto';
    input.focus();

    // Show thinking indicator
    const thinkingEl = appendThinking();

    isStreaming = true;
    document.getElementById('send-btn').disabled = true;

    try {
        const resp = await fetch(`${API_BASE}/api/chat`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${TOKEN}`,
            },
            body: JSON.stringify({
                messages: conversationHistory,
                continuation_context: continuationContext || undefined,
            }),
        });

        if (!resp.ok) {
            throw new Error(`HTTP ${resp.status}`);
        }

        // Remove thinking indicator
        thinkingEl.remove();

        // Create assistant message container
        const msgContainer = createMessageElement('assistant');
        const contentEl = msgContainer.querySelector('.message-content');
        document.getElementById('messages').appendChild(msgContainer);

        let fullText = '';
        const toolCards = [];

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                const jsonStr = line.slice(6).trim();
                if (!jsonStr) continue;

                let event;
                try {
                    event = JSON.parse(jsonStr);
                } catch { continue; }

                if (event.type === 'tool_call') {
                    const card = createToolCard(event.name, event.args);
                    contentEl.appendChild(card);
                    toolCards.push(card);
                    scrollToBottom();
                } else if (event.type === 'tool_result') {
                    const card = toolCards[toolCards.length - 1];
                    if (card) {
                        finishToolCard(card, event.result);
                    }
                    scrollToBottom();
                } else if (event.type === 'text') {
                    fullText += event.content;
                    renderMarkdown(contentEl, fullText, toolCards);
                    scrollToBottom();
                } else if (event.type === 'error') {
                    fullText += `\n\n⚠️ Error: ${event.message}`;
                    renderMarkdown(contentEl, fullText, toolCards);
                } else if (event.type === 'done') {
                    // Finished
                }
            }
        }

        conversationHistory.push({ role: 'assistant', content: fullText });

    } catch (e) {
        thinkingEl.remove();
        appendMessage('assistant', `⚠️ Connection error: ${e.message}\n\nMake sure the server is running.`);
    } finally {
        isStreaming = false;
        document.getElementById('send-btn').disabled = false;
    }
}

// ── Message Rendering ───────────────────────────────────────────────────

function createMessageElement(role) {
    const msg = document.createElement('div');
    msg.className = `message ${role}`;
    const content = document.createElement('div');
    content.className = 'message-content';
    msg.appendChild(content);
    return msg;
}

function appendMessage(role, text) {
    const msg = createMessageElement(role);
    const content = msg.querySelector('.message-content');

    if (role === 'user') {
        content.textContent = text;
    } else {
        renderMarkdown(content, text);
    }

    document.getElementById('messages').appendChild(msg);
    scrollToBottom();
    return msg;
}

function appendThinking() {
    const el = document.createElement('div');
    el.className = 'thinking';
    el.innerHTML = `
        <div class="thinking-dots">
            <span></span><span></span><span></span>
        </div>
        <span>Thinking...</span>
    `;
    document.getElementById('messages').appendChild(el);
    scrollToBottom();
    return el;
}

function renderMarkdown(container, text, preserveCards) {
    // Save existing tool cards
    const existingCards = preserveCards || [];
    const savedCards = [];
    existingCards.forEach(card => {
        if (card.parentNode === container) {
            savedCards.push(card);
            card.remove();
        }
    });

    // Configure marked
    marked.setOptions({
        highlight: function(code, lang) {
            if (lang && hljs.getLanguage(lang)) {
                return hljs.highlight(code, { language: lang }).value;
            }
            return hljs.highlightAuto(code).value;
        },
        breaks: true,
        gfm: true,
    });

    container.innerHTML = marked.parse(text);

    // Re-insert tool cards at the beginning
    savedCards.reverse().forEach(card => {
        container.insertBefore(card, container.firstChild);
    });
}

function scrollToBottom() {
    const area = document.getElementById('chat-area');
    requestAnimationFrame(() => {
        area.scrollTop = area.scrollHeight;
    });
}

// ── Tool Call Cards ─────────────────────────────────────────────────────

function createToolCard(name, args) {
    const card = document.createElement('div');
    card.className = 'tool-card';
    card.innerHTML = `
        <div class="tool-card-header" onclick="this.parentElement.classList.toggle('expanded')">
            <span class="tool-card-icon">🔧</span>
            <span class="tool-card-name">${escapeHtml(name)}</span>
            <span class="tool-card-status running">⏳</span>
            <span class="tool-card-chevron">▶</span>
        </div>
        <div class="tool-card-body">
            <div class="tool-args"><strong>Args:</strong> ${escapeHtml(JSON.stringify(args, null, 2))}</div>
            <div class="tool-result"></div>
        </div>
    `;
    return card;
}

function finishToolCard(card, result) {
    const status = card.querySelector('.tool-card-status');
    status.textContent = '✅';
    status.className = 'tool-card-status done';

    const resultEl = card.querySelector('.tool-result');
    const truncated = result.length > 500 ? result.substring(0, 500) + '...' : result;
    resultEl.innerHTML = `\n<strong>Result:</strong>\n${escapeHtml(truncated)}`;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ── File Browser ────────────────────────────────────────────────────────

let fileDrawerOpen = false;

function toggleFileDrawer() {
    fileDrawerOpen = !fileDrawerOpen;
    document.getElementById('file-drawer').classList.toggle('open', fileDrawerOpen);
    document.getElementById('file-overlay').classList.toggle('open', fileDrawerOpen);

    if (fileDrawerOpen) {
        loadFiles('~/Research');
    }
}

async function loadFiles(path) {
    const breadcrumb = document.getElementById('breadcrumb');
    const list = document.getElementById('file-list');
    breadcrumb.textContent = path;
    list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-tertiary)">Loading...</div>';

    try {
        const resp = await fetch(
            `${API_BASE}/api/files?path=${encodeURIComponent(path)}&token=${TOKEN}`
        );
        const data = await resp.json();

        list.innerHTML = '';

        // Back button
        if (path !== '~/Research') {
            const backItem = document.createElement('div');
            backItem.className = 'file-item';
            backItem.innerHTML = `
                <span class="file-icon">⬆️</span>
                <div class="file-info"><span class="file-name">..</span></div>
            `;
            const parentPath = data.path.split('/').slice(0, -1).join('/');
            backItem.onclick = () => loadFiles(parentPath);
            list.appendChild(backItem);
        }

        // Root shortcuts
        if (path === '~/Research') {
            const roots = [
                { name: 'Research', path: '~/Research', icon: '🔬' },
                { name: 'Projects', path: '~/Projects', icon: '💻' },
                { name: 'Documents', path: '~/Documents', icon: '📄' },
                { name: 'Antigravity', path: '~/.gemini/antigravity', icon: '⚡' },
            ];
            roots.forEach(root => {
                const item = document.createElement('div');
                item.className = 'file-item';
                item.innerHTML = `
                    <span class="file-icon">${root.icon}</span>
                    <div class="file-info"><span class="file-name">${root.name}</span></div>
                `;
                item.onclick = () => loadFiles(root.path);
                list.appendChild(item);
            });
            return;
        }

        // File entries
        for (const entry of data.entries) {
            const item = document.createElement('div');
            item.className = 'file-item';
            const icon = entry.is_dir ? '📁' : getFileIcon(entry.ext || '');
            const meta = entry.is_dir
                ? `${entry.children || 0} items`
                : formatSize(entry.size || 0);

            item.innerHTML = `
                <span class="file-icon">${icon}</span>
                <div class="file-info">
                    <span class="file-name">${escapeHtml(entry.name)}</span>
                    <span class="file-meta">${meta}</span>
                </div>
            `;

            if (entry.is_dir) {
                item.onclick = () => loadFiles(entry.path);
            } else {
                item.onclick = () => openFile(entry.path, entry.name);
            }

            list.appendChild(item);
        }
    } catch (e) {
        list.innerHTML = `<div style="padding:20px;text-align:center;color:var(--red)">Error: ${e.message}</div>`;
    }
}

function getFileIcon(ext) {
    const icons = {
        '.py': '🐍', '.js': '📜', '.ts': '📘', '.json': '📋',
        '.md': '📝', '.txt': '📄', '.csv': '📊', '.html': '🌐',
        '.css': '🎨', '.sh': '⚙️', '.yaml': '⚙️', '.yml': '⚙️',
        '.tex': '📐', '.pdf': '📕', '.docx': '📘', '.pptx': '📙',
        '.png': '🖼️', '.jpg': '🖼️', '.svg': '🖼️',
        '.r': '📈', '.R': '📈', '.ipynb': '📓',
    };
    return icons[ext] || '📄';
}

function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

async function openFile(path, name) {
    toggleFileDrawer();
    const modal = document.getElementById('file-modal');
    const title = document.getElementById('file-modal-title');
    const content = document.getElementById('file-modal-content');
    const saveBtn = document.getElementById('file-save-btn');

    title.textContent = name;
    content.textContent = 'Loading...';
    modal.classList.remove('hidden');
    currentFilePath = path;

    try {
        const resp = await fetch(
            `${API_BASE}/api/file?path=${encodeURIComponent(path)}&token=${TOKEN}`
        );
        const data = await resp.json();
        content.textContent = data.content;

        // Enable editing for text files
        const editableExts = ['.py', '.js', '.ts', '.json', '.md', '.txt', '.css', '.html', '.sh', '.yaml', '.yml', '.tex', '.csv', '.r', '.R'];
        const ext = '.' + name.split('.').pop();
        if (editableExts.includes(ext)) {
            content.contentEditable = 'true';
            saveBtn.style.display = 'flex';
            currentFileEditable = true;
        } else {
            content.contentEditable = 'false';
            saveBtn.style.display = 'none';
            currentFileEditable = false;
        }

        // Apply syntax highlighting for code
        if (hljs.getLanguage(ext.slice(1))) {
            content.innerHTML = hljs.highlight(data.content, { language: ext.slice(1) }).value;
        }
    } catch (e) {
        content.textContent = `Error loading file: ${e.message}`;
    }
}

async function saveFile() {
    if (!currentFilePath || !currentFileEditable) return;
    const content = document.getElementById('file-modal-content');
    const text = content.innerText;

    try {
        await fetch(`${API_BASE}/api/file`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${TOKEN}`,
            },
            body: JSON.stringify({ path: currentFilePath, content: text }),
        });
        // Flash save indicator
        const btn = document.getElementById('file-save-btn');
        btn.textContent = '✅';
        setTimeout(() => btn.textContent = '💾', 1500);
    } catch (e) {
        alert('Save failed: ' + e.message);
    }
}

function closeFileModal() {
    document.getElementById('file-modal').classList.add('hidden');
    currentFilePath = '';
}

// ── Conversation History ────────────────────────────────────────────────

let historyDrawerOpen = false;

function toggleHistoryDrawer() {
    historyDrawerOpen = !historyDrawerOpen;
    document.getElementById('history-drawer').classList.toggle('open', historyDrawerOpen);
    document.getElementById('history-overlay').classList.toggle('open', historyDrawerOpen);

    if (historyDrawerOpen) {
        loadConversations();
    }
}

async function loadConversations() {
    const list = document.getElementById('history-list');
    list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-tertiary)">Loading...</div>';

    try {
        const resp = await fetch(`${API_BASE}/api/conversations?token=${TOKEN}`);
        const data = await resp.json();

        list.innerHTML = '';

        for (const conv of data.conversations) {
            const item = document.createElement('div');
            item.className = 'history-item';

            const timeAgo = formatTimeAgo(conv.last_active);
            item.innerHTML = `
                <div class="history-item-title">${escapeHtml(conv.title)}</div>
                <div class="history-item-time">${timeAgo}</div>
            `;
            item.onclick = () => openConversation(conv.id, conv.title);
            list.appendChild(item);
        }

        if (data.conversations.length === 0) {
            list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-tertiary)">No conversations found</div>';
        }
    } catch (e) {
        list.innerHTML = `<div style="padding:20px;text-align:center;color:var(--red)">Error: ${e.message}</div>`;
    }
}

async function openConversation(convId, title) {
    toggleHistoryDrawer();

    const modal = document.getElementById('conv-modal');
    const titleEl = document.getElementById('conv-modal-title');
    const content = document.getElementById('conv-modal-content');

    titleEl.textContent = title;
    content.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-tertiary)">Loading...</div>';
    modal.classList.remove('hidden');

    try {
        const resp = await fetch(`${API_BASE}/api/conversation/${convId}?token=${TOKEN}`);
        const data = await resp.json();

        let html = '';

        // Continue button
        html += `<button class="continue-btn" onclick="continueConversation('${convId}', '${escapeHtml(title)}')">
            ↗ Continue this conversation
        </button>`;

        // Render digest as markdown
        if (data.digest) {
            marked.setOptions({ breaks: true, gfm: true });
            html += '<div class="conv-digest">' + marked.parse(data.digest) + '</div>';
        }

        // List artifacts
        if (data.artifacts && data.artifacts.length > 0) {
            html += '<div class="conv-artifacts"><h3 style="margin:16px 0 8px;font-size:14px;color:var(--text-secondary)">Artifacts</h3>';
            for (const art of data.artifacts) {
                const icon = getFileIcon(art.ext || '');
                const size = formatSize(art.size || 0);
                if (art.is_readable) {
                    html += `<div class="file-item" onclick="openFile('${escapeHtml(art.path)}', '${escapeHtml(art.name)}')">
                        <span class="file-icon">${icon}</span>
                        <div class="file-info">
                            <span class="file-name">${escapeHtml(art.name)}</span>
                            <span class="file-meta">${size}</span>
                        </div>
                    </div>`;
                } else {
                    html += `<div class="file-item" style="opacity:0.5;cursor:default">
                        <span class="file-icon">${icon}</span>
                        <div class="file-info">
                            <span class="file-name">${escapeHtml(art.name)}</span>
                            <span class="file-meta">${size}</span>
                        </div>
                    </div>`;
                }
            }
            html += '</div>';
        }

        content.innerHTML = html || '<div style="padding:20px;color:var(--text-tertiary)">No digest available for this conversation.</div>';
    } catch (e) {
        content.innerHTML = `<div style="padding:20px;color:var(--red)">Error: ${e.message}</div>`;
    }
}

// ── Conversation Continuation ───────────────────────────────────────────

let continuationContext = '';
let continuationTitle = '';

async function continueConversation(convId, title) {
    closeConvModal();

    // Clear current chat
    conversationHistory = [];
    const chatArea = document.getElementById('chat-area');
    const welcome = document.getElementById('welcome');
    if (welcome) welcome.classList.add('hidden');

    // Remove previous messages
    chatArea.querySelectorAll('.message').forEach(el => el.remove());

    // Show loading state
    const loadingEl = document.createElement('div');
    loadingEl.className = 'message assistant';
    loadingEl.innerHTML = '<div class="message-content" style="color:var(--text-tertiary)">Loading conversation context...</div>';
    chatArea.appendChild(loadingEl);

    try {
        const resp = await fetch(`${API_BASE}/api/conversation/${convId}/context?token=${TOKEN}`);
        const data = await resp.json();

        continuationContext = data.context;
        continuationTitle = title;

        // Replace loading with context notification
        loadingEl.innerHTML = `<div class="message-content">
            <div class="continuation-badge">↗ Continuing: <strong>${escapeHtml(title)}</strong></div>
            <div style="font-size:12px;color:var(--text-tertiary);margin-top:4px">${(data.context_length / 1000).toFixed(1)}K context loaded — send a message to continue</div>
        </div>`;

        // Focus the input
        document.getElementById('input').focus();
        document.getElementById('input').placeholder = `Continue: ${title.substring(0, 40)}...`;
    } catch (e) {
        loadingEl.innerHTML = `<div class="message-content" style="color:var(--red)">Failed to load context: ${e.message}</div>`;
    }
}

function closeConvModal() {
    document.getElementById('conv-modal').classList.add('hidden');
}

function formatTimeAgo(isoStr) {
    if (!isoStr) return '';
    try {
        const date = new Date(isoStr);
        const now = new Date();
        const diffMs = now - date;
        const diffMin = Math.floor(diffMs / 60000);
        const diffHr = Math.floor(diffMs / 3600000);
        const diffDay = Math.floor(diffMs / 86400000);

        if (diffMin < 1) return 'just now';
        if (diffMin < 60) return `${diffMin}m ago`;
        if (diffHr < 24) return `${diffHr}h ago`;
        if (diffDay < 7) return `${diffDay}d ago`;
        if (diffDay < 30) return `${Math.floor(diffDay/7)}w ago`;
        return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    } catch { return isoStr.substring(0, 10); }
}

// ── Shortcuts Panel ─────────────────────────────────────────────────────

let shortcutsOpen = false;

function toggleShortcuts() {
    shortcutsOpen = !shortcutsOpen;
    document.getElementById('shortcuts-sheet').classList.toggle('open', shortcutsOpen);
    document.getElementById('shortcuts-overlay').classList.toggle('open', shortcutsOpen);
}

async function runShortcut(name) {
    toggleShortcuts();
    document.getElementById('welcome').classList.add('hidden');

    const labels = {
        dashboard: '📊 Dashboard',
        goals: '🎯 Goals',
        energy: '🔋 Energy',
        linear: '📋 Linear',
        commitments: '🤝 Commitments',
        circadian: '🌙 Circadian',
    };

    const thinkingEl = appendThinking();

    try {
        const resp = await fetch(`${API_BASE}/api/shortcuts/${name}?token=${TOKEN}`);
        const data = await resp.json();
        thinkingEl.remove();

        const msg = createMessageElement('assistant');
        const content = msg.querySelector('.message-content');

        let text = `**${labels[name] || name}**\n\n`;
        if (data.markdown) {
            text += data.markdown;
        } else if (data.display) {
            text += '```\n' + data.display + '\n```';
        } else if (data.text) {
            text += data.text;
        } else {
            text += '```json\n' + JSON.stringify(data, null, 2) + '\n```';
        }

        renderMarkdown(content, text);
        addCopyButtons(content);
        document.getElementById('messages').appendChild(msg);
        scrollToBottom();
    } catch (e) {
        thinkingEl.remove();
        appendMessage('assistant', `⚠️ Shortcut failed: ${e.message}`);
    }
}

// ── Settings ────────────────────────────────────────────────────────────

async function openSettings() {
    const modal = document.getElementById('settings-modal');
    document.getElementById('settings-server').textContent = API_BASE || '—';
    document.getElementById('settings-status').innerHTML = '<span style="color:var(--text-tertiary)">Checking...</span>';
    document.getElementById('settings-tools').textContent = '—';
    modal.classList.remove('hidden');

    try {
        const resp = await fetch(`${API_BASE}/api/health?token=${TOKEN}`);
        const data = await resp.json();
        const statusColor = data.mcp_proxy === 'connected' ? 'var(--green)' : 'var(--red)';
        document.getElementById('settings-status').innerHTML = `<span style="color:${statusColor}">${data.status} — MCP ${data.mcp_proxy}</span>`;
        document.getElementById('settings-tools').textContent = `${data.mcp_info?.tools_exposed || '?'} tools`;
    } catch (e) {
        document.getElementById('settings-status').innerHTML = `<span style="color:var(--red)">Unreachable</span>`;
    }
}

function closeSettings() {
    document.getElementById('settings-modal').classList.add('hidden');
}

function disconnect() {
    localStorage.removeItem('ag_server');
    localStorage.removeItem('ag_token');
    localStorage.removeItem('ag_history');
    API_BASE = '';
    TOKEN = '';
    conversationHistory = [];
    document.getElementById('app').classList.add('hidden');
    document.getElementById('auth-screen').classList.remove('hidden');
    document.getElementById('server-input').value = '';
    document.getElementById('token-input').value = '';
    closeSettings();
}

// ── Conversation Persistence ────────────────────────────────────────────

function saveConversation() {
    try {
        const data = {
            messages: conversationHistory.slice(-40), // Keep last 40 messages
            continuation: continuationContext ? { ctx: continuationContext.substring(0, 500), title: continuationTitle } : null,
            timestamp: Date.now(),
        };
        localStorage.setItem('ag_history', JSON.stringify(data));
    } catch (e) {
        // localStorage full, silently skip
    }
}

function restoreConversation() {
    try {
        const raw = localStorage.getItem('ag_history');
        if (!raw) return;
        const data = JSON.parse(raw);

        // Only restore if less than 2 hours old
        if (Date.now() - data.timestamp > 2 * 60 * 60 * 1000) {
            localStorage.removeItem('ag_history');
            return;
        }

        if (data.messages && data.messages.length > 0) {
            conversationHistory = data.messages;
            document.getElementById('welcome').classList.add('hidden');

            for (const msg of data.messages) {
                const el = createMessageElement(msg.role);
                const content = el.querySelector('.message-content');
                if (msg.role === 'user') {
                    content.textContent = msg.content;
                } else {
                    renderMarkdown(content, msg.content);
                    addCopyButtons(content);
                }
                document.getElementById('messages').appendChild(el);
            }
            scrollToBottom();
        }

        if (data.continuation) {
            continuationContext = data.continuation.ctx;
            continuationTitle = data.continuation.title;
        }
    } catch (e) {
        // Corrupted data, clear it
        localStorage.removeItem('ag_history');
    }
}

// Hook persistence into sendMessage — save after each exchange
const _originalSendMessage = sendMessage;
sendMessage = async function() {
    await _originalSendMessage();
    saveConversation();
};

// Restore on load
if (TOKEN && API_BASE) {
    setTimeout(restoreConversation, 100);
}

// ── Code Copy Buttons ───────────────────────────────────────────────────

function addCopyButtons(container) {
    container.querySelectorAll('pre').forEach(pre => {
        if (pre.querySelector('.code-copy-btn')) return;
        const btn = document.createElement('button');
        btn.className = 'code-copy-btn';
        btn.textContent = '📋';
        btn.onclick = (e) => {
            e.stopPropagation();
            const code = pre.querySelector('code');
            navigator.clipboard.writeText(code ? code.textContent : pre.textContent).then(() => {
                btn.textContent = '✅';
                setTimeout(() => btn.textContent = '📋', 1500);
            });
            if (navigator.vibrate) navigator.vibrate(30);
        };
        pre.style.position = 'relative';
        pre.appendChild(btn);
    });
}

// Patch renderMarkdown to add copy buttons automatically
const _originalRenderMarkdown = renderMarkdown;
renderMarkdown = function(container, text, preserveCards) {
    _originalRenderMarkdown(container, text, preserveCards);
    addCopyButtons(container);
};

// ── PWA Install ─────────────────────────────────────────────────────────

let deferredPrompt = null;

window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredPrompt = e;
});

// ── Service Worker ──────────────────────────────────────────────────────

if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('sw.js').catch(() => {});
}

