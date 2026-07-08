// ── State ─────────────────────────────────────────────────────────────────
let currentRunId = null;
let allLeads = [];
let allMessages = [];
let eventSource = null;
let statusPollTimer = null;
let sseRetryTimer = null;
let pipelineActive = false;
let lastEventAt = 0;
const PROGRESS_STEPS = { planner: 10, finder: 28, verifier: 45, scorer: 62, research: 80, writer: 92, sender: 100 };

// ── Example prompt chips ────────────────────────────────────────────────────
document.querySelectorAll('#promptExamples .ex-chip').forEach(chip => {
  chip.addEventListener('click', () => {
    const input = document.getElementById('promptInput');
    input.value = chip.textContent.trim();
    input.focus();
  });
});

// ── Slider ────────────────────────────────────────────────────────────────
const slider = document.getElementById('leadCount');
const sliderVal = document.getElementById('leadCountVal');
if (slider) {
  slider.addEventListener('input', () => {
    sliderVal.textContent = slider.value;
    const pct = ((slider.value - slider.min) / (slider.max - slider.min)) * 100;
    slider.style.setProperty('--pct', pct + '%');
  });
}

// ── Tab Switching ─────────────────────────────────────────────────────────
function switchTab(name) {
  ['leads','messages','log'].forEach(t => {
    document.getElementById('tab'+cap(t)).classList.remove('active');
    document.getElementById('panel'+cap(t)).style.display = 'none';
  });
  document.getElementById('tab'+cap(name)).classList.add('active');
  document.getElementById('panel'+cap(name)).style.display = 'block';
}
function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

// ── Start Campaign ────────────────────────────────────────────────────────
async function startCampaign() {
  const btn = document.getElementById('startBtn');
  const leadCount = parseInt(document.getElementById('leadCount').value, 10) || 10;
  const prompt = (document.getElementById('promptInput').value || '').trim();

  if (!prompt) {
    const input = document.getElementById('promptInput');
    input.classList.add('shake');
    input.focus();
    setTimeout(() => input.classList.remove('shake'), 600);
    return;
  }

  // Always allow a new launch — tear down any in-flight UI/stream first.
  // The server cancels any previous campaign so re-runs never 409.
  const previousRunId = currentRunId;
  teardownStream();
  pipelineActive = true;
  btn.disabled = true;
  document.getElementById('startBtnText').textContent = 'Starting...';

  // Reset UI for a fresh run
  document.getElementById('mainContent').style.display = 'block';
  document.getElementById('progressOverlay').style.display = 'flex';
  document.getElementById('progressBar').style.width = '5%';
  document.getElementById('progressTitle').textContent = 'Starting campaign...';
  document.getElementById('progressAgent').textContent = 'Initializing agents';
  ['planner','finder','verifier','scorer','research','writer'].forEach(s => {
    const el = document.getElementById('step-' + s);
    if (el) el.classList.remove('active', 'done');
  });
  document.getElementById('leadsGrid').innerHTML = '';
  document.getElementById('messagesList').innerHTML = '';
  document.getElementById('logFeed').innerHTML = '';
  document.getElementById('logDot').classList.add('pulsing');
  allLeads = []; allMessages = [];
  updateStats(0, 0, 0, 0);
  document.getElementById('leadsCount').textContent = '0';
  document.getElementById('messagesCount').textContent = '0';
  document.getElementById('exportBtn').disabled = true;

  if (previousRunId) {
    // Best-effort cancel so the old SSE/status poll stops server-side too
    try {
      fetch(`/api/cancel-campaign/${previousRunId}`, { method: 'POST' }).catch(() => {});
    } catch {}
  }

  try {
    const res = await fetch('/api/start-campaign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt: prompt,
        max_leads: leadCount,
        no_review: true,
        dry_run: true,
      }),
    });

    let data = {};
    try {
      data = await res.json();
    } catch {
      data = {};
    }

    if (!res.ok) {
      const detail = data.detail || data.message || res.statusText || 'Request failed';
      throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    }

    if (!data.run_id) {
      throw new Error('Server did not return a run_id');
    }

    currentRunId = data.run_id;
    document.getElementById('startBtnText').textContent = 'Running...';
    if (data.superseded_runs && data.superseded_runs.length) {
      appendLog({
        agent: 'Pipeline',
        message: `Previous campaign stopped — starting fresh run ${currentRunId}`,
        status: 'info',
      });
    } else {
      appendLog({ agent: 'Pipeline', message: `Campaign started (run ${currentRunId})`, status: 'info' });
    }
    connectSSE(currentRunId);
    startStatusPolling(currentRunId);
  } catch (err) {
    showError('Failed to start campaign: ' + (err.message || err));
    onPipelineDone(true);
  }
}

