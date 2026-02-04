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
            case 'task_started':
                this.loadQueueTasks();  // Reload to show running status  
                break;
            case 'task_complete':
                this.loadQueueTasks();  // Reload to remove completed task
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

        const haltBtn = document.getElementById('halt-all-btn');
        if (haltBtn) {
            haltBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (confirm('Are you sure you want to halt ALL tasks?')) {
                    this.cancelAllTasks();
                }
            });
        }

        // Load initial tasks
        this.loadQueueTasks();
    },

    async cancelAllTasks() {
        try {
            await this.api('POST', '/queue/cancel/all');
            await this.loadQueueTasks();
        } catch (err) {
            console.error('Halt all failed:', err);
        }
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

        // Filter active tasks and sort: running first, then pending by oldest first
        const pending = this.queueTasks
            .filter(t => t.status === 'pending' || t.status === 'running')
            .sort((a, b) => {
                // Running tasks come first
                if (a.status === 'running' && b.status !== 'running') return -1;
                if (b.status === 'running' && a.status !== 'running') return 1;
                // Then sort by created_at (oldest first)
                return new Date(a.created_at) - new Date(b.created_at);
            });

        if (pending.length === 0) {
            list.innerHTML = '<div class="empty-queue">No pending tasks</div>';
            return;
        }

        list.innerHTML = pending.map(task => {
            const progress = task.size_bytes > 0
                ? Math.round((task.bytes_transferred || 0) / task.size_bytes * 100)
                : 0;
            const isRunning = task.status === 'running';

            const icon = task.task_type === 'copy'
                ? '📋'
                : task.task_type === 'move'
                    ? '📁'
                    : task.task_type === 'verify'
                        ? '🔍'
                        : '🗑️';

            return `
            <div class="queue-item queue-item-${task.status}" data-task-id="${task.id}">
                <span class="queue-icon">${icon}</span>
                <div class="queue-info">
                    <div class="queue-path">${this.truncatePath(task.src_relpath || task.dst_relpath || task.verify_folder)}</div>
                    <div class="queue-meta">
                        ${isRunning
                    ? `⚡ ${progress}% • ${this.formatBytes(task.bytes_transferred || 0)} / ${this.formatBytes(task.size_bytes)}`
                    : `⏳ Pending${task.size_bytes ? ' • ' + this.formatBytes(task.size_bytes) : ''}`}
                    </div>
                    ${isRunning ? `<div class="queue-progress"><div class="queue-progress-bar" style="width: ${progress}%"></div></div>` : ''}
                </div>
                <button class="btn-icon btn-cancel" data-task-id="${task.id}" title="Cancel">✕</button>
            </div>
        `}).join('');

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
        // Find the queue item for this task and update its progress
        const item = document.querySelector(`.queue-item[data-task-id="${data.task_id}"]`);
        if (item) {
            const progressBar = item.querySelector('.queue-progress-bar');
            const meta = item.querySelector('.queue-meta');

            if (progressBar) {
                progressBar.style.width = `${data.progress_pct}%`;
            }
            if (meta) {
                meta.textContent = `⚡ ${data.progress_pct}% • ${this.formatBytes(data.bytes_transferred)} / ${this.formatBytes(data.total_bytes)}`;
            }
        }
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
