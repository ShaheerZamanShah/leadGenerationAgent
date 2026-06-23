// ── State ─────────────────────────────────────────────────────────────────
let currentRunId = null;
let allLeads = [];
let allMessages = [];
let eventSource = null;
const PROGRESS_STEPS = { finder: 15, scorer: 35, research: 60, writer: 85, sender: 100 };

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
  const leadCount = parseInt(document.getElementById('leadCount').value) || 18;

  btn.disabled = true;
  document.getElementById('startBtnText').textContent = 'Starting...';

  // Show main content & progress overlay
  document.getElementById('mainContent').style.display = 'block';
  document.getElementById('progressOverlay').style.display = 'flex';
  document.getElementById('leadsGrid').innerHTML = '';
  document.getElementById('messagesList').innerHTML = '';
  document.getElementById('logFeed').innerHTML = '';
  allLeads = []; allMessages = [];
  updateStats(0,0,0,0);

  try {
    const res = await fetch('/api/start-campaign', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        max_leads: leadCount,
        no_review: true,
        dry_run: true,
      })
    });
    const data = await res.json();
    currentRunId = data.run_id;
    connectSSE(currentRunId);
  } catch (err) {
    showError('Failed to start campaign: ' + err.message);
    btn.disabled = false;
    document.getElementById('startBtnText').textContent = 'Start AI Research Campaign';
  }
}

// ── SSE Connection ────────────────────────────────────────────────────────
function connectSSE(runId) {
  if (eventSource) eventSource.close();
  eventSource = new EventSource(`/api/stream/${runId}`);

  eventSource.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);
      handleEvent(event);
    } catch {}
  };

  eventSource.onerror = () => {
    eventSource.close();
  };
}

// ── Event Handler ─────────────────────────────────────────────────────────
function handleEvent(ev) {
  switch (ev.type) {
    case 'log':
      appendLog(ev);
      updateProgressFromAgent(ev.agent, ev.message);
      break;
    case 'results':
      handleResults(ev.data);
      break;
    case 'done':
      onPipelineDone();
      break;
    case 'error':
      showError(ev.message);
      onPipelineDone();
      break;
    case 'ping':
      break;
  }
}

// ── Progress Tracking ─────────────────────────────────────────────────────
function updateProgressFromAgent(agent, message) {
  const a = (agent || '').toLowerCase();
  const m = (message || '').toLowerCase();
  let agentKey = null;
  let pct = null;
  let label = '';

  if (a.includes('finder')) { agentKey='finder'; pct=PROGRESS_STEPS.finder; label='Discovering prospects...'; }
  else if (a.includes('scorer')) { agentKey='scorer'; pct=PROGRESS_STEPS.scorer; label='Scoring & qualifying leads...'; }
  else if (a.includes('research')) { agentKey='research'; pct=PROGRESS_STEPS.research; label='Deep researching companies...'; }
  else if (a.includes('writer')) { agentKey='writer'; pct=PROGRESS_STEPS.writer; label='Writing personalised messages...'; }
  else if (a.includes('sender')) { agentKey='sender'; pct=PROGRESS_STEPS.sender; label='Finalising results...'; }

  if (agentKey) {
    setProgressStep(agentKey, pct, label, message);
  }

  // Update stats from log messages
  const discovered = message.match(/discovered (\d+) prospects/i);
  const qualified = message.match(/(\d+)\/(\d+) leads qualify/i);
  const researched = message.match(/researched (\d+) leads/i);
  const generated = message.match(/generated (\d+) messages/i);
  if (discovered) updateStat('statDiscovered', parseInt(discovered[1]));
  if (qualified) { updateStat('statDiscovered', parseInt(qualified[2])); updateStat('statQualified', parseInt(qualified[1])); }
  if (researched) updateStat('statResearched', parseInt(researched[1]));
  if (generated) updateStat('statMessages', parseInt(generated[1]));
}

function setProgressStep(key, pct, title, agentMsg) {
  document.getElementById('progressBar').style.width = pct + '%';
  document.getElementById('progressTitle').textContent = title;
  document.getElementById('progressAgent').textContent = agentMsg || '';

  // Mark steps
  const order = ['finder','scorer','research','writer','sender'];
  const idx = order.indexOf(key);
  order.forEach((s, i) => {
    const el = document.getElementById('step-' + s);
    if (!el) return;
    if (i < idx) { el.classList.add('done'); el.classList.remove('active'); }
    else if (i === idx) { el.classList.add('active'); el.classList.remove('done'); }
    else { el.classList.remove('active','done'); }
  });
}

