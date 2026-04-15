/**
 * app_main.js — SkillRadar Main Application JS
 */

// ==================== GLOBAL CONFIG & DEFAULTS ====================
const DEBUG = true;
const ANALYTICS_TIMEOUT_MS = 10000;
const CHART_INSTANCES = {};

// Helper to escape HTML and prevent XSS
function escapeHtml(text) {
    if (!text || typeof text !== 'string') return "";
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return text.replace(/[&<>"']/g, m => map[m]);
}

function destroyChartInstance(key) {
    const chart = CHART_INSTANCES[key];
    if (chart && typeof chart.destroy === 'function') {
        chart.destroy();
    }
    CHART_INSTANCES[key] = null;
}

async function fetchWithTimeout(url, options = {}, timeoutMs = ANALYTICS_TIMEOUT_MS) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    try {
        return await fetch(url, { ...options, signal: controller.signal });
    } finally {
        clearTimeout(timeoutId);
    }
}

// ==================== CORE INITIALIZATION ====================
document.addEventListener('DOMContentLoaded', () => {
    // 1. Initialize Lucide Icons
    if (typeof lucide !== 'undefined') {
        lucide.createIcons();
    }

    // 2. Sidebar Toggle Logic
    initSidebar();

    // 3. Page-Specific Inits
    const path = window.location.pathname;
    if (path === '/dashboard') {
        loadAnalytics();
    } else if (path === '/groups') {
        loadStudentGroups();
    } else if (path === '/discovery') {
        loadDiscovery();
    } else if (path === '/radar') {
        initRadarPage();
    } else if (path === '/roadmap') {
        // Roadmap logic is mostly Jinja-rendered, but switchRole is available
    }
});

// ==================== SIDEBAR LOGIC ====================
function initSidebar() {
    const toggle = document.getElementById('sidebar-toggle');
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebar-overlay');

    if (!toggle || !sidebar || !overlay) return;

    function openSidebar() {
        sidebar.classList.add('open');
        overlay.classList.add('visible');
        toggle.setAttribute('aria-expanded', 'true');
        document.body.style.overflow = 'hidden';
    }

    function closeSidebar() {
        sidebar.classList.remove('open');
        overlay.classList.remove('visible');
        toggle.setAttribute('aria-expanded', 'false');
        document.body.style.overflow = '';
    }

    toggle.addEventListener('click', function () {
        sidebar.classList.contains('open') ? closeSidebar() : openSidebar();
    });

    overlay.addEventListener('click', closeSidebar);

    sidebar.querySelectorAll('nav a').forEach(function (link) {
        link.addEventListener('click', function () {
            if (window.innerWidth <= 768) closeSidebar();
        });
    });

    window.addEventListener('resize', function () {
        if (window.innerWidth > 768) closeSidebar();
    });
}

// ==================== AI MENTOR CHAT ====================
let chatHistory = [];

window.toggleChat = function() {
    const panel = document.getElementById('ai-chat-panel');
    if (panel) panel.classList.toggle('active');
};

window.handleChatEnter = function(e) {
    if (e.key === 'Enter') sendChat();
};

window.sendChat = async function(forceMessage = null) {
    const input = document.getElementById('chat-input');
    const msg = forceMessage || (input ? input.value.trim() : null);
    if (!msg) return;

    if (input) input.value = '';

    const quickPrompts = document.getElementById('quick-prompts');
    if (quickPrompts) quickPrompts.style.display = 'none';

    const chatBody = document.getElementById('chat-messages');
    if (!chatBody) return;

    chatBody.insertAdjacentHTML('beforeend',
        `<div class="chat-bubble bubble-user">${escapeHtml(msg)}</div>`
    );
    chatBody.scrollTop = chatBody.scrollHeight;

    const typingId = 'typing-' + Date.now();
    chatBody.insertAdjacentHTML('beforeend', `
        <div class="chat-bubble bubble-ai typing-indicator" id="${typingId}">
            <div class="typing-dot"></div>
            <div class="typing-dot"></div>
            <div class="typing-dot"></div>
        </div>
    `);
    chatBody.scrollTop = chatBody.scrollHeight;

    try {
        const csrfToken = window.APP_CONFIG?.csrfToken || "";
        const response = await fetch('/chat', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken,
            },
            body: JSON.stringify({ message: msg, history: chatHistory })
        });

        const typingEl = document.getElementById(typingId);
        if (typingEl) typingEl.remove();

        const data = await response.json();

        if (response.status === 400 || !response.ok) {
            chatBody.insertAdjacentHTML('beforeend',
                `<div class="chat-bubble bubble-error">${data.error || "Something went wrong"}</div>`
            );
        } else if (data.reply) {
            const formattedReply = data.reply.replace(/\n/g, '<br>');
            chatBody.insertAdjacentHTML('beforeend',
                `<div class="chat-bubble bubble-ai">${formattedReply}</div>`
            );
            chatHistory.push({ sender: 'user', text: msg });
            chatHistory.push({ sender: 'assistant', text: data.reply });
        }
    } catch (err) {
        const typingEl = document.getElementById(typingId);
        if (typingEl) typingEl.remove();
        chatBody.insertAdjacentHTML('beforeend',
            `<div class="chat-bubble bubble-error">Network disconnection. Wait a moment and try again.</div>`
        );
    }

    chatBody.scrollTop = chatBody.scrollHeight;
    if (typeof lucide !== 'undefined') lucide.createIcons();
};

