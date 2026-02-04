/* Downloader UI JS */

const jobsEl = document.getElementById("download-jobs");
const urlEl = document.getElementById("download-url");
const filenameEl = document.getElementById("download-filename");
const apiKeyEl = document.getElementById("download-api-key");
const providerEl = document.getElementById("download-provider");
const startBtn = document.getElementById("download-start-btn");
const startNowBtn = document.getElementById("download-start-now-btn");
const cancelAllBtn = document.getElementById("download-cancel-all");
const agentQueryEl = document.getElementById("agent-query");
const agentHashEl = document.getElementById("agent-hash");
const agentExactEl = document.getElementById("agent-exact");
const agentStartBtn = document.getElementById("agent-start-btn");
const agentCancelBtn = document.getElementById("agent-cancel-btn");
const agentTraceEl = document.getElementById("agent-trace");
let agentJobId = null;
const toolCivitaiQuery = document.getElementById("tool-civitai-query");
const toolCivitaiLimit = document.getElementById("tool-civitai-limit");
const toolCivitaiPage = document.getElementById("tool-civitai-page");
const toolCivitaiCursor = document.getElementById("tool-civitai-cursor");
const toolCivitaiType = document.getElementById("tool-civitai-type");
const toolCivitaiSupports = document.getElementById("tool-civitai-supports");
const toolCivitaiNsfw = document.getElementById("tool-civitai-nsfw");
const toolCivitaiSearchBtn = document.getElementById("tool-civitai-search-btn");
const toolCivitaiSearchResult = document.getElementById("tool-civitai-search-result");
const toolCivitaiVersion = document.getElementById("tool-civitai-version");
const toolCivitaiVersionBtn = document.getElementById("tool-civitai-version-btn");
const toolCivitaiVersionResult = document.getElementById("tool-civitai-version-result");
const toolCivitaiHash = document.getElementById("tool-civitai-hash");
const toolCivitaiHashBtn = document.getElementById("tool-civitai-hash-btn");
const toolCivitaiHashResult = document.getElementById("tool-civitai-hash-result");
const toolHfQuery = document.getElementById("tool-hf-query");
const toolHfLimit = document.getElementById("tool-hf-limit");
const toolHfSearchBtn = document.getElementById("tool-hf-search-btn");
const toolHfSearchResult = document.getElementById("tool-hf-search-result");
const toolHfRepo = document.getElementById("tool-hf-repo");
const toolHfInfoBtn = document.getElementById("tool-hf-info-btn");
const toolHfInfoResult = document.getElementById("tool-hf-info-result");
const toolHfResolveRepo = document.getElementById("tool-hf-resolve-repo");
const toolHfResolveFile = document.getElementById("tool-hf-resolve-file");
const toolHfResolveRev = document.getElementById("tool-hf-resolve-rev");
const toolHfResolveBtn = document.getElementById("tool-hf-resolve-btn");
const toolHfResolveResult = document.getElementById("tool-hf-resolve-result");
const toolUrl = document.getElementById("tool-url");
const toolUrlBtn = document.getElementById("tool-url-btn");
const toolUrlResult = document.getElementById("tool-url-result");