// ── Results Handler ───────────────────────────────────────────────────────
function handleResults(data) {
  allLeads = data.leads || [];
  allMessages = allLeads.map(l => l.message ? {...l.message, leadId: l.id, leadName: l.name, company: l.company} : null).filter(Boolean);

  // Update stats
  const s = data.stats || {};
  updateStat('statDiscovered', s.discovered || allLeads.length);
  updateStat('statQualified', s.qualified || allLeads.length);
  updateStat('statResearched', s.researched || allLeads.length);
  updateStat('statMessages', s.messages_generated || allMessages.length);
  document.getElementById('leadsCount').textContent = allLeads.length;
  document.getElementById('messagesCount').textContent = allMessages.length;

  // Render leads
  renderLeads(allLeads);
  renderMessages(allLeads);

  document.getElementById('exportBtn').disabled = false;
}

// ── Render Leads ──────────────────────────────────────────────────────────
function renderLeads(leads) {
  const grid = document.getElementById('leadsGrid');
  grid.innerHTML = '';
  if (!leads.length) {
    grid.innerHTML = '<div class="empty-state"><div class="empty-icon">🔍</div><div>No leads found</div></div>';
    return;
  }
  leads.forEach((lead, idx) => {
    grid.appendChild(buildLeadCard(lead, idx));
  });
}

function buildLeadCard(lead, idx) {
  const colors = ['#6378ff','#a855f7','#06b6d4','#10b981','#f59e0b','#ef4444','#ec4899','#8b5cf6'];
  const bg = colors[idx % colors.length];
  const initials = (lead.name || 'X').split(' ').map(w=>w[0]).join('').slice(0,2).toUpperCase();
  const score = lead.score || 0;
  const scoreClass = score >= 80 ? 'score-high' : score >= 65 ? 'score-mid' : 'score-low';

  const painPoints = (lead.pain_points || []).slice(0,2);
  const painHtml = painPoints.map(p => `<span class="pain-pill">${escHtml(p.slice(0,50))}</span>`).join('');

  const linkedinHtml = lead.linkedin_url
    ? `<a class="link-btn link-linkedin" href="${escHtml(lead.linkedin_url)}" target="_blank" onclick="event.stopPropagation()">💼 LinkedIn</a>` : '';
  const emailHtml = lead.email
    ? `<span class="link-btn link-email" onclick="copyText('${escHtml(lead.email)}', this); event.stopPropagation()">📧 ${escHtml(lead.email)}</span>` : '';
  const webHtml = lead.company_website
    ? `<a class="link-btn link-web" href="${escHtml(lead.company_website)}" target="_blank" onclick="event.stopPropagation()">🌐 Website</a>` : '';

  const hasMsg = lead.message && lead.message.body;
  const channelIcon = {'email':'📧','linkedin':'💼','reddit':'🤖'}[lead.best_channel] || '📨';

  const card = document.createElement('div');
  card.className = 'lead-card';
  card.onclick = () => openLeadModal(lead);
  card.innerHTML = `
    <div class="lead-card-header">
      <div class="lead-avatar" style="background:linear-gradient(135deg,${bg}aa,${bg}66)">${initials}</div>
      <div class="lead-info">
        <div class="lead-name">${escHtml(lead.name)}</div>
        <div class="lead-title">${escHtml(lead.title)}</div>
      </div>
      <div class="score-badge ${scoreClass}">${score}</div>
    </div>
    <div class="lead-company">
      <strong>${escHtml(lead.company)}</strong>
      <span class="industry-tag">${escHtml(lead.industry)}</span>
    </div>
    <div style="font-size:12px;color:var(--text-dim);margin-bottom:10px">
      📍 ${escHtml(lead.location || '')} &nbsp;·&nbsp; 👥 ${escHtml(lead.company_size || '')}
    </div>
    <div class="lead-links">${linkedinHtml}${emailHtml}${webHtml}</div>
    ${painPoints.length ? `<div class="pain-points"><div class="pain-label">Pain Points</div>${painHtml}</div>` : ''}
    <div class="lead-service">${escHtml(lead.recommended_service || '')}</div>
    <div class="lead-card-footer">
      <span class="channel-icon">${channelIcon}</span>
      ${hasMsg ? `<button class="view-msg-btn" onclick="openMessageModal(allLeads[${allLeads.indexOf(lead)}]); event.stopPropagation()">View Message</button>` : '<span style="font-size:12px;color:var(--text-dim)">No message yet</span>'}
    </div>
  `;
  return card;
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
  const chClass = {'email':'ch-email','linkedin':'ch-linkedin','reddit':'ch-reddit'}[ch] || 'ch-email';
  const chLabel = ch.charAt(0).toUpperCase() + ch.slice(1);

  const card = document.createElement('div');
  card.className = 'message-card';
  card.innerHTML = `
    <div class="message-card-header">
      <div class="message-to"><strong>${escHtml(lead.name)}</strong> <span>· ${escHtml(lead.title)} @ ${escHtml(lead.company)}</span></div>
      <div class="message-meta">
        <span class="channel-badge ${chClass}">${chLabel}</span>
        <span class="score-mini">P: ${Math.round((msg.personalization_score||0)*100)}% · T: ${Math.round((msg.tone_score||0)*100)}%</span>
      </div>
    </div>
    ${msg.subject ? `<div class="message-subject">📌 ${escHtml(msg.subject)}</div>` : ''}
    <div class="message-body">${escHtml(msg.body || '')}</div>
    <button class="copy-btn" onclick="copyText(${JSON.stringify((msg.subject ? 'Subject: '+msg.subject+'\n\n' : '') + (msg.body||''))}, this)">
      📋 Copy to Clipboard
    </button>
  `;
  return card;
}

