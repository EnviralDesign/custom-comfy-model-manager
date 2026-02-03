document.addEventListener('DOMContentLoaded', () => {
    const els = {
        badge: document.getElementById('session-status-badge'),
        activeUi: document.getElementById('active-session-ui'),
        inactiveUi: document.getElementById('inactive-session-ui'),
        keyVal: document.getElementById('api-key-value'),
        baseUrl: document.getElementById('base-url-display'),
        countdown: document.getElementById('session-countdown'),
        enableBtn: document.getElementById('enable-session-btn'),
        endBtn: document.getElementById('end-session-btn'),

        agentIndicator: document.getElementById('agent-indicator'),
        agentText: document.getElementById('agent-status-text'),
        agentDetails: document.getElementById('agent-details'),
        agentConnectedUi: document.getElementById('agent-connected-ui'),
        hostName: document.getElementById('agent-hostname'),
        osName: document.getElementById('agent-os'),
        heartbeat: document.getElementById('agent-heartbeat'),

        actionsPanel: document.getElementById('actions-panel'),
        btnInstallComfy: document.getElementById('btn-install-comfy'),
        taskList: document.getElementById('remote-task-list')
    };

    let pollInterval = null;
    let expiryTime = null;

    // --- Actions ---

    async function fetchStatus() {
        try {
            const res = await fetch('/api/remote/status');
            const data = await res.json();
            render(data);
        } catch (e) {
            console.error('Status fetch failed', e);
        }
    }

    async function enableSession() {
        try {
            const res = await fetch('/api/remote/session/enable', { method: 'POST' });
            if (res.ok) {
                fetchStatus();
            }
        } catch (e) {
            alert('Failed to enable session');
        }
    }

    async function endSession() {
        if (!confirm('Are you sure you want to end the remote session? The agent will be disconnected.')) return;

        try {
            const res = await fetch('/api/remote/session/end', { method: 'POST' });
            if (res.ok) {
                fetchStatus();
                // Clear tasks view
                els.taskList.innerHTML = '<div class="text-secondary text-sm">Session ended.</div>';
            }
        } catch (e) {
            alert('Failed to end session');
        }
    }

    async function enqueueTask(type, payload = {}, label = "") {
        try {
            const res = await fetch('/api/remote/tasks/enqueue?label=' + encodeURIComponent(label), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ type, payload })
            });
            if (res.ok) {
                fetchTasks();
            } else {
                alert('Failed to enqueue task');
            }
        } catch (e) {
            console.error(e);
            alert('Error enqueueing task');
        }
    }

    async function fetchTasks() {
        // Only fetch if session active
        if (els.activeUi.style.display === 'none') return;

        try {
            const res = await fetch('/api/remote/tasks');
            const tasks = await res.json();
            renderTasks(tasks);
        } catch (e) {
            console.error('Task fetch failed', e);
        }
    }

    // --- Rendering ---

    function render(data) {
        // Base URL
        els.baseUrl.textContent = data.remote_base_url;

        // Session State
        if (data.is_active) {
            els.badge.textContent = 'ARMED / ACTIVE';
            els.badge.className = 'status-badge status-active';

            els.activeUi.style.display = 'block';
            els.inactiveUi.style.display = 'none';

            els.keyVal.textContent = data.api_key;
            expiryTime = new Date(data.expires_at);
        } else {
            els.badge.textContent = 'OFFLINE';
            els.badge.className = 'status-badge status-off';

            els.activeUi.style.display = 'none';
            els.inactiveUi.style.display = 'block';
            expiryTime = null;
        }

        // Agent State
        if (data.agent_connected) {
            els.agentIndicator.className = 'agent-status-indicator agent-connected';
            els.agentText.textContent = 'Connected';
            els.agentDetails.style.display = 'none';
            els.agentConnectedUi.style.display = 'block';

            els.actionsPanel.style.display = 'block';

            els.hostName.textContent = data.agent_info.hostname || 'Unknown';
            els.osName.textContent = data.agent_info.os || 'Unknown';

            if (data.last_heartbeat) {
                const hb = new Date(data.last_heartbeat);
                els.heartbeat.textContent = hb.toLocaleTimeString();
            }
        } else {
            els.agentIndicator.className = 'agent-status-indicator';
            els.agentText.textContent = 'Disconnected';
            els.agentDetails.style.display = 'block';
            els.agentConnectedUi.style.display = 'none';
            els.actionsPanel.style.display = 'none';
        }
    }

    function renderTasks(tasks) {
        if (tasks.length === 0) {
            els.taskList.innerHTML = '<div class="text-secondary text-sm">No tasks queued.</div>';
            return;
        }

        els.taskList.innerHTML = tasks.slice().reverse().map(t => {
            let statusClass = 'text-secondary';
            if (t.status === 'running') statusClass = 'text-primary';
            if (t.status === 'completed') statusClass = 'text-success';
            if (t.status === 'failed') statusClass = 'text-danger';

            const progress = t.progress ? `(${Math.round(t.progress * 100)}%)` : '';

            return `
            <div class="queue-item" style="padding: 8px; border-bottom: 1px solid var(--border-dim);">
                <div style="display:flex; justify-content:space-between; margin-bottom: 4px;">
                    <span style="font-weight:500;">${t.label || t.type}</span>
                    <span class="${statusClass} text-sm" style="text-transform:uppercase; font-size:0.75rem; font-weight:600;">
                        ${t.status} ${progress}
                    </span>
                </div>
                <div class="text-xs text-secondary" style="white-space: pre-wrap;">${t.message || ''}</div>
                ${t.error ? `<div class="text-xs text-danger mt-1">${t.error}</div>` : ''}
            </div>
            `;
        }).join('');
    }

    // --- Utils ---

    window.copyApiKey = () => {
        const key = els.keyVal.textContent;
        navigator.clipboard.writeText(key).then(() => {
            const btn = document.querySelector('.copy-btn');
            const orig = btn.textContent;
            btn.textContent = 'Copied!';
            setTimeout(() => btn.textContent = orig, 2000);
        });
    };

    function updateCountdown() {
        if (!expiryTime) {
            els.countdown.textContent = '--:--';
            return;
        }

        const now = new Date();
        const diff = expiryTime - now;

        if (diff <= 0) {
            els.countdown.textContent = 'EXPIRED';
            fetchStatus(); // Sync with server cleanup
            return;
        }

        const m = Math.floor(diff / 60000);
        const s = Math.floor((diff % 60000) / 1000);
        els.countdown.textContent = `${m}m ${s.toString().padStart(2, '0')}s`;
    }

    // --- Init ---

    els.enableBtn.addEventListener('click', enableSession);
    els.endBtn.addEventListener('click', endSession);

    els.btnInstallComfy.addEventListener('click', () => {
        enqueueTask('COMFY_GIT_CLONE', {}, 'Install ComfyUI');
    });

    // Initial load
    fetchStatus();
    fetchTasks();

    // Polling (every 2s is enough for status)
    pollInterval = setInterval(() => {
        fetchStatus();
        fetchTasks();
    }, 2000);

    // Countdown ticker (every 1s)
    setInterval(updateCountdown, 1000);
});