// ── SSE Connection ────────────────────────────────────────────────────────
function connectSSE(runId) {
  if (eventSource) {
    try { eventSource.close(); } catch {}
    eventSource = null;
  }
  if (sseRetryTimer) {
    clearTimeout(sseRetryTimer);
    sseRetryTimer = null;
  }

  lastEventAt = Date.now();
  eventSource = new EventSource(`/api/stream/${runId}`);

  eventSource.onmessage = (e) => {
    lastEventAt = Date.now();
    if (!e.data || e.data === '[DONE]') return;
    try {
      const event = JSON.parse(e.data);
      handleEvent(event);
    } catch (err) {
      console.warn('SSE parse error:', err, e.data);
    }
  };

  eventSource.onerror = () => {
    // Browser fires onerror on normal close too — only reconnect while active
    if (!pipelineActive || currentRunId !== runId) {
      if (eventSource) {
        try { eventSource.close(); } catch {}
        eventSource = null;
      }
      return;
    }
    if (eventSource) {
      try { eventSource.close(); } catch {}
      eventSource = null;
    }
    // Reconnect shortly; status poll will finish the run if SSE stays down
    sseRetryTimer = setTimeout(() => {
      if (pipelineActive && currentRunId === runId) {
        connectSSE(runId);
      }
    }, 1500);
  };
}

function startStatusPolling(runId) {
  if (statusPollTimer) clearInterval(statusPollTimer);
  statusPollTimer = setInterval(async () => {
    if (!pipelineActive || currentRunId !== runId) {
      clearInterval(statusPollTimer);
      statusPollTimer = null;
      return;
    }
    try {
      const res = await fetch(`/api/status/${runId}`);
      if (!res.ok) return;
      const st = await res.json();
      if (st.status === 'done') {
        await fetchAndApplyResults(runId, null);
      } else if (st.status === 'error') {
        await fetchAndApplyResults(runId, st.error || 'Pipeline failed');
      } else if (st.status === 'cancelled') {
        // Superseded by a newer campaign — ignore; the new run owns the UI
        if (currentRunId === runId) {
          onPipelineDone(true);
        }
      } else if (Date.now() - lastEventAt > 45000) {
        // Stream went quiet — nudge progress text so UI doesn't look frozen
        const agent = document.getElementById('progressAgent');
        if (agent && !agent.textContent.includes('still working')) {
          agent.textContent = (agent.textContent || 'Working') + ' (still working…)';
        }
      }
    } catch {
      // ignore transient poll errors
    }
  }, 4000);
}

async function fetchAndApplyResults(runId, errorMsg) {
  if (!pipelineActive || currentRunId !== runId) return;
  try {
    if (errorMsg) {
      showError(errorMsg);
      onPipelineDone();
      return;
    }
    const res = await fetch(`/api/results/${runId}`);
    if (res.status === 202) return; // still running
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError(err.detail || 'Failed to load results');
      onPipelineDone();
      return;
    }
    const data = await res.json();
    handleResults(data);
    onPipelineDone();
  } catch (err) {
    showError('Failed to load results: ' + (err.message || err));
    onPipelineDone();
  }
}

function teardownStream() {
  if (eventSource) {
    try { eventSource.close(); } catch {}
    eventSource = null;
  }
  if (statusPollTimer) {
    clearInterval(statusPollTimer);
    statusPollTimer = null;
  }
  if (sseRetryTimer) {
    clearTimeout(sseRetryTimer);
    sseRetryTimer = null;
  }
}

