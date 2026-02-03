/**
 * Dedupe Page JS
 */

const Dedupe = {
    scanId: null,
    side: null,
    groups: [],
    currentGroupIndex: 0,
    selections: new Map(), // group_id -> keep_relpath

    init() {
        this.bindEvents();
        this.checkPreviousScan();
    },

    async checkPreviousScan() {
        try {
            const result = await App.api('GET', '/dedupe/scan/latest');
            if (result && result.scan_id) {
                console.log('Found previous scan:', result);
                this.scanId = result.scan_id;
                this.side = result.side;
                await this.loadGroups();
                this.showStep('wizard');
            }
        } catch (err) {
            console.log('No previous scan or error loading it:', err);
        }
    },

    bindEvents() {
        document.getElementById('scan-local')?.addEventListener('click', () => this.startScan('local'));
        document.getElementById('scan-lake')?.addEventListener('click', () => this.startScan('lake'));
        document.getElementById('prev-group')?.addEventListener('click', () => this.prevGroup());
        document.getElementById('next-group')?.addEventListener('click', () => this.nextGroup());
        document.getElementById('back-to-wizard')?.addEventListener('click', () => this.showStep('wizard'));
        document.getElementById('confirm-delete')?.addEventListener('click', () => this.executeDelete());
        document.getElementById('start-over')?.addEventListener('click', () => this.reset());

        document.getElementById('start-over')?.addEventListener('click', () => this.reset());
    },

    showStep(step) {
        document.querySelectorAll('.dedupe-step').forEach(el => el.classList.add('hidden'));
        document.getElementById(`step-${step}`)?.classList.remove('hidden');
    },

    async startScan(side) {
        this.side = side;
        const modeFast = document.getElementById('scan-mode-fast')?.checked;
        const mode = modeFast ? 'fast' : 'full';
        this.showStep('scanning');

        try {
            const result = await App.api('POST', '/dedupe/scan', { side, mode });
            console.log('Scan queued:', result);
            this.waitForScan(result.task_id);
        } catch (err) {
            alert('Scan failed: ' + err.message);
            this.showStep('select');
        }
    },

    waitForScan(taskId) {
        const progressBar = document.getElementById('scan-progress');
        const statusText = document.getElementById('scan-status');

        // Progress Handler
        const progressHandler = (e) => {
            const data = e.detail;
            if (data.task_id === taskId) {
                if (progressBar) progressBar.style.width = `${data.progress_pct}%`;
                if (statusText) statusText.textContent = data.message || `Hashing... ${data.progress_pct}%`;
            }
        };

        // Completion Handler
        const completionHandler = async (e) => {
            const data = e.detail;
            if (data.task_id === taskId) {
                // Cleanup listeners
                document.removeEventListener('ws:queue_progress', progressHandler);
                document.removeEventListener('ws:task_complete', completionHandler);

                if (data.status === 'completed') {
                    // Result contains scan stats
                    const result = data.result;
                    this.scanId = result.scan_id;

                    if (result.duplicate_groups === 0) {
                        document.getElementById('complete-message').textContent = 'No duplicates found!';
                        this.showStep('complete');
                    } else {
                        await this.loadGroups();
                        this.showStep('wizard');
                    }
                } else {
                    alert('Scan failed: ' + (data.error || 'Unknown error'));
                    this.showStep('select');
                }
            }
        };

        document.addEventListener('ws:queue_progress', progressHandler);
        document.addEventListener('ws:task_complete', completionHandler);
    },

    async loadGroups() {
        this.groups = await App.api('GET', `/dedupe/results/${this.scanId}`);
        document.getElementById('total-groups').textContent = this.groups.length;

        // Default selection: first file in each group
        for (const group of this.groups) {
            if (group.files.length > 0) {
                this.selections.set(group.id, group.files[0].relpath);
            }
        }

        this.renderCurrentGroup();
    },

    renderCurrentGroup() {
        const group = this.groups[this.currentGroupIndex];
        if (!group) return;

        document.getElementById('current-group').textContent = this.currentGroupIndex + 1;

        const container = document.getElementById('current-duplicate-group');
        const selectedRelpath = this.selections.get(group.id);

        let html = `<div class="font-mono text-muted" style="margin-bottom: 12px;">Hash: ${group.hash.substring(0, 16)}...</div>`;

        for (const file of group.files) {
            const isSelected = file.relpath === selectedRelpath;
            html += `
                <div class="dup-file ${isSelected ? 'selected' : ''}" data-relpath="${file.relpath}" data-group="${group.id}">
                    <span>${isSelected ? '✅' : '⬜'}</span>
                    <span class="file-name" style="flex: 1;">${file.relpath}</span>
                    <span class="file-size">${App.formatBytes(file.size)}</span>
                </div>
            `;
        }

        container.innerHTML = html;

        // Bind selection
        container.querySelectorAll('.dup-file').forEach(el => {
            el.addEventListener('click', () => {
                const relpath = el.dataset.relpath;
                const groupId = parseInt(el.dataset.group);
                this.selections.set(groupId, relpath);
                this.renderCurrentGroup();
            });
        });

        // Update nav buttons
        document.getElementById('prev-group').disabled = this.currentGroupIndex === 0;
        document.getElementById('next-group').textContent =
            this.currentGroupIndex === this.groups.length - 1 ? 'Review' : 'Next →';
    },

    prevGroup() {
        if (this.currentGroupIndex > 0) {
            this.currentGroupIndex--;
            this.renderCurrentGroup();
        }
    },

    nextGroup() {
        if (this.currentGroupIndex < this.groups.length - 1) {
            this.currentGroupIndex++;
            this.renderCurrentGroup();
        } else {
            this.showReview();
        }
    },

    showReview() {
        let deleteCount = 0;
        let reclaimBytes = 0;

        for (const group of this.groups) {
            const keepRelpath = this.selections.get(group.id);
            for (const file of group.files) {
                if (file.relpath !== keepRelpath) {
                    deleteCount++;
                    reclaimBytes += file.size;
                }
            }
        }

        document.getElementById('delete-count').textContent = deleteCount;
        document.getElementById('reclaim-size').textContent = App.formatBytes(reclaimBytes);

        this.showStep('review');
    },

    async executeDelete() {
        const selections = Array.from(this.selections.entries()).map(([groupId, keepRelpath]) => ({
            group_id: groupId,
            keep_relpath: keepRelpath,
        }));

        try {
            const result = await App.api('POST', '/dedupe/execute', {
                scan_id: this.scanId,
                selections,
            });

            document.getElementById('complete-message').textContent =
                `Deleted ${result.deleted} files, freed ${App.formatBytes(result.freed_bytes)}.`;
            this.showStep('complete');
        } catch (err) {
            alert('Delete failed: ' + err.message);
        }
    },



    reset() {
        this.scanId = null;
        this.side = null;
        this.groups = [];
        this.currentGroupIndex = 0;
        this.selections.clear();
        this.showStep('select');
    }
};

document.addEventListener('DOMContentLoaded', () => Dedupe.init());
