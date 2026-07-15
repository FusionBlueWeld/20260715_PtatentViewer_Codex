const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  environment: 'normal', researches: [], dashboard: null, visible: [], selected: null,
  cellFilter: null, view: 'overview'
};

const statusLabel = { published: '公開', registered: '登録', unknown: '未設定' };
const analysisValue = (patent, key) => patent.analysis_state === 'ready' ? patent[key] : null;

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data;
}

function toast(message) {
  const element = $('#toast'); element.textContent = message; element.classList.add('show');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => element.classList.remove('show'), 2400);
}

async function bootstrap() {
  const env = await api('/api/environment');
  state.environment = env.environment;
  renderEnvironment();
  await loadResearches();
  bindEvents();
}

async function loadResearches(preferred) {
  const data = await api(`/api/researches?environment=${state.environment}`);
  state.researches = data.items;
  const select = $('#research-select'); select.innerHTML = '';
  data.items.forEach(item => select.add(new Option(item.name, item.id)));
  if (!data.items.length) { $('#research-title').textContent = 'リサーチがありません'; return; }
  select.value = preferred && data.items.some(item => item.id === preferred) ? preferred : data.items[0].id;
  await loadDashboard(select.value);
}

async function loadDashboard(researchId) {
  state.dashboard = await api(`/api/researches/${encodeURIComponent(researchId)}/dashboard?environment=${state.environment}`);
  state.selected = null; state.cellFilter = null;
  $('#research-title').textContent = state.dashboard.research.name;
  $('#research-description').textContent = state.dashboard.research.description;
  $('#pool-count').textContent = state.dashboard.pool_count;
  rebuildSubresearch(); rebuildYears(); applyFilters(); renderInterpretation();
}

function rebuildSubresearch() {
  const select = $('#subresearch-select'); select.innerHTML = '<option value="all">すべてのサブリサーチ</option>';
  const unique = new Map(state.dashboard.patents.map(p => [p.subresearch_id, p.subresearch_name]));
  unique.forEach((name, id) => select.add(new Option(name, id)));
}

function rebuildYears() {
  const select = $('#year-select'); select.innerHTML = '<option value="all">すべて</option>';
  [...new Set(state.dashboard.patents.map(p => p.year).filter(Boolean))].sort((a,b) => b-a).forEach(year => select.add(new Option(`${year}年`, year)));
}

function filters() {
  return {
    sub: $('#subresearch-select').value,
    year: $('#year-select').value,
    query: $('#search-input').value.trim().toLowerCase(),
    statuses: new Set($$('fieldset input:checked').map(el => el.value)),
  };
}

function applyFilters() {
  if (!state.dashboard) return;
  const f = filters();
  state.visible = state.dashboard.patents.filter(p => {
    const haystack = [p.publication_number,p.title,p.applicant,p.category,...p.tags].join(' ').toLowerCase();
    return (f.sub === 'all' || p.subresearch_id === f.sub)
      && (f.year === 'all' || String(p.year) === f.year)
      && f.statuses.has(p.status)
      && (!f.query || haystack.includes(f.query));
  });
  renderMetrics(); renderThreatMap(); renderTechnologyMap(); renderPatentList();
}

function renderMetrics() {
  const total = state.dashboard.patents.length, visible = state.visible.length;
  const ready = state.visible.filter(p => p.analysis_state === 'ready');
  const registered = state.visible.filter(p => p.status === 'registered').length;
  const threat = ready.filter(p => Number(p.similarity) >= 4 && Number(p.concept_level) >= 4).length;
  $('#metric-visible').textContent = visible; $('#metric-total').textContent = `/ ${total} total`;
  $('#metric-ready').textContent = ready.length; $('#metric-registered').textContent = visible ? `${Math.round(registered/visible*100)}%` : '0%';
  $('#registered-bar').style.width = visible ? `${registered/visible*100}%` : '0%'; $('#metric-threat').textContent = threat;
}

function density(count, max) {
  const alpha = count ? .18 + .65 * count / Math.max(1,max) : .04;
  return `rgba(48,199,228,${alpha})`;
}

function renderThreatMap() {
  const cells = new Map();
  state.visible.filter(p => p.analysis_state === 'ready').forEach(p => {
    const x = Number(p.similarity), y = Number(p.concept_level);
    if (x >= 1 && x <= 5 && y >= 1 && y <= 5) {
      const key = `${x},${y}`; if (!cells.has(key)) cells.set(key, []); cells.get(key).push(p);
    }
  });
  const max = Math.max(1,...[...cells.values()].map(v=>v.length));
  const map = $('#threat-map'); map.innerHTML = '';
  for (let y=5; y>=1; y--) for (let x=1; x<=5; x++) {
    const patents = cells.get(`${x},${y}`) || [];
    const button = document.createElement('button'); button.className = 'map-cell'; button.dataset.count = patents.length;
    button.dataset.agentId = `threat-cell-${x}-${y}`; button.style.background = density(patents.length,max);
    button.setAttribute('aria-label', `類似度${x}、概念高さ${y}、${patents.length}件`);
    button.innerHTML = `<strong>${patents.length}</strong><small>${x} × ${y}</small>`;
    if (state.cellFilter?.type === 'threat' && state.cellFilter.x === x && state.cellFilter.y === y) button.classList.add('selected');
    button.addEventListener('click', () => selectCell({ type:'threat', x, y, patents, label:`脅威マップ：類似度 ${x} / 概念高さ ${y}` }));
    map.append(button);
  }
}

