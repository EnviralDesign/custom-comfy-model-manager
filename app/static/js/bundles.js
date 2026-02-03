/**
 * Bundles Page JS
 */

const Bundles = {
    bundles: [],
    activeBundle: null,

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

        return `
            <table class="asset-table">
                <thead>
                    <tr>
                        <th>Path</th>
                        <th style="width: 100px;">Override</th>
                        <th style="width: 80px;">Actions</th>
                    </tr>
                </thead>
                <tbody>
                    ${assets.map(a => `
                        <tr>
                            <td title="${a.relpath}">
                                <div style="font-family: var(--font-mono); font-size: 13px;">${a.relpath}</div>
                                ${a.hash ? `<div style="font-size: 11px; color: var(--text-muted);">Hash: ${a.hash.slice(0, 12)}...</div>` : ''}
                            </td>
                            <td class="asset-url-cell">
                                ${a.source_url_override || a.source_url ? `
                                    <div style="display: flex; align-items: center; gap: 8px;">
                                        <span title="Linked">✅</span>
                                        <button class="btn btn-small" style="font-size: 10px; padding: 2px 6px;" 
                                                onclick="Bundles.testUrl('${a.source_url_override || a.source_url}', this)">Test</button>
                                    </div>
                                ` : '<span style="color: var(--text-muted);">-</span>'}
                            </td>
                            <td>
                                <button class="btn-icon btn-danger" onclick="Bundles.removeAsset('${a.relpath}')" title="Remove from bundle">✕</button>
                            </td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        `;
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