function formatBytes(bytes) {
    if (bytes === null || bytes === undefined) return "—";
    if (bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${(bytes / Math.pow(k, i)).toFixed(2)} ${sizes[i]}`;
}

function renderJobs(jobs) {
    if (!jobs || jobs.length === 0) {
        jobsEl.innerHTML = '<div class="text-secondary text-sm">No downloads yet.</div>';
        return;
    }

    const html = jobs
        .map((job) => {
            const pct =
                job.total_bytes && job.total_bytes > 0
                    ? Math.min(100, (job.bytes_downloaded / job.total_bytes) * 100)
                    : null;
            const progressText = pct !== null ? `${pct.toFixed(1)}%` : "—";
            const statusClass =
                job.status === "completed"
                    ? "text-success"
                    : job.status === "failed"
                    ? "text-danger"
                    : "text-secondary";

            const actions = [];
            if (job.status === "queued") {
                actions.push(`<button class="btn btn-small btn-primary" data-action="start" data-id="${job.id}">Start Now</button>`);
            }
            if (job.status === "queued" || job.status === "running") {
                actions.push(`<button class="btn btn-small btn-danger" data-action="cancel" data-id="${job.id}">Cancel</button>`);
            }

            return `
            <div style="background: var(--bg-secondary); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 12px; margin-bottom: 10px;">
                <div style="display: flex; justify-content: space-between; gap: 12px; align-items: center;">
                    <div>
                        <div style="font-weight: 600;">${job.filename || "download"}</div>
                        <div class="text-secondary text-sm">${job.url}</div>
                    </div>
                    <div style="display: flex; gap: 8px; align-items: center;">
                        ${actions.join("")}
                        <div class="${statusClass}" style="min-width: 120px; text-align: right;">
                        ${job.status}
                        </div>
                    </div>
                </div>
                <div style="margin-top: 8px; display: flex; justify-content: space-between; gap: 12px; align-items: center;">
                    <div class="text-secondary text-sm">${formatBytes(job.bytes_downloaded)} / ${formatBytes(job.total_bytes)}</div>
                    <div class="text-secondary text-sm">${progressText}</div>
                </div>
                ${
                    pct !== null
                        ? `<div style="margin-top: 6px; background: var(--bg-tertiary); border-radius: 6px; height: 6px; overflow: hidden;">
                            <div style="height: 100%; width: ${pct}%; background: var(--accent);"></div>
                          </div>`
                        : ""
                }
                ${
                    job.error_message
                        ? `<div class="text-danger text-sm" style="margin-top: 6px;">${job.error_message}</div>`
                        : ""
                }
            </div>
        `;
        })
        .join("");

    jobsEl.innerHTML = html;
}

async function refreshJobs() {
    try {
        const res = await fetch("/api/downloader/jobs");
        if (!res.ok) return;
        const data = await res.json();
        renderJobs(data);
    } catch (err) {
        console.warn(err);
    }
}

async function startDownload() {
    const url = urlEl.value.trim();
    const filename = filenameEl.value.trim();
    const apiKey = apiKeyEl.value.trim();
    const provider = providerEl.value;

    if (!url) return;

    startBtn.disabled = true;
    try {
        const res = await fetch("/api/downloader/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                url,
                filename: filename || null,
                provider,
                api_key: apiKey || null,
                start_now: false,
            }),
        });
        if (res.ok) {
            urlEl.value = "";
            filenameEl.value = "";
            apiKeyEl.value = "";
            await refreshJobs();
        }
    } finally {
        startBtn.disabled = false;
    }
}

async function startDownloadNow() {
    const url = urlEl.value.trim();
    const filename = filenameEl.value.trim();
    const apiKey = apiKeyEl.value.trim();
    const provider = providerEl.value;

    if (!url) return;

    startNowBtn.disabled = true;
    try {
        const res = await fetch("/api/downloader/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                url,
                filename: filename || null,
                provider,
                api_key: apiKey || null,
                start_now: true,
            }),
        });
        if (res.ok) {
            urlEl.value = "";
            filenameEl.value = "";
            apiKeyEl.value = "";
            await refreshJobs();
        }
    } finally {
        startNowBtn.disabled = false;
    }
}

async function handleJobAction(event) {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const jobId = button.getAttribute("data-id");
    const action = button.getAttribute("data-action");

    if (action === "start") {
        await fetch(`/api/downloader/jobs/${jobId}/start`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ force: true }),
        });
        await refreshJobs();
    }

    if (action === "cancel") {
        await fetch(`/api/downloader/jobs/${jobId}/cancel`, { method: "POST" });
        await refreshJobs();
    }
}

async function cancelAllJobs() {
    if (!cancelAllBtn) return;
    cancelAllBtn.disabled = true;
    try {
        await fetch("/api/downloader/jobs/cancel-all", { method: "POST" });
        await refreshJobs();
    } finally {
        cancelAllBtn.disabled = false;
    }
}

startBtn.addEventListener("click", startDownload);
startNowBtn.addEventListener("click", startDownloadNow);
jobsEl.addEventListener("click", handleJobAction);
cancelAllBtn.addEventListener("click", cancelAllJobs);

function renderAgentTrace(job) {
    if (!job) {
        agentTraceEl.innerHTML = '<div class="text-secondary text-sm">No trace yet.</div>';
        return;
    }
    const trace = job.trace || [];
    const header = `
        <div style="margin-bottom: 10px;">
            <div style="font-weight: 600;">Status: ${job.status}</div>
            ${job.result && job.result.url ? `<div class="text-secondary text-sm">URL: ${job.result.url}</div>` : ""}
            ${job.error ? `<div class="text-danger text-sm">Error: ${job.error}</div>` : ""}
        </div>`;

    const rows = trace
        .map((entry) => {
            const msg = entry.text || entry.message || JSON.stringify(entry);
            return `<div style="padding: 6px 0; border-bottom: 1px solid var(--border);">
                <div class="text-secondary text-sm">${entry.time || ""} ${entry.type || ""}</div>
                <div style="white-space: pre-wrap;">${msg}</div>
            </div>`;
        })
        .join("");

    agentTraceEl.innerHTML = header + (rows || '<div class="text-secondary text-sm">No trace steps yet.</div>');
}

async function refreshAgentTrace() {
    if (!agentJobId) return;
    try {
        const res = await fetch(`/api/agent-debug/jobs/${agentJobId}`);
        if (!res.ok) return;
        const data = await res.json();
        renderAgentTrace(data);
    } catch (err) {
        console.warn(err);
    }
}

async function startAgentTrace() {
    const query = agentQueryEl.value.trim();
    if (!query) return;

    agentStartBtn.disabled = true;
    try {
        const res = await fetch("/api/agent-debug/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                query,
                file_hash: agentHashEl.value.trim() || null,
                require_exact_filename: agentExactEl.checked,
            }),
        });
        if (res.ok) {
            const data = await res.json();
            agentJobId = data.id;
            renderAgentTrace(data);
        }
    } finally {
        agentStartBtn.disabled = false;
    }
}

async function cancelAgentTrace() {
    if (!agentJobId) return;
    agentCancelBtn.disabled = true;
    try {
        await fetch(`/api/agent-debug/jobs/${agentJobId}/cancel`, { method: "POST" });
        await refreshAgentTrace();
    } finally {
        agentCancelBtn.disabled = false;
    }
}

agentStartBtn.addEventListener("click", startAgentTrace);
agentCancelBtn.addEventListener("click", cancelAgentTrace);

refreshJobs();
setInterval(refreshJobs, 1500);
setInterval(refreshAgentTrace, 1200);

function renderJson(target, data) {
    if (!target) return;
    target.textContent = JSON.stringify(data, null, 2);
}

async function postJson(url, payload) {
    const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
    });
    if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "Request failed");
    }
    return res.json();
}

if (toolCivitaiSearchBtn) {
    toolCivitaiSearchBtn.addEventListener("click", async () => {
        try {
            const data = await postJson("/api/agent-tools/civitai/search", {
                query: toolCivitaiQuery.value.trim(),
                limit: Number(toolCivitaiLimit.value || 20),
                page: Number(toolCivitaiPage.value || 1),
                cursor: toolCivitaiCursor.value.trim() || null,
                types: toolCivitaiType.value || null,
                supports_generation:
                    toolCivitaiSupports.value === ""
                        ? null
                        : toolCivitaiSupports.value === "true",
                nsfw:
                    toolCivitaiNsfw.value === ""
                        ? null
                        : toolCivitaiNsfw.value === "true",
            });
            renderJson(toolCivitaiSearchResult, data);
        } catch (err) {
            renderJson(toolCivitaiSearchResult, { error: String(err) });
        }
    });
}

if (toolCivitaiVersionBtn) {
    toolCivitaiVersionBtn.addEventListener("click", async () => {
        try {
            const data = await postJson("/api/agent-tools/civitai/model-version", {
                id: Number(toolCivitaiVersion.value),
            });
            renderJson(toolCivitaiVersionResult, data);
        } catch (err) {
            renderJson(toolCivitaiVersionResult, { error: String(err) });
        }
    });
}

if (toolCivitaiHashBtn) {
    toolCivitaiHashBtn.addEventListener("click", async () => {
        try {
            const data = await postJson("/api/agent-tools/civitai/by-hash", {
                hash: toolCivitaiHash.value.trim(),
            });
            renderJson(toolCivitaiHashResult, data);
        } catch (err) {
            renderJson(toolCivitaiHashResult, { error: String(err) });
        }
    });
}

if (toolHfSearchBtn) {
    toolHfSearchBtn.addEventListener("click", async () => {
        try {
            const data = await postJson("/api/agent-tools/hf/search", {
                query: toolHfQuery.value.trim(),
                limit: Number(toolHfLimit.value || 20),
            });
            renderJson(toolHfSearchResult, data);
        } catch (err) {
            renderJson(toolHfSearchResult, { error: String(err) });
        }
    });
}

if (toolHfInfoBtn) {
    toolHfInfoBtn.addEventListener("click", async () => {
        try {
            const data = await postJson("/api/agent-tools/hf/model-info", {
                repo_id: toolHfRepo.value.trim(),
            });
            renderJson(toolHfInfoResult, data);
        } catch (err) {
            renderJson(toolHfInfoResult, { error: String(err) });
        }
    });
}

if (toolHfResolveBtn) {
    toolHfResolveBtn.addEventListener("click", async () => {
        try {
            const data = await postJson("/api/agent-tools/hf/resolve", {
                repo_id: toolHfResolveRepo.value.trim(),
                file: toolHfResolveFile.value.trim(),
                revision: toolHfResolveRev.value.trim() || null,
                validate: true,
            });
            renderJson(toolHfResolveResult, data);
        } catch (err) {
            renderJson(toolHfResolveResult, { error: String(err) });
        }
    });
}

if (toolUrlBtn) {
    toolUrlBtn.addEventListener("click", async () => {
        try {
            const data = await postJson("/api/agent-tools/url/validate", {
                url: toolUrl.value.trim(),
            });
            renderJson(toolUrlResult, data);
        } catch (err) {
            renderJson(toolUrlResult, { error: String(err) });
        }
    });
}