// ── Modals ────────────────────────────────────────────────────────────────
function openLeadModal(lead) {
  const mo = document.getElementById('modalOverlay');
  const mc = document.getElementById('modalContent');
  const score = lead.score || 0;
  const scoreClass = score >= 80 ? 'score-high' : score >= 65 ? 'score-mid' : 'score-low';
  const painPoints = (lead.pain_points || []).map(p=>`<li>${escHtml(p)}</li>`).join('');
  const opportunities = (lead.opportunities || []).map(o=>`<li>${escHtml(o)}</li>`).join('');
  const techStack = (lead.tech_stack || []).join(', ');

  mc.innerHTML = `
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:20px">
      <div class="score-badge ${scoreClass}" style="width:52px;height:52px;font-size:16px">${score}</div>
      <div>
        <div style="font-size:20px;font-weight:800">${escHtml(lead.name)}</div>
        <div style="color:var(--text-dim);font-size:13px">${escHtml(lead.title)} · ${escHtml(lead.company)}</div>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:18px">
      ${infoRow('Industry', lead.industry)}
      ${infoRow('Location', lead.location)}
      ${infoRow('Company Size', lead.company_size)}
      ${infoRow('Channel', lead.best_channel)}
    </div>
    ${lead.company_summary ? `<div style="background:var(--bg2);border-radius:10px;padding:14px;margin-bottom:14px;font-size:13px;line-height:1.7;border:1px solid var(--card-border)">${escHtml(lead.company_summary)}</div>` : ''}
    ${lead.recent_news && lead.recent_news !== 'No recent news found.' ? `<div style="margin-bottom:14px"><div class="pain-label">Recent News</div><div style="font-size:13px;color:var(--accent3)">${escHtml(lead.recent_news)}</div></div>` : ''}
    ${painPoints ? `<div style="margin-bottom:14px"><div class="pain-label">Pain Points</div><ul style="font-size:13px;padding-left:18px;display:flex;flex-direction:column;gap:4px">${painPoints}</ul></div>` : ''}
    ${opportunities ? `<div style="margin-bottom:14px"><div class="pain-label">Opportunities for Shaheer</div><ul style="font-size:13px;padding-left:18px;color:var(--green);display:flex;flex-direction:column;gap:4px">${opportunities}</ul></div>` : ''}
    ${techStack ? `<div style="margin-bottom:14px"><div class="pain-label">Tech Stack</div><div style="font-size:13px">${escHtml(techStack)}</div></div>` : ''}
    <div class="lead-links" style="margin-bottom:16px">
      ${lead.linkedin_url ? `<a class="link-btn link-linkedin" href="${escHtml(lead.linkedin_url)}" target="_blank">💼 LinkedIn</a>` : ''}
      ${lead.email ? `<span class="link-btn link-email" onclick="copyText('${escHtml(lead.email)}',this)">📧 ${escHtml(lead.email)}</span>` : ''}
      ${lead.company_website ? `<a class="link-btn link-web" href="${escHtml(lead.company_website)}" target="_blank">🌐 Website</a>` : ''}
    </div>
    ${lead.message && lead.message.body ? `<button class="view-msg-btn" style="width:100%;padding:12px" onclick="closeModal();openMessageModal(findLead('${lead.id}'))">✉ View Outreach Message</button>` : ''}
  `;
  mo.style.display = 'flex';
}

