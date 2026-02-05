/**
 * ComfyUI Model Manager - Core Application JS
 * Handles WebSocket connection, utilities, and shared state
 */

const App = {
    ws: null,
    wsReconnectMs: 1000,
    queueTasks: [],
    downloadJobs: [],
    downloadRefreshHandle: null,

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

        if (!this.downloadRefreshHandle) {
            this.downloadRefreshHandle = setInterval(() => this.loadDownloadJobs(), 2000);
        }
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
            await this.loadDownloadJobs(false);
            this.renderQueue();
        } catch (err) {
            console.error('Failed to load queue:', err);
        }
    },

    async loadDownloadJobs(render = true) {
        try {
            const jobs = await this.api('GET', '/downloader/jobs');
            this.downloadJobs = Array.isArray(jobs) ? jobs : [];
            if (render) {
                this.renderQueue();
            }
        } catch (err) {
            console.error('Failed to load downloads:', err);
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

        const downloadPending = (this.downloadJobs || [])
            .filter(j => j.status === 'queued' || j.status === 'running')
            .sort((a, b) => a.id - b.id);

        if (pending.length === 0 && downloadPending.length === 0) {
            list.innerHTML = '<div class="empty-queue">No pending tasks</div>';
            return;
        }

        const queueHtml = pending.map(task => {
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
                        : task.task_type === 'hash_file'
                            ? '🔑'
                            : task.task_type === 'dedupe_scan'
                                ? '🧹'
                                : task.task_type === 'delete'
                                    ? '🗑️'
                                    : '•';

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

        const downloadHtml = downloadPending.map(job => {
            const pct =
                job.total_bytes && job.total_bytes > 0
                    ? Math.min(100, (job.bytes_downloaded / job.total_bytes) * 100)
                    : null;
            const progressText = pct !== null ? `${pct.toFixed(0)}%` : '—';
            const sizeText = `${this.formatBytes(job.bytes_downloaded || 0)} / ${this.formatBytes(job.total_bytes || 0)}`;
            const statusText = job.status === 'running' ? `⚡ ${progressText}` : '⏳ Pending';

            return `
            <div class="queue-item queue-item-${job.status} download-item" data-download-id="${job.id}">
                <span class="queue-icon">⬇️</span>
                <div class="queue-info">
                    <div class="queue-path">${job.filename || 'download'}</div>
                    <div class="queue-meta">${statusText} • ${sizeText}</div>
                    ${pct !== null ? `<div class="queue-progress"><div class="queue-progress-bar" style="width: ${pct}%"></div></div>` : ''}
                </div>
                <button class="btn-icon btn-cancel btn-cancel-download" data-download-id="${job.id}" title="Cancel">✕</button>
            </div>
        `;
        }).join('');

        const sections = [];
        if (queueHtml) {
            sections.push(`
                <div class="queue-section">
                    <div class="queue-section-title">Queue</div>
                    ${queueHtml}
                </div>
            `);
        }
        if (downloadHtml) {
            sections.push(`
                <div class="queue-section">
                    <div class="queue-section-title">Downloads</div>
                    ${downloadHtml}
                </div>
            `);
        }

        list.innerHTML = sections.join('');

        // Bind cancel buttons
        list.querySelectorAll('.btn-cancel').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const taskId = btn.dataset.taskId;
                if (taskId) {
                    this.cancelTask(taskId);
                }
                const downloadId = btn.dataset.downloadId;
                if (downloadId) {
                    this.cancelDownload(downloadId);
                }
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

    async cancelDownload(jobId) {
        try {
            await this.api('POST', `/downloader/jobs/${jobId}/cancel`);
            await this.loadDownloadJobs();
        } catch (err) {
            console.error('Cancel download failed:', err);
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

        // Keep cached queueTasks in sync so periodic re-render doesn't regress
        const task = this.queueTasks.find(t => String(t.id) === String(data.task_id));
        if (task) {
            task.bytes_transferred = data.bytes_transferred;
            task.size_bytes = data.total_bytes;
            task.status = 'running';
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