// ── Event Handler ─────────────────────────────────────────────────────────
function handleEvent(ev) {
  // Ignore late events from a superseded run
  if (ev.run_id && currentRunId && ev.run_id !== currentRunId) return;

  switch (ev.type) {
    case 'log':
      appendLog(ev);
      updateProgressFromAgent(ev.agent, ev.message);
      break;
    case 'status':
      if (ev.message) {
        document.getElementById('progressAgent').textContent = ev.message;
        appendLog({ agent: 'Pipeline', message: ev.message, status: 'info' });
      }
      break;
    case 'results':
      handleResults(ev.data);
      break;
    case 'done':
      // If the results event was dropped, pull from the REST endpoint
      if (!allLeads.length && currentRunId) {
        fetchAndApplyResults(currentRunId, null);
      } else {
        onPipelineDone();
      }
      break;
    case 'error': {
      const msg = (ev.message || 'Pipeline error').toLowerCase();
      // Superseded/cancelled is expected when the user starts a new campaign
      if (msg.includes('superseded') || msg.includes('cancelled')) {
        if (pipelineActive && currentRunId) return;
        onPipelineDone(true);
        return;
      }
      showError(ev.message || 'Pipeline error');
      onPipelineDone();
      break;
    }
    case 'ping':
      break;
  }
}

// ── Progress Tracking ─────────────────────────────────────────────────────
function updateProgressFromAgent(agent, message) {
  const a = (agent || '').toLowerCase();
  let agentKey = null;
  let pct = null;
  let label = '';

  if (a.includes('planner')) { agentKey = 'planner'; pct = PROGRESS_STEPS.planner; label = 'Planning your campaign...'; }
  else if (a.includes('finder')) { agentKey = 'finder'; pct = PROGRESS_STEPS.finder; label = 'Discovering real prospects...'; }
  else if (a.includes('verifier')) { agentKey = 'verifier'; pct = PROGRESS_STEPS.verifier; label = 'Verifying leads are real...'; }
  else if (a.includes('scorer')) { agentKey = 'scorer'; pct = PROGRESS_STEPS.scorer; label = 'Scoring & qualifying leads...'; }
  else if (a.includes('research')) { agentKey = 'research'; pct = PROGRESS_STEPS.research; label = 'Deep researching companies...'; }
  else if (a.includes('writer')) { agentKey = 'writer'; pct = PROGRESS_STEPS.writer; label = 'Writing personalised messages...'; }
  else if (a.includes('sender') || a.includes('review')) { agentKey = 'sender'; pct = PROGRESS_STEPS.sender; label = 'Finalising results...'; }

  if (agentKey) {
    setProgressStep(agentKey, pct, label, message);
  }

  const discovered = message.match(/discovered (\d+) real prospects/i)
    || message.match(/discovery complete:\s*(\d+)/i);
  const verified = message.match(/(\d+)\/(\d+) leads verified/i);
  const verifiedAlt = message.match(/(\d+)\/(\d+) leads verified,\s*(\d+) partial/i);
  const qualified = message.match(/(\d+)\/(\d+) leads qualify/i);
  const generated = message.match(/generated (\d+) messages/i);
  if (discovered) updateStat('statDiscovered', parseInt(discovered[1], 10));
  if (verified) {
    updateStat('statDiscovered', parseInt(verified[2], 10));
    updateStat('statVerified', parseInt(verified[1], 10));
  } else if (verifiedAlt) {
    updateStat('statDiscovered', parseInt(verifiedAlt[2], 10));
    updateStat('statVerified', parseInt(verifiedAlt[1], 10) + parseInt(verifiedAlt[3], 10));
  }
  if (qualified) updateStat('statQualified', parseInt(qualified[1], 10));
  if (generated) updateStat('statMessages', parseInt(generated[1], 10));
}

function setProgressStep(key, pct, title, agentMsg) {
  document.getElementById('progressBar').style.width = pct + '%';
  document.getElementById('progressTitle').textContent = title;
  document.getElementById('progressAgent').textContent = agentMsg || '';

  const order = ['planner', 'finder', 'verifier', 'scorer', 'research', 'writer'];
  const idx = order.indexOf(key === 'sender' ? 'writer' : key);
  order.forEach((s, i) => {
    const el = document.getElementById('step-' + s);
    if (!el) return;
    if (i < idx) { el.classList.add('done'); el.classList.remove('active'); }
    else if (i === idx) { el.classList.add('active'); el.classList.remove('done'); }
    else { el.classList.remove('active', 'done'); }
  });
  if (key === 'sender') {
    order.forEach(s => {
      const el = document.getElementById('step-' + s);
      if (el) { el.classList.add('done'); el.classList.remove('active'); }
    });
  }
}