function openMessageModal(lead) {
  if (!lead || !lead.message) return;
  const msg = lead.message;
  const ch = msg.channel || 'email';
  const chClass = {'email':'ch-email','linkedin':'ch-linkedin','reddit':'ch-reddit'}[ch] || 'ch-email';

  const mc = document.getElementById('modalContent');
  const mo = document.getElementById('modalOverlay');

  mc.innerHTML = `
    <div style="margin-bottom:20px">
      <div style="font-size:18px;font-weight:800;margin-bottom:4px">Message to ${escHtml(lead.name)}</div>
      <div style="display:flex;gap:8px;align-items:center">
        <span class="channel-badge ${chClass}">${ch}</span>
        <span style="font-size:12px;color:var(--text-dim)">${escHtml(lead.title)} · ${escHtml(lead.company)}</span>
      </div>
    </div>
    <div style="display:flex;gap:12px;margin-bottom:16px">
      <div style="flex:1;background:var(--bg2);border-radius:10px;padding:12px;text-align:center;border:1px solid var(--card-border)">
        <div style="font-size:20px;font-weight:800;color:var(--accent)">${Math.round((msg.personalization_score||0)*100)}%</div>
        <div style="font-size:11px;color:var(--text-dim)">Personalization</div>
      </div>
      <div style="flex:1;background:var(--bg2);border-radius:10px;padding:12px;text-align:center;border:1px solid var(--card-border)">
        <div style="font-size:20px;font-weight:800;color:var(--accent2)">${Math.round((msg.tone_score||0)*100)}%</div>
        <div style="font-size:11px;color:var(--text-dim)">Human Tone</div>
      </div>
      <div style="flex:1;background:var(--bg2);border-radius:10px;padding:12px;text-align:center;border:1px solid var(--card-border)">
        <div style="font-size:20px;font-weight:800;color:var(--green)">${lead.score || 0}</div>
        <div style="font-size:11px;color:var(--text-dim)">Lead Score</div>
      </div>
    </div>
    ${msg.subject ? `<div class="message-subject" style="margin-bottom:12px">📌 ${escHtml(msg.subject)}</div>` : ''}
    <div class="message-body">${escHtml(msg.body || '')}</div>
    <div style="display:flex;gap:10px;margin-top:16px">
      <button class="copy-btn" style="flex:1" onclick="copyText(${JSON.stringify((msg.subject?'Subject: '+msg.subject+'\n\n':'')+(msg.body||''))}, this)">📋 Copy Message</button>
      ${lead.email ? `<button class="copy-btn" style="flex:1" onclick="copyText('${escHtml(lead.email)}',this)">📧 Copy Email</button>` : ''}
      ${lead.linkedin_url ? `<a class="link-btn link-linkedin" style="flex:1;justify-content:center" href="${escHtml(lead.linkedin_url)}" target="_blank">💼 Open LinkedIn</a>` : ''}
    </div>
  `;
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
  const ph = feed.querySelector('.log-placeholder');
  if (ph) ph.remove();

  const agentKey = (ev.agent || '').toLowerCase().replace('agent','').trim();
  const tagClass = `tag-${agentKey.split(/[^a-z]/)[0] || 'pipeline'}`;
  const lineClass = `log-${ev.status || 'info'}`;
  const time = new Date().toLocaleTimeString('en-US', {hour12:false, hour:'2-digit', minute:'2-digit', second:'2-digit'});

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
function onPipelineDone() {
  document.getElementById('progressOverlay').style.display = 'none';
  const btn = document.getElementById('startBtn');
  btn.disabled = false;
  document.getElementById('startBtnText').textContent = 'Run New Campaign';
  if (eventSource) { eventSource.close(); eventSource = null; }
  document.getElementById('logDot').classList.remove('pulsing');
}

// ── Helpers ───────────────────────────────────────────────────────────────
function updateStats(d, q, r, m) {
  document.getElementById('statDiscovered').textContent = d;
  document.getElementById('statQualified').textContent = q;
  document.getElementById('statResearched').textContent = r;
  document.getElementById('statMessages').textContent = m;
}
function updateStat(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}
function escHtml(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = '✅ Copied!';
      btn.classList.add('copied');
      setTimeout(() => { btn.textContent = orig; btn.classList.remove('copied'); }, 2000);
    }
  });
}
function showError(msg) {
  appendLog({ agent: 'System', message: 'ERROR: ' + msg, status: 'error' });
  document.getElementById('progressTitle').textContent = 'Error occurred';
  document.getElementById('progressAgent').textContent = msg;
}

// ── Export CSV ────────────────────────────────────────────────────────────
function exportResults() {
  if (!allLeads.length) return;
  const headers = ['Name','Title','Company','Industry','Company Size','Location','Email','LinkedIn URL','Website','Score','Recommended Service','Best Channel','Pain Points','Company Summary','Message Subject','Message Body'];
  const rows = allLeads.map(l => [
    l.name, l.title, l.company, l.industry, l.company_size, l.location,
    l.email, l.linkedin_url, l.company_website, l.score, l.recommended_service,
    l.best_channel, (l.pain_points||[]).join('; '), l.company_summary,
    l.message?.subject || '', l.message?.body || ''
  ].map(v => `"${String(v||'').replace(/"/g,'""')}"`));

  const csv = [headers.join(','), ...rows.map(r=>r.join(','))].join('\n');
  const blob = new Blob([csv], {type:'text/csv'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `outreach_leads_${currentRunId || 'export'}.csv`;
  a.click(); URL.revokeObjectURL(url);
}
