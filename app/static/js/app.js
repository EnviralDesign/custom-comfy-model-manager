/**
 * ComfyUI Model Manager - Core Application JS
 * Handles WebSocket connection, utilities, and shared state
 */

const App = {
    ws: null,
    wsReconnectMs: 1000,
    queueTasks: [],

    init() {
        this.connectWebSocket();
        this.setupQueuePanel();
    },

    // WebSocket for real-time updates
    connectWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;

        this.ws = new WebSocket(wsUrl);

        this.ws.onopen = () => {
            console.log('WebSocket connected');
            this.wsReconnectMs = 1000;
        };

        this.ws.onmessage = (event) => {
            const msg = JSON.parse(event.data);
            this.handleWsMessage(msg);
        };

        this.ws.onclose = () => {
            console.log('WebSocket disconnected, reconnecting...');
            setTimeout(() => this.connectWebSocket(), this.wsReconnectMs);
            this.wsReconnectMs = Math.min(this.wsReconnectMs * 2, 30000);
        };
    },

    handleWsMessage(msg) {
        switch (msg.type) {
            case 'queue_progress':
                this.updateQueueProgress(msg.data);
                break;
            case 'task_complete':
                this.loadQueueTasks();
                break;
        }
        // Dispatch custom event for page-specific handlers
        document.dispatchEvent(new CustomEvent('ws:' + msg.type, { detail: msg.data }));
    },

    // Queue Panel
    setupQueuePanel() {
        const panel = document.getElementById('queue-panel');
        const header = document.querySelector('.queue-header');

        if (header && panel) {
            header.addEventListener('click', () => {
                panel.classList.toggle('expanded');
            });
        }

        // Load initial tasks
        this.loadQueueTasks();
    },

    async loadQueueTasks() {
        try {
            const tasks = await this.api('GET', '/queue/tasks');
            this.queueTasks = tasks;
            this.renderQueue();
        } catch (err) {
            console.error('Failed to load queue:', err);
        }
    },

    renderQueue() {
        const list = document.getElementById('queue-list');
        if (!list) return;

        const pending = this.queueTasks.filter(t => t.status === 'pending' || t.status === 'running');

        if (pending.length === 0) {
            list.innerHTML = '<div class="empty-queue">No pending tasks</div>';
            return;
        }

        list.innerHTML = pending.map(task => `
            <div class="queue-item queue-item-${task.status}">
                <span class="queue-icon">${task.task_type === 'copy' ? '📋' : '🗑️'}</span>
                <div class="queue-info">
                    <div class="queue-path">${this.truncatePath(task.src_relpath || task.dst_relpath)}</div>
                    <div class="queue-meta">
                        ${task.status === 'running' ? '⏳ Running...' : '⏸️ Pending'}
                        ${task.size_bytes ? ' • ' + this.formatBytes(task.size_bytes) : ''}
                    </div>
                </div>
                <button class="btn-icon btn-cancel" data-task-id="${task.id}" title="Cancel">✕</button>
            </div>
        `).join('');

        // Bind cancel buttons
        list.querySelectorAll('.btn-cancel').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.cancelTask(btn.dataset.taskId);
            });
        });
    },

    async cancelTask(taskId) {
        try {
            await this.api('POST', `/queue/cancel/${taskId}`);
            await this.loadQueueTasks();
        } catch (err) {
            console.error('Cancel failed:', err);
        }
    },

    truncatePath(path) {
        if (!path) return '';
        if (path.length <= 40) return path;
        return '...' + path.slice(-37);
    },

    updateQueueProgress(data) {
        // Update queue badge if exists
        const badge = document.querySelector('.queue-count');
        if (badge) badge.textContent = data.pending || 0;
    },

    // Utilities
    formatBytes(bytes) {
        if (bytes === 0 || bytes === null) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    },

    formatDate(isoString) {
        return new Date(isoString).toLocaleString();
    },

    async api(method, path, data = null) {
        const opts = {
            method,
            headers: { 'Content-Type': 'application/json' },
        };
        if (data) opts.body = JSON.stringify(data);

        const res = await fetch('/api' + path, opts);
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || res.statusText);
        }
        return res.json();
    }
};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => App.init());