// ==================== DASHBOARD ANALYTICS ====================
async function loadAnalytics() {
    try {
        const res = await fetchWithTimeout('/user/analytics', {}, ANALYTICS_TIMEOUT_MS);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        
        if (data.error) throw new Error(data.error);
        
        // Update stats
        updateStat('stat-analysis-count', data.stats?.analysis_count);
        updateStat('stat-current-score', data.stats?.total_score);
        updateStat('stat-skills-verified', data.stats?.matched_skills_count);
        updateStat('stat-gaps', data.stats?.missing_skills_count);
        
        // Charts
        if (data.radar && Object.keys(data.radar).length > 0) {
            renderSkillRadarChart(data.radar);
        } else {
            hideLoader('radar-loading', 'skillRadarChart', 'No radar analytics available yet.');
        }
        
        if (Array.isArray(data.progress) && data.progress.length > 0) {
            renderProgressChart(data.progress);
        } else {
            hideLoader('progress-loading', 'progressChart', 'Run a skill analysis to view progress.');
        }
        
        if (data.match_trend) {
            renderMatchTrend(data.match_trend);
        }
        
        document.getElementById('radar-loading')?.remove();
        document.getElementById('progress-loading')?.remove();
        document.getElementById('trend-loading')?.remove();

        // Phase 6: Career
        loadCareerData();
        
    } catch (error) {
        console.error('[Analytics] Error:', error);
        showAnalyticsFallback('Analytics timed out. Retry in a moment.');
    }
}

function updateStat(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value !== undefined ? value : '-';
}

function hideLoader(loaderId, canvasId, message) {
    const loader = document.getElementById(loaderId);
    const canvas = document.getElementById(canvasId);
    if (loader && canvas) {
        canvas.parentElement.innerHTML = `<div class="text-muted text-center" style="padding: 80px 20px;">${message}</div>`;
    }
}

function showAnalyticsFallback(message) {
    updateStat('stat-analysis-count', '-');
    updateStat('stat-current-score', '-');
    updateStat('stat-skills-verified', '-');
    updateStat('stat-gaps', '-');

    destroyChartInstance('skillRadarChart');
    destroyChartInstance('progressChart');
    destroyChartInstance('matchTrendChart');

    hideLoader('radar-loading', 'skillRadarChart', message);
    hideLoader('progress-loading', 'progressChart', message);
    hideLoader('trend-loading', 'matchTrendChart', message);
}

