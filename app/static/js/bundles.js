/**
 * Bundles Page JS
 */

const Bundles = {
    bundles: [],
    activeBundle: null,
    activeSourceContext: null,
    customNodeSearchResults: [],

    async init() {
        this.bindEvents();
        await this.loadBundles();

        // Handle initial selection if hash provided
        const hash = window.location.hash.slice(1);
        if (hash) {
            this.selectBundle(decodeURIComponent(hash));
        }
    },

    bindEvents() {
        document.getElementById('create-bundle-btn').addEventListener('click', () => this.openModal());
    },

    async loadBundles() {
        try {
            const result = await App.api('GET', '/bundles');
            this.bundles = result.bundles;
            this.renderSidebar();
        } catch (err) {
            console.error('Failed to load bundles:', err);
            document.getElementById('bundles-list').innerHTML = `<div class="error" style="padding:16px;">Failed to load bundles: ${err.message}</div>`;
        }
    },

    renderSidebar() {
        const list = document.getElementById('bundles-list');
        if (this.bundles.length === 0) {
            list.innerHTML = `<div style="padding: 16px; color: var(--text-muted); text-align: center;">No bundles created yet.<br>Click "+ New" to start.</div>`;
            return;
        }

        list.innerHTML = this.bundles.map(b => `
            <div class="bundle-item ${this.activeBundle?.name === b.name ? 'active' : ''}" onclick="Bundles.selectBundle('${b.name}')">
                <div class="bundle-name">${b.name}</div>
                <div class="bundle-meta">
                    <span>${b.assets.length || b.asset_count || 0} items</span>
                    <span>${new Date(b.updated_at).toLocaleDateString()}</span>
                </div>
            </div>
        `).join('');
    },

    async selectBundle(name) {
        try {
            window.location.hash = name;
            const bundle = await App.api('GET', `/bundles/${encodeURIComponent(name)}`);
            this.activeBundle = bundle;

            // Sync with sidebar list
            const idx = this.bundles.findIndex(b => b.name === name);
            if (idx > -1) {
                this.bundles[idx].asset_count = bundle.asset_count;
            }

            this.renderSidebar(); // update active state
            this.renderDetail();
        } catch (err) {
            console.error('Failed to load bundle details:', err);
            alert('Failed to load bundle: ' + err.message);
        }
    },

    renderDetail() {
        const container = document.getElementById('bundle-detail');
        const b = this.activeBundle;
        if (!b) return;

        container.innerHTML = `
            <div class="bundle-header-detail">
                <div>
                    <h2 style="margin-bottom: 8px;">${b.name}</h2>
                    <p style="color: var(--text-secondary); max-width: 600px;">${b.description || 'No description'}</p>
                </div>
                <div style="display: flex; gap: 8px;">
                    <button class="btn" onclick="Bundles.openInputAssetModal()">+ Input File</button>
                    <button class="btn" onclick="Bundles.openCustomNodeModal()">+ Custom Node</button>
                    <button class="btn" onclick="Bundles.openModal(true)">Edit</button>
                    <button class="btn btn-danger" onclick="Bundles.deleteBundle('${b.name}')">Delete Bundle</button>
                </div>
            </div>

            <div class="bundle-assets">
                <h3 style="margin-bottom: 16px;">Custom Nodes (${(b.custom_nodes || []).length})</h3>
                ${this.renderCustomNodesTable(b.custom_nodes || [])}
            </div>

            <div class="bundle-assets">
                <h3 style="margin-bottom: 16px;">Assets (${b.assets.length})</h3>
                ${this.renderAssetsTable(b.assets)}
            </div>
        `;
    },

    renderAssetsTable(assets) {
        if (!assets || assets.length === 0) {
            return `<div style="padding: 32px; text-align: center; border: 1px dashed var(--border); border-radius: var(--radius-md); color: var(--text-muted);">
                No assets in this bundle.<br>
                Go to <a href="/sync">Sync</a> to add model files, or add source files used by workflows here.<br>
                <button class="btn btn-primary" style="margin-top: 16px;" onclick="Bundles.openInputAssetModal()">+ Input File</button>
            </div>`;
        }

        const grouped = new Map();
        for (const asset of assets) {
            const rootType = asset.root_type || 'models';
            const group = `${this.formatRootType(rootType)} / ${this.getAssetGroup(asset.relpath)}`;
            if (!grouped.has(group)) grouped.set(group, []);
            grouped.get(group).push(asset);
        }

        const groupEntries = Array.from(grouped.entries()).map(([group, items]) => ({
            group,
            items,
            totalBytes: items.reduce((sum, item) => sum + (item.size || 0), 0),
        }));

        groupEntries.sort((a, b) => {
            if (b.totalBytes !== a.totalBytes) return b.totalBytes - a.totalBytes;
            return a.group.localeCompare(b.group);
        });

        const rows = [];
        for (const entry of groupEntries) {
            const { group, items, totalBytes } = entry;
            rows.push(`
                <tr class="asset-group-row">
                    <td colspan="4">
                        <span class="asset-group-badge">${group}</span>
                        <span class="asset-group-meta">${items.length} item${items.length === 1 ? '' : 's'} • ${totalBytes ? App.formatBytes(totalBytes) : '—'}</span>
                    </td>
                </tr>
            `);

            for (const a of items) {
                const rootType = a.root_type || 'models';
                rows.push(`
                    <tr>
                        <td title="${a.relpath}">
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <span class="source-badge ${rootType === 'input' ? 'web' : 'local'}">${this.formatRootType(rootType)}</span>
                                <span style="font-family: var(--font-mono); font-size: 13px;">${a.relpath}</span>
                            </div>
                            ${a.hash ? `<div style="font-size: 11px; color: var(--text-muted);">Hash: ${a.hash.slice(0, 12)}...</div>` : ''}
                        </td>
                        <td class="asset-size-cell">${a.size ? App.formatBytes(a.size) : '—'}</td>
                        <td class="asset-url-cell">
                            <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
                                ${a.source_url_override || a.source_url ? `<span title="Linked">✅</span>` : '<span style="color: var(--text-muted);">-</span>'}
                                ${a.source_url_override || a.source_url ? `
                                    <button class="btn btn-small" style="font-size: 10px; padding: 2px 6px;" 
                                            onclick="Bundles.testUrl('${a.source_url_override || a.source_url}', this)">Test</button>
                                ` : ''}
                                <button class="btn btn-small" style="font-size: 10px; padding: 2px 6px;" 
                                        onclick="Bundles.openSourceUrlModal('${rootType}', '${a.relpath}', '${a.hash || ''}')">${a.source_url_override || a.source_url ? 'Edit' : 'Link'}</button>
                            </div>
                        </td>
                        <td>
                            <button class="btn-icon btn-danger" onclick="Bundles.removeAsset('${rootType}', '${a.relpath}')" title="Remove from bundle">✕</button>
                        </td>
                    </tr>
                `);
            }
        }

        return `
            <table class="asset-table">
                <thead>
                    <tr>
                        <th>Path</th>
                        <th style="width: 110px; text-align: right;">Size</th>
                        <th style="width: 100px;">Override</th>
                        <th style="width: 80px;">Actions</th>
                    </tr>
                </thead>
                <tbody>
                    ${rows.join('')}
                </tbody>
            </table>
        `;
    },

    renderCustomNodesTable(nodes) {
        if (!nodes || nodes.length === 0) {
            return `<div style="padding: 20px; text-align: center; border: 1px dashed var(--border); border-radius: var(--radius-md); color: var(--text-muted);">
                No custom node packs in this bundle.<br>
                <button class="btn btn-primary" style="margin-top: 12px;" onclick="Bundles.openCustomNodeModal()">+ Custom Node</button>
            </div>`;
        }

        const rows = nodes.map(n => {
            const installType = n.install_type || 'registry';
            const label = n.name || n.node_id;
            const meta = installType === 'git' ? (n.repository || n.node_id) : n.node_id;
            return `
                <tr>
                    <td>
                        <div style="display: flex; align-items: center; gap: 8px;">
                            <span class="source-badge ${installType === 'git' ? 'web' : 'local'}">${installType}</span>
                            <span style="font-weight: 600;">${label}</span>
                        </div>
                        <div style="font-family: var(--font-mono); font-size: 11px; color: var(--text-muted);">${meta}${n.version ? ` @ ${n.version}` : ''}</div>
                    </td>
                    <td style="width: 80px;">
                        <button class="btn-icon btn-danger" onclick="Bundles.removeCustomNode('${installType}', '${n.node_id}')" title="Remove custom node">✕</button>
                    </td>
                </tr>
            `;
        }).join('');

        return `
            <table class="asset-table">
                <thead>
                    <tr>
                        <th>Node Pack</th>
                        <th style="width: 80px;">Actions</th>
                    </tr>
                </thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    },

    getAssetGroup(relpath) {
        if (!relpath) return 'root';
        const parts = relpath.split('/');
        return parts[0] || 'root';
    },

    formatRootType(rootType) {
        return rootType === 'input' ? 'input' : 'models';
    },

    async testUrl(url, btn) {
        const originalText = btn.textContent;
        btn.disabled = true;
        btn.textContent = '...';

        try {
            const res = await App.api('GET', `/index/check-url?url=${encodeURIComponent(url)}`);
            if (res.ok) {
                const sizeStr = res.size ? ` (${(res.size / (1024 * 1024)).toFixed(1)} MB)` : '';
                alert(`✅ Link OK!\nStatus: ${res.status}${sizeStr}\nType: ${res.type || 'unknown'}`);
            } else {
                alert(`❌ Link Failed!\nError: ${res.error || 'HTTP ' + res.status}`);
            }
        } catch (err) {
            alert(`❌ Error: ${err.message}`);
        } finally {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    },

    // ==================== Source URL Modal ====================

    openSourceUrlModal(rootType, relpath, hash) {
        rootType = rootType || 'models';
        let modal = document.getElementById('bundle-source-url-modal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'bundle-source-url-modal';
            modal.className = 'modal-overlay';
            modal.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">
                        <h3>🔗 Source URL</h3>
                        <button class="modal-close" onclick="Bundles.closeSourceUrlModal()">×</button>
                    </div>
                    <div class="modal-body">
                        <p class="modal-filename"></p>
                        <p class="modal-hash"></p>
                        <label for="bundle-source-url-input">Public Web URL:</label>
                        <input type="url" id="bundle-source-url-input" class="modal-input" placeholder="https://huggingface.co/model-org/model-name/resolve/main/model.safetensors" />
                        <div id="bundle-url-test-result" style="margin-top: -8px; margin-bottom: 12px; font-size: 12px; display: none;"></div>
                        <p class="modal-hint">Enter the public download URL for this file. This allows remote provisioning to download directly from the source.</p>
                        <p class="modal-hash-hint"></p>
                    </div>
                    <div class="modal-footer">
                        <button class="btn btn-danger" id="bundle-source-url-delete" style="margin-right: auto;">Delete</button>
                        <button class="btn" id="bundle-source-url-test">🔍 Test Link</button>
                        <button class="btn" onclick="Bundles.closeSourceUrlModal()">Cancel</button>
                        <button class="btn btn-primary" id="bundle-source-url-save">Save</button>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);

            modal.addEventListener('click', (e) => {
                if (e.target === modal) this.closeSourceUrlModal();
            });
        }

        const filename = relpath ? relpath.split('/').pop() : '';
        const existingAsset = this.activeBundle?.assets?.find(a => (a.root_type || 'models') === rootType && a.relpath === relpath);
        const existingUrl = existingAsset?.source_url_override || existingAsset?.source_url || '';

        this.activeSourceContext = { rootType, relpath, hash, filename };
        modal.querySelector('.modal-filename').textContent = `File: ${filename || relpath}`;
        modal.querySelector('.modal-hash').textContent = hash ? `Root: ${rootType} · Hash: ${hash}` : `Root: ${rootType} · Path: ${relpath}`;
        modal.querySelector('#bundle-source-url-input').value = existingUrl;

        const hashHint = modal.querySelector('.modal-hash-hint');
        if (!hash && rootType === 'models') {
            hashHint.textContent = '⚠️ File not yet hashed. URL will be saved by path and a hash will be queued.';
            hashHint.style.color = 'var(--warning)';
            hashHint.style.marginTop = '8px';
        } else if (rootType === 'input') {
            hashHint.textContent = 'Input files stream from your local ComfyUI/input folder. A URL is only a fallback.';
            hashHint.style.color = 'var(--text-muted)';
            hashHint.style.marginTop = '8px';
        } else {
            hashHint.textContent = '';
        }

        const deleteBtn = modal.querySelector('#bundle-source-url-delete');
        deleteBtn.style.display = existingUrl ? 'block' : 'none';

        const saveBtn = modal.querySelector('#bundle-source-url-save');
        saveBtn.onclick = () => this.saveSourceUrl();

        deleteBtn.onclick = () => this.deleteSourceUrl();

        const testBtn = modal.querySelector('#bundle-source-url-test');
        const testResult = modal.querySelector('#bundle-url-test-result');
        testResult.style.display = 'none';
        testBtn.onclick = () => this.testSourceUrl();

        modal.classList.add('visible');
        modal.querySelector('#bundle-source-url-input').focus();
    },

    async testSourceUrl() {
        const input = document.getElementById('bundle-source-url-input');
        const url = input.value.trim();
        const resultDiv = document.getElementById('bundle-url-test-result');
        const testBtn = document.getElementById('bundle-source-url-test');

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
            }
            resultDiv.style.color = 'var(--danger)';
            if (res.is_webpage) {
                resultDiv.textContent = `❌ Error: URL is a webpage, not a file download (HTTP ${res.status})`;
            } else {
                resultDiv.textContent = `❌ Failed: ${res.error || 'Status ' + res.status}`;
            }
            return res;
        } catch (err) {
            resultDiv.style.color = 'var(--danger)';
            resultDiv.textContent = `❌ Error: ${err.message}`;
            return { ok: false, error: err.message };
        } finally {
            testBtn.disabled = false;
            testBtn.textContent = '🔍 Test Link';
        }
    },

    async saveSourceUrl() {
        const ctx = this.activeSourceContext;
        if (!ctx) return;

        const input = document.getElementById('bundle-source-url-input');
        const url = input.value.trim();
        if (!url) {
            alert('Please enter a URL');
            return;
        }

        const testResult = await this.testSourceUrl();
        if (testResult && !testResult.ok) {
            const msg = testResult.is_webpage
                ? "This URL looks like a webpage, not a direct file download. Remote downloads will likely fail.\n\nSave anyway?"
                : "The link validation failed. Remote downloads will likely fail.\n\nSave anyway?";
            if (!confirm(msg)) return;
        }

        try {
            if (ctx.rootType === 'input') {
                await App.api('POST', `/bundles/${encodeURIComponent(this.activeBundle.name)}/assets`, {
                    root_type: 'input',
                    relpath: ctx.relpath,
                    hash: ctx.hash || null,
                    source_url_override: url,
                });
            } else if (ctx.hash) {
                await App.api('PUT', `/index/sources/${ctx.hash}`, {
                    url,
                    filename_hint: ctx.filename,
                });
            } else {
                await App.api('PUT', `/index/sources/by-relpath/${encodeURIComponent(ctx.relpath)}`, {
                    url,
                    filename_hint: ctx.filename,
                    queue_hash: true,
                });
            }

            this.closeSourceUrlModal();
            if (this.activeBundle?.name) {
                await this.selectBundle(this.activeBundle.name);
            }
        } catch (err) {
            alert('Failed to save source URL: ' + err.message);
        }
    },

    async deleteSourceUrl() {
        const ctx = this.activeSourceContext;
        if (!ctx) return;

        if (!confirm('Remove the source URL for this file?')) return;

        try {
            if (ctx.rootType === 'input') {
                await App.api('POST', `/bundles/${encodeURIComponent(this.activeBundle.name)}/assets`, {
                    root_type: 'input',
                    relpath: ctx.relpath,
                    hash: ctx.hash || null,
                    source_url_override: null,
                });
            } else if (ctx.hash) {
                try {
                    await App.api('DELETE', `/index/sources/${ctx.hash}`);
                } catch (err) {
                    await App.api('DELETE', `/index/sources/by-relpath/${encodeURIComponent(ctx.relpath)}`);
                }
            } else {
                await App.api('DELETE', `/index/sources/by-relpath/${encodeURIComponent(ctx.relpath)}`);
            }

            this.closeSourceUrlModal();
            if (this.activeBundle?.name) {
                await this.selectBundle(this.activeBundle.name);
            }
        } catch (err) {
            alert('Failed to delete source URL: ' + err.message);
        }
    },

    closeSourceUrlModal() {
        const modal = document.getElementById('bundle-source-url-modal');
        if (modal) {
            modal.classList.remove('visible');
        }
        this.activeSourceContext = null;
    },

    // ==================== Input Asset Modal ====================

    openInputAssetModal() {
        let modal = document.getElementById('bundle-input-asset-modal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'bundle-input-asset-modal';
            modal.className = 'modal-overlay';
            modal.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">
                        <h3>Add Input File</h3>
                        <button class="modal-close" onclick="Bundles.closeInputAssetModal()">×</button>
                    </div>
                    <div class="modal-body">
                        <label for="bundle-input-relpath">Input-relative path:</label>
                        <input type="text" id="bundle-input-relpath" class="modal-input" placeholder="3D/example.glb" />
                        <label for="bundle-input-url">Optional fallback Web URL:</label>
                        <input type="url" id="bundle-input-url" class="modal-input" placeholder="https://example.com/source-file.png" />
                        <p class="modal-hint">This streams from your local ComfyUI/input folder through the bootstrapper and deploys to ComfyUI/input/&lt;path&gt; on the remote machine. A URL is only needed as a fallback.</p>
                    </div>
                    <div class="modal-footer">
                        <button class="btn" onclick="Bundles.closeInputAssetModal()">Cancel</button>
                        <button class="btn btn-primary" onclick="Bundles.saveInputAsset()">Add Input File</button>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);

            modal.addEventListener('click', (e) => {
                if (e.target === modal) this.closeInputAssetModal();
            });
        }

        modal.querySelector('#bundle-input-relpath').value = '';
        modal.querySelector('#bundle-input-url').value = '';
        modal.classList.add('visible');
        modal.querySelector('#bundle-input-relpath').focus();
    },

    closeInputAssetModal() {
        const modal = document.getElementById('bundle-input-asset-modal');
        if (modal) {
            modal.classList.remove('visible');
        }
    },

    async saveInputAsset() {
        const relpath = document.getElementById('bundle-input-relpath').value.trim().replaceAll('\\', '/');
        const url = document.getElementById('bundle-input-url').value.trim();

        if (!relpath) {
            alert('Input-relative path is required');
            return;
        }
        if (relpath.startsWith('/') || relpath.includes('..')) {
            alert('Use a safe relative path inside the ComfyUI input folder.');
            return;
        }
        try {
            await App.api('POST', `/bundles/${encodeURIComponent(this.activeBundle.name)}/assets`, {
                root_type: 'input',
                relpath,
                source_url_override: url || null,
            });
            this.closeInputAssetModal();
            await this.selectBundle(this.activeBundle.name);
        } catch (err) {
            alert('Failed to add input file: ' + err.message);
        }
    },

    // ==================== Custom Node Modal ====================

    openCustomNodeModal() {
        let modal = document.getElementById('bundle-custom-node-modal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'bundle-custom-node-modal';
            modal.className = 'modal-overlay';
            modal.innerHTML = `
                <div class="modal-content">
                    <div class="modal-header">
                        <h3>Add Custom Node</h3>
                        <button class="modal-close" onclick="Bundles.closeCustomNodeModal()">×</button>
                    </div>
                    <div class="modal-body">
                        <label for="bundle-node-search">Search Comfy Registry:</label>
                        <div style="display: flex; gap: 8px;">
                            <input type="text" id="bundle-node-search" class="modal-input" style="margin-bottom: 8px;" placeholder="trellis, impact pack, geompack..." />
                            <button class="btn" style="height: 36px; margin-top: 8px;" onclick="Bundles.searchCustomNodes()">Search</button>
                        </div>
                        <div id="bundle-node-results" style="max-height: 260px; overflow: auto; border: 1px solid var(--border); border-radius: var(--radius-sm); margin-bottom: 16px;"></div>

                        <label for="bundle-node-git-url">Git URL fallback:</label>
                        <input type="url" id="bundle-node-git-url" class="modal-input" placeholder="https://github.com/org/ComfyUI-custom-node" />
                        <p class="modal-hint">Registry installs use Comfy CLI so the native Manager can recognize the pack. Git URL is for packs missing from the registry.</p>
                    </div>
                    <div class="modal-footer">
                        <button class="btn" onclick="Bundles.closeCustomNodeModal()">Cancel</button>
                        <button class="btn btn-primary" onclick="Bundles.addGitCustomNode()">Add Git URL</button>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);
            modal.addEventListener('click', (e) => {
                if (e.target === modal) this.closeCustomNodeModal();
            });
            modal.querySelector('#bundle-node-search').addEventListener('keydown', (e) => {
                if (e.key === 'Enter') this.searchCustomNodes();
            });
        }

        modal.querySelector('#bundle-node-search').value = '';
        modal.querySelector('#bundle-node-git-url').value = '';
        modal.querySelector('#bundle-node-results').innerHTML = '<div style="padding: 12px; color: var(--text-muted);">Search for a node pack.</div>';
        modal.classList.add('visible');
        modal.querySelector('#bundle-node-search').focus();
    },

    closeCustomNodeModal() {
        const modal = document.getElementById('bundle-custom-node-modal');
        if (modal) modal.classList.remove('visible');
    },

    async searchCustomNodes() {
        const input = document.getElementById('bundle-node-search');
        const results = document.getElementById('bundle-node-results');
        const q = input.value.trim();
        if (!q) return;

        results.innerHTML = '<div style="padding: 12px; color: var(--text-muted);">Searching...</div>';
        try {
            const data = await App.api('GET', `/bundles/registry/search?q=${encodeURIComponent(q)}&limit=20`);
            const nodes = data.nodes || [];
            if (nodes.length === 0) {
                results.innerHTML = '<div style="padding: 12px; color: var(--text-muted);">No registry results.</div>';
                return;
            }

            this.customNodeSearchResults = nodes;
            results.innerHTML = nodes.map((n, idx) => `
                <div style="padding: 12px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; gap: 12px;">
                    <div>
                        <div style="font-weight: 600;">${n.name || n.id}</div>
                        <div style="font-family: var(--font-mono); font-size: 11px; color: var(--text-muted);">${n.install_type || 'registry'} · ${n.id}${n.version ? ` @ ${n.version}` : ''}</div>
                        <div style="font-size: 12px; color: var(--text-secondary); max-width: 620px;">${(n.description || '').slice(0, 180)}</div>
                    </div>
                    <button class="btn btn-small" onclick="Bundles.addRegistryCustomNode(${idx})">Add</button>
                </div>
            `).join('');
        } catch (err) {
            results.innerHTML = `<div style="padding: 12px; color: var(--danger);">Search failed: ${err.message}</div>`;
        }
    },

    async addRegistryCustomNode(index) {
        const node = this.customNodeSearchResults[index];
        if (!node) return;
        try {
            await App.api('POST', `/bundles/${encodeURIComponent(this.activeBundle.name)}/custom-nodes`, {
                install_type: node.install_type || 'registry',
                node_id: node.id,
                name: node.name,
                repository: node.repository || null,
                version: node.version || null,
            });
            this.closeCustomNodeModal();
            await this.selectBundle(this.activeBundle.name);
        } catch (err) {
            alert('Failed to add custom node: ' + err.message);
        }
    },

    async addGitCustomNode() {
        const input = document.getElementById('bundle-node-git-url');
        const url = input.value.trim();
        if (!url) {
            alert('Enter a Git URL first.');
            return;
        }
        try {
            await App.api('POST', `/bundles/${encodeURIComponent(this.activeBundle.name)}/custom-nodes`, {
                install_type: 'git',
                node_id: url,
                name: url.split('/').pop().replace(/\\.git$/, ''),
                repository: url,
            });
            this.closeCustomNodeModal();
            await this.selectBundle(this.activeBundle.name);
        } catch (err) {
            alert('Failed to add Git custom node: ' + err.message);
        }
    },

    async removeCustomNode(installType, nodeId) {
        if (!confirm('Remove this custom node from the bundle?')) return;
        try {
            const encodedNode = nodeId.split('/').map(encodeURIComponent).join('/');
            await App.api('DELETE', `/bundles/${encodeURIComponent(this.activeBundle.name)}/custom-nodes/${encodedNode}?install_type=${encodeURIComponent(installType || 'registry')}`);
            await this.selectBundle(this.activeBundle.name);
        } catch (err) {
            alert('Failed to remove custom node: ' + err.message);
        }
    },

    // ==================== Actions ====================

    openModal(isEdit = false) {
        const modal = document.getElementById('bundle-modal');
        const title = document.getElementById('modal-title');
        const nameInput = document.getElementById('bundle-name');
        const descInput = document.getElementById('bundle-desc');
        const saveBtn = document.getElementById('modal-save-btn');

        if (isEdit && this.activeBundle) {
            title.textContent = 'Edit Bundle';
            nameInput.value = this.activeBundle.name;
            descInput.value = this.activeBundle.description || '';
            nameInput.disabled = true; // For now don't allow renaming to simplify logic
            saveBtn.onclick = () => this.saveBundle(true);
        } else {
            title.textContent = 'Create Bundle';
            nameInput.value = '';
            descInput.value = '';
            nameInput.disabled = false;
            saveBtn.onclick = () => this.saveBundle(false);
        }

        modal.style.display = 'flex';
        modal.classList.add('visible');
        if (!isEdit) nameInput.focus();
    },

    closeModal() {
        const modal = document.getElementById('bundle-modal');
        modal.style.display = 'none';
        modal.classList.remove('visible');
    },

    async saveBundle(isEdit) {
        const name = document.getElementById('bundle-name').value.trim();
        const description = document.getElementById('bundle-desc').value.trim();

        if (!name) {
            alert('Name is required');
            return;
        }

        try {
            if (isEdit) {
                await App.api('PUT', `/bundles/${encodeURIComponent(this.activeBundle.name)}`, { description });
                // If we allow renaming, we'd pass name param too
            } else {
                await App.api('POST', '/bundles', { name, description });
            }

            this.closeModal();
            await this.loadBundles();

            if (isEdit) {
                this.selectBundle(this.activeBundle.name); // refresh details
            } else {
                this.selectBundle(name); // select new bundle
            }

        } catch (err) {
            alert('Failed to save bundle: ' + err.message);
        }
    },

    async deleteBundle(name) {
        if (!confirm(`Are you sure you want to delete bundle "${name}"?`)) return;

        try {
            await App.api('DELETE', `/bundles/${encodeURIComponent(name)}`);
            await this.loadBundles();

            if (this.activeBundle?.name === name) {
                this.activeBundle = null;
                document.getElementById('bundle-detail').innerHTML = `
                    <div class="empty-state">
                        <span style="font-size: 48px; margin-bottom: 16px;">📦</span>
                        <h3>Select a bundle to view details</h3>
                    </div>
                `;
            }
        } catch (err) {
            alert('Failed to delete bundle: ' + err.message);
        }
    },

    async removeAsset(rootType, relpath) {
        if (!confirm('Remove this asset from the bundle?')) return;

        try {
            const encodedPath = relpath.split('/').map(encodeURIComponent).join('/');
            await App.api('DELETE', `/bundles/${encodeURIComponent(this.activeBundle.name)}/assets/${encodedPath}?root_type=${encodeURIComponent(rootType || 'models')}`);
            // Refresh detailed view
            this.selectBundle(this.activeBundle.name);
        } catch (err) {
            alert('Failed to remove asset: ' + err.message);
        }
    }
};

document.addEventListener('DOMContentLoaded', () => Bundles.init());
