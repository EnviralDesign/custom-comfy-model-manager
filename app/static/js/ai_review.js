/**
 * AI Review Page JS - Review queued Grok lookup jobs
 */

const AiReview = {
    jobs: [],
    includeDecided: false,

    async init() {
        this.bindEvents();
        await this.loadJobs();
    },

    bindEvents() {
        document.getElementById('ai-refresh-btn')?.addEventListener('click', () => this.loadJobs());
        document.getElementById('ai-toggle-decided')?.addEventListener('click', () => this.toggleDecided());
        document.getElementById('ai-review-list')?.addEventListener('click', (e) => this.handleActionClick(e));

        document.addEventListener('ws:ai_lookup_update', (e) => {
            this.upsertJob(e.detail);
        });
    },

    async loadJobs() {
        try {
            const qs = this.includeDecided ? '?include_decided=true' : '';
            this.jobs = await App.api('GET', `/ai/lookup/jobs${qs}`);
            this.render();
        } catch (err) {
            const container = document.getElementById('ai-review-list');
            if (container) {
                container.innerHTML = `<div class="text-danger">Failed to load jobs: ${this.escapeHtml(err.message)}</div>`;
            }
        }
    },

    toggleDecided() {
        this.includeDecided = !this.includeDecided;
        const btn = document.getElementById('ai-toggle-decided');
        if (btn) {
            btn.textContent = this.includeDecided ? 'Hide decided' : 'Show decided';
        }
        this.loadJobs();
    },

    upsertJob(job) {
        const idx = this.jobs.findIndex(j => j.id === job.id);
        if (idx >= 0) {
            this.jobs[idx] = job;
        } else {
            this.jobs.unshift(job);
        }

        if (!this.includeDecided && job.decision) {
            this.jobs = this.jobs.filter(j => !j.decision);
        }

        this.render();
    },

    render() {
        const container = document.getElementById('ai-review-list');
        const countEl = document.getElementById('ai-review-count');
        if (!container) return;

        if (countEl) {
            countEl.textContent = this.jobs.length;
        }

        if (this.jobs.length === 0) {
            container.innerHTML = `<div class="text-secondary text-sm">No AI lookup jobs to review.</div>`;
            return;
        }

        container.innerHTML = this.jobs.map(job => this.renderJob(job)).join('');
    },

    renderJob(job) {
        const status = this.getStatus(job);
        const steps = (job.steps || []).slice(-6);
        const stepsHtml = steps.length
            ? `<div class="ai-review-steps"><ul>${steps.map(s => `<li><strong>${this.escapeHtml(s.source || 'system')}:</strong> ${this.escapeHtml(s.message || '')}</li>`).join('')}</ul></div>`
            : '';

        const urlHtml = job.candidate_url
            ? `<div class="ai-review-url"><strong>Candidate URL:</strong> ${this.escapeHtml(job.candidate_url)}</div>`
            : '';

        const sourceHtml = job.candidate_source
            ? `<div class="ai-review-url"><strong>Source:</strong> ${this.escapeHtml(job.candidate_source)}</div>`
            : '';

        const validation = job.validation;
        const validationHtml = validation
            ? `<div class="ai-review-url"><strong>Validation:</strong> ${this.escapeHtml(validation.ok ? 'OK' : 'Failed')} ${validation.status ? `(HTTP ${validation.status})` : ''}</div>`
            : '';

        const notes = job.candidate_notes || (job.error_message ? `Error: ${job.error_message}` : '');
        const notesHtml = notes
            ? `<div class="ai-review-url"><strong>Notes:</strong> ${this.escapeHtml(notes)}</div>`
            : '';

        const actions = this.renderActions(job, status.key);

        return `
            <div class="ai-review-card" data-job-id="${job.id}">
                <div class="ai-review-header">
                    <div class="ai-review-title">${this.escapeHtml(job.filename)}</div>
                    <span class="status-pill ${status.key}">${status.label}</span>
                </div>
                <div class="ai-review-meta">${this.escapeHtml(job.relpath || '')}</div>
                ${urlHtml}
                ${sourceHtml}
                ${validationHtml}
                ${notesHtml}
                ${stepsHtml}
                <div class="ai-review-actions">
                    ${actions}
                </div>
            </div>
        `;
    },

    renderActions(job, statusKey) {
        const actions = [];

        if (statusKey === 'pending' || statusKey === 'running') {
            actions.push(`<button class="btn btn-danger" data-action="cancel">Cancel</button>`);
        }

        if (statusKey === 'ready') {
            actions.push(`<button class="btn btn-primary" data-action="apply">Apply URL</button>`);
            actions.push(`<button class="btn" data-action="reject">Reject</button>`);
        }

        if (statusKey === 'review') {
            actions.push(`<button class="btn" data-action="reject">Dismiss</button>`);
            actions.push(`<button class="btn btn-secondary" data-action="retry">Retry</button>`);
        }

        if (statusKey === 'failed') {
            actions.push(`<button class="btn btn-secondary" data-action="retry">Retry</button>`);
            actions.push(`<button class="btn" data-action="reject">Dismiss</button>`);
        }

        if (job.decision === 'approved') {
            actions.push(`<span class="text-success text-sm">Applied</span>`);
        }

        if (job.decision === 'rejected') {
            actions.push(`<span class="text-muted text-sm">Dismissed</span>`);
        }

        return actions.join('');
    },

    getStatus(job) {
        if (job.decision === 'approved') return { key: 'ready', label: 'Applied' };
        if (job.decision === 'rejected') return { key: 'review', label: 'Dismissed' };

        if (job.status === 'pending') return { key: 'pending', label: 'Queued' };
        if (job.status === 'running') return { key: 'running', label: 'Searching' };
        if (job.status === 'failed') return { key: 'failed', label: 'Failed' };
        if (job.status === 'cancelled') return { key: 'review', label: 'Cancelled' };

        if (job.status === 'completed' && job.accepted) {
            return { key: 'ready', label: 'Ready' };
        }
        if (job.status === 'completed' && !job.accepted) {
            return { key: 'review', label: 'Needs Review' };
        }

        return { key: 'review', label: 'Review' };
    },

    async handleActionClick(event) {
        const btn = event.target.closest('button[data-action]');
        if (!btn) return;
        const card = btn.closest('.ai-review-card');
        if (!card) return;
        const jobId = card.dataset.jobId;
        const action = btn.dataset.action;

        try {
            if (action === 'apply') {
                await App.api('POST', `/ai/lookup/jobs/${jobId}/approve`);
            } else if (action === 'reject') {
                await App.api('POST', `/ai/lookup/jobs/${jobId}/reject`);
            } else if (action === 'retry') {
                await App.api('POST', `/ai/lookup/jobs/${jobId}/retry`);
            } else if (action === 'cancel') {
                await App.api('POST', `/ai/lookup/jobs/${jobId}/cancel`);
            }
            await this.loadJobs();
        } catch (err) {
            alert('Action failed: ' + err.message);
        }
    },

    escapeHtml(str) {
        if (!str) return '';
        return str
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }
};

document.addEventListener('DOMContentLoaded', () => AiReview.init());
