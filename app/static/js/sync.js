/**
 * Sync Page JS
 */

const Sync = {
    currentFolder: { local: '', lake: '' },
    selectedFiles: { local: new Set(), lake: new Set() },

    async init() {
        await this.refresh();
        this.bindEvents();
    },

    bindEvents() {
        // Refresh button
        document.getElementById('refresh-btn')?.addEventListener('click', () => this.refresh());

        // Search
        let searchTimeout;
        document.getElementById('search-input')?.addEventListener('input', (e) => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => this.search(e.target.value), 200);
        });

        // Copy buttons
        document.getElementById('copy-to-lake')?.addEventListener('click', () => this.copySelected('local', 'lake'));
        document.getElementById('copy-to-local')?.addEventListener('click', () => this.copySelected('lake', 'local'));

        // Queue controls
        document.getElementById('pause-queue')?.addEventListener('click', () => App.api('POST', '/queue/pause'));
        document.getElementById('resume-queue')?.addEventListener('click', () => App.api('POST', '/queue/resume'));

        // Listen for index refresh
        document.addEventListener('index:refreshed', () => this.loadFiles());
    },

    async refresh() {
        try {
            await App.api('POST', '/index/refresh', { side: 'both' });
            await this.loadFiles();
            await this.loadStats();
        } catch (err) {
            console.error('Refresh failed:', err);
        }
    },

    async loadFiles() {
        await Promise.all([
            this.loadSideFiles('local'),
            this.loadSideFiles('lake'),
        ]);
        this.updateDiff();
    },

    async loadSideFiles(side) {
        try {
            const folder = this.currentFolder[side];
            const files = await App.api('GET', `/index/files?side=${side}&folder=${encodeURIComponent(folder)}`);
            this.renderFiles(side, files);

            const folders = await App.api('GET', `/index/folders?side=${side}&parent=${encodeURIComponent(folder)}`);
            this.renderFolders(side, folders.folders);
        } catch (err) {
            console.error(`Failed to load ${side} files:`, err);
        }
    },

    async loadStats() {
        try {
            const stats = await App.api('GET', '/index/stats');
            document.getElementById('local-stats').innerHTML =
                `${stats.local.file_count} files • ${App.formatBytes(stats.local.total_bytes)}`;
            document.getElementById('lake-stats').innerHTML =
                `${stats.lake.file_count} files • ${App.formatBytes(stats.lake.total_bytes)}`;
        } catch (err) {
            console.error('Failed to load stats:', err);
        }
    },

    renderFolders(side, folders) {
        const container = document.getElementById(`${side}-folders`);
        if (!container) return;

        let html = '';

        // Parent folder link
        if (this.currentFolder[side]) {
            html += `<div class="folder-item" data-action="up">📁 ..</div>`;
        }

        for (const folder of folders) {
            html += `<div class="folder-item" data-folder="${folder}">📁 ${folder}</div>`;
        }

        container.innerHTML = html || '<div class="text-muted" style="padding: 12px;">No subfolders</div>';

        // Bind click handlers
        container.querySelectorAll('.folder-item').forEach(el => {
            el.addEventListener('click', () => {
                const action = el.dataset.action;
                const folder = el.dataset.folder;

                if (action === 'up') {
                    const parts = this.currentFolder[side].split('/');
                    parts.pop();
                    this.currentFolder[side] = parts.join('/');
                } else {
                    this.currentFolder[side] = this.currentFolder[side]
                        ? `${this.currentFolder[side]}/${folder}`
                        : folder;
                }

                this.loadSideFiles(side);
            });
        });
    },

    renderFiles(side, files) {
        const container = document.getElementById(`${side}-files`);
        if (!container) return;

        let html = '';
        for (const file of files) {
            const filename = file.relpath.split('/').pop();
            const selected = this.selectedFiles[side].has(file.relpath) ? 'selected' : '';

            html += `
                <div class="file-item ${selected}" data-relpath="${file.relpath}">
                    <span class="file-icon">📄</span>
                    <span class="file-name" title="${file.relpath}">${filename}</span>
                    <span class="file-size">${App.formatBytes(file.size)}</span>
                </div>
            `;
        }

        container.innerHTML = html || '<div class="text-muted" style="padding: 12px;">No files</div>';

        // Bind click handlers
        container.querySelectorAll('.file-item').forEach(el => {
            el.addEventListener('click', () => {
                const relpath = el.dataset.relpath;
                if (this.selectedFiles[side].has(relpath)) {
                    this.selectedFiles[side].delete(relpath);
                    el.classList.remove('selected');
                } else {
                    this.selectedFiles[side].add(relpath);
                    el.classList.add('selected');
                }
                this.updateCopyButtons();
            });
        });
    },

    updateDiff() {
        // TODO: Fetch diff and apply status classes to file items
    },

    updateCopyButtons() {
        const toLake = document.getElementById('copy-to-lake');
        const toLocal = document.getElementById('copy-to-local');

        if (toLake) toLake.disabled = this.selectedFiles.local.size === 0;
        if (toLocal) toLocal.disabled = this.selectedFiles.lake.size === 0;
    },

    async copySelected(srcSide, dstSide) {
        const files = Array.from(this.selectedFiles[srcSide]);
        if (files.length === 0) return;

        try {
            for (const relpath of files) {
                await App.api('POST', '/queue/copy', {
                    src_side: srcSide,
                    src_relpath: relpath,
                    dst_side: dstSide,
                });
            }

            this.selectedFiles[srcSide].clear();
            this.loadSideFiles(srcSide);
        } catch (err) {
            alert('Copy failed: ' + err.message);
        }
    },

    async search(query) {
        if (!query) {
            this.loadFiles();
            return;
        }

        try {
            const files = await App.api('GET', `/index/files?side=local&query=${encodeURIComponent(query)}`);
            this.renderFiles('local', files);

            const lakeFiles = await App.api('GET', `/index/files?side=lake&query=${encodeURIComponent(query)}`);
            this.renderFiles('lake', lakeFiles);
        } catch (err) {
            console.error('Search failed:', err);
        }
    }
};

document.addEventListener('DOMContentLoaded', () => Sync.init());