// ── Results Handler ───────────────────────────────────────────────────────
function handleResults(data) {
  if (!data) return;
  allLeads = data.leads || [];
  allMessages = allLeads
    .map(l => (l.message ? { ...l.message, leadId: l.id, leadName: l.name, company: l.company } : null))
    .filter(Boolean);

  const s = data.stats || {};
  updateStat('statDiscovered', s.discovered ?? allLeads.length);
  updateStat('statVerified', s.verified ?? 0);
  updateStat('statQualified', s.qualified ?? 0);
  updateStat('statMessages', s.messages_generated ?? allMessages.length);
  document.getElementById('leadsCount').textContent = String(allLeads.length);
  document.getElementById('messagesCount').textContent = String(allMessages.length);

  renderLeads(allLeads);
  renderMessages(allLeads);
  document.getElementById('exportBtn').disabled = !allLeads.length;

  if (data.summary) {
    appendLog({ agent: 'Pipeline', message: data.summary, status: data.end_reason === 'complete' ? 'done' : 'warn' });
  }
}

// ── Render Leads ──────────────────────────────────────────────────────────
function renderLeads(leads) {
  const grid = document.getElementById('leadsGrid');
  grid.innerHTML = '';
  if (!leads.length) {
    const hint = 'No leads to display yet. Check the Live Feed for agent progress or try a broader prompt.';
    grid.innerHTML = `<div class="empty-state"><div class="empty-icon">🔍</div><div>No leads found</div><div style="margin-top:8px;font-size:12px;color:var(--text-dim)">${escHtml(hint)}</div></div>`;
    return;
  }
  leads.forEach((lead, idx) => {
    grid.appendChild(buildLeadCard(lead, idx));
  });
}

function buildLeadCard(lead, idx) {
  const colors = ['#6378ff', '#a855f7', '#06b6d4', '#10b981', '#f59e0b', '#ef4444', '#ec4899', '#8b5cf6'];
  const bg = colors[idx % colors.length];
  const initials = (lead.name || 'X').split(' ').map(w => w[0]).filter(Boolean).join('').slice(0, 2).toUpperCase() || 'X';
  const score = lead.score || 0;
  const scoreClass = score >= 80 ? 'score-high' : score >= 65 ? 'score-mid' : 'score-low';

  const painPoints = (lead.pain_points || []).slice(0, 2);
  const painHtml = painPoints.map(p => `<span class="pain-pill">${escHtml(String(p).slice(0, 50))}</span>`).join('');

  const linkedinHtml = lead.linkedin_url
    ? `<a class="link-btn link-linkedin" href="${escAttr(lead.linkedin_url)}" target="_blank" rel="noopener">💼 LinkedIn</a>` : '';
  const webHtml = lead.company_website
    ? `<a class="link-btn link-web" href="${escAttr(lead.company_website)}" target="_blank" rel="noopener">🌐 Website</a>` : '';

  const hasMsg = lead.message && lead.message.body;
  const channelIcon = { email: '📧', linkedin: '💼', reddit: '🤖' }[lead.best_channel] || '📨';
  const verifyHtml = verifyBadge(lead.verification);
  const leadIdx = allLeads.indexOf(lead);

  const card = document.createElement('div');
  card.className = 'lead-card';
  card.onclick = () => openLeadModal(lead);
  card.innerHTML = `
    <div class="lead-card-header">
      <div class="lead-avatar" style="background:linear-gradient(135deg,${bg}aa,${bg}66)">${escHtml(initials)}</div>
      <div class="lead-info">
        <div class="lead-name">${escHtml(lead.name)}</div>
        <div class="lead-title">${escHtml(lead.title)}</div>
      </div>
      <div class="score-badge ${scoreClass}">${score}</div>
    </div>
    ${verifyHtml}
    <div class="lead-company">
      <strong>${escHtml(lead.company)}</strong>
      <span class="industry-tag">${escHtml(lead.industry)}</span>
    </div>
    <div style="font-size:12px;color:var(--text-dim);margin-bottom:10px">
      📍 ${escHtml(lead.location || '')} &nbsp;·&nbsp; 👥 ${escHtml(lead.company_size || '')}
    </div>
    <div class="lead-links"></div>
    ${painPoints.length ? `<div class="pain-points"><div class="pain-label">Pain Points</div>${painHtml}</div>` : ''}
    <div class="lead-service">${escHtml(lead.recommended_service || '')}</div>
    <div class="lead-card-footer">
      <span class="channel-icon">${channelIcon}</span>
      <span class="msg-slot"></span>
    </div>
  `;

  const links = card.querySelector('.lead-links');
  if (linkedinHtml) links.insertAdjacentHTML('beforeend', linkedinHtml);
  if (lead.email) {
    const emailBtn = document.createElement('span');
    emailBtn.className = 'link-btn link-email';
    emailBtn.textContent = '📧 ' + lead.email;
    emailBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      copyText(lead.email, emailBtn);
    });
    links.appendChild(emailBtn);
  }
  if (webHtml) links.insertAdjacentHTML('beforeend', webHtml);

  const msgSlot = card.querySelector('.msg-slot');
  if (hasMsg) {
    const btn = document.createElement('button');
    btn.className = 'view-msg-btn';
    btn.textContent = 'View Message';
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      openMessageModal(allLeads[leadIdx] || lead);
    });
    msgSlot.appendChild(btn);
  } else {
    msgSlot.innerHTML = '<span style="font-size:12px;color:var(--text-dim)">No message yet</span>';
  }

  return card;
}

