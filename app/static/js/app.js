/**
 * ComfyUI Model Manager - Core Application JS
 * Handles WebSocket connection, utilities, and shared state
 */

const App = {
    ws: null,
    wsReconnectMs: 1000,

    init() {
        this.connectWebSocket();
        this.setupQueueWidget();
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
            case 'hash_progress':
                this.updateHashProgress(msg.data);
                break;
            case 'scan_progress':
                this.updateScanProgress(msg.data);
                break;
            case 'index_refreshed':
                this.onIndexRefreshed(msg.data);
                break;
        }

        // Dispatch custom event for page-specific handlers
        document.dispatchEvent(new CustomEvent('ws:' + msg.type, { detail: msg.data }));
    },

    // Queue widget
    setupQueueWidget() {
        const widget = document.getElementById('queue-widget');
        const panel = document.getElementById('queue-panel');

        if (widget && panel) {
            widget.addEventListener('click', () => {
                panel.classList.toggle('expanded');
            });
        }
    },

    updateQueueProgress(data) {
        const countEl = document.querySelector('.queue-count');
        if (countEl) countEl.textContent = data.pending || 0;
    },

    updateHashProgress(data) {
        document.dispatchEvent(new CustomEvent('hash:progress', { detail: data }));
    },

    updateScanProgress(data) {
        document.dispatchEvent(new CustomEvent('scan:progress', { detail: data }));
    },

    onIndexRefreshed(data) {
        document.dispatchEvent(new CustomEvent('index:refreshed', { detail: data }));
    },

    // Utilities
    formatBytes(bytes) {
        if (bytes === 0) return '0 B';
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
