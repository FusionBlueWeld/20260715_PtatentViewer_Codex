const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  environment: 'normal', researches: [], dashboard: null, visible: [], selected: null,
  cellFilter: null, view: 'threat', pipelineJob: null, pipelineReady: false, pipelineRunnable: false, pipelinePending: 0, techClusterMinSize: 1
};

const statusLabel = { published: '公開', registered: '登録', unknown: '未設定' };
const displayStatus = patent => patent.source_status || statusLabel[patent.status] || '未設定';
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
  const thresholdControl=$('#technology-cluster-min-size');const minimum=Math.max(1,Number(thresholdControl?.value||state.techClusterMinSize||1));state.techClusterMinSize=minimum;
  const counts=values=>values.reduce((map,value)=>(map.set(value,(map.get(value)||0)+1),map),new Map());
  const techCounts=counts(ready.map(p=>p.tech_cluster)),problemCounts=counts(ready.map(p=>p.problem_cluster));
  const allTechs=[...techCounts.keys()],allProblems=[...problemCounts.keys()];
  const techs=allTechs.filter(name=>techCounts.get(name)>=minimum),problems=allProblems.filter(name=>problemCounts.get(name)>=minimum);
  const eligible=ready.filter(p=>techCounts.get(p.tech_cluster)>=minimum&&problemCounts.get(p.problem_cluster)>=minimum);
  const map = $('#technology-map');map.innerHTML='';
  if(!techs.length||!problems.length){map.style.setProperty('--tech-cols',1);map.style.setProperty('--tech-rows',1);map.innerHTML=`<div class="map-empty"><strong>表示対象のクラスタがありません</strong><small>最小件数を下げてください</small></div>`;$('#cluster-count').textContent=`0 clusters / min ${minimum}`;$('#technology-map-note').textContent=`${minimum}件未満を非表示。技術 ${allTechs.length}・課題 ${allProblems.length}クラスタの計算結果は保持しています。`;return;}
  map.style.setProperty('--tech-cols',techs.length);map.style.setProperty('--tech-rows',problems.length);
  const buckets = new Map(); eligible.forEach(p => { const k=`${p.tech_cluster}|${p.problem_cluster}`; if(!buckets.has(k))buckets.set(k,[]); buckets.get(k).push(p); });
  const max = Math.max(1,...[...buckets.values()].map(v=>v.length));
  problems.forEach(problem => techs.forEach(tech => {
    const patents = buckets.get(`${tech}|${problem}`) || []; const button=document.createElement('button'); button.className='map-cell';button.dataset.count=patents.length;
    button.dataset.agentId=`technology-cell-${slug(tech)}-${slug(problem)}`;button.style.background=density(patents.length,max);
    button.setAttribute('aria-label',`${tech}、${problem}、${patents.length}件`);button.title=`技術: ${tech}\n課題: ${problem}`;
    button.innerHTML=`<strong>${patents.length}</strong><small>${escapeHtml(tech)}</small>`;
    if(state.cellFilter?.type==='technology'&&state.cellFilter.tech===tech&&state.cellFilter.problem===problem)button.classList.add('selected');
    button.addEventListener('click',()=>selectCell({type:'technology',tech,problem,patents,label:`技術：${tech} / 課題：${problem}`}));map.append(button);
  }));
  const hiddenTechs=allTechs.length-techs.length,hiddenProblems=allProblems.length-problems.length;
  $('#cluster-count').textContent=`${techs.length} × ${problems.length} / min ${minimum}`;
  $('#technology-map-note').textContent=`${minimum}件未満を非表示（技術 ${hiddenTechs}・課題 ${hiddenProblems}クラスタ）。計算結果と文献一覧は保持しています。`;
}

function selectCell(filter) { state.cellFilter = filter; $('#clear-cell').classList.remove('hidden'); $('#active-filter').classList.remove('hidden'); $('#active-filter').textContent=filter.label; renderThreatMap();renderTechnologyMap();renderPatentList(); }
function clearCell() { state.cellFilter=null;$('#clear-cell').classList.add('hidden');$('#active-filter').classList.add('hidden');renderThreatMap();renderTechnologyMap();renderPatentList(); }