function renderTechnologyMap() {
  const ready = state.visible.filter(p => p.analysis_state === 'ready' && p.tech_cluster && p.problem_cluster);
  const techs = [...new Set(ready.map(p=>p.tech_cluster))]; const problems = [...new Set(ready.map(p=>p.problem_cluster))];
  const fallbackTechs = techs.length ? techs : ['分析待ち']; const fallbackProblems = problems.length ? problems : ['分析待ち'];
  const map = $('#technology-map'); map.style.setProperty('--tech-cols', fallbackTechs.length); map.style.setProperty('--tech-rows', fallbackProblems.length); map.innerHTML='';
  const buckets = new Map(); ready.forEach(p => { const k=`${p.tech_cluster}|${p.problem_cluster}`; if(!buckets.has(k))buckets.set(k,[]); buckets.get(k).push(p); });
  const max = Math.max(1,...[...buckets.values()].map(v=>v.length));
  fallbackProblems.forEach(problem => fallbackTechs.forEach(tech => {
    const patents = buckets.get(`${tech}|${problem}`) || []; const button=document.createElement('button'); button.className='map-cell';button.dataset.count=patents.length;
    button.dataset.agentId=`technology-cell-${slug(tech)}-${slug(problem)}`;button.style.background=density(patents.length,max);
    button.setAttribute('aria-label',`${tech}、${problem}、${patents.length}件`);button.title=`技術: ${tech}\n課題: ${problem}`;
    button.innerHTML=`<strong>${patents.length}</strong><small>${escapeHtml(tech)}</small>`;
    if(state.cellFilter?.type==='technology'&&state.cellFilter.tech===tech&&state.cellFilter.problem===problem)button.classList.add('selected');
    button.addEventListener('click',()=>selectCell({type:'technology',tech,problem,patents,label:`技術：${tech} / 課題：${problem}`}));map.append(button);
  }));
  $('#cluster-count').textContent=`${techs.length} × ${problems.length} clusters`;
}

function selectCell(filter) { state.cellFilter = filter; $('#clear-cell').classList.remove('hidden'); $('#active-filter').classList.remove('hidden'); $('#active-filter').textContent=filter.label; renderThreatMap();renderTechnologyMap();renderPatentList(); }
function clearCell() { state.cellFilter=null;$('#clear-cell').classList.add('hidden');$('#active-filter').classList.add('hidden');renderThreatMap();renderTechnologyMap();renderPatentList(); }

function renderPatentList() {
  const patents = state.cellFilter ? state.cellFilter.patents : state.visible; const list=$('#patent-list');list.innerHTML='';
  if(!patents.length){list.innerHTML='<div class="empty-state"><strong>該当文献はありません</strong><p>条件またはセル選択を変更してください。</p></div>';return;}
  patents.forEach(p=>{const button=document.createElement('button');button.className='patent-row';if(state.selected?.id===p.id)button.classList.add('selected');button.dataset.agentId=`patent-open-${p.id}`;
    button.innerHTML=`<span class="doc-icon">PDF</span><span><strong>${escapeHtml(p.title)}</strong><span>${escapeHtml(p.publication_number)} · ${escapeHtml(p.applicant)}</span></span><span class="row-badges"><span class="badge">${statusLabel[p.status]}</span><span class="badge ${p.analysis_state}">${p.analysis_state}</span></span>`;
    button.addEventListener('click',()=>selectPatent(p));list.append(button);});
}

function selectPatent(patent) {
  state.selected=patent;renderPatentList();$('#interpretation-empty').classList.add('hidden');$('#interpretation-content').classList.remove('hidden');
  $('#selected-status').textContent=statusLabel[patent.status]||'未設定';$('#selected-title').textContent=patent.title;$('#selected-number').textContent=`${patent.publication_number} · ${patent.applicant}`;
  $('#selected-similarity').textContent=analysisValue(patent,'similarity')??'待機';$('#selected-concept').textContent=analysisValue(patent,'concept_level')??'待機';
  $('#selected-tech-summary').textContent=analysisValue(patent,'tech_summary')||'ローカルLLM分析待ちです。';$('#selected-problem-summary').textContent=analysisValue(patent,'problem_summary')||'ローカルLLM分析待ちです。';
  $('#selected-reasoning').textContent=analysisValue(patent,'reasoning')||'スコア根拠は分析JSON生成後に表示されます。';$('#interpretation-note').value='';$('#save-feedback').textContent='';
}