// ── Verification badge ──────────────────────────────────────────────────────
function verifyBadge(v) {
  if (!v) return '';
  const status = v.status || 'unverified';
  const conf = v.confidence || 0;
  const map = {
    verified: { cls: 'vb-verified', icon: '✅', label: 'Verified' },
    partial: { cls: 'vb-partial', icon: '🟡', label: 'Partially verified' },
    unverified: { cls: 'vb-unverified', icon: '⚠️', label: 'Unverified' },
  };
  const m = map[status] || map.unverified;
  const bits = [];
  if (v.linkedin_valid) bits.push('LinkedIn');
  if (v.domain_live) bits.push('Website');
  if (v.email_valid) bits.push('Email');
  const detail = bits.length ? bits.join(' · ') : 'no anchors confirmed';
  return `<div class="verify-badge ${m.cls}" title="Confidence ${conf}%">
    <span>${m.icon} ${m.label}</span>
    <span class="verify-detail">${escHtml(detail)}</span>
  </div>`;
}

// ── Render Messages ───────────────────────────────────────────────────────
function renderMessages(leads) {
  const list = document.getElementById('messagesList');
  list.innerHTML = '';
  const withMsg = leads.filter(l => l.message && l.message.body);
  if (!withMsg.length) {
    list.innerHTML = '<div class="empty-state"><div class="empty-icon">✉</div><div>No messages generated yet</div></div>';
    return;
  }
  withMsg.forEach(lead => list.appendChild(buildMessageCard(lead)));
}

function buildMessageCard(lead) {
  const msg = lead.message;
  const ch = msg.channel || 'email';
  const chClass = { email: 'ch-email', linkedin: 'ch-linkedin', reddit: 'ch-reddit' }[ch] || 'ch-email';
  const chLabel = ch.charAt(0).toUpperCase() + ch.slice(1);
  const copyPayload = (msg.subject ? 'Subject: ' + msg.subject + '\n\n' : '') + (msg.body || '');

  const card = document.createElement('div');
  card.className = 'message-card';
  card.innerHTML = `
    <div class="message-card-header">
      <div class="message-to"><strong>${escHtml(lead.name)}</strong> <span>· ${escHtml(lead.title)} @ ${escHtml(lead.company)}</span></div>
      <div class="message-meta">
        <span class="channel-badge ${chClass}">${escHtml(chLabel)}</span>
        <span class="score-mini">P: ${Math.round((msg.personalization_score || 0) * 100)}% · T: ${Math.round((msg.tone_score || 0) * 100)}%</span>
      </div>
    </div>
    ${msg.subject ? `<div class="message-subject">📌 ${escHtml(msg.subject)}</div>` : ''}
    <div class="message-body">${escHtml(msg.body || '')}</div>
  `;

  const copyBtn = document.createElement('button');
  copyBtn.className = 'copy-btn';
  copyBtn.textContent = '📋 Copy to Clipboard';
  copyBtn.addEventListener('click', () => copyText(copyPayload, copyBtn));
  card.appendChild(copyBtn);
  return card;
}

