/**
 * Sync Page JS - Unified Diff Tree View
 */

const Sync = {
    diffData: [],      // Raw diff entries from API
    treeData: null,    // Hierarchical tree structure
    expandedFolders: new Set(),
    selectedItems: new Set(),

    async init() {
        this.bindEvents();
        await this.refresh();
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

            html += `
                <div class="diff-row diff-row-folder" data-path="${folderPath}" data-depth="${depth}">
                    <div class="diff-col diff-col-local">
                        <span class="folder-status ${folderStatus.local}">${folderStatus.localIcon}</span>
                    </div>
                    <div class="diff-col diff-col-path">
                        <span class="tree-indent" style="width: ${depth * 20}px"></span>
                        <span class="folder-toggle ${isExpanded ? 'expanded' : ''}" data-folder="${folderPath}">
                            ${isExpanded ? '▼' : '▶'}
                        </span>
                        <span class="folder-icon">📁</span>
                        <span class="folder-name">${folderName}</span>
                        <span class="folder-count">(${itemCount})</span>
                    </div>
                    <div class="diff-col diff-col-lake">
                        <span class="folder-status ${folderStatus.lake}">${folderStatus.lakeIcon}</span>
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

            html += `
                <div class="diff-row diff-row-file ${statusClass}" data-relpath="${file.relpath}" data-depth="${depth}">
                    <div class="diff-col diff-col-local">
                        ${file.local_size !== null ? `
                            <span class="file-size">${App.formatBytes(file.local_size)}</span>
                            <button class="btn-icon btn-copy" data-action="copy-to-lake" data-relpath="${file.relpath}" title="Copy to Lake →">→</button>
                        ` : '<span class="missing">—</span>'}
                    </div>
                    <div class="diff-col diff-col-path">
                        <span class="tree-indent" style="width: ${depth * 20}px"></span>
                        <span class="status-icon ${statusClass}" title="${this.getStatusTooltip(file.status)}">${statusIcon}</span>
                        <span class="file-name" title="${file.relpath}">${file.filename}</span>
                    </div>
                    <div class="diff-col diff-col-lake">
                        ${file.lake_size !== null ? `
                            <button class="btn-icon btn-copy" data-action="copy-to-local" data-relpath="${file.relpath}" title="← Copy to Local">←</button>
                            <span class="file-size">${App.formatBytes(file.lake_size)}</span>
                        ` : '<span class="missing">—</span>'}
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
        let hasLocal = false, hasLake = false, hasConflict = false;

        const checkNode = (n) => {
            for (const file of n.files) {
                if (file.status === 'conflict') hasConflict = true;
                if (file.local_size !== null) hasLocal = true;
                if (file.lake_size !== null) hasLake = true;
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

        // Folder toggle
        if (target.classList.contains('folder-toggle') || target.closest('.folder-toggle')) {
            const toggle = target.classList.contains('folder-toggle') ? target : target.closest('.folder-toggle');
            const folderPath = toggle.dataset.folder;
            this.toggleFolder(folderPath);
            return;
        }

        // Copy buttons
        if (target.dataset.action === 'copy-to-lake' || target.dataset.action === 'copy-to-local') {
            const relpath = target.dataset.relpath;
            const srcSide = target.dataset.action === 'copy-to-lake' ? 'local' : 'lake';
            const dstSide = target.dataset.action === 'copy-to-lake' ? 'lake' : 'local';
            this.enqueueCopy(srcSide, relpath, dstSide);
            return;
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
            await App.api('POST', '/queue/copy', {
                src_side: srcSide,
                src_relpath: relpath,
                dst_side: dstSide,
            });
            // Visual feedback
            const row = document.querySelector(`[data-relpath="${relpath}"]`);
            if (row) {
                row.classList.add('queued');
                setTimeout(() => row.classList.remove('queued'), 1000);
            }
        } catch (err) {
            alert('Copy failed: ' + err.message);
        }
    }
};

document.addEventListener('DOMContentLoaded', () => Sync.init());
