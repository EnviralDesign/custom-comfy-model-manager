/**
 * Bundles Page JS
 */

const Bundles = {
    bundles: [],
    activeBundle: null,
    activeSourceContext: null,

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
                    <button class="btn" onclick="Bundles.openModal(true)">Edit</button>
                    <button class="btn btn-danger" onclick="Bundles.deleteBundle('${b.name}')">Delete Bundle</button>
                </div>
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
                Go to <a href="/sync">Sync</a> to add files.
            </div>`;
        }

        const grouped = new Map();
        for (const asset of assets) {
            const group = this.getAssetGroup(asset.relpath);
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
                rows.push(`
                    <tr>
                        <td title="${a.relpath}">
                            <div style="font-family: var(--font-mono); font-size: 13px;">${a.relpath}</div>
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
                                        onclick="Bundles.openSourceUrlModal('${a.relpath}', '${a.hash || ''}')">${a.source_url_override || a.source_url ? 'Edit' : 'Link'}</button>
                            </div>
                        </td>
                        <td>
                            <button class="btn-icon btn-danger" onclick="Bundles.removeAsset('${a.relpath}')" title="Remove from bundle">✕</button>
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

    getAssetGroup(relpath) {
        if (!relpath) return 'root';
        const parts = relpath.split('/');
        return parts[0] || 'root';
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

    openSourceUrlModal(relpath, hash) {
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
                        <p class="modal-hint">Enter the public download URL for this model. This allows remote provisioning to download directly from the source.</p>
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
        const existingAsset = this.activeBundle?.assets?.find(a => a.relpath === relpath);
        const existingUrl = existingAsset?.source_url_override || existingAsset?.source_url || '';

        this.activeSourceContext = { relpath, hash, filename };
        modal.querySelector('.modal-filename').textContent = `File: ${filename || relpath}`;
        modal.querySelector('.modal-hash').textContent = hash ? `Hash: ${hash}` : `Path: ${relpath}`;
        modal.querySelector('#bundle-source-url-input').value = existingUrl;

        const hashHint = modal.querySelector('.modal-hash-hint');
        if (!hash) {
            hashHint.textContent = '⚠️ File not yet hashed. URL will be saved by path and a hash will be queued.';
            hashHint.style.color = 'var(--warning)';
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
            if (ctx.hash) {
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
            if (ctx.hash) {
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

    async removeAsset(relpath) {
        if (!confirm('Remove this asset from the bundle?')) return;

        try {
            await App.api('DELETE', `/bundles/${encodeURIComponent(this.activeBundle.name)}/assets/${relpath}`);
            // Refresh detailed view
            this.selectBundle(this.activeBundle.name);
        } catch (err) {
            alert('Failed to remove asset: ' + err.message);
        }
    }
};

document.addEventListener('DOMContentLoaded', () => Bundles.init());