// ── Modals ────────────────────────────────────────────────────────────────
function openLeadModal(lead) {
  const mo = document.getElementById('modalOverlay');
  const mc = document.getElementById('modalContent');
  const score = lead.score || 0;
  const scoreClass = score >= 80 ? 'score-high' : score >= 65 ? 'score-mid' : 'score-low';
  const painPoints = (lead.pain_points || []).map(p => `<li>${escHtml(p)}</li>`).join('');
  const opportunities = (lead.opportunities || []).map(o => `<li>${escHtml(o)}</li>`).join('');
  const techStack = (lead.tech_stack || []).join(', ');

  mc.innerHTML = `
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:20px">
      <div class="score-badge ${scoreClass}" style="width:52px;height:52px;font-size:16px">${score}</div>
      <div>
        <div style="font-size:20px;font-weight:800">${escHtml(lead.name)}</div>
        <div style="color:var(--text-dim);font-size:13px">${escHtml(lead.title)} · ${escHtml(lead.company)}</div>
      </div>
    </div>
    ${verifyBadge(lead.verification)}
    ${(lead.verification && lead.verification.checks && lead.verification.checks.length)
      ? `<div style="margin:10px 0 16px"><div class="pain-label">Verification checks</div><ul style="font-size:12px;padding-left:18px;display:flex;flex-direction:column;gap:3px;color:var(--text-dim)">${lead.verification.checks.map(c => `<li>${escHtml(c)}</li>`).join('')}</ul></div>`
      : ''}
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:18px">
      ${infoRow('Industry', lead.industry)}
      ${infoRow('Location', lead.location)}
      ${infoRow('Company Size', lead.company_size)}
      ${infoRow('Channel', lead.best_channel)}
      ${infoRow('Source', lead.source)}
      ${lead.fit_reason ? infoRow('Why they fit', lead.fit_reason) : ''}
    </div>
    ${lead.company_summary ? `<div style="background:var(--bg2);border-radius:10px;padding:14px;margin-bottom:14px;font-size:13px;line-height:1.7;border:1px solid var(--card-border)">${escHtml(lead.company_summary)}</div>` : ''}
    ${lead.recent_news && lead.recent_news !== 'No recent news found.' ? `<div style="margin-bottom:14px"><div class="pain-label">Recent News</div><div style="font-size:13px;color:var(--accent3)">${escHtml(lead.recent_news)}</div></div>` : ''}
    ${painPoints ? `<div style="margin-bottom:14px"><div class="pain-label">Pain Points</div><ul style="font-size:13px;padding-left:18px;display:flex;flex-direction:column;gap:4px">${painPoints}</ul></div>` : ''}
    ${opportunities ? `<div style="margin-bottom:14px"><div class="pain-label">Opportunities for Shaheer</div><ul style="font-size:13px;padding-left:18px;color:var(--green);display:flex;flex-direction:column;gap:4px">${opportunities}</ul></div>` : ''}
    ${techStack ? `<div style="margin-bottom:14px"><div class="pain-label">Tech Stack</div><div style="font-size:13px">${escHtml(techStack)}</div></div>` : ''}
    <div class="lead-links modal-links" style="margin-bottom:16px"></div>
    <div class="modal-actions"></div>
  `;

  const links = mc.querySelector('.modal-links');
  if (lead.linkedin_url) {
    links.insertAdjacentHTML('beforeend',
      `<a class="link-btn link-linkedin" href="${escAttr(lead.linkedin_url)}" target="_blank" rel="noopener">💼 LinkedIn</a>`);
  }
  if (lead.email) {
    const emailBtn = document.createElement('span');
    emailBtn.className = 'link-btn link-email';
    emailBtn.textContent = '📧 ' + lead.email;
    emailBtn.addEventListener('click', () => copyText(lead.email, emailBtn));
    links.appendChild(emailBtn);
  }
  if (lead.company_website) {
    links.insertAdjacentHTML('beforeend',
      `<a class="link-btn link-web" href="${escAttr(lead.company_website)}" target="_blank" rel="noopener">🌐 Website</a>`);
  }

  const actions = mc.querySelector('.modal-actions');
  if (lead.message && lead.message.body) {
    const btn = document.createElement('button');
    btn.className = 'view-msg-btn';
    btn.style.cssText = 'width:100%;padding:12px';
    btn.textContent = '✉ View Outreach Message';
    btn.addEventListener('click', () => {
      closeModal();
      openMessageModal(findLead(lead.id) || lead);
    });
    actions.appendChild(btn);
  }

  mo.style.display = 'flex';
}