function renderPatentList() {
  const patents = state.cellFilter ? state.cellFilter.patents : state.visible; const list=$('#patent-list');list.innerHTML='';
  if(!patents.length){list.innerHTML='<div class="empty-state"><strong>該当文献はありません</strong><p>条件またはセル選択を変更してください。</p></div>';return;}
  patents.forEach(p=>{const button=document.createElement('button');button.className='patent-row';if(state.selected?.id===p.id)button.classList.add('selected');button.dataset.agentId=`patent-open-${p.id}`;
    button.innerHTML=`<span class="doc-icon">PDF</span><span><strong>${escapeHtml(p.title)}</strong><span>${escapeHtml(p.publication_number)} · ${escapeHtml(p.applicant)}</span></span><span class="row-badges"><span class="badge">${escapeHtml(displayStatus(p))}</span><span class="badge ${p.analysis_state}">${p.analysis_state}</span></span>`;
    button.addEventListener('click',()=>selectPatent(p));list.append(button);});
}

function selectPatent(patent) {
  state.selected=patent;renderPatentList();$('#interpretation-empty').classList.add('hidden');$('#interpretation-content').classList.remove('hidden');$('#preview-selected').disabled=!patent.pdf_available;
  $('#selected-status').textContent=displayStatus(patent);$('#selected-title').textContent=patent.title;$('#selected-number').textContent=`${patent.publication_number} · ${patent.applicant}`;
  const pendingMessage=patent.analysis_state==='skipped'?`スキップ: ${patent.skip_reason||'理由未設定'}${patent.skip_detail?` / ${patent.skip_detail}`:''}`:'ローカルLLM分析待ちです。';
  $('#selected-similarity').textContent=analysisValue(patent,'similarity')??'待機';$('#selected-concept').textContent=analysisValue(patent,'concept_level')??'待機';
  $('#selected-tech-summary').textContent=analysisValue(patent,'tech_summary')||pendingMessage;$('#selected-problem-summary').textContent=analysisValue(patent,'problem_summary')||pendingMessage;
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

function renderPipeline(overview,preflight){
  state.pipelineReady=!!preflight.ready;state.pipelineRunnable=(preflight.checks||[]).filter(check=>check.id!=='claim-structure').every(check=>check.ok);const c=overview.counts;state.pipelinePending=Number(c.pending||0);
  const preflightLabel=state.pipelineReady?'READY':(state.pipelineRunnable?'前処理待ち':'BLOCKED');
  $('#pipeline-policy').textContent=`${overview.environment.toUpperCase()} / ${overview.research_id} / 抽出: ${overview.source_policy.extraction||'verify_original'} / 読取: ${overview.source_policy.reading||'adaptive'} / preflight: ${preflightLabel}`;
  $('#pipeline-counts').innerHTML=`<span class="pending"><b>${state.pipelinePending}</b>未確定</span><span><b>${c.total}</b>CSV対象</span><span><b>${c.llm_pending??state.pipelinePending}</b>LLM待ち</span><span><b>${c.finalized}</b>JSON済み</span><span><b>${c.skipped||0}</b>スキップ</span><span><b>${c.failed||0}</b>前回失敗</span>`;
  $('#pipeline-execute').textContent=state.pipelinePending?`未処理 ${state.pipelinePending}件の夜間一括分析を開始`:'すべて処理済み';
  $('#pipeline-execute').disabled=!state.pipelineRunnable||!state.pipelinePending||!$('#pipeline-confirm').checked;
  const active=overview.jobs.find(job=>['running','paused','cancelling'].includes(job.status));const latest=active||overview.jobs[0];
  if(latest){state.pipelineJob=latest;renderPipelineJob(latest);}else{state.pipelineJob=null;$('#pipeline-job').classList.add('hidden');renderPipelineStages({});}
}

async function showPipeline(){
  if(!state.dashboard)return;const dialog=$('#pipeline-dialog');if(!dialog.open)dialog.showModal();$('#pipeline-policy').textContent='状態を確認しています…';
  const id=encodeURIComponent(state.dashboard.research.id);const [overview,preflight]=await Promise.all([api(`/api/researches/${id}/pipeline?environment=${state.environment}`),api(`/api/researches/${id}/llm-preflight?environment=${state.environment}`)]);const previous=state.pipelineJob?.id;renderPipeline(overview,preflight);const active=overview.jobs.find(job=>['running','paused','cancelling'].includes(job.status));if(active&&active.id!==previous)monitorPipelineJob(active.id,false);
}

function renderPipelineJob(job){
  const jobPanel=$('#pipeline-job');jobPanel.classList.remove('hidden');jobPanel.dataset.status=job.status;
  const progress=job.progress||{};const detail=progress.detail||{};const total=Number(detail.total||0);const completed=Number(detail.completed??(progress.stage==='completed'?total:Math.max(0,Number(detail.current||1)-1)));const percent=Number(detail.percent??(total?completed/total*100:0));
  const statusLabels={running:'実行中',paused:'一時停止',cancelling:'停止処理中',completed:'完了',failed:'バッチ停止',cancelled:'キャンセル'};$('#pipeline-job-state').textContent=statusLabels[job.status]||job.status.toUpperCase();
  const taskLabels={similarity:'① 類似度評価',concept_level:'② 概念レベル評価',problem_summary:'③ 課題要約',technology_summary:'④ 技術要約',embeddings:'⑤ Embedding',cluster_name:'クラスタ名生成'};
  const stageLabels={initializing:'初期化',preparing:'前処理',analyzing:'LLM分析開始',llm_batch:taskLabels[detail.task]||'LLMバッチ処理',llm_task:taskLabels[detail.task]||'LLM処理',document_completed:'1件完了',document_failed:'1件失敗・次の文献へ継続',skipped:'スキップ・次の文献へ継続',cooldown:'冷却中',checkpoint_reused:'分析済み再利用',clustering:'クラスタリング',cluster_naming:'クラスタ名生成',completed:'バッチ完了',failed:'バッチ停止'};const stageLabel=stageLabels[progress.stage]||progress.stage||'待機';
  $('#pipeline-job-progress').textContent=[stageLabel,detail.patent_id,total?`${completed}/${total}`:''].filter(Boolean).join(' · ');
  const bar=$('#pipeline-progress-bar'),track=bar.parentElement;bar.style.width=`${Math.max(0,Math.min(100,percent))}%`;track.setAttribute('aria-valuenow',String(Math.round(percent)));
  const cooldown=Number(detail.remaining_seconds||0);const attempt=Number(detail.attempt||0);const attemptText=attempt?` · 試行 ${attempt}/${detail.max_attempts||1}`:'';$('#pipeline-progress-label').textContent=cooldown?`GPU冷却中 · 残り ${Math.floor(cooldown/60)}:${String(cooldown%60).padStart(2,'0')} · 処理済み ${completed}/${total}`:total?`${stageLabel}${attemptText} · 処理済み ${completed}/${total}（${percent.toFixed(1)}%）`:`${stageLabel}${attemptText}`;
  const processed=Number(detail.processed??completed);const succeeded=Number(detail.succeeded??detail.llm_completed??Math.max(0,processed-Number(detail.failed||0)-Number(detail.skipped||0)));const failed=Number(detail.failed||0);const skipped=Number(detail.skipped||0);const remaining=Math.max(0,total-processed);
  $('#pipeline-live-counts').innerHTML=`<span><b>${processed}</b>処理済み</span><span><b>${succeeded}</b>成功</span><span class="failed"><b>${failed}</b>失敗</span><span><b>${skipped}</b>スキップ</span><span><b>${remaining}</b>残り</span>`;
  renderPipelineStages(progress);
  const errorText=job.error||detail.error||'';$('#pipeline-job-detail').textContent=errorText?`最終エラー: ${errorText}\n\n${JSON.stringify(progress,null,2)}`:JSON.stringify(progress,null,2);const terminal=['completed','failed','cancelled'].includes(job.status);
  $('#pipeline-pause').disabled=terminal||job.status==='paused';$('#pipeline-resume').disabled=terminal||job.status==='running';$('#pipeline-cancel').disabled=terminal;
}

function renderPipelineStages(progress){
  const stage=progress.stage||'';const detail=progress.detail||{};
  const phases=[['前処理',['initializing','preparing']],['文献分析',['analyzing','llm_batch','llm_task','document_completed','document_failed','skipped','cooldown','checkpoint_reused']],['クラスタリング',['clustering','cluster_naming']],['結果確定',['completed']]];
  const effectiveStage=stage==='failed'?(detail.failed_at_stage||stage):stage;const activeIndex=phases.findIndex(([,values])=>values.includes(effectiveStage));
  $('#pipeline-stages').innerHTML=phases.map(([label],index)=>`<span class="${stage==='completed'||index<activeIndex?'done':index===activeIndex?(stage==='failed'?'failed':'active'):''}">${index+1}. ${label}</span>`).join('');
  const tasks=[['similarity','類似度'],['concept_level','概念レベル'],['problem_summary','課題要約'],['technology_summary','技術要約'],['embeddings','Embedding']];const taskIndex=Number(detail.task_index||0);const documentDone=['document_completed','cooldown'].includes(stage);const documentFailed=stage==='document_failed';
  $('#pipeline-document-stages').innerHTML=tasks.map(([key,label],index)=>{const number=index+1;let cls=documentDone?'done':taskIndex>number?'done':taskIndex===number?'active':'';if(documentFailed&&detail.task===key)cls='failed';return `<span class="${cls}">${number}. ${label}</span>`;}).join('');
}

async function controlPipeline(control){if(!state.pipelineJob)return;state.pipelineJob=await api(`/api/pipeline/jobs/${state.pipelineJob.id}/control`,{method:'POST',body:JSON.stringify({control})});renderPipelineJob(state.pipelineJob);}

async function monitorPipelineJob(jobId,bridgeOwned){
  let lastControl='run';
  while(true){
    const job=await api(`/api/pipeline/jobs/${jobId}`);state.pipelineJob=job;renderPipelineJob(job);
    if(['completed','failed','cancelled'].includes(job.status)){
      await loadDashboard(state.dashboard.research.id);await showPipeline();
      if(bridgeOwned&&job.status==='cancelled')throw new DOMException('pipeline job cancelled','AbortError');
      if(job.status==='failed'){if(bridgeOwned)throw new Error(job.error||'pipeline job failed');toast('バッチが停止しました。画面に最終件数とエラーを保持しています。');}
      return job;
    }
    if(bridgeOwned&&window.PatentViewerBridge?.currentControl){const control=await window.PatentViewerBridge.currentControl();if(control!==lastControl){await controlPipeline(control);lastControl=control;}}
    await new Promise(resolve=>setTimeout(resolve,750));
  }
}

async function runPipelineMode(mode){
  const source=window.PatentViewerBridge?.currentCommandId?'codex':'human';const job=await api(`/api/researches/${encodeURIComponent(state.dashboard.research.id)}/pipeline/jobs`,{method:'POST',headers:window.PatentViewerBridge?.authorizationHeaders?.()||{},body:JSON.stringify({environment:state.environment,mode,source,confirmation:mode==='execute'?'RUN_LOCAL_LLM':undefined,cooldown_seconds:mode==='execute'?30:0})});state.pipelineJob=job;renderPipelineJob(job);return monitorPipelineJob(job.id,source==='codex');
}

function startPipeline(mode,button){
  button.patentViewerActionPromise=(async()=>{button.disabled=true;try{return await runPipelineMode(mode);}finally{button.disabled=false;}})();
  return button.patentViewerActionPromise;
}

function processPending(button){
  button.patentViewerActionPromise=(async()=>{button.disabled=true;try{
    await runPipelineMode('prepare');
    if(!state.pipelineReady)throw new Error('前処理後もpreflightを通過できませんでした。スキップ理由を確認してください。');
    return await runPipelineMode('execute');
  }finally{button.disabled=!state.pipelineRunnable||!state.pipelinePending||!$('#pipeline-confirm').checked;}})();
  return button.patentViewerActionPromise;
}

function openPipelineFrom(button){button.patentViewerActionPromise=showPipeline();return button.patentViewerActionPromise;}

function resetFilters(){ $('#subresearch-select').value='all';$('#year-select').value='all';$('#search-input').value='';$('#technology-cluster-min-size').value='1';state.techClusterMinSize=1;$$('fieldset input').forEach(x=>x.checked=true);clearCell();applyFilters(); }
function setView(view){
  if(!['threat','technology'].includes(view))return;
  if(state.cellFilter&&state.cellFilter.type!==view)clearCell();
  state.view=view;$$('.tab').forEach(t=>{const active=t.dataset.view===view;t.classList.toggle('active',active);t.setAttribute('aria-pressed',String(active));});
  $('#threat-panel').classList.toggle('hidden',view!=='threat');$('#technology-panel').classList.toggle('hidden',view!=='technology');
}
function slug(value){return String(value).normalize('NFKC').replace(/[^A-Za-z0-9_-]+/g,'-').replace(/^-|-$/g,'').slice(0,40)||'none';}
function escapeHtml(value){const d=document.createElement('div');d.textContent=String(value??'');return d.innerHTML;}

function bindEvents(){
  $('#research-select').addEventListener('change',e=>loadDashboard(e.target.value));$('#subresearch-select').addEventListener('change',applyFilters);$('#year-select').addEventListener('change',applyFilters);$('#search-input').addEventListener('input',applyFilters);$$('fieldset input').forEach(x=>x.addEventListener('change',applyFilters));
  $('#reset-filters').addEventListener('click',resetFilters);$('#clear-cell').addEventListener('click',clearCell);$('#preview-selected').addEventListener('click',()=>{if(state.selected?.pdf_available)openPdf(state.selected);});$('#pdf-close').addEventListener('click',()=>{$('#pdf-frame').src='about:blank';$('#pdf-dialog').close();});$('#save-note').addEventListener('click',saveNote);$('#debug-toggle').addEventListener('click',toggleEnvironment);$('#preflight-button').addEventListener('click',showPreflight);
  $('#technology-cluster-min-size').addEventListener('change',event=>{state.techClusterMinSize=Math.max(1,Number(event.target.value)||1);if(state.cellFilter?.type==='technology')clearCell();else renderTechnologyMap();});
  $('#pipeline-button').addEventListener('click',()=>openPipelineFrom($('#pipeline-button')));$('#pipeline-refresh').addEventListener('click',()=>openPipelineFrom($('#pipeline-refresh')));$('#pipeline-confirm').addEventListener('change',()=>{$('#pipeline-execute').disabled=!state.pipelineRunnable||!state.pipelinePending||!$('#pipeline-confirm').checked;});$('#pipeline-prepare').addEventListener('click',()=>startPipeline('prepare',$('#pipeline-prepare')));$('#pipeline-execute').addEventListener('click',()=>processPending($('#pipeline-execute')));$('#pipeline-pause').addEventListener('click',()=>controlPipeline('pause'));$('#pipeline-resume').addEventListener('click',()=>controlPipeline('run'));$('#pipeline-cancel').addEventListener('click',()=>controlPipeline('cancel'));
  $$('[data-close-dialog]').forEach(b=>b.addEventListener('click',()=>document.getElementById(b.dataset.closeDialog).close()));$$('.tab').forEach(t=>t.addEventListener('click',()=>setView(t.dataset.view)));
  $('#bridge-open').addEventListener('click',()=>$('#bridge-panel').classList.add('open'));$('#bridge-close').addEventListener('click',()=>$('#bridge-panel').classList.remove('open'));
  setView(state.view);
}

window.PatentViewer={state,api,applyFilters,openPdf,toast};
bootstrap().catch(error=>{console.error(error);toast(error.message);$('#research-description').textContent=`読み込みエラー: ${error.message}`;});