function renderInterpretation(){if(!state.selected){$('#interpretation-empty').classList.remove('hidden');$('#interpretation-content').classList.add('hidden');$('#selected-status').textContent='文献未選択';}}
function openPdf(patent){if(!patent)return toast('文献を選択してください');const url=`/api/pdfs/${encodeURIComponent(patent.pdf)}`;$('#pdf-title').textContent=patent.publication_number;$('#pdf-frame').src=url;$('#pdf-new-tab').href=url;$('#pdf-dialog').showModal();}

async function saveNote() {
  if(!state.selected)return; const button=$('#save-note');
  button.patentViewerActionPromise=(async()=>{button.disabled=true;try{const result=await api('/api/interpretations',{method:'POST',body:JSON.stringify({environment:state.environment,research_id:state.dashboard.research.id,patent_id:state.selected.id,note:$('#interpretation-note').value,source:window.PatentViewerBridge?.currentCommandId?'codex':'human'}),headers:window.PatentViewerBridge?.authorizationHeaders?.()||{}});$('#save-feedback').textContent=`保存しました: ${result.path}`;}catch(error){$('#save-feedback').textContent=error.message;throw error;}finally{button.disabled=false;}})();
  return button.patentViewerActionPromise;
}

async function toggleEnvironment(){const next=state.environment==='normal'?'debug':'normal';if(!confirm(`${next.toUpperCase()}環境へ切り替えます。保存先と入力データは分離されています。`))return;const button=$('#debug-toggle');button.patentViewerActionPromise=(async()=>{await api('/api/environment',{method:'POST',body:JSON.stringify({environment:next})});setTimeout(()=>location.reload(),250);})();return button.patentViewerActionPromise;}
function renderEnvironment(){const badge=$('#environment-badge');badge.textContent=state.environment.toUpperCase();badge.classList.toggle('debug',state.environment==='debug');document.body.dataset.environment=state.environment;$('#debug-toggle').setAttribute('aria-label',state.environment==='normal'?'DEBUG環境へ切替':'NORMAL環境へ戻る');}

async function showPreflight(){if(!state.dashboard)return;const dialog=$('#preflight-dialog'),result=$('#preflight-results');result.innerHTML='<p>診断中…</p>';dialog.showModal();const data=await api(`/api/researches/${state.dashboard.research.id}/llm-preflight?environment=${state.environment}`);result.innerHTML=data.checks.map(c=>`<div class="check-item ${c.ok?'':'failed'}"><i>${c.ok?'●':'▲'}</i><div><strong>${escapeHtml(c.id)}</strong><br><small>${escapeHtml(c.detail)}</small></div></div>`).join('')+`<p class="muted">Selection: ${data.selection_fingerprint}</p>`;}

function resetFilters(){ $('#subresearch-select').value='all';$('#year-select').value='all';$('#search-input').value='';$$('fieldset input').forEach(x=>x.checked=true);clearCell();applyFilters(); }
function setView(view){state.view=view;$$('.tab').forEach(t=>t.classList.toggle('active',t.dataset.view===view));$('#threat-panel').classList.toggle('hidden',view==='technology');$('#technology-panel').classList.toggle('hidden',view==='threat');if(view==='overview'){$('#threat-panel').classList.remove('hidden');$('#technology-panel').classList.remove('hidden');}}
function slug(value){return String(value).normalize('NFKC').replace(/[^A-Za-z0-9_-]+/g,'-').replace(/^-|-$/g,'').slice(0,40)||'none';}
function escapeHtml(value){const d=document.createElement('div');d.textContent=String(value??'');return d.innerHTML;}

function bindEvents(){
  $('#research-select').addEventListener('change',e=>loadDashboard(e.target.value));$('#subresearch-select').addEventListener('change',applyFilters);$('#year-select').addEventListener('change',applyFilters);$('#search-input').addEventListener('input',applyFilters);$$('fieldset input').forEach(x=>x.addEventListener('change',applyFilters));
  $('#reset-filters').addEventListener('click',resetFilters);$('#clear-cell').addEventListener('click',clearCell);$('#preview-selected').addEventListener('click',()=>openPdf(state.selected));$('#pdf-close').addEventListener('click',()=>{$('#pdf-frame').src='about:blank';$('#pdf-dialog').close();});$('#save-note').addEventListener('click',saveNote);$('#debug-toggle').addEventListener('click',toggleEnvironment);$('#preflight-button').addEventListener('click',showPreflight);
  $$('[data-close-dialog]').forEach(b=>b.addEventListener('click',()=>document.getElementById(b.dataset.closeDialog).close()));$$('.tab').forEach(t=>t.addEventListener('click',()=>setView(t.dataset.view)));
  $('#bridge-open').addEventListener('click',()=>$('#bridge-panel').classList.add('open'));$('#bridge-close').addEventListener('click',()=>$('#bridge-panel').classList.remove('open'));
}

window.PatentViewer={state,api,applyFilters,openPdf,toast};
bootstrap().catch(error=>{console.error(error);toast(error.message);$('#research-description').textContent=`読み込みエラー: ${error.message}`;});