function openMessageModal(lead) {
  if (!lead || !lead.message) return;
  const msg = lead.message;
  const ch = msg.channel || 'email';
  const chClass = { email: 'ch-email', linkedin: 'ch-linkedin', reddit: 'ch-reddit' }[ch] || 'ch-email';
  const copyPayload = (msg.subject ? 'Subject: ' + msg.subject + '\n\n' : '') + (msg.body || '');

  const mc = document.getElementById('modalContent');
  const mo = document.getElementById('modalOverlay');

  mc.innerHTML = `
    <div style="margin-bottom:20px">
      <div style="font-size:18px;font-weight:800;margin-bottom:4px">Message to ${escHtml(lead.name)}</div>
      <div style="display:flex;gap:8px;align-items:center">
        <span class="channel-badge ${chClass}">${escHtml(ch)}</span>
        <span style="font-size:12px;color:var(--text-dim)">${escHtml(lead.title)} · ${escHtml(lead.company)}</span>
      </div>
    </div>
    <div style="display:flex;gap:12px;margin-bottom:16px">
      <div style="flex:1;background:var(--bg2);border-radius:10px;padding:12px;text-align:center;border:1px solid var(--card-border)">
        <div style="font-size:20px;font-weight:800;color:var(--accent)">${Math.round((msg.personalization_score || 0) * 100)}%</div>
        <div style="font-size:11px;color:var(--text-dim)">Personalization</div>
      </div>
      <div style="flex:1;background:var(--bg2);border-radius:10px;padding:12px;text-align:center;border:1px solid var(--card-border)">
        <div style="font-size:20px;font-weight:800;color:var(--accent2)">${Math.round((msg.tone_score || 0) * 100)}%</div>
        <div style="font-size:11px;color:var(--text-dim)">Human Tone</div>
      </div>
      <div style="flex:1;background:var(--bg2);border-radius:10px;padding:12px;text-align:center;border:1px solid var(--card-border)">
        <div style="font-size:20px;font-weight:800;color:var(--green)">${lead.score || 0}</div>
        <div style="font-size:11px;color:var(--text-dim)">Lead Score</div>
      </div>
    </div>
    ${msg.subject ? `<div class="message-subject" style="margin-bottom:12px">📌 ${escHtml(msg.subject)}</div>` : ''}
    <div class="message-body">${escHtml(msg.body || '')}</div>
    <div class="modal-msg-actions" style="display:flex;gap:10px;margin-top:16px"></div>
  `;

  const actions = mc.querySelector('.modal-msg-actions');
  const copyBtn = document.createElement('button');
  copyBtn.className = 'copy-btn';
  copyBtn.style.flex = '1';
  copyBtn.textContent = '📋 Copy Message';
  copyBtn.addEventListener('click', () => copyText(copyPayload, copyBtn));
  actions.appendChild(copyBtn);

  if (lead.email) {
    const emailBtn = document.createElement('button');
    emailBtn.className = 'copy-btn';
    emailBtn.style.flex = '1';
    emailBtn.textContent = '📧 Copy Email';
    emailBtn.addEventListener('click', () => copyText(lead.email, emailBtn));
    actions.appendChild(emailBtn);
  }
  if (lead.linkedin_url) {
    actions.insertAdjacentHTML('beforeend',
      `<a class="link-btn link-linkedin" style="flex:1;justify-content:center" href="${escAttr(lead.linkedin_url)}" target="_blank" rel="noopener">💼 Open LinkedIn</a>`);
  }

  mo.style.display = 'flex';
}

