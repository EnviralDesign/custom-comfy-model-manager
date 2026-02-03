/**
 * Sync Page JS - Unified Diff Tree View
 */

const Sync = {
    diffData: [],      // Raw diff entries from API
    treeData: null,    // Hierarchical tree structure
    expandedFolders: new Set(),
    selectedItems: new Set(),
    queuedFiles: new Map(),  // relpath -> {status: 'pending'|'running', taskId: n}

    async init() {
        this.bindEvents();
        await this.loadQueueState();
        await this.refresh();
    },

    async loadQueueState() {
        try {
            const tasks = await App.api('GET', '/queue/tasks');
            this.queuedFiles.clear();
            for (const task of tasks) {
                if (task.status === 'pending' || task.status === 'running') {
                    const relpath = task.src_relpath || task.dst_relpath;
                    this.queuedFiles.set(relpath, { status: task.status, taskId: task.id });
                }
            }
        } catch (err) {
            console.error('Failed to load queue state:', err);
        }
    },

    bindEvents() {
        document.getElementById('refresh-btn')?.addEventListener('click', () => this.refresh());
        document.getElementById('expand-all-btn')?.addEventListener('click', () => this.expandAll());
        document.getElementById('collapse-all-btn')?.addEventListener('click', () => this.collapseAll());

        // Search with debounce
        let searchTimeout;
        document.getElementById('search-input')?.addEventListener('input', (e) => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => this.search(e.target.value), 200);
        });

        // Queue controls
        document.getElementById('pause-queue')?.addEventListener('click', () => App.api('POST', '/queue/pause'));
        document.getElementById('resume-queue')?.addEventListener('click', () => App.api('POST', '/queue/resume'));

        // Delegate click events on the tree
        document.getElementById('diff-tree')?.addEventListener('click', (e) => this.handleTreeClick(e));

        // Listen for WebSocket task started events
        document.addEventListener('ws:task_started', async (e) => {
            const { task_id } = e.detail;
            // Find the relpath for this task and update status
            await this.loadQueueState();
            this.updateRowQueueStatus();
        });

        // Listen for WebSocket task completion events
        let refreshDebounce;
        document.addEventListener('ws:task_complete', async (e) => {
            const { task_id, status } = e.detail;

            // Remove from queued files
            for (const [relpath, info] of this.queuedFiles) {
                if (info.taskId === task_id) {
                    this.queuedFiles.delete(relpath);
                    break;
                }
            }

            // Update UI immediately for the completed file
            this.updateRowQueueStatus();

            // Debounce full refresh to avoid multiple rapid re-renders
            clearTimeout(refreshDebounce);
            refreshDebounce = setTimeout(() => this.refreshDiff(), 500);

            // Also refresh the queue panel
            App.loadQueueTasks();
        });

        // Listen for WebSocket verify progress
        document.addEventListener('ws:verify_progress', (e) => {
            const { folder, current, total } = e.detail;
            if (!folder) return;

            // Update folder button
            // Use CSS.escape for the selector but handle the folder path correctly
            // The folder path acts as an ID, so we need to be careful with selectors
            try {
                // We use querySelectorAll and filter because standard querySelector might choke on paths with special chars
                const buttons = document.querySelectorAll(`button[data-action="verify-folder"]`);
                for (const btn of buttons) {
                    if (btn.dataset.folder === folder) {
                        btn.textContent = `${current}/${total}`;
                        btn.disabled = true;
                        break;
                    }
                }
            } catch (err) {
                console.error('Error updating progress:', err);
            }
        });
    },

    updateRowQueueStatus() {
        // Update all rows based on queued state
        document.querySelectorAll('.diff-row-file').forEach(row => {
            const relpath = row.dataset.relpath;
            const queueInfo = this.queuedFiles.get(relpath);

            row.classList.remove('queue-pending', 'queue-running');

            if (queueInfo) {
                row.classList.add(`queue-${queueInfo.status}`);
            }
        });
    },

    async refresh() {
        const container = document.getElementById('diff-tree');
        container.innerHTML = '<div class="loading">Scanning files...</div>';

        try {
            // Refresh index first
            await App.api('POST', '/index/refresh', { side: 'both' });

            // Get diff data
            this.diffData = await App.api('GET', '/index/diff');

            // Build tree and render
            this.treeData = this.buildTree(this.diffData);
            this.render();

            // Load stats
            await this.loadStats();
        } catch (err) {
            container.innerHTML = `<div class="error">Error: ${err.message}</div>`;
            console.error('Refresh failed:', err);
        }
    },

    async loadStats() {
        try {
            const stats = await App.api('GET', '/index/stats');
            document.getElementById('local-stats').textContent =
                `${stats.local.file_count} files • ${App.formatBytes(stats.local.total_bytes)}`;
            document.getElementById('lake-stats').textContent =
                `${stats.lake.file_count} files • ${App.formatBytes(stats.lake.total_bytes)}`;
        } catch (err) {
            console.error('Failed to load stats:', err);
        }
    },

    /**
     * Quick refresh - rescan files and update diff without full UI reset
     * Preserves expanded folder state
     */
    async refreshDiff() {
        try {
            // Quick refresh of the index
            await App.api('POST', '/index/refresh', { side: 'both' });

            // Get fresh diff data
            this.diffData = await App.api('GET', '/index/diff');

            // Rebuild tree and re-render (preserves expandedFolders)
            this.treeData = this.buildTree(this.diffData);
            this.render(document.getElementById('search-input')?.value || '');

            // Update stats
            await this.loadStats();

            console.log('Diff refreshed after task completion');
        } catch (err) {
            console.error('Diff refresh failed:', err);
        }
    },

    /**
     * Build hierarchical tree from flat diff entries
     */
    buildTree(entries) {
        const root = { name: '', children: {}, files: [] };

        for (const entry of entries) {
            const parts = entry.relpath.split('/');
            const filename = parts.pop();

            // Navigate/create folder path
            let current = root;
            for (const part of parts) {
                if (!current.children[part]) {
                    current.children[part] = { name: part, children: {}, files: [] };
                }
                current = current.children[part];
            }

            // Add file to current folder
            current.files.push({ ...entry, filename });
        }

        return root;
    },

    /**
     * Render the tree to HTML
     */
    render(filterQuery = '') {
        const container = document.getElementById('diff-tree');
        const html = this.renderNode(this.treeData, '', filterQuery.toLowerCase(), 0);

        if (html.trim() === '') {
            container.innerHTML = '<div class="empty-state">No files found</div>';
        } else {
            container.innerHTML = html;
        }
    },

    /**
     * Recursively render a tree node
     */
    renderNode(node, path, filter, depth) {
        let html = '';

        // Sort folders alphabetically
        const folderNames = Object.keys(node.children).sort();

        // Sort files alphabetically
        const files = [...node.files].sort((a, b) => a.filename.localeCompare(b.filename));

        // Render folders
        for (const folderName of folderNames) {
            const folder = node.children[folderName];
            const folderPath = path ? `${path}/${folderName}` : folderName;
            const isExpanded = this.expandedFolders.has(folderPath);

            // Count items in folder (for display)
            const itemCount = this.countItems(folder);

            // Get folder diff status
            const folderStatus = this.getFolderStatus(folder);

            // If filtering, check if folder contains matching items
            const childContent = this.renderNode(folder, folderPath, filter, depth + 1);
            if (filter && !childContent && !folderName.toLowerCase().includes(filter)) {
                continue;
            }

            // Determine if sync buttons should show
            const showSyncToLake = folderStatus.hasOnlyLocal;
            const showSyncToLocal = folderStatus.hasOnlyLake;
            const showVerify = folderStatus.hasProbableSame;

            html += `
                <div class="diff-row diff-row-folder" data-path="${folderPath}" data-depth="${depth}">
                    <div class="diff-col diff-col-local">
                        <span class="btn-slot">
                            ${showSyncToLake ? `<button class="btn-icon btn-copy" data-action="sync-folder-to-lake" data-folder="${folderPath}" title="Copy ${folderStatus.onlyLocalCount || ''} to Lake →">→</button>` : ''}
                        </span>
                        <span class="presence-bar ${folderStatus.local === 'has-files' ? 'present' : 'absent'}"></span>
                    </div>
                    <div class="diff-col diff-col-path">
                        <span class="tree-indent" style="width: ${depth * 20}px"></span>
                        <span class="folder-toggle ${isExpanded ? 'expanded' : ''}" data-folder="${folderPath}">
                            ${isExpanded ? '▼' : '▶'}
                        </span>
                        <span class="folder-icon">📁</span>
                        <span class="folder-name">${folderName}</span>
                        <span class="folder-count">(${itemCount})</span>
                        ${showVerify ? `<button class="btn-verify" data-action="verify-folder" data-folder="${folderPath}" title="Verify hashes for this folder" style="margin-left: auto">✓?</button>` : ''}
                    </div>
                    <div class="diff-col diff-col-lake">
                        <span class="presence-bar ${folderStatus.lake === 'has-files' ? 'present' : 'absent'}"></span>
                        <span class="btn-slot">
                            ${showSyncToLocal ? `<button class="btn-icon btn-copy" data-action="sync-folder-to-local" data-folder="${folderPath}" title="← Copy ${folderStatus.onlyLakeCount || ''} to Local">←</button>` : ''}
                        </span>
                    </div>
                </div>
            `;

            // Render children if expanded
            if (isExpanded) {
                html += `<div class="folder-children" data-parent="${folderPath}">${childContent}</div>`;
            }
        }

        // Render files
        for (const file of files) {
            // Apply filter
            if (filter && !file.filename.toLowerCase().includes(filter) && !file.relpath.toLowerCase().includes(filter)) {
                continue;
            }

            const statusClass = this.getStatusClass(file.status);
            const statusIcon = this.getStatusIcon(file.status);
            const hasLocal = file.local_size !== null;
            const hasLake = file.lake_size !== null;
            const isProbableSame = file.status === 'probable_same';

            // Check if file is in queue
            const queueInfo = this.queuedFiles.get(file.relpath);
            const queueClass = queueInfo ? `queue-${queueInfo.status}` : '';

            html += `
                <div class="diff-row diff-row-file ${statusClass} ${queueClass}" data-relpath="${file.relpath}" data-depth="${depth}">
                    <div class="diff-col diff-col-local">
                        <span class="file-size">${hasLocal ? App.formatBytes(file.local_size) : ''}</span>
                        <span class="btn-slot">
                            ${hasLocal && !hasLake && !queueInfo ? `<button class="btn-icon btn-copy" data-action="copy-to-lake" data-relpath="${file.relpath}" title="Copy to Lake →">→</button>` : ''}
                        </span>
                        <span class="presence-bar ${hasLocal ? 'present' : 'absent'}"></span>
                    </div>
                    <div class="diff-col diff-col-path">
                        <span class="tree-indent" style="width: ${depth * 20}px"></span>
                        <span class="status-icon ${statusClass}" title="${this.getStatusTooltip(file.status)}">${statusIcon}</span>
                        <span class="file-name" title="${file.relpath}">${file.filename}</span>
                        ${isProbableSame ? `<button class="btn-verify btn-verify-file" data-action="verify-file" data-relpath="${file.relpath}" title="Verify hash">✓?</button>` : ''}
                    </div>
                    <div class="diff-col diff-col-lake">
                        <span class="presence-bar ${hasLake ? 'present' : 'absent'}"></span>
                        <span class="btn-slot">
                            ${hasLake && !hasLocal && !queueInfo ? `<button class="btn-icon btn-copy" data-action="copy-to-local" data-relpath="${file.relpath}" title="← Copy to Local">←</button>` : ''}
                        </span>
                        <span class="file-size">${hasLake ? App.formatBytes(file.lake_size) : ''}</span>
                    </div>
                </div>
            `;
        }

        return html;
    },

    countItems(node) {
        let count = node.files.length;
        for (const child of Object.values(node.children)) {
            count += this.countItems(child);
        }
        return count;
    },

    getFolderStatus(node) {
        let hasLocal = false, hasLake = false, hasOnlyLocal = false, hasOnlyLake = false, hasProbableSame = false;

        const checkNode = (n) => {
            for (const file of n.files) {
                if (file.local_size !== null) hasLocal = true;
                if (file.lake_size !== null) hasLake = true;
                if (file.status === 'only_local') hasOnlyLocal = true;
                if (file.status === 'only_lake') hasOnlyLake = true;
                if (file.status === 'probable_same') hasProbableSame = true;
            }
            for (const child of Object.values(n.children)) {
                checkNode(child);
            }
        };
        checkNode(node);

        return {
            local: hasLocal ? 'has-files' : 'no-files',
            lake: hasLake ? 'has-files' : 'no-files',
            localIcon: hasLocal ? '●' : '○',
            lakeIcon: hasLake ? '●' : '○',
            hasOnlyLocal,
            hasOnlyLake,
            hasProbableSame,
        };
    },

    getStatusClass(status) {
        const map = {
            'only_local': 'status-only-local',
            'only_lake': 'status-only-lake',
            'same': 'status-same',
            'probable_same': 'status-probable',
            'conflict': 'status-conflict',
        };
        return map[status] || '';
    },

    getStatusIcon(status) {
        const map = {
            'only_local': '◀',
            'only_lake': '▶',
            'same': '✓',
            'probable_same': '≈',
            'conflict': '⚠',
        };
        return map[status] || '?';
    },

    getStatusTooltip(status) {
        const map = {
            'only_local': 'Only on Local',
            'only_lake': 'Only on Lake',
            'same': 'Identical (hash verified)',
            'probable_same': 'Probably same (size+mtime match, hash pending)',
            'conflict': 'CONFLICT: Same path, different content!',
        };
        return map[status] || status;
    },

    handleTreeClick(e) {
        const target = e.target;

        // Folder toggle (clicking the arrow)
        if (target.classList.contains('folder-toggle')) {
            const folderPath = target.dataset.folder;
            this.toggleFolder(folderPath);
            return;
        }

        // Folder sync buttons
        if (target.dataset.action === 'sync-folder-to-lake' || target.dataset.action === 'sync-folder-to-local') {
            const folderPath = target.dataset.folder;
            const srcSide = target.dataset.action === 'sync-folder-to-lake' ? 'local' : 'lake';
            const dstSide = target.dataset.action === 'sync-folder-to-lake' ? 'lake' : 'local';
            this.enqueueFolderCopy(srcSide, folderPath, dstSide);
            return;
        }

        // File copy buttons
        if (target.dataset.action === 'copy-to-lake' || target.dataset.action === 'copy-to-local') {
            const relpath = target.dataset.relpath;
            const srcSide = target.dataset.action === 'copy-to-lake' ? 'local' : 'lake';
            const dstSide = target.dataset.action === 'copy-to-lake' ? 'lake' : 'local';
            this.enqueueCopy(srcSide, relpath, dstSide);
            return;
        }

        // Verify folder button
        if (target.dataset.action === 'verify-folder') {
            const folderPath = target.dataset.folder;
            this.verifyFolder(folderPath);
            return;
        }

        // Verify file button
        if (target.dataset.action === 'verify-file') {
            const relpath = target.dataset.relpath;
            this.verifyFile(relpath);
            return;
        }
    },

    async verifyFolder(folderPath) {
        const btn = document.querySelector(`[data-action="verify-folder"][data-folder="${CSS.escape(folderPath)}"]`);
        if (btn) {
            btn.textContent = 'Queueing...';
            btn.disabled = true;
        }

        try {
            await App.api('POST', '/index/verify', { folder: folderPath });
            // The queue poller will pick it up and update the button/queue panel
            if (btn) btn.textContent = 'Queued';
            App.loadQueueTasks();
        } catch (err) {
            console.error('Verify failed:', err);
            alert('Verification request failed: ' + err.message);
            if (btn) {
                btn.textContent = '✓?';
                btn.disabled = false;
            }
        }
    },

    async verifyFile(relpath) {
        const btn = document.querySelector(`[data-action="verify-file"][data-relpath="${CSS.escape(relpath)}"]`);
        if (btn) {
            btn.textContent = '...';
            btn.disabled = true;
        }

        try {
            await App.api('POST', '/index/verify', { relpath: relpath });
            if (btn) btn.textContent = 'Queued';
            App.loadQueueTasks();
        } catch (err) {
            console.error('Verify failed:', err);
            alert('Verification request failed: ' + err.message);
            if (btn) {
                btn.textContent = '✓?';
                btn.disabled = false;
            }
        }
    },

    toggleFolder(folderPath) {
        if (this.expandedFolders.has(folderPath)) {
            this.expandedFolders.delete(folderPath);
        } else {
            this.expandedFolders.add(folderPath);
        }
        this.render(document.getElementById('search-input')?.value || '');
    },

    expandAll() {
        const addAllFolders = (node, path) => {
            for (const [name, child] of Object.entries(node.children)) {
                const childPath = path ? `${path}/${name}` : name;
                this.expandedFolders.add(childPath);
                addAllFolders(child, childPath);
            }
        };
        addAllFolders(this.treeData, '');
        this.render(document.getElementById('search-input')?.value || '');
    },

    collapseAll() {
        this.expandedFolders.clear();
        this.render(document.getElementById('search-input')?.value || '');
    },

    search(query) {
        this.render(query);
    },

    async enqueueCopy(srcSide, relpath, dstSide) {
        try {
            const result = await App.api('POST', '/queue/copy', {
                src_side: srcSide,
                src_relpath: relpath,
                dst_side: dstSide,
            });

            // Add to local tracking
            this.queuedFiles.set(relpath, { status: 'pending', taskId: result.task_id });

            // Visual feedback - add pending class
            const row = document.querySelector(`[data-relpath="${CSS.escape(relpath)}"]`);
            if (row) {
                row.classList.add('queue-pending');
            }

            // Refresh queue panel
            App.loadQueueTasks();
        } catch (err) {
            alert('Copy failed: ' + err.message);
        }
    },

    async enqueueFolderCopy(srcSide, folderPath, dstSide) {
        // Find all files in this folder that need copying
        const filesToCopy = this.diffData.filter(entry => {
            if (!entry.relpath.startsWith(folderPath + '/') && entry.relpath !== folderPath) return false;
            // Only copy files that exist on source but not destination
            if (srcSide === 'local' && entry.status === 'only_local') return true;
            if (srcSide === 'lake' && entry.status === 'only_lake') return true;
            return false;
        });

        if (filesToCopy.length === 0) {
            alert('No files to copy in this folder');
            return;
        }

        const confirmed = confirm(`Copy ${filesToCopy.length} files from ${srcSide} to ${dstSide}?`);
        if (!confirmed) return;

        try {
            for (const file of filesToCopy) {
                await App.api('POST', '/queue/copy', {
                    src_side: srcSide,
                    src_relpath: file.relpath,
                    dst_side: dstSide,
                });

                // Track in local state
                this.queuedFiles.set(file.relpath, { status: 'pending', taskId: null });
            }

            // Refresh queue panel and update row styles
            App.loadQueueTasks();
            this.updateRowQueueStatus();
        } catch (err) {
            alert('Folder copy failed: ' + err.message);
        }
    }
};

document.addEventListener('DOMContentLoaded', () => Sync.init());