function renderSkillRadarChart(radarData) {
    const ctx = document.getElementById('skillRadarChart');
    if (!ctx || typeof Chart === 'undefined') return;
    destroyChartInstance('skillRadarChart');
    
    const labels = Object.keys(radarData).map(k => k.charAt(0).toUpperCase() + k.slice(1));
    const values = Object.values(radarData);
    
    CHART_INSTANCES.skillRadarChart = new Chart(ctx, {
        type: 'radar',
        data: {
            labels: labels,
            datasets: [{
                label: 'Skill Score',
                data: values,
                backgroundColor: 'rgba(0, 245, 255, 0.15)',
                borderColor: '#00f5ff',
                borderWidth: 2,
                pointBackgroundColor: '#00f5ff',
                pointRadius: 4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                r: {
                    suggestedMin: 0,
                    suggestedMax: 100,
                    ticks: { display: false },
                    grid: { color: 'rgba(255,255,255,0.08)' },
                    pointLabels: { color: 'rgba(255,255,255,0.8)', font: { size: 10, weight: '600' } }
                }
            },
            plugins: { legend: { display: false } }
        }
    });
}

function renderProgressChart(progressData) {
    const ctx = document.getElementById('progressChart');
    if (!ctx || typeof Chart === 'undefined') return;
    destroyChartInstance('progressChart');
    
    const labels = progressData.map(p => {
        const date = new Date(p.date);
        return `${date.getMonth()+1}/${date.getDate()}`;
    });
    const values = progressData.map(p => p.score);
    
    CHART_INSTANCES.progressChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'Skill Score',
                data: values,
                borderColor: '#4ade80',
                backgroundColor: 'rgba(74, 222, 128, 0.1)',
                borderWidth: 3,
                fill: true,
                tension: 0.4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                y: { suggestedMin: 0, grid: { color: 'rgba(255,255,255,0.05)' } },
                x: { grid: { display: false } }
            },
            plugins: { legend: { display: false } }
        }
    });
}

function renderMatchTrend(trendData) {
    const ctx = document.getElementById('matchTrendChart');
    if (!ctx || typeof Chart === 'undefined') return;
    destroyChartInstance('matchTrendChart');
    
    const deltaEl = document.getElementById('delta-value');
    if (deltaEl) {
        const improvement = trendData.improvement || 0;
        deltaEl.textContent = (improvement >= 0 ? '+' : '') + improvement + '%';
        deltaEl.style.color = improvement >= 0 ? '#4ade80' : '#f87171';
    }
    
    CHART_INSTANCES.matchTrendChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: ['Before', 'After'],
            datasets: [{
                data: [trendData.before || 0, trendData.after || 0],
                backgroundColor: ['rgba(251, 191, 36, 0.5)', 'rgba(74, 222, 128, 0.7)'],
                borderColor: ['#fbbf24', '#4ade80'],
                borderWidth: 2,
                borderRadius: 8
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                y: { suggestedMin: 0, suggestedMax: 100 },
                x: { grid: { display: false } }
            },
            plugins: { legend: { display: false } }
        }
    });
}

// ==================== CAREER INTELLIGENCE ====================
async function loadCareerData() {
    const emptyEl = document.getElementById('career-empty');
    const contentEl = document.getElementById('career-content');
    const loadingEl = document.getElementById('career-loading');
    if (!emptyEl || !contentEl) return;

    try {
        loadingEl.style.display = 'block';
        emptyEl.style.display = 'none';
        
        const res = await fetch('/career/compare');
        const data = await res.json();
        
        if (!data.user_skill_count || data.user_skill_count === 0) {
            loadingEl.style.display = 'none';
            emptyEl.style.display = 'block';
            return;
        }
        
        loadingEl.style.display = 'none';
        contentEl.style.display = 'block';
        
        if (data.best_fit) {
            updateText('best-fit-role', data.best_fit.role);
            updateText('rec-role', data.best_fit.role);
            updateText('rec-strengths', data.best_fit.matched_skills.slice(0, 3).join(', '));
            document.getElementById('career-best-fit').style.display = 'block';
        }
        
        renderCareerSuggestions(data.suggested);
        renderCareerChart(data.all_roles);
        
    } catch (error) {
        console.error('[Career] Error:', error);
        loadingEl.style.display = 'none';
        emptyEl.style.display = 'block';
    }
}