function findLead(id) { return allLeads.find(l => l.id === id); }
function closeModal() { document.getElementById('modalOverlay').style.display = 'none'; }
function infoRow(label, val) {
  if (!val) return '';
  return `<div style="background:var(--bg2);border-radius:8px;padding:10px;border:1px solid var(--card-border)">
    <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--text-dim);margin-bottom:3px">${escHtml(label)}</div>
    <div style="font-size:13px">${escHtml(val)}</div>
  </div>`;
}

// ── Log Feed ──────────────────────────────────────────────────────────────
function appendLog(ev) {
  const feed = document.getElementById('logFeed');
  if (!feed) return;
  const ph = feed.querySelector('.log-placeholder');
  if (ph) ph.remove();

  const agentKey = (ev.agent || '').toLowerCase().replace('agent', '').trim();
  const tagClass = `tag-${agentKey.split(/[^a-z]/)[0] || 'pipeline'}`;
  const lineClass = `log-${ev.status || 'info'}`;
  const time = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });

  const line = document.createElement('div');
  line.className = `log-line ${lineClass}`;
  line.innerHTML = `
    <span class="log-time">${time}</span>
    <span class="log-agent-tag ${tagClass}">${escHtml(ev.agent || 'System')}</span>
    <span class="log-msg">${escHtml(ev.message || '')}</span>
  `;
  feed.appendChild(line);
  feed.scrollTop = feed.scrollHeight;
}

// ── Pipeline Done ─────────────────────────────────────────────────────────
function onPipelineDone(failedStart) {
  pipelineActive = false;
  teardownStream();
  document.getElementById('progressOverlay').style.display = 'none';
  const btn = document.getElementById('startBtn');
  btn.disabled = false;
  document.getElementById('startBtnText').textContent = failedStart ? 'Launch Research Campaign' : 'Run New Campaign';
  document.getElementById('logDot').classList.remove('pulsing');
}

// ── Helpers ───────────────────────────────────────────────────────────────
function updateStats(discovered, verified, qualified, messages) {
  updateStat('statDiscovered', discovered);
  updateStat('statVerified', verified);
  updateStat('statQualified', qualified);
  updateStat('statMessages', messages);
}
function updateStat(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}
function escHtml(s) {
  if (s == null) return '';
  if (typeof s === 'object') {
    try { s = JSON.stringify(s); } catch { s = String(s); }
  }
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
function escAttr(s) {
  return escHtml(s).replace(/`/g, '&#96;');
}
function copyText(text, btn) {
  const value = text == null ? '' : String(text);
  const done = () => {
    if (!btn) return;
    const orig = btn.textContent;
    btn.textContent = '✅ Copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = orig; btn.classList.remove('copied'); }, 2000);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(value).then(done).catch(() => fallbackCopy(value, done));
  } else {
    fallbackCopy(value, done);
  }
}
function fallbackCopy(text, done) {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    if (done) done();
  } catch (e) {
    console.warn('Copy failed', e);
  }
}
function showError(msg) {
  appendLog({ agent: 'System', message: 'ERROR: ' + msg, status: 'error' });
  document.getElementById('progressTitle').textContent = 'Error occurred';
  document.getElementById('progressAgent').textContent = msg;
  document.getElementById('mainContent').style.display = 'block';
}

// ── Export CSV ────────────────────────────────────────────────────────────
function exportResults() {
  if (!allLeads.length) return;
  const headers = ['Name', 'Title', 'Company', 'Industry', 'Company Size', 'Location', 'Email', 'Email Source', 'LinkedIn URL', 'Website', 'Verification', 'Confidence', 'Score', 'Recommended Service', 'Best Channel', 'Source', 'Pain Points', 'Company Summary', 'Message Subject', 'Message Body'];
  const rows = allLeads.map(l => [
    l.name, l.title, l.company, l.industry, l.company_size, l.location,
    l.email, l.email_source, l.linkedin_url, l.company_website,
    l.verification?.status || '', l.verification?.confidence || '',
    l.score, l.recommended_service,
    l.best_channel, l.source, (l.pain_points || []).join('; '), l.company_summary,
    l.message?.subject || '', l.message?.body || '',
  ].map(v => `"${String(v || '').replace(/"/g, '""')}"`));

  const csv = [headers.join(','), ...rows.map(r => r.join(','))].join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `outreach_leads_${currentRunId || 'export'}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}
