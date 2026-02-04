/**
 * Sync Page JS - Unified Diff Tree View
 */

const Sync = {
    diffData: [],      // Raw diff entries from API
    treeData: null,    // Hierarchical tree structure
    expandedFolders: new Set(),
    selectedItems: new Set(),
    queuedFiles: new Map(),  // relpath -> {status: 'pending'|'running', taskId: n}
    sourceUrls: new Map(),   // hash -> {url, added_at, notes}
    bundles: [],             // List of bundle names for quick add
    activeSourceContext: null,
    confirmModal: null,
    confirmResolve: null,
    confirmElements: null,
    safetensorsTags: new Map(),
    safetensorsPending: new Set(),
    safetensorsRenderTimer: null,
    minMetricSizeBytes: 5 * 1024 * 1024,

    async init() {
        this.bindEvents();
        await this.loadConfig();
        await this.loadQueueState();
        await this.loadSourceUrls();
        await this.loadBundles();
        await this.refresh();
    },

    async loadConfig() {
        try {
            this.config = await App.api('GET', '/index/config');
        } catch (err) {
            console.error('Failed to load config:', err);
            // Default to safe values
            this.config = { local_allow_delete: false, lake_allow_delete: false };
        }
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

    async loadSourceUrls() {
        try {
            const result = await App.api('GET', '/index/sources');
            this.sourceUrls.clear();
            this.sourceUrlsByRelpath = this.sourceUrlsByRelpath || new Map();
            this.sourceUrlsByRelpath.clear();

            for (const source of result.sources) {
                if (source.key.startsWith('relpath:')) {
                    // Relpath-based (unhashed)
                    const relpath = source.key.substring(8); // Remove 'relpath:' prefix
                    this.sourceUrlsByRelpath.set(relpath, source);
                } else {
                    // Hash-based
                    this.sourceUrls.set(source.key, source);
                }
            }
        } catch (err) {
            console.error('Failed to load source URLs:', err);
        }
    },

    async loadBundles() {
        try {
            const result = await App.api('GET', '/bundles');
            this.bundles = result.bundles || [];
        } catch (err) {
            console.error('Failed to load bundles:', err);
            this.bundles = [];
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
            const { task_id, status, task_type, src_relpath, dst_relpath, src_side, dst_side } = e.detail;

            // Remove from queued files
            for (const [relpath, info] of this.queuedFiles) {
                if (info.taskId === task_id) {
                    this.queuedFiles.delete(relpath);
                    break;
                }
            }

            // For copy tasks, immediately update the diff data and row
            if (task_type === 'copy' && status === 'completed' && src_relpath) {
                this.handleCopyComplete(src_relpath, src_side, dst_side);
            }

            // For delete tasks, update immediately
            if (task_type === 'delete' && status === 'completed' && dst_relpath) {
                this.handleDeleteComplete(dst_relpath, dst_side);
            }

            // Update UI immediately for the completed file
            this.updateRowQueueStatus();

            // Debounce full refresh to heal any discrepancies
            clearTimeout(refreshDebounce);
            refreshDebounce = setTimeout(() => this.refreshDiff(), 2000);

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

    handleCopyComplete(relpath, srcSide, dstSide) {
        // Find the file in diffData and update its state
        const file = this.diffData.find(f => f.relpath === relpath);
        if (!file) return;

        // After copy, file exists on both sides with same size
        // Update the diff entry optimistically
        if (srcSide === 'local' && dstSide === 'lake') {
            // Copied from local to lake
            file.lake_size = file.local_size;
            file.lake_hash = file.local_hash;
            file.status = file.local_hash ? 'same' : 'probable_same';
        } else if (srcSide === 'lake' && dstSide === 'local') {
            // Copied from lake to local
            file.local_size = file.lake_size;
            file.local_hash = file.lake_hash;
            file.status = file.lake_hash ? 'same' : 'probable_same';
        }

        // Rebuild tree and re-render
        this.treeData = this.buildTree(this.diffData);
        this.render(document.getElementById('search-input')?.value || '');
    },

    handleDeleteComplete(relpath, side) {
        // Find the file in diffData and update its state
        const file = this.diffData.find(f => f.relpath === relpath);
        if (!file) return;

        // After delete, remove the file from that side
        if (side === 'local') {
            file.local_size = null;
            file.local_hash = null;
            file.status = 'only_lake';
        } else if (side === 'lake') {
            file.lake_size = null;
            file.lake_hash = null;
            file.status = 'only_local';
        }

        // If file no longer exists on either side, remove from diffData
        if (file.local_size === null && file.lake_size === null) {
            const idx = this.diffData.indexOf(file);
            if (idx > -1) this.diffData.splice(idx, 1);
        }

        // Rebuild tree and re-render
        this.treeData = this.buildTree(this.diffData);
        this.render(document.getElementById('search-input')?.value || '');
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
            // Force expand folders if searching so results are visible
            const isExpanded = filter ? true : this.expandedFolders.has(folderPath);

            // Count items and derive status metrics for display
            const folderMetrics = this.getFolderMetrics(folder);
            const itemCount = folderMetrics.total;

            // Get folder diff status
            const folderStatus = this.getFolderStatus(folder);
            const folderHashStats = this.getFolderHashStats(folder);

            // If filtering, check if folder contains matching items
            const childContent = this.renderNode(folder, folderPath, filter, depth + 1);
            if (filter && !childContent && !folderName.toLowerCase().includes(filter)) {
                continue;
            }

            // Determine if sync buttons should show
            const showSyncToLake = folderStatus.hasOnlyLocal;
            const showSyncToLocal = folderStatus.hasOnlyLake;
            const showVerify = folderStatus.hasProbableSame;

            const emptyMetrics = folderMetrics.total === 0;
            const hashComplete = folderMetrics.total > 0 && folderMetrics.hashed === folderMetrics.total;
            const linkComplete = folderMetrics.total > 0 && folderMetrics.linked === folderMetrics.total;
            const sizeLabel = folderMetrics.totalBytes > 0 ? App.formatBytes(folderMetrics.totalBytes) : '';
            const metricsHtml = folderMetrics.total > 0 || sizeLabel
                ? `<span class="folder-metrics">
                        <span class="folder-metric ${hashComplete ? 'complete' : ''} ${emptyMetrics ? 'empty' : ''}" title="${emptyMetrics ? 'No files ≥ 5 MB' : 'Counts files ≥ 5 MB'}">hashed ${folderMetrics.hashed}/${folderMetrics.total}</span>
                        <span class="folder-metric ${linkComplete ? 'complete' : ''} ${emptyMetrics ? 'empty' : ''}" title="${emptyMetrics ? 'No files ≥ 5 MB' : 'Counts files ≥ 5 MB'}">linked ${folderMetrics.linked}/${folderMetrics.total}</span>
                        ${sizeLabel ? `<span class="folder-metric" title="Total size across files">${sizeLabel}</span>` : ''}
                   </span>`
                : '';
            const hashFolderBtn = folderHashStats.total > 0
                ? `<button class="btn-hash-folder" data-action="hash-folder" data-folder="${folderPath}" title="${folderHashStats.missing > 0 ? `Queue hash for ${folderHashStats.missing} file(s) in this folder` : 'All files already hashed'}">#️⃣</button>`
                : '';

            html += `
                <div class="diff-row diff-row-folder" data-path="${folderPath}" data-depth="${depth}">
                    <div class="diff-col diff-col-local">
                        ${this.config.local_allow_delete && folderStatus.local === 'has-files' ? `<button class="btn-icon btn-delete" data-action="delete-folder" data-side="local" data-folder="${folderPath}" title="Delete all in folder from Local">🗑️</button>` : ''}
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
                        ${metricsHtml}
                        <div class="path-actions">
                            ${showVerify ? `<button class="btn-verify" data-action="verify-folder" data-folder="${folderPath}" title="Verify hashes for this folder">✓?</button>` : ''}
                            ${hashFolderBtn}
                            <button class="btn-ai-lookup" data-action="ai-lookup-folder" data-folder="${folderPath}" title="AI lookup source URLs for this folder">✨</button>
                            <button class="btn-add-bundle" data-action="add-folder-to-bundle" data-folder="${folderPath}" title="Add all in folder to bundle">📦</button>
                        </div>
                    </div>
                    <div class="diff-col diff-col-lake">
                        <span class="presence-bar ${folderStatus.lake === 'has-files' ? 'present' : 'absent'}"></span>
                        <span class="btn-slot">
                            ${showSyncToLocal ? `<button class="btn-icon btn-copy" data-action="sync-folder-to-local" data-folder="${folderPath}" title="← Copy ${folderStatus.onlyLakeCount || ''} to Local">←</button>` : ''}
                        </span>
                        ${this.config.lake_allow_delete && folderStatus.lake === 'has-files' ? `<button class="btn-icon btn-delete" data-action="delete-folder" data-side="lake" data-folder="${folderPath}" title="Delete all in folder from Lake">🗑️</button>` : ''}
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

            // Check for hash and source URL
            const fileHash = file.lake_hash || file.local_hash;
            const hasHash = !!fileHash;

            // Check for source URL (by hash or by relpath)
            const hasSourceUrlByHash = fileHash && this.sourceUrls.has(fileHash);
            const hasSourceUrlByRelpath = this.sourceUrlsByRelpath?.has(file.relpath);
            const hasSourceUrl = hasSourceUrlByHash || hasSourceUrlByRelpath;

            // Hash button - shows on unhashed files (on lake side which is the archive)
            const hashBtn = (!hasHash && hasLake)
                ? `<button class="btn-hash-file" data-action="hash-file" data-relpath="${file.relpath}" title="Compute hash for this file">#️⃣</button>`
                : '';

            // URL and Bundle buttons
            const sourceUrlBtn = `<button class="btn-source-url ${hasSourceUrl ? 'has-url' : ''}" data-action="source-url" data-hash="${fileHash || ''}" data-relpath="${file.relpath}" data-filename="${file.filename}" title="${hasSourceUrl ? 'Edit source URL' : 'Add source URL'}">🔗</button>`;
            const bundleBtn = `<button class="btn-add-bundle" data-action="add-to-bundle" data-hash="${fileHash || ''}" data-relpath="${file.relpath}" data-filename="${file.filename}" title="Add to bundle">📦</button>`;

            html += `
                <div class="diff-row diff-row-file ${statusClass} ${queueClass}" data-relpath="${file.relpath}" data-depth="${depth}">
                    <div class="diff-col diff-col-local">
                        ${this.config.local_allow_delete && hasLocal && !queueInfo ? `<button class="btn-icon btn-delete" data-action="delete-file" data-side="local" data-relpath="${file.relpath}" title="Delete from Local">🗑️</button>` : ''}
                        <span class="file-size">${hasLocal ? App.formatBytes(file.local_size) : ''}</span>
                        <span class="btn-slot">
                            ${hasLocal && !hasLake && !queueInfo ? `<button class="btn-icon btn-copy" data-action="copy-to-lake" data-relpath="${file.relpath}" title="Copy to Lake →">→</button>` : ''}
                        </span>
                        <span class="presence-bar ${hasLocal ? 'present' : 'absent'}"></span>
                    </div>
                    <div class="diff-col diff-col-path">
                        <span class="tree-indent" style="width: ${depth * 20}px"></span>
                        <span class="status-icon ${statusClass}" title="${this.getStatusTooltip(file.status)}">${statusIcon}</span>
                        <div class="file-meta">
                            <span class="file-name" title="${file.relpath}">${file.filename}</span>
                            ${this.renderSafetensorsTags(file, hasLocal, hasLake)}
                        </div>
                        <div class="path-actions">
                            ${isProbableSame ? `<button class="btn-verify btn-verify-file" data-action="verify-file" data-relpath="${file.relpath}" title="Verify hash">✓?</button>` : ''}
                            ${hashBtn}
                            ${sourceUrlBtn}
                            ${bundleBtn}
                        </div>
                    </div>
                    <div class="diff-col diff-col-lake">
                        <span class="presence-bar ${hasLake ? 'present' : 'absent'}"></span>
                        <span class="btn-slot">
                            ${hasLake && !hasLocal && !queueInfo ? `<button class="btn-icon btn-copy" data-action="copy-to-local" data-relpath="${file.relpath}" title="← Copy to Local">←</button>` : ''}
                        </span>
                        <span class="file-size">${hasLake ? App.formatBytes(file.lake_size) : ''}</span>
                        ${this.config.lake_allow_delete && hasLake && !queueInfo ? `<button class="btn-icon btn-delete" data-action="delete-file" data-side="lake" data-relpath="${file.relpath}" title="Delete from Lake">🗑️</button>` : ''}
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

    getFolderMetrics(node) {
        let total = 0;
        let hashed = 0;
        let linked = 0;
        let totalBytes = 0;

        const visit = (n) => {
            for (const file of n.files) {
                totalBytes += this.getEntrySize(file);
                if (!this.isMetricFile(file)) continue;
                total += 1;
                const fileHash = file.lake_hash || file.local_hash;
                if (fileHash) hashed += 1;

                const hasSourceUrlByHash = fileHash && this.sourceUrls.has(fileHash);
                const hasSourceUrlByRelpath = this.sourceUrlsByRelpath?.has(file.relpath);
                if (hasSourceUrlByHash || hasSourceUrlByRelpath) linked += 1;
            }
            for (const child of Object.values(n.children)) {
                visit(child);
            }
        };
        visit(node);

        return { total, hashed, linked, totalBytes };
    },

    getFolderHashStats(node) {
        let total = 0;
        let missing = 0;

        const visit = (n) => {
            for (const file of n.files) {
                total += 1;
                const fileHash = file.lake_hash || file.local_hash;
                if (!fileHash) missing += 1;
            }
            for (const child of Object.values(n.children)) {
                visit(child);
            }
        };
        visit(node);

        return { total, missing };
    },

    isMetricFile(file) {
        const size = this.getEntrySize(file);
        return size >= this.minMetricSizeBytes;
    },

    getEntrySize(file) {
        if (typeof file.lake_size === 'number' && file.lake_size > 0) return file.lake_size;
        if (typeof file.local_size === 'number' && file.local_size > 0) return file.local_size;
        return 0;
    },

    isSafetensorsFile(filename) {
        return typeof filename === 'string' && filename.toLowerCase().endsWith('.safetensors');
    },

    escapeHtml(text) {
        return String(text).replace(/[&<>"']/g, (match) => {
            switch (match) {
                case '&': return '&amp;';
                case '<': return '&lt;';
                case '>': return '&gt;';
                case '"': return '&quot;';
                case "'": return '&#39;';
                default: return match;
            }
        });
    },

    renderSafetensorsTags(file, hasLocal, hasLake) {
        if (!this.isSafetensorsFile(file.filename)) return '';

        const side = hasLocal ? 'local' : (hasLake ? 'lake' : 'auto');
        const relpath = file.relpath;

        if (!this.safetensorsTags.has(relpath) && !this.safetensorsPending.has(relpath)) {
            this.fetchSafetensorsTags(relpath, side);
        }

        const info = this.safetensorsTags.get(relpath);
        const tags = [];

        const stBtn = `<button class="btn-safetensors" data-action="safetensors-header" data-relpath="${relpath}" data-side="${side}" title="View safetensors header JSON">ST</button>`;
        tags.push(stBtn);

        const eligibleTags = (info?.tags || []).filter(tag => (tag?.confidence ?? 0) >= 0.6);
        if (eligibleTags.length) {
            eligibleTags.forEach((tag, index) => {
                if (!tag?.name) return;
                const confidence = typeof tag.confidence === 'number' ? tag.confidence : info.confidence || 0;
                const label = tag.name.toUpperCase() + (confidence >= 0.85 ? '' : '?');
                const level = confidence >= 0.85 ? 'high' : 'low';
                const role = index === 0 ? 'primary' : 'secondary';
                tags.push(`<span class="file-tag tag-${level} tag-${role}" title="Confidence ${(confidence * 100).toFixed(0)}%">${label}</span>`);
            });
        } else if (info?.status === 'error') {
            tags.push(`<span class="file-tag tag-error" title="${this.escapeHtml(info.error || 'Classification failed')}">?</span>`);
        } else if (this.safetensorsPending.has(relpath)) {
            tags.push(`<span class="file-tag tag-pending">...</span>`);
        }

        return `<span class="file-tags">${tags.join('')}</span>`;
    },

    scheduleSafetensorsRender() {
        clearTimeout(this.safetensorsRenderTimer);
        this.safetensorsRenderTimer = setTimeout(() => {
            this.render(document.getElementById('search-input')?.value || '');
        }, 120);
    },

    async fetchSafetensorsTags(relpath, side) {
        this.safetensorsPending.add(relpath);
        this.scheduleSafetensorsRender();
        try {
            const res = await App.api(
                'GET',
                `/index/safetensors/classify?relpath=${encodeURIComponent(relpath)}&side=${encodeURIComponent(side)}`
            );
            this.safetensorsTags.set(relpath, {
                tags: res.tags || [],
                confidence: res.confidence || 0,
                signals: res.signals || [],
            });
        } catch (err) {
            this.safetensorsTags.set(relpath, {
                status: 'error',
                error: err.message || 'Classification failed',
            });
        } finally {
            this.safetensorsPending.delete(relpath);
            this.scheduleSafetensorsRender();
        }
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

        // File delete buttons
        if (target.dataset.action === 'delete-file') {
            const relpath = target.dataset.relpath;
            const side = target.dataset.side;
            this.enqueueDelete(side, relpath);
            return;
        }

        // Folder delete buttons
        if (target.dataset.action === 'delete-folder') {
            const folderPath = target.dataset.folder;
            const side = target.dataset.side;
            this.enqueueFolderDelete(side, folderPath);
            return;
        }

        // Verify folder button
        if (target.dataset.action === 'verify-folder') {
            const folderPath = target.dataset.folder;
            this.verifyFolder(folderPath);
            return;
        }

        // Hash folder button
        if (target.dataset.action === 'hash-folder') {
            const folderPath = target.dataset.folder || '';
            this.enqueueFolderHash(folderPath);
            return;
        }

        // Verify file button
        if (target.dataset.action === 'verify-file') {
            const relpath = target.dataset.relpath;
            this.verifyFile(relpath);
            return;
        }

        // Hash file button
        if (target.dataset.action === 'hash-file') {
            const relpath = target.dataset.relpath;
            this.queueHashFile(relpath);
            return;
        }

        // Safetensors header button
        if (target.dataset.action === 'safetensors-header') {
            const relpath = target.dataset.relpath;
            const side = target.dataset.side || 'auto';
            this.openSafetensorsHeaderModal(relpath, side);
            return;
        }

        // Source URL button
        if (target.dataset.action === 'source-url') {
            const hash = target.dataset.hash;
            const relpath = target.dataset.relpath;
            const filename = target.dataset.filename;
            this.openSourceUrlModal(hash, relpath, filename);
            return;
        }

        // AI lookup for folder
        if (target.dataset.action === 'ai-lookup-folder') {
            const folderPath = target.dataset.folder || '';
            this.enqueueFolderAiLookup(folderPath);
            return;
        }

        // Add to Bundle button (File)
        if (target.dataset.action === 'add-to-bundle') {
            const hash = target.dataset.hash;
            const relpath = target.dataset.relpath;
            const filename = target.dataset.filename;
            this.openAddToBundleModal(hash, relpath, filename);
            return;
        }

        // Add to Bundle button (Folder)
        if (target.dataset.action === 'add-folder-to-bundle') {
            const folderPath = target.dataset.folder;
            this.openAddFolderToBundleModal(folderPath);
            return;
        }
    },

    async queueHashFile(relpath) {
        try {
            await App.api('POST', `/index/hash-file?relpath=${encodeURIComponent(relpath)}`);
            App.loadQueueTasks();
        } catch (err) {
            console.error('Failed to queue hash:', err);
            alert('Failed to queue hash: ' + err.message);
        }
    },

    async enqueueFolderHash(folderPath) {
        const prefix = folderPath ? `${folderPath}/` : '';
        const candidates = this.diffData.filter(entry => {
            if (folderPath) {
                if (!(entry.relpath === folderPath || entry.relpath.startsWith(prefix))) return false;
            }
            const fileHash = entry.lake_hash || entry.local_hash;
            return !fileHash;
        });

        if (candidates.length === 0) {
            alert('No unhashed files found in this folder.');
            return;
        }

        const displayPath = folderPath || '(root)';
        const confirmed = await this.confirmAction({
            title: 'Queue Hashes',
            message: `This will queue hash jobs for ${candidates.length} file(s) in:\n${displayPath}\n\nContinue?`,
            confirmText: 'Queue',
            confirmClass: 'btn-primary',
        });
        if (!confirmed) return;

        try {
            for (const entry of candidates) {
                await App.api('POST', `/index/hash-file?relpath=${encodeURIComponent(entry.relpath)}`);
            }
            App.loadQueueTasks();
        } catch (err) {
            console.error('Failed to queue folder hash:', err);
            alert('Failed to queue folder hash: ' + err.message);
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

    async enqueueDelete(side, relpath) {
        const confirmed = await this.confirmAction({
            title: 'Delete File',
            message: `Are you sure you want to delete this file from ${side}?\n\n${relpath}\n\nThis cannot be undone.`,
            confirmText: 'Delete',
            confirmClass: 'btn-danger',
            rememberKey: 'skip_delete_confirm',
            rememberLabel: 'Do not ask again for deletes',
        });
        if (!confirmed) return;

        try {
            const result = await App.api('POST', '/queue/delete', {
                side: side,
                relpath: relpath,
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
            alert('Delete failed: ' + err.message);
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

        const confirmed = await this.confirmAction({
            title: 'Copy Folder',
            message: `Copy ${filesToCopy.length} files from ${srcSide} to ${dstSide}?`,
            confirmText: 'Copy',
            confirmClass: 'btn-primary',
        });
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
    },

    async enqueueFolderDelete(side, folderPath) {
        // Find all files in this folder that exist on the target side
        const filesToDelete = this.diffData.filter(entry => {
            if (!entry.relpath.startsWith(folderPath + '/') && entry.relpath !== folderPath) return false;
            // Check existence
            if (side === 'local' && entry.local_size !== null) return true;
            if (side === 'lake' && entry.lake_size !== null) return true;
            return false;
        });

        if (filesToDelete.length === 0) {
            alert('No files to delete in this folder');
            return;
        }

        const confirmed = await this.confirmAction({
            title: 'Delete Folder',
            message: `Are you SURE you want to DELETE ${filesToDelete.length} files from ${side}?\n\nFolder: ${folderPath}\n\nThis cannot be undone.`,
            confirmText: 'Delete',
            confirmClass: 'btn-danger',
            rememberKey: 'skip_delete_confirm',
            rememberLabel: 'Do not ask again for deletes',
        });
        if (!confirmed) return;

        try {
            for (const file of filesToDelete) {
                await App.api('POST', '/queue/delete', {
                    side: side,
                    relpath: file.relpath,
                });

                // Track in local state
                this.queuedFiles.set(file.relpath, { status: 'pending', taskId: null });
            }

            // Refresh queue panel and update row styles
            App.loadQueueTasks();
            this.updateRowQueueStatus();
        } catch (err) {
            alert('Folder delete failed: ' + err.message);
        }
    },

    async enqueueAiLookups(items) {
        if (!items || items.length === 0) {
            alert('No eligible files found for AI lookup.');
            return;
        }

        try {
            const res = await App.api('POST', '/ai/lookup/jobs', { items });
            const created = res.created || 0;
            const skipped = res.skipped || 0;
            let msg = `Queued ${created} AI lookup job${created === 1 ? '' : 's'}.`;
            if (skipped) {
                msg += ` Skipped ${skipped} existing job${skipped === 1 ? '' : 's'}.`;
            }
            msg += ' Review results in the AI Review tab.';
            alert(msg);
        } catch (err) {
            alert('Failed to enqueue AI lookups: ' + err.message);
        }
    },

    async enqueueFolderAiLookup(folderPath) {
        const prefix = folderPath ? `${folderPath}/` : '';
        const candidates = this.diffData.filter(entry => {
            if (folderPath) {
                if (!(entry.relpath === folderPath || entry.relpath.startsWith(prefix))) return false;
            }
            if (!this.isMetricFile(entry)) return false;
            const fileHash = entry.lake_hash || entry.local_hash;
            const hasSourceUrlByHash = fileHash && this.sourceUrls.has(fileHash);
            const hasSourceUrlByRelpath = this.sourceUrlsByRelpath?.has(entry.relpath);
            return !(hasSourceUrlByHash || hasSourceUrlByRelpath);
        });

        if (candidates.length === 0) {
            alert('No files without source URLs found in this folder.');
            return;
        }

        const displayPath = folderPath || '(root)';
        const confirmed = await this.confirmAction({
            title: 'Start AI Lookup',
            message: `This will spawn ${candidates.length} AI searches for files in:\n${displayPath}\n\nContinue?`,
            confirmText: 'Start',
            confirmClass: 'btn-primary',
        });
        if (!confirmed) return;

        const items = candidates.map(entry => ({
            filename: entry.relpath.split('/').pop(),
            relpath: entry.relpath,
            file_hash: entry.lake_hash || entry.local_hash || null,
        }));

        this.enqueueAiLookups(items);
    },

    // ==================== Confirm Modal ====================

    ensureConfirmModal() {
        if (this.confirmModal) return this.confirmModal;

        const modal = document.createElement('div');
        modal.id = 'confirm-modal';
        modal.className = 'modal-overlay';
        modal.innerHTML = `
            <div class="modal-content">
                <div class="modal-header">
                    <h3 class="confirm-title">Confirm</h3>
                    <button class="modal-close" data-action="confirm-close">×</button>
                </div>
                <div class="modal-body">
                    <p class="confirm-message"></p>
                    <label class="confirm-remember">
                        <input type="checkbox" class="confirm-remember-checkbox" />
                        <span class="confirm-remember-label"></span>
                    </label>
                </div>
                <div class="modal-footer">
                    <button class="btn" data-action="confirm-cancel">Cancel</button>
                    <button class="btn btn-primary" data-action="confirm-ok">OK</button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);

        const title = modal.querySelector('.confirm-title');
        const message = modal.querySelector('.confirm-message');
        const remember = modal.querySelector('.confirm-remember');
        const rememberCheckbox = modal.querySelector('.confirm-remember-checkbox');
        const rememberLabel = modal.querySelector('.confirm-remember-label');
        const okBtn = modal.querySelector('[data-action="confirm-ok"]');
        const cancelBtn = modal.querySelector('[data-action="confirm-cancel"]');
        const closeBtn = modal.querySelector('[data-action="confirm-close"]');

        const close = (result) => {
            modal.classList.remove('visible');
            if (this.confirmResolve) {
                const resolve = this.confirmResolve;
                this.confirmResolve = null;
                resolve(result);
            }
        };

        okBtn.addEventListener('click', () => close(true));
        cancelBtn.addEventListener('click', () => close(false));
        closeBtn.addEventListener('click', () => close(false));
        modal.addEventListener('click', (e) => {
            if (e.target === modal) close(false);
        });

        this.confirmModal = modal;
        this.confirmElements = {
            title,
            message,
            remember,
            rememberCheckbox,
            rememberLabel,
            okBtn,
        };
        return modal;
    },

    getRememberFlag(key) {
        try {
            return localStorage.getItem(key) === '1';
        } catch (err) {
            return false;
        }
    },

    setRememberFlag(key, value) {
        try {
            if (value) {
                localStorage.setItem(key, '1');
            } else {
                localStorage.removeItem(key);
            }
        } catch (err) {
            // Ignore storage errors
        }
    },

    confirmAction({
        title = 'Confirm',
        message = '',
        confirmText = 'OK',
        confirmClass = 'btn-primary',
        rememberKey = null,
        rememberLabel = '',
    }) {
        if (rememberKey && this.getRememberFlag(rememberKey)) {
            return Promise.resolve(true);
        }

        this.ensureConfirmModal();
        const { title: titleEl, message: messageEl, remember, rememberCheckbox, rememberLabel: rememberLabelEl, okBtn } = this.confirmElements;

        titleEl.textContent = title;
        messageEl.textContent = message;
        okBtn.textContent = confirmText;
        okBtn.className = `btn ${confirmClass}`;

        if (rememberKey) {
            remember.style.display = 'flex';
            rememberCheckbox.checked = false;
            rememberLabelEl.textContent = rememberLabel;
        } else {
            remember.style.display = 'none';
            rememberCheckbox.checked = false;
            rememberLabelEl.textContent = '';
        }

        this.confirmModal.classList.add('visible');

        if (this.confirmResolve) {
            const pending = this.confirmResolve;
            this.confirmResolve = null;
            pending(false);
        }

        return new Promise((resolve) => {
            this.confirmResolve = (result) => {
                if (result && rememberKey && rememberCheckbox.checked) {
                    this.setRememberFlag(rememberKey, true);
                }
                resolve(result);
            };
        });
    },

    // ==================== Safetensors Header Modal ====================

    async openSafetensorsHeaderModal(relpath, side = 'auto') {
        let modal = document.getElementById('safetensors-header-modal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'safetensors-header-modal';
            modal.className = 'modal-overlay';
            modal.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">
                        <h3>Safetensors Header</h3>
                        <button class="modal-close" onclick="Sync.closeSafetensorsHeaderModal()">×</button>
                    </div>
                    <div class="modal-body">
                        <p class="modal-filename"></p>
                        <p class="modal-hash safetensors-source"></p>
                        <div class="safetensors-json">Loading...</div>
                    </div>
                    <div class="modal-footer">
                        <button class="btn" data-action="safetensors-copy">Copy All</button>
                        <button class="btn" onclick="Sync.closeSafetensorsHeaderModal()">Close</button>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);

            modal.addEventListener('click', (e) => {
                if (e.target === modal) this.closeSafetensorsHeaderModal();
            });

            const copyBtn = modal.querySelector('[data-action="safetensors-copy"]');
            copyBtn.addEventListener('click', () => this.copySafetensorsHeader());
        }

        modal.querySelector('.modal-filename').textContent = `File: ${relpath}`;
        const sourceEl = modal.querySelector('.safetensors-source');
        sourceEl.textContent = side === 'auto' ? 'Source: auto' : `Source: ${side}`;

        const jsonEl = modal.querySelector('.safetensors-json');
        jsonEl.textContent = 'Loading...';

        modal.classList.add('visible');

        try {
            const res = await App.api(
                'GET',
                `/index/safetensors/header?relpath=${encodeURIComponent(relpath)}&side=${encodeURIComponent(side)}`
            );
            sourceEl.textContent = `Source: ${res.side || side}`;
            jsonEl.textContent = JSON.stringify(res.header, null, 2);
        } catch (err) {
            jsonEl.textContent = `Error: ${err.message}`;
        }
    },

    closeSafetensorsHeaderModal() {
        const modal = document.getElementById('safetensors-header-modal');
        if (modal) {
            modal.classList.remove('visible');
        }
    },

    async copySafetensorsHeader() {
        const modal = document.getElementById('safetensors-header-modal');
        if (!modal) return;
        const jsonEl = modal.querySelector('.safetensors-json');
        if (!jsonEl) return;
        const text = jsonEl.textContent || '';
        if (!text.trim()) return;

        try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                await navigator.clipboard.writeText(text);
                return;
            }
        } catch (err) {
            // Fall back below
        }

        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.top = '-1000px';
        textarea.style.left = '-1000px';
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        try {
            document.execCommand('copy');
        } catch (err) {
            // Ignore copy errors
        }
        document.body.removeChild(textarea);
    },

    // ==================== Source URL Modal ====================

    openSourceUrlModal(hash, relpath, filename) {
        // Check if modal exists, create if not
        let modal = document.getElementById('source-url-modal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'source-url-modal';
            modal.className = 'modal-overlay';
            modal.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">
                        <h3>🔗 Source URL</h3>
                        <button class="modal-close" onclick="Sync.closeSourceUrlModal()">×</button>
                    </div>
                    <div class="modal-body">
                        <p class="modal-filename"></p>
                        <p class="modal-hash"></p>
                        <label for="source-url-input">Public Web URL:</label>
                        <input type="url" id="source-url-input" class="modal-input" placeholder="https://huggingface.co/model-org/model-name/resolve/main/model.safetensors" />
                        <div id="url-test-result" style="margin-top: -8px; margin-bottom: 12px; font-size: 12px; display: none;"></div>
                        <div id="ai-lookup-result" class="ai-lookup-result"></div>
                        <p class="modal-hint">Enter the public download URL for this model. This allows remote provisioning to download directly from the source.</p>
                        <p class="modal-hash-hint"></p>
                    </div>
                    <div class="modal-footer">
                        <button class="btn btn-danger" id="source-url-delete" style="margin-right: auto;">Delete</button>
                        <button class="btn btn-ai" id="source-url-ai">✨ Find URL</button>
                        <button class="btn" id="source-url-test">🔍 Test Link</button>
                        <button class="btn" onclick="Sync.closeSourceUrlModal()">Cancel</button>
                        <button class="btn btn-primary" id="source-url-save">Save</button>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);

            // Close on overlay click
            modal.addEventListener('click', (e) => {
                if (e.target === modal) this.closeSourceUrlModal();
            });
        }

        // Check for existing source URL (by hash or relpath)
        const hasHash = hash && hash.length > 0;
        const existingByHash = hasHash ? this.sourceUrls.get(hash) : null;
        const existingByRelpath = this.sourceUrlsByRelpath?.get(relpath);
        const existing = existingByHash || existingByRelpath;

        // Populate modal
        this.activeSourceContext = { filename, relpath, hash };
        modal.querySelector('.modal-filename').textContent = `File: ${filename}`;
        modal.querySelector('.modal-hash').textContent = hasHash ? `Hash: ${hash}` : `Path: ${relpath}`;
        modal.querySelector('#source-url-input').value = existing?.url || '';

        // Show hint if unhashed
        const hashHint = modal.querySelector('.modal-hash-hint');
        if (!hasHash) {
            hashHint.textContent = '⚠️ File not yet hashed. URL will be saved by path and a hash will be queued.';
            hashHint.style.color = 'var(--warning)';
            hashHint.style.marginTop = '8px';
        } else {
            hashHint.textContent = '';
        }

        // Show/hide delete button
        const deleteBtn = modal.querySelector('#source-url-delete');
        deleteBtn.style.display = existing ? 'block' : 'none';

        // Bind save action
        const saveBtn = modal.querySelector('#source-url-save');
        saveBtn.onclick = () => this.saveSourceUrl(hash, relpath, filename);

        // Bind delete action
        deleteBtn.onclick = () => this.deleteSourceUrl(hash, relpath);

        // Bind test action
        const testBtn = modal.querySelector('#source-url-test');
        const testResult = modal.querySelector('#url-test-result');
        testResult.style.display = 'none';
        testBtn.onclick = () => this.testSourceUrl();

        // Bind AI lookup
        const aiBtn = modal.querySelector('#source-url-ai');
        const aiResult = modal.querySelector('#ai-lookup-result');
        if (aiResult) {
            aiResult.style.display = 'none';
            aiResult.textContent = '';
        }
        if (aiBtn) {
            aiBtn.disabled = false;
            aiBtn.textContent = '✨ Find URL';
            aiBtn.onclick = () => this.findSourceUrlWithAI();
        }

        // Show modal
        modal.classList.add('visible');
        modal.querySelector('#source-url-input').focus();
    },

    async findSourceUrlWithAI() {
        const context = this.activeSourceContext;
        if (!context?.filename) return;

        const resultDiv = document.getElementById('ai-lookup-result');
        const aiBtn = document.getElementById('source-url-ai');

        if (!resultDiv || !aiBtn) return;

        aiBtn.disabled = true;
        aiBtn.textContent = '✨ Searching...';
        resultDiv.style.display = 'block';
        resultDiv.style.color = 'var(--text-muted)';
        resultDiv.textContent = 'Queued AI lookup. You can review progress in the AI Review tab.';

        try {
            const res = await App.api('POST', '/ai/lookup/jobs', {
                items: [
                    {
                        filename: context.filename,
                        relpath: context.relpath || null,
                        file_hash: context.hash || null,
                    }
                ]
            });

            const created = res.created || 0;
            const skipped = res.skipped || 0;
            resultDiv.style.color = 'var(--text-secondary)';
            if (created > 0) {
                resultDiv.textContent = `✅ Queued ${created} AI lookup job. Check AI Review for updates.`;
            } else if (skipped > 0) {
                resultDiv.textContent = 'ℹ️ An AI lookup is already queued or completed for this file. Check AI Review.';
            } else {
                resultDiv.textContent = 'No job created.';
            }
        } catch (err) {
            resultDiv.style.color = 'var(--danger)';
            resultDiv.textContent = `❌ ${err.message}`;
        } finally {
            aiBtn.disabled = false;
            aiBtn.textContent = '✨ Find URL';
        }
    },

    async testSourceUrl() {
        const input = document.getElementById('source-url-input');
        const url = input.value.trim();
        const resultDiv = document.getElementById('url-test-result');
        const testBtn = document.getElementById('source-url-test');

        if (!url) return null;

        testBtn.disabled = true;
        testBtn.textContent = '⏱ Testing...';
        resultDiv.style.display = 'block';
        resultDiv.style.color = 'var(--text-muted)';
        resultDiv.textContent = 'Checking URL connectivity...';

        try {
            const res = await App.api('GET', `/index/check-url?url=${encodeURIComponent(url)}`);
            if (res.ok) {
                resultDiv.style.color = 'var(--success)';
                const sizeStr = res.size ? ` (${(res.size / (1024 * 1024)).toFixed(1)} MB)` : '';
                resultDiv.innerHTML = `✅ Link OK! HTTP ${res.status}${sizeStr}`;
                return res;
            } else {
                resultDiv.style.color = 'var(--danger)';
                if (res.is_webpage) {
                    resultDiv.textContent = `❌ Error: URL is a webpage, not a file download (HTTP ${res.status})`;
                } else {
                    resultDiv.textContent = `❌ Failed: ${res.error || 'Status ' + res.status}`;
                }
                return res;
            }
        } catch (err) {
            resultDiv.style.color = 'var(--danger)';
            resultDiv.textContent = `❌ Error: ${err.message}`;
            return { ok: false, error: err.message };
        } finally {
            testBtn.disabled = false;
            testBtn.textContent = '🔍 Test Link';
        }
    },

    closeSourceUrlModal() {
        const modal = document.getElementById('source-url-modal');
        if (modal) {
            modal.classList.remove('visible');
        }
        this.activeSourceContext = null;
    },

    async saveSourceUrl(hash, relpath, filename) {
        const input = document.getElementById('source-url-input');
        const url = input.value.trim();

        if (!url) {
            alert('Please enter a URL');
            return;
        }

        // Auto-test before save
        const testResult = await this.testSourceUrl();
        if (testResult && !testResult.ok) {
            const msg = testResult.is_webpage
                ? "This URL looks like a webpage, not a direct file download. Remote downloads will likely fail.\n\nAre you sure you want to save it anyway?"
                : "The link validation failed. Remote downloads will likely fail.\n\nAre you sure you want to save it anyway?";

            const confirmed = await this.confirmAction({
                title: 'Save Source URL',
                message: msg,
                confirmText: 'Save',
                confirmClass: 'btn-primary',
            });
            if (!confirmed) return;
        }

        const hasHash = hash && hash.length > 0;

        try {
            if (hasHash) {
                // Has hash - save by hash
                await App.api('PUT', `/index/sources/${hash}`, {
                    url: url,
                    filename_hint: filename,
                });
                // Update local cache
                this.sourceUrls.set(hash, { key: hash, url, filename_hint: filename });
            } else {
                // No hash - save by relpath and queue hash
                await App.api('PUT', `/index/sources/by-relpath/${encodeURIComponent(relpath)}`, {
                    url: url,
                    filename_hint: filename,
                    queue_hash: true,
                });
                // Update local cache
                this.sourceUrlsByRelpath = this.sourceUrlsByRelpath || new Map();
                this.sourceUrlsByRelpath.set(relpath, { key: `relpath:${relpath}`, url, filename_hint: filename, relpath });
                // Refresh queue
                App.loadQueueTasks();
            }

            // Close modal and re-render to update button state
            this.closeSourceUrlModal();
            this.render(document.getElementById('search-input')?.value || '');

        } catch (err) {
            alert('Failed to save source URL: ' + err.message);
        }
    },

    async deleteSourceUrl(hash, relpath) {
        const confirmed = await this.confirmAction({
            title: 'Remove Source URL',
            message: 'Remove the source URL for this file?',
            confirmText: 'Remove',
            confirmClass: 'btn-danger',
        });
        if (!confirmed) return;

        const hasHash = hash && hash.length > 0;
        const existingByHash = hasHash ? this.sourceUrls.get(hash) : null;

        try {
            if (existingByHash) {
                // Delete by hash
                await App.api('DELETE', `/index/sources/${hash}`);
                this.sourceUrls.delete(hash);
            } else {
                // Delete by relpath
                await App.api('DELETE', `/index/sources/by-relpath/${encodeURIComponent(relpath)}`);
                this.sourceUrlsByRelpath?.delete(relpath);
            }

            // Close modal and re-render
            this.closeSourceUrlModal();
            this.render(document.getElementById('search-input')?.value || '');

        } catch (err) {
            alert('Failed to delete source URL: ' + err.message);
        }
    },

    // ==================== Add to Bundle Modal ====================

    openAddToBundleModal(hash, relpath, filename) {
        let modal = document.getElementById('add-to-bundle-modal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'add-to-bundle-modal';
            modal.className = 'modal-overlay';
            modal.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">
                        <h3>📦 Add to Bundle</h3>
                        <button class="modal-close" onclick="Sync.closeAddToBundleModal()">×</button>
                    </div>
                    <div class="modal-body">
                        <p class="modal-filename" style="font-weight: 500; margin-bottom: 12px;"></p>
                        <p style="margin-bottom: 8px;">Select a bundle to add this file to:</p>
                        <div id="bundle-options-list" style="max-height: 200px; overflow-y: auto; border: 1px solid var(--border); border-radius: var(--radius-sm);">
                            <!-- Bundle list injected here -->
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button class="btn" onclick="Sync.closeAddToBundleModal()">Cancel</button>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);

            modal.addEventListener('click', (e) => {
                if (e.target === modal) this.closeAddToBundleModal();
            });
        }

        modal.querySelector('.modal-filename').textContent = filename;
        const list = document.getElementById('bundle-options-list');

        if (this.bundles.length === 0) {
            list.innerHTML = `<div style="padding: 12px; color: var(--text-muted); text-align: center;">No bundles found. Create one in the <a href="/bundles">Bundles</a> page first.</div>`;
        } else {
            list.innerHTML = this.bundles.map(b => `
                <div class="bundle-option" onclick="Sync.addAssetToBundle('${b.name}', '${relpath}', '${hash || ''}')" 
                     style="padding: 10px 12px; border-bottom: 1px solid var(--border); cursor: pointer; transition: background 0.2s;">
                    ${b.name}
                </div>
            `).join('');

            // Add hover effect
            list.querySelectorAll('.bundle-option').forEach(opt => {
                opt.onmouseover = () => opt.style.background = 'var(--bg-hover)';
                opt.onmouseout = () => opt.style.background = 'transparent';
            });
        }

        modal.style.display = 'flex';
        modal.classList.add('visible');
    },

    openAddFolderToBundleModal(folderPath) {
        let modal = document.getElementById('add-folder-to-bundle-modal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'add-folder-to-bundle-modal';
            modal.className = 'modal-overlay';
            modal.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">
                        <h3>📦 Add Folder to Bundle</h3>
                        <button class="modal-close" onclick="Sync.closeAddFolderToBundleModal()">×</button>
                    </div>
                    <div class="modal-body">
                        <p class="modal-folder-path" style="font-weight: 500; margin-bottom: 12px; font-family: var(--font-mono); font-size: 13px;"></p>
                        <p style="margin-bottom: 8px;">Select a bundle to add ALL files in this folder to:</p>
                        <div id="bundle-folder-options-list" style="max-height: 200px; overflow-y: auto; border: 1px solid var(--border); border-radius: var(--radius-sm);">
                            <!-- Bundle list injected here -->
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button class="btn" onclick="Sync.closeAddFolderToBundleModal()">Cancel</button>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);

            modal.addEventListener('click', (e) => {
                if (e.target === modal) this.closeAddFolderToBundleModal();
            });
        }

        modal.querySelector('.modal-folder-path').textContent = folderPath || '(root)';
        const list = document.getElementById('bundle-folder-options-list');

        if (this.bundles.length === 0) {
            list.innerHTML = `<div style="padding: 12px; color: var(--text-muted); text-align: center;">No bundles found. Create one first.</div>`;
        } else {
            list.innerHTML = this.bundles.map(b => `
                <div class="bundle-option" onclick="Sync.addFolderToBundle('${b.name}', '${folderPath}')" 
                     style="padding: 10px 12px; border-bottom: 1px solid var(--border); cursor: pointer; transition: background 0.2s;">
                    ${b.name}
                </div>
            `).join('');

            list.querySelectorAll('.bundle-option').forEach(opt => {
                opt.onmouseover = () => opt.style.background = 'var(--bg-hover)';
                opt.onmouseout = () => opt.style.background = 'transparent';
            });
        }

        modal.style.display = 'flex';
        modal.classList.add('visible');
    },

    closeAddFolderToBundleModal() {
        const modal = document.getElementById('add-folder-to-bundle-modal');
        if (modal) {
            modal.style.display = 'none';
            modal.classList.remove('visible');
        }
    },

    async addFolderToBundle(bundleName, folderPath) {
        try {
            await App.api('POST', `/bundles/${encodeURIComponent(bundleName)}/assets/folder?folder_path=${encodeURIComponent(folderPath)}`);
            this.closeAddFolderToBundleModal();
        } catch (err) {
            alert('Failed to add folder: ' + err.message);
        }
    },

    closeAddToBundleModal() {
        const modal = document.getElementById('add-to-bundle-modal');
        if (modal) {
            modal.style.display = 'none';
            modal.classList.remove('visible');
        }
    },

    async addAssetToBundle(bundleName, relpath, hash) {
        try {
            await App.api('POST', `/bundles/${encodeURIComponent(bundleName)}/assets`, {
                relpath: relpath,
                hash: hash || null
            });

            this.closeAddToBundleModal();

            // Just a small notification would be nice, but for now just close
            console.log(`Added ${relpath} to ${bundleName}`);
        } catch (err) {
            alert('Failed to add to bundle: ' + err.message);
        }
    }
};

document.addEventListener('DOMContentLoaded', () => Sync.init());