function updateText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

function renderCareerSuggestions(suggestions) {
    const container = document.getElementById('career-suggestions');
    if (!container) return;
    
    const colors = [
        { bg: 'rgba(167, 139, 250, 0.15)', border: '#a78bfa', text: '#a78bfa' },
        { bg: 'rgba(0, 245, 255, 0.15)', border: '#00f5ff', text: '#00f5ff' },
        { bg: 'rgba(74, 222, 128, 0.15)', border: '#4ade80', text: '#4ade80' }
    ];
    
    container.innerHTML = suggestions.map((s, i) => {
        const color = colors[i] || colors[0];
        return `
            <div class="glass-card" style="padding: 20px; background: ${color.bg}; border: 1px solid ${color.border}; text-align: center;">
                <div style="font-size: 0.75rem; text-transform: uppercase; color: var(--text-muted); margin-bottom: 8px;">#${i + 1} Match</div>
                <h4 style="font-size: 1.1rem; margin-bottom: 8px; color: ${color.text};">${escapeHtml(s.role)}</h4>
                <div style="font-size: 2rem; font-weight: bold; margin-bottom: 8px;">${s.score}%</div>
                <p class="text-muted" style="font-size: 0.8rem; line-height: 1.4;">${escapeHtml(s.description)}</p>
                <div style="margin-top: 12px; display: flex; flex-wrap: wrap; gap: 4px; justify-content: center;">
                    ${s.matched_skills.slice(0, 3).map(skill => `
                        <span style="font-size: 0.7rem; padding: 2px 8px; background: rgba(255,255,255,0.1); border-radius: 10px;">${escapeHtml(skill)}</span>
                    `).join('')}
                </div>
            </div>
        `;
    }).join('');
}

function renderCareerChart(allRoles) {
    const ctx = document.getElementById('careerChart');
    if (!ctx || typeof Chart === 'undefined') return;
    destroyChartInstance('careerChart');
    
    const topRoles = allRoles.slice(0, 5);
    
    CHART_INSTANCES.careerChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: topRoles.map(r => r.role),
            datasets: [{
                label: 'Match %',
                data: topRoles.map(r => r.score),
                backgroundColor: ['#a78bfa', '#00f5ff', '#4ade80', 'rgba(255,255,255,0.2)', 'rgba(255,255,255,0.2)'],
                borderWidth: 0,
                borderRadius: 8
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            indexAxis: 'y',
            scales: { x: { suggestedMin: 0, suggestedMax: 100 }, y: { grid: { display: false } } },
            plugins: { legend: { display: false } }
        }
    });
}

window.selectCareerRole = async function() {
    const role = document.getElementById('rec-role')?.textContent;
    if (!role || role === '-') return;
    
    try {
        const formData = new FormData();
        formData.append('new_role', role);
        formData.append('csrf_token', window.APP_CONFIG?.csrfToken || "");
        
        const res = await fetch('/switch-role', { method: 'POST', body: formData });
        const data = await res.json();
        
        if (data.success) {
            alert(`Career role updated to: ${role}.`);
            window.location.reload();
        }
    } catch (e) { console.error(e); }
};

// ==================== RADAR PAGE LOGIC ====================
function initRadarPage() {
    const analysisData = window.PAGE_DATA?.analysis;
    const canvas = document.getElementById('skillRadar');
    if (!analysisData || !canvas || typeof Chart === 'undefined') return;
    destroyChartInstance('skillRadar');

    const labels = [...analysisData.matched_skills, ...analysisData.missing_skills].map(s => s.toUpperCase());
    const values = [...analysisData.matched_skills.map(()=>100), ...analysisData.missing_skills.map(()=>0)];

    CHART_INSTANCES.skillRadar = new Chart(canvas.getContext('2d'), {
        type: 'radar',
        data: {
            labels: labels,
            datasets: [{
                data: values,
                backgroundColor: 'rgba(0, 245, 255, 0.05)',
                borderColor: '#00f5ff',
                pointRadius: 2
            }]
        },
        options: {
            scales: { r: { suggestedMin: 0, suggestedMax: 100, ticks: {display: false} } },
            plugins: { legend: { display: false } }
        }
    });
}

window.switchRole = async function(newRole) {
    const formData = new FormData();
    formData.append("new_role", newRole);
    formData.append('csrf_token', window.APP_CONFIG?.csrfToken || "");
    
    try {
        const res = await fetch("/switch-role", { method: "POST", body: formData });
        const data = await res.json();
        if(data.success) window.location.reload();
    } catch (e) { console.error(e); }
};

// ==================== COLLABORATION LOGIC ====================
window.loadStudentGroups = async function() {
    const grid = document.getElementById('my-collaborations-grid');
    if (!grid) return;
    
    try {
        const res = await fetch('/student/projects');
        const json = await res.json();
        const projects = json.data?.projects || json.projects || (Array.isArray(json) ? json : null);
        
        if (!projects) return;
        
        const appliedProjects = projects.filter(p => p.applied);
        
        updateStat('stat-pending', appliedProjects.filter(p => p.status === 'pending').length);
        updateStat('stat-accepted-collab', appliedProjects.filter(p => p.status === 'accepted').length);
        updateStat('stat-rejected', appliedProjects.filter(p => p.status === 'rejected').length);
        
        if (appliedProjects.length === 0) {
            grid.innerHTML = `<div class="glass-card text-center py-16" style="grid-column: 1/-1;">No active collaborations.</div>`;
            return;
        }
        
        grid.innerHTML = appliedProjects.map(p => {
            const status = p.status || 'pending';
            const color = status === 'accepted' ? '#4ade80' : status === 'rejected' ? '#f87171' : '#fbbf24';
            return `
                <div class="glass-card flex-col" style="border-top: 3px solid ${color};">
                    <h4 class="mb-2">${escapeHtml(p.title)}</h4>
                    <p class="text-muted text-sm mb-4">${escapeHtml(p.description)}</p>
                    <div class="badge" style="color: ${color}; border-color: ${color};">${status.toUpperCase()}</div>
                </div>
            `;
        }).join('');
    } catch (e) { console.error(e); }
};

window.loadDiscovery = async function() {
    const grid = document.getElementById('discovery-grid');
    if (!grid) return;

    try {
        const res = await fetch('/get_groups');
        const data = await res.json();
        
        if (data.groups.length === 0) {
            grid.innerHTML = '<div class="glass-card w-full text-center py-6" style="grid-column: 1/-1;">No cohorts detected.</div>';
            return;
        }

        grid.innerHTML = data.groups.map(g => `
            <div class="glass-card flex-col" style="border-top: 3px solid #8b5cf6;">
                <h4 class="mb-2">${escapeHtml(g.name)}</h4>
                <p class="text-accent text-sm font-bold mb-4">${escapeHtml(g.project_title)}</p>
                <button class="glass-btn glass-btn-primary w-full mt-auto" onclick="joinGroup(${g.id})">Join cohort</button>
            </div>
        `).join('');
    } catch (e) { console.error(e); }
};

window.joinGroup = async function(id) {
    const formData = new FormData();
    formData.append('group_id', id);
    formData.append('csrf_token', window.APP_CONFIG?.csrfToken || "");
    
    try {
        const res = await fetch('/join_group', { method: 'POST', body: formData });
        const data = await res.json();
        if (data.success) {
            alert('Joined cohort!');
            loadDiscovery();
        }
    } catch (e) { console.error(e); }
};
