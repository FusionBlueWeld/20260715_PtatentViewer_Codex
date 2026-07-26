const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const uiClientId=sessionStorage.getItem('patentViewerClientId')||crypto.randomUUID();sessionStorage.setItem('patentViewerClientId',uiClientId);

const state = {
  environment: 'normal', researches: [], dashboard: null, visible: [], selected: null,
  cellFilter: null, view: 'threat', pipelineJob: null, pipelineReady: false, pipelineRunnable: false, pipelinePending: 0, pipelineAvailable: 0, techClusterMinSize: 1,
  organizationMode: 'organization', selectedOrganization: null, editingOrganizationGroup: null,
  pipelineResearchId: null, managedResearches: [], legalStatusDraft: { rights_acquired: [], under_examination: [] },
  documentPage: { total: 0, offset: 0, limit: 200, hasMore: false, loading: false, request: 0 }, filterTimer: null,
  trendRequest: 0, trendCache: new Map(), cellTrendData: null, cellChartMode: 'year'
};

const statusLabel = { rights_acquired: '権利化', under_examination: '審査中', published: '公開', registered: '権利化', unknown: '公開' };
const legalCategory = p => p.legal_status_category||'published';
const displayStatus = patent => patent.source_status || statusLabel[patent.legal_status_category] || statusLabel[patent.status] || '公開';
const analysisValue = (patent, key) => patent.analysis_state === 'ready' ? patent[key] : null;

async function api(path, options = {}) {
  const request={...options},bridge=window.PatentViewerBridge;
  request.headers={'X-PatentViewer-UI-Client':uiClientId,...(request.headers||{})};
  if(bridge?.currentCommandId&&String(request.method||'GET').toUpperCase()!=='GET'){
    request.headers={...(request.headers||{}),...(bridge.authorizationHeaders?.()||{})};
    if(typeof request.body==='string'){try{const payload=JSON.parse(request.body);if(payload&&typeof payload==='object'&&!Array.isArray(payload)&&!payload.source){payload.source='codex';request.body=JSON.stringify(payload);}}catch{}}
  }
  const response = await fetch(path, { ...request, headers: { 'Content-Type': 'application/json', ...(request.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
  return data;
}

function fileBase64(file){
  return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]||'');reader.onerror=()=>reject(reader.error||new Error('ファイルを読み込めません'));reader.readAsDataURL(file);});
}

async function csvPayload(file){
  if(!file)throw new Error('CSVを選択してください');
  return {csv_filename:file.name,csv_base64:await fileBase64(file)};
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
  if (!data.items.length) { state.dashboard=null;state.visible=[];$('#research-title').textContent = '使用中のリサーチがありません';$('#research-description').textContent='リサーチ管理から新規作成または復元してください。';$('#metric-visible').textContent='0';$('#metric-total').textContent='/ 0 total';$('#metric-ready').textContent='0';$('#metric-registered').textContent='0%';$('#metric-threat').textContent='0';return; }
  select.value = preferred && data.items.some(item => item.id === preferred) ? preferred : data.items[0].id;
  await loadDashboard(select.value);
}

async function loadDashboard(researchId) {
  state.dashboard = await api(`/api/researches/${encodeURIComponent(researchId)}/dashboard?environment=${state.environment}&include_patents=false`);
  state.selected = null; state.cellFilter = null; state.selectedOrganization = null; state.editingOrganizationGroup = null;state.trendCache.clear();state.trendRequest++;state.cellTrendData=null;
  $('#cell-trend-panel').classList.add('hidden');
  $('#research-title').textContent = state.dashboard.research.name;
  $('#research-description').textContent = state.dashboard.research.description;
  $('#pool-count').textContent = state.dashboard.pool_count;
  rebuildYears(); await refreshDocuments(true); renderInterpretation();
}

function rebuildYears() {
  const years = (state.dashboard.aggregates?.years||[]).map(Number).filter(Boolean).sort((a,b) => a-b);
  for (const selector of ['#year-from','#year-to']) {
    const select=$(selector);select.innerHTML='<option value="">指定なし</option>';
    years.forEach(year=>select.add(new Option(`${year}年`,year)));
  }
}

function filters() {
  return {
    yearFrom: Number($('#year-from').value) || null,
    yearTo: Number($('#year-to').value) || null,
    query: $('#search-input').value.trim().toLowerCase(),
    statuses: new Set($$('#legal-status-filters input:checked').map(el => el.value)),
  };
}

function applyFilters() {
  if (!state.dashboard) return Promise.resolve();
  clearTimeout(state.filterTimer);
  return new Promise(resolve=>{state.filterTimer=setTimeout(()=>Promise.all([refreshDocuments(true),state.cellFilter?refreshCellTrend():Promise.resolve()]).finally(resolve),120);});
}

function documentQuery(offset=0){
  const f=filters(),parameters=new URLSearchParams({environment:state.environment,limit:String(state.documentPage.limit),offset:String(offset)});
  if(f.query)parameters.set('q',f.query);
  if(f.yearFrom)parameters.set('year_from',String(f.yearFrom));
  if(f.yearTo)parameters.set('year_to',String(f.yearTo));
  parameters.set('status',f.statuses.size?[...f.statuses].join(','):'__none__');
  if(state.cellFilter?.type==='threat'){parameters.set('similarity',String(state.cellFilter.x));parameters.set('concept_level',String(state.cellFilter.y));}
  if(state.cellFilter?.type==='technology'){parameters.set('tech_cluster_id',String(state.cellFilter.tech));parameters.set('problem_cluster_id',String(state.cellFilter.problem));}
  if(state.selectedOrganization?.memberIds?.length)parameters.set('organization_id',state.selectedOrganization.memberIds.join(','));
  return parameters;
}

function trendQuery(){
  const f=filters(),parameters=new URLSearchParams({environment:state.environment,cell_type:state.cellFilter.type});
  if(f.query)parameters.set('q',f.query);
  parameters.set('status',f.statuses.size?[...f.statuses].join(','):'__none__');
  if(f.yearFrom)parameters.set('selected_year_from',String(f.yearFrom));
  if(f.yearTo)parameters.set('selected_year_to',String(f.yearTo));
  if(state.cellFilter.type==='threat'){parameters.set('similarity',String(state.cellFilter.x));parameters.set('concept_level',String(state.cellFilter.y));}
  if(state.cellFilter.type==='technology'){parameters.set('tech_cluster_id',String(state.cellFilter.tech));parameters.set('problem_cluster_id',String(state.cellFilter.problem));}
  if(state.selectedOrganization?.memberIds?.length)parameters.set('organization_id',state.selectedOrganization.memberIds.join(','));
  return parameters;
}

async function refreshCellTrend(){
  if(!state.dashboard||!state.cellFilter)return;
  const request=++state.trendRequest,researchId=encodeURIComponent(state.dashboard.research.id),query=trendQuery();
  const key=`${researchId}?${query}`;const panel=$('#cell-trend-panel');
  panel.classList.remove('hidden');$('#cell-trend-loading').classList.remove('hidden');$('#cell-trend-content').classList.add('hidden');$('#cell-trend-error').classList.add('hidden');
  try{
    const data=state.trendCache.get(key)||await api(`/api/researches/${researchId}/cell-trend?${query}`);
    state.trendCache.set(key,data);if(request!==state.trendRequest)return;
    if(Number(data.summary?.selected_count||0)===0){panel.classList.add('hidden');state.cellTrendData=data;return;}
    renderCellTrend(data);
  }catch(error){
    if(request!==state.trendRequest)return;
    $('#cell-trend-loading').classList.add('hidden');$('#cell-trend-error').classList.remove('hidden');$('#cell-trend-error').textContent=`年次推移を取得できません: ${error.message}`;
  }
}

const trendStatusKeys=['rights_acquired','under_examination','published'];
const trendStatusLabels={rights_acquired:'権利化',under_examination:'審査中',published:'公開'};
function trendStatusLegend(extra=''){
  return `${trendStatusKeys.map(key=>`<span><i class="${key}"></i>${trendStatusLabels[key]}</span>`).join('')}${extra}`;
}
function trendStatusSegments(counts,fallbackTotal=0){
  const values=trendStatusKeys.map(key=>[key,Number(counts?.[key]||0)]),statusTotal=values.reduce((sum,[,value])=>sum+value,0);
  if(statusTotal===0&&fallbackTotal>0)return `<i class="status-segment unclassified" style="flex:${fallbackTotal}" title="法的状態の内訳未取得 ${fallbackTotal}件"></i>`;
  return values.map(([key,value])=>value?`<i class="status-segment ${key}" style="flex:${value}" title="${trendStatusLabels[key]} ${value}件"></i>`:'').join('');
}

function renderCellTrend(data){
  const selection=state.cellFilter;if(!selection)return;state.cellTrendData=data;
  const summary=data.summary||{},title=selection.label+(state.selectedOrganization&&data.cell_type==='threat'?` / ${state.selectedOrganization.name}`:'');
  $('#cell-trend-title').textContent=title;
  $('#cell-trend-loading').classList.add('hidden');$('#cell-trend-error').classList.add('hidden');$('#cell-trend-content').classList.remove('hidden');$('#cell-trend-panel').classList.remove('hidden');
  renderCellTrendMode();
}

function renderCellTrendMode(){
  const data=state.cellTrendData,selection=state.cellFilter;if(!data||!selection)return;
  $$('[data-trend-mode]').forEach(button=>{const active=button.dataset.trendMode===state.cellChartMode;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});
  if(state.cellChartMode==='organization')renderOrganizationTrend(data);else renderYearTrend(data);
}

function renderYearTrend(data){
  const chart=$('#cell-trend-chart'),series=data.series||[],summary=data.summary||{},maximum=Math.max(1,...series.map(item=>Number(item.count||0)));
  const range=data.selected_range||{},hasRange=range.from!=null||range.to!=null;
  const inRange=year=>(range.from==null||year>=range.from)&&(range.to==null||year<=range.to);
  const directionLabels=data.cell_type==='threat'
    ? {unobserved:'未観測',undated:'年不明',sparse:'散発',emerging:'脅威流入',growing:'脅威増加',stable:'安定',declining:'脅威減少'}
    : {unobserved:'未観測',undated:'年不明',sparse:'散発',emerging:'新興',growing:'成長',stable:'安定',declining:'減速'};
  const applicationYearOnly=data.year_basis==='application_year';
  $('#cell-trend-status').textContent=applicationYearOnly?'出願年基準':'年情報基準（補完年を含む）';
  $('#cell-trend-total-label').textContent='累計件数';$('#cell-trend-peak-label').textContent='ピーク年';$('#cell-trend-recent-label').textContent='直近3年';$('#cell-trend-direction-label').textContent='傾向';
  $('#cell-trend-total').textContent=`${Number(summary.total||0)}件`;
  $('#cell-trend-peak').textContent=summary.peak_year?`${summary.peak_year}年 / ${summary.peak_count}件`:'—';
  $('#cell-trend-recent').textContent=`${Number(summary.recent_three_years||0)}件`;
  $('#cell-trend-direction').textContent=`${directionLabels[summary.direction]||summary.direction||'—'}${summary.provisional?'（暫定）':''}`;
  chart.className='trend-chart';chart.innerHTML='';
  series.forEach(item=>{
    const count=Number(item.count||0),year=Number(item.year),bar=document.createElement('div');
    bar.className=`trend-year${count?'':' zero'}${hasRange&&inRange(year)?' in-range':''}${year>=Number(data.immature_from)?' immature':''}`;
    bar.setAttribute('aria-label',`${year}年、${count}件、全体${Number(item.total||0)}件、構成比${(Number(item.share||0)*100).toFixed(1)}%`);
    bar.title=`${year}年\n選択セル: ${count}件\n全体: ${Number(item.total||0)}件\n構成比: ${(Number(item.share||0)*100).toFixed(1)}%`;
    const height=count?Math.max(4,count/maximum*100):0;
    bar.innerHTML=`<span class="value">${count}</span><span class="bar-track"><span class="stacked-bar" style="height:${height}%">${trendStatusSegments(item.status_counts,count)}</span></span><small class="year">${year}</small>`;
    chart.append(bar);
  });
  chart.setAttribute('aria-label',`${$('#cell-trend-title').textContent}の年別件数。累計${Number(summary.total||0)}件、傾向${directionLabels[summary.direction]||summary.direction||'未判定'}`);
  const hasStatusBreakdown=series.some(item=>trendStatusKeys.some(key=>Number(item.status_counts?.[key]||0)>0));
  $('#cell-trend-legend').innerHTML=trendStatusLegend(`${hasStatusBreakdown?'':'<span><i class="unclassified"></i>状態内訳未取得</span>'}<span><i class="selected"></i>選択中の年範囲</span><span><i class="immature"></i>公開遅延の影響を受ける期間</span>`);
  const rangeNote=hasRange?`年フィルター ${range.from||'開始'}〜${range.to||'終了'} は背景で強調し、グラフ自体は全期間を表示しています。`:'グラフは全期間を表示しています。';
  const undatedNote=Number(summary.undated||0)?` 出願年不明の${Number(summary.undated)}件は棒グラフから除外しています。`:'';
  const basisNote=applicationYearOnly?'':` 出願日を取得できない文献では、収録年または公開番号から補完した年を使用しています。`;
  const statusNote=hasStatusBreakdown?'':' 法的状態の内訳は未取得です。PatentViewerを再起動すると3色の内訳が反映されます。';
  $('#cell-trend-note').textContent=`${rangeNote}${undatedNote}${basisNote} 法的状態は各年当時ではなく現在の状態です。${Number(data.immature_from)}年以降は公開遅延により少なく見える可能性があります。${statusNote}`;
}

function renderOrganizationTrend(data){
  const chart=$('#cell-trend-chart'),organizations=data.organizations||[],summary=data.summary||{},top=organizations[0],total=Number(summary.selected_count||0);
  $('#cell-trend-status').textContent='上位10出願人';
  $('#cell-trend-total-label').textContent='対象文献';$('#cell-trend-peak-label').textContent='出願人数';$('#cell-trend-recent-label').textContent='首位出願人';$('#cell-trend-direction-label').textContent='首位割合';
  $('#cell-trend-total').textContent=`${total}件`;$('#cell-trend-peak').textContent=`${Number(data.organization_count||0)}社`;$('#cell-trend-recent').textContent=top?.name||'—';$('#cell-trend-direction').textContent=top?`${(Number(top.share||0)*100).toFixed(1)}%`:'—';
  chart.className='trend-chart organization-chart';chart.innerHTML='';
  organizations.forEach((item,index)=>{
    const row=document.createElement('div'),share=Math.max(0,Math.min(1,Number(item.share||0)));
    row.className='organization-trend-row';row.setAttribute('aria-label',`${index+1}位、${item.name}、${item.count}件、選択セルの${(share*100).toFixed(1)}%`);
    row.title=`${item.name}\n${item.count}件 / ${(share*100).toFixed(1)}%\n${trendStatusKeys.map(key=>`${trendStatusLabels[key]} ${Number(item.status_counts?.[key]||0)}件`).join('・')}`;
    row.innerHTML=`<span class="organization-trend-name">${index+1}. ${escapeHtml(item.name)}</span><span class="organization-trend-track"><span class="organization-trend-stack" style="width:${share*100}%">${trendStatusSegments(item.status_counts)}</span></span><span class="organization-trend-value">${item.count}件 · ${(share*100).toFixed(1)}%</span>`;
    chart.append(row);
  });
  if(!organizations.length)chart.innerHTML='<div class="organization-empty">出願人情報が登録された文献はありません</div>';
  chart.setAttribute('aria-label',`${$('#cell-trend-title').textContent}の上位出願人構成`);
  $('#cell-trend-legend').innerHTML=trendStatusLegend();
  $('#cell-trend-note').textContent='現在の検索・年・法的状態・企業条件に該当する選択セルの文献数を100%として、出願人ごとの割合を表示しています。共同出願は各出願人に1件ずつ数えるため、割合の合計は100%を超える場合があります。';
}

function setCellTrendMode(mode){
  if(!['year','organization'].includes(mode))return;state.cellChartMode=mode;renderCellTrendMode();
}

async function refreshDocuments(reset=true){
  if(!state.dashboard||(state.documentPage.loading&&!reset))return;
  const request=++state.documentPage.request,offset=reset?0:state.visible.length;state.documentPage.loading=true;
  try{
    const researchId=encodeURIComponent(state.dashboard.research.id);
    const page=await api(`/api/researches/${researchId}/documents?${documentQuery(offset)}`);
    if(request!==state.documentPage.request)return;
    state.visible=reset?page.items:[...state.visible,...page.items];
    state.documentPage={...state.documentPage,total:page.total,offset:page.offset,hasMore:page.has_more,loading:false,request};
    state.dashboard.aggregates=page.aggregates;
    state.dashboard.selectionAggregates=page.selection_aggregates;
    state.dashboard.overlayAggregates=page.overlay_aggregates;
    renderMetrics();renderThreatMap();renderTechnologyMap();renderPatentList();renderOrganizationRanking();
  }finally{if(request===state.documentPage.request)state.documentPage.loading=false;}
}

function loadMoreDocuments(){if(state.documentPage.hasMore&&!state.documentPage.loading)return refreshDocuments(false);}

function renderMetrics() {
  const metrics=state.dashboard.aggregates?.metrics||{},total=Number(metrics.total||0),registered=Number(metrics.registered||0);
  $('#metric-visible').textContent=total;$('#metric-total').textContent=`/ ${state.researches.find(item=>item.id===state.dashboard.research.id)?.document_count||total} total`;
  $('#metric-ready').textContent=Number(metrics.ready||0);$('#metric-registered').textContent=total?`${Math.round(registered/total*100)}%`:'0%';
  $('#registered-bar').style.width=total?`${registered/total*100}%`:'0%';$('#metric-threat').textContent=Number(metrics.threat||0);
}

function organizationRegistry(){return state.dashboard?.organization_registry||{organizations:[],groups:[]};}
function patentOrganizationIds(patent){return new Set(patent.applicant_organization_ids||[]);}
function organizationMatches(patent,selection=state.selectedOrganization){
  if(!selection)return true;const ids=patentOrganizationIds(patent);return selection.memberIds.some(id=>ids.has(id));
}
function organizationRankingItems(){
  const registry=organizationRegistry();
  const counts=new Map((state.dashboard?.aggregates?.organization_counts||[]).map(item=>[item.organization_id,Number(item.count||0)]));
  if(state.organizationMode==='group')return (registry.groups||[]).map(group=>{
    const members=group.member_ids||[];const count=members.reduce((total,id)=>total+(counts.get(id)||0),0);
    return {type:'group',id:group.id,name:group.name,memberIds:members,count,scope:group.scope};
  }).filter(item=>item.count>0).sort((a,b)=>b.count-a.count||a.name.localeCompare(b.name,'ja'));
  return (registry.organizations||[]).map(org=>({type:'organization',id:org.id,name:org.name,memberIds:[org.id],count:counts.get(org.id)||0}))
    .filter(item=>item.count>0).sort((a,b)=>b.count-a.count||a.name.localeCompare(b.name,'ja'));
}
function renderOrganizationRanking(){
  const panel=$('#organization-panel');if(!panel)return;panel.classList.toggle('hidden',state.view!=='technology');
  const items=organizationRankingItems(),ranking=$('#organization-ranking'),total=Number(state.dashboard?.aggregates?.metrics?.total||0);$('#organization-scope').textContent=`現在の表示条件 · ${total}件`;
  ranking.innerHTML='';if(!items.length){ranking.innerHTML=`<div class="organization-empty">${state.organizationMode==='group'?'該当する登録グループがありません':'企業名が登録された文献がありません'}</div>`;return;}
  items.forEach((item,index)=>{const button=document.createElement('button'),share=total>0?Math.min(1,item.count/total):0;button.className='organization-rank';button.classList.toggle('active',state.selectedOrganization?.type===item.type&&state.selectedOrganization?.id===item.id);button.dataset.agentId=`organization-rank-${slug(item.id)}`;button.style.setProperty('--organization-share',`${share*100}%`);button.title=`${item.name}: ${item.count}件（全体の${(share*100).toFixed(1)}%）`;button.innerHTML=`<span class="rank">${index+1}</span><span class="name">${escapeHtml(item.name)}</span><span class="count">${item.count}</span>`;button.addEventListener('click',()=>selectOrganization(item));ranking.append(button);});
}
function selectOrganization(item){state.selectedOrganization=item;if(state.cellFilter)clearCell();else refreshDocuments(true);}
function clearOrganization(){state.selectedOrganization=null;if(state.cellFilter)clearCell();else refreshDocuments(true);}
function setOrganizationMode(mode){
  if(!['organization','group'].includes(mode))return;state.organizationMode=mode;state.selectedOrganization=null;if(state.cellFilter)clearCell();else refreshDocuments(true);
  $$('[data-organization-mode]').forEach(button=>{const active=button.dataset.organizationMode===mode;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});renderOrganizationRanking();renderTechnologyMap();
}

function density(count, max) {
  const alpha = count ? .18 + .65 * count / Math.max(1,max) : .04;
  return `rgba(48,199,228,${alpha})`;
}

function renderThreatMap() {
  const cells=new Map((state.dashboard?.aggregates?.threat_cells||[]).map(item=>[`${item.similarity},${item.concept_level}`,Number(item.count||0)]));
  const max = Math.max(1,...cells.values());
  const map = $('#threat-map'); map.innerHTML = '';
  for (let y=5; y>=1; y--) for (let x=1; x<=5; x++) {
    const count = cells.get(`${x},${y}`) || 0;
    const button = document.createElement('button'); button.className = 'map-cell'; button.dataset.count = count;
    button.dataset.agentId = `threat-cell-${x}-${y}`; button.style.background = density(count,max);
    button.setAttribute('aria-label', `類似度${x}、権利範囲の広さ${y}、${count}件`);
    button.innerHTML = `<strong>${count}</strong><small>${x} × ${y}</small>`;
    button.disabled=count===0;button.setAttribute('aria-disabled',String(count===0));
    if (count>0&&state.cellFilter?.type === 'threat' && state.cellFilter.x === x && state.cellFilter.y === y) button.classList.add('selected');
    if(count>0)button.addEventListener('click', () => selectCell({ type:'threat', x, y, label:`脅威マップ：類似度 ${x} / 権利範囲の広さ ${y}` }));
    map.append(button);
  }
}

function renderTechnologyMap() {
  const cells=state.dashboard?.aggregates?.technology_cells||[];
  const overlayCells=state.selectedOrganization?(state.dashboard?.overlayAggregates?.technology_cells||[]):cells;
  const thresholdControl=$('#technology-cluster-min-size');const minimum=Math.max(1,Number(thresholdControl?.value||state.techClusterMinSize||1));state.techClusterMinSize=minimum;
  const techCounts=new Map(),problemCounts=new Map(),techNames=new Map(),problemNames=new Map();
  cells.forEach(item=>{const tech=String(item.tech_cluster_id),problem=String(item.problem_cluster_id),count=Number(item.count||0);techCounts.set(tech,(techCounts.get(tech)||0)+count);problemCounts.set(problem,(problemCounts.get(problem)||0)+count);techNames.set(tech,item.tech_cluster||tech);problemNames.set(problem,item.problem_cluster||problem);});
  const allTechs=[...techCounts.keys()],allProblems=[...problemCounts.keys()];
  const metadata=state.dashboard?.technology_map||{};
  const semanticOrder=(available,order)=>[...(order||[]).map(String).filter(id=>available.includes(id)),...available.filter(id=>!(order||[]).map(String).includes(id))];
  const techs=semanticOrder(allTechs.filter(id=>techCounts.get(id)>=minimum),metadata.technology_order);
  const problems=semanticOrder(allProblems.filter(id=>problemCounts.get(id)>=minimum),metadata.problem_order);
  const techMeta=metadata.technology_clusters||{},problemMeta=metadata.problem_clusters||{};
  const techName=id=>techMeta[id]?.name||techNames.get(id)||id;
  const problemName=id=>problemMeta[id]?.name||problemNames.get(id)||id;
  const strength=(items,id)=>Math.max(0,Math.min(1,Number(items[id]?.relative_strength||0)));
  const map = $('#technology-map');map.innerHTML='';
  if(!techs.length||!problems.length){map.style.setProperty('--tech-cols',1);map.style.setProperty('--tech-rows',1);map.innerHTML=`<div class="map-empty"><strong>表示対象のクラスタがありません</strong><small>最小件数を下げてください</small></div>`;$('#cluster-count').textContent=`0 clusters / min ${minimum}`;$('#technology-map-note').textContent=`${minimum}件未満を非表示。技術 ${allTechs.length}・課題 ${allProblems.length}クラスタの計算結果は保持しています。`;return;}
  map.style.setProperty('--tech-cols',techs.length+1);map.style.setProperty('--tech-rows',problems.length+1);
  map.style.gridTemplateColumns=`minmax(92px,1.1fr) repeat(${techs.length},minmax(62px,1fr))`;map.style.gridTemplateRows=`minmax(54px,auto) repeat(${problems.length},minmax(58px,1fr))`;
  const corner=document.createElement('div');corner.className='map-axis-corner';corner.textContent='課題 × 技術';map.append(corner);
  techs.forEach(id=>{const label=document.createElement('div');const glow=strength(techMeta,id);label.className='map-axis-label column';label.textContent=techName(id);label.title=`自社技術とのコサイン類似度: ${Number(techMeta[id]?.cosine_similarity||0).toFixed(3)}`;label.style.setProperty('--axis-glow',glow);map.append(label);});
  const buckets=new Map(cells.map(item=>[`${item.tech_cluster_id}|${item.problem_cluster_id}`,Number(item.count||0)]));
  const overlayBuckets=new Map(overlayCells.map(item=>[`${item.tech_cluster_id}|${item.problem_cluster_id}`,Number(item.count||0)]));
  const max = Math.max(1,...buckets.values());
  problems.forEach(problem=>{
    const rowLabel=document.createElement('div');const rowGlow=strength(problemMeta,problem);rowLabel.className='map-axis-label row';rowLabel.textContent=problemName(problem);rowLabel.title=`自社の対象課題とのコサイン類似度: ${Number(problemMeta[problem]?.cosine_similarity||0).toFixed(3)}`;rowLabel.style.setProperty('--axis-glow',rowGlow);map.append(rowLabel);
    techs.forEach(tech => {
      const count=buckets.get(`${tech}|${problem}`)||0,overlayCount=overlayBuckets.get(`${tech}|${problem}`)||0,interactiveCount=state.selectedOrganization?overlayCount:count;const button=document.createElement('button');button.className='map-cell';button.dataset.count=count;
      const columnGlow=strength(techMeta,tech);button.style.background=density(count,max);button.style.setProperty('--column-glow',columnGlow);button.style.setProperty('--row-glow',rowGlow);
      button.dataset.agentId=`technology-cell-${slug(tech)}-${slug(problem)}`;
      const countLabel=state.selectedOrganization?`${state.selectedOrganization.name} ${overlayCount}件、母集団${count}件`:`${count}件`;
      button.setAttribute('aria-label',`${techName(tech)}、${problemName(problem)}、${countLabel}、自社技術近接度${Math.round(columnGlow*100)}%、自社課題近接度${Math.round(rowGlow*100)}%`);button.title=`技術: ${techName(tech)}\n課題: ${problemName(problem)}\n${countLabel}\n自社技術との類似度: ${Number(techMeta[tech]?.cosine_similarity||0).toFixed(3)}\n自社課題との類似度: ${Number(problemMeta[problem]?.cosine_similarity||0).toFixed(3)}`;
      button.innerHTML=state.selectedOrganization?`<strong class="overlay-count">${overlayCount}</strong><small class="population-count">/ ${count} 母集団</small>`:`<strong>${count}</strong>`;
      button.disabled=interactiveCount===0;button.setAttribute('aria-disabled',String(interactiveCount===0));
      if(interactiveCount>0&&state.cellFilter?.type==='technology'&&state.cellFilter.tech===tech&&state.cellFilter.problem===problem)button.classList.add('selected');
      if(interactiveCount>0)button.addEventListener('click',()=>selectCell({type:'technology',tech,problem,label:`技術：${techName(tech)} / 課題：${problemName(problem)}${state.selectedOrganization?` / ${state.selectedOrganization.name}`:''}`}));map.append(button);
    });
  });
  const hiddenTechs=allTechs.length-techs.length,hiddenProblems=allProblems.length-problems.length;
  $('#cluster-count').textContent=`${techs.length} × ${problems.length} / min ${minimum}`;
  const overlayNote=state.selectedOrganization?`セル主数字は「${state.selectedOrganization.name}」、小数字は母集団です。`:'';
  $('#technology-map-note').textContent=`意味の近い順に配置。背景は母集団の文献量、左右の発光は自社技術との近さ、上下の発光は自社の対象課題との近さです。${overlayNote}${minimum}件未満を非表示（技術 ${hiddenTechs}・課題 ${hiddenProblems}）。`;
}

function selectCell(filter) { state.cellFilter=filter;$('#clear-cell').classList.remove('hidden');$('#active-filter').classList.remove('hidden');$('#active-filter').textContent=filter.label;refreshDocuments(true);refreshCellTrend(); }
function clearCell() { const changed=!!state.cellFilter;state.cellFilter=null;state.trendRequest++;$('#clear-cell').classList.add('hidden');$('#active-filter').classList.add('hidden');$('#cell-trend-panel').classList.add('hidden');if(changed)refreshDocuments(true);else{renderThreatMap();renderTechnologyMap();renderPatentList();} }

function renderPatentList() {
  const patents=state.visible;const list=$('#patent-list');list.innerHTML='';
  if(!patents.length){list.innerHTML='<div class="empty-state"><strong>該当文献はありません</strong><p>条件またはセル選択を変更してください。</p></div>';return;}
  patents.forEach(p=>{const button=document.createElement('button');button.className='patent-row';if(state.selected?.id===p.id)button.classList.add('selected');button.dataset.agentId=`patent-open-${p.id}`;
    button.innerHTML=`<span class="doc-icon">PDF</span><span><strong>${escapeHtml(p.title)}</strong><span>${escapeHtml(p.publication_number)} · ${escapeHtml(p.applicant)}</span></span><span class="row-badges"><span class="badge">${escapeHtml(displayStatus(p))}</span><span class="badge ${p.analysis_state}">${p.analysis_state}</span></span>`;
    button.addEventListener('click',()=>selectPatent(p));list.append(button);});
}

function verifiedClaimEvidence(patent){
  const trace=patent?.claim_traceability||{};
  return [
    ['類似度',trace.similarity],
    ['権利範囲の広さ',trace.concept_level],
  ].filter(([,item])=>item?.verified===true&&Number.isInteger(item.claim_number)&&typeof item.claim_text==='string'&&item.claim_text.trim());
}

function openClaimEvidence(label,evidence){
  $('#claim-dialog-title').textContent=`請求項${evidence.claim_number}`;
  $('#claim-dialog-meta').textContent=`${label}の根拠 · Python検証済み · SHA-256 ${String(evidence.claim_sha256||'').slice(0,12)}…`;
  $('#claim-dialog-text').textContent=evidence.claim_text;
  $('#claim-dialog').showModal();
}

function renderClaimEvidence(patent){
  const container=$('#selected-evidence'),heading=$('#selected-evidence-label'),items=verifiedClaimEvidence(patent);
  container.innerHTML='';heading.classList.toggle('hidden',!items.length);container.classList.toggle('hidden',!items.length);
  items.forEach(([label,evidence])=>{const button=document.createElement('button');button.type='button';button.textContent=`${label}: 請求項${evidence.claim_number}`;button.addEventListener('click',()=>openClaimEvidence(label,evidence));container.append(button);});
}

function selectPatent(patent) {
  state.selected=patent;renderPatentList();$('#interpretation-empty').classList.add('hidden');$('#interpretation-content').classList.remove('hidden');$('#preview-selected').disabled=!patent.pdf_available;
  $('#selected-status').textContent=displayStatus(patent);$('#selected-title').textContent=patent.title;$('#selected-number').textContent=`${patent.publication_number} · ${patent.applicant}`;
  const pendingMessage=patent.analysis_state==='skipped'?`スキップ: ${patent.skip_reason||'理由未設定'}${patent.skip_detail?` / ${patent.skip_detail}`:''}`:'ローカルLLM分析待ちです。';
  $('#selected-similarity').textContent=analysisValue(patent,'similarity')??'待機';$('#selected-concept').textContent=analysisValue(patent,'concept_level')??'待機';
  $('#selected-tech-summary').textContent=analysisValue(patent,'tech_summary')||pendingMessage;$('#selected-problem-summary').textContent=analysisValue(patent,'problem_summary')||pendingMessage;
  $('#selected-reasoning').textContent=analysisValue(patent,'reasoning')||'スコア根拠は分析JSON生成後に表示されます。';renderClaimEvidence(patent);$('#interpretation-note').value='';$('#save-feedback').textContent='';
}

function renderInterpretation(){if(!state.selected){$('#interpretation-empty').classList.remove('hidden');$('#interpretation-content').classList.add('hidden');$('#selected-status').textContent='文献未選択';}}
function openPdf(patent){if(!patent)return toast('文献を選択してください');const url=`/api/pdfs/${encodeURIComponent(patent.pdf)}`;$('#pdf-title').textContent=patent.publication_number;$('#pdf-frame').src=url;$('#pdf-new-tab').href=url;$('#pdf-dialog').showModal();}

async function saveNote() {
  if(!state.selected)return; const button=$('#save-note');
  button.patentViewerActionPromise=(async()=>{button.disabled=true;try{const result=await api('/api/interpretations',{method:'POST',body:JSON.stringify({environment:state.environment,research_id:state.dashboard.research.id,patent_id:state.selected.id,note:$('#interpretation-note').value,source:window.PatentViewerBridge?.currentCommandId?'codex':'human'}),headers:window.PatentViewerBridge?.authorizationHeaders?.()||{}});$('#save-feedback').textContent=`保存しました: ${result.path}`;}catch(error){$('#save-feedback').textContent=error.message;throw error;}finally{button.disabled=false;}})();
  return button.patentViewerActionPromise;
}

async function toggleEnvironment(){const next=state.environment==='normal'?'debug':'normal';if(!confirm(`${next.toUpperCase()}環境へ切り替えます。保存先と入力データは分離されています。`))return;const button=$('#debug-toggle');button.patentViewerActionPromise=(async()=>{await api('/api/environment',{method:'POST',body:JSON.stringify({environment:next})});setTimeout(()=>location.reload(),250);})();return button.patentViewerActionPromise;}
function renderEnvironment(){const badge=$('#environment-badge');badge.textContent=state.environment.toUpperCase();badge.classList.toggle('debug',state.environment==='debug');document.body.dataset.environment=state.environment;$('#debug-toggle').setAttribute('aria-label',state.environment==='normal'?'DEBUG環境へ切替':'NORMAL環境へ戻る');$('#research-manage-button').disabled=state.environment!=='normal';}

async function showPreflight(){if(!state.dashboard)return;const dialog=$('#preflight-dialog'),result=$('#preflight-results');result.innerHTML='<p>診断中…</p>';dialog.showModal();const data=await api(`/api/researches/${state.dashboard.research.id}/llm-preflight?environment=${state.environment}`);result.innerHTML=data.checks.map(c=>`<div class="check-item ${c.ok?'':'failed'}"><i>${c.ok?'●':'▲'}</i><div><strong>${escapeHtml(c.id)}</strong><br><small>${escapeHtml(c.detail)}</small></div></div>`).join('')+`<p class="muted">Selection: ${data.selection_fingerprint}</p>`;}

function renderPipeline(overview,preflight){
  state.pipelineReady=!!preflight.ready;state.pipelineRunnable=(preflight.checks||[]).filter(check=>check.id!=='claim-structure').every(check=>check.ok);const c=overview.counts;state.pipelinePending=Number(c.pending||0);state.pipelineAvailable=Number(c.available??c.total??0);
  const preflightLabel=state.pipelineReady?'READY':(state.pipelineRunnable?'前処理待ち':'BLOCKED');
  const stale=!!overview.analysis_stale;$('#pipeline-policy').classList.toggle('pipeline-stale-warning',stale);$('#pipeline-policy').textContent=`${overview.environment.toUpperCase()} / ${overview.research_id} / 抽出: ${overview.source_policy.extraction||'verify_original'} / 読取: ${overview.source_policy.reading||'adaptive'} / preflight: ${preflightLabel}${stale?' / 最新CSVへ切替済み・全件再分析が必要':''}`;
  $('#pipeline-counts').innerHTML=`<span id="pipeline-target-count" class="pending"><b>${state.pipelinePending}</b>未確定</span><span><b>${c.total}</b>CSV対象</span><span><b>${c.llm_pending??state.pipelinePending}</b>LLM待ち</span><span><b>${c.finalized}</b>JSON済み</span><span><b>${c.skipped||0}</b>スキップ</span><span><b>${c.failed||0}</b>前回失敗</span>`;
  $('#pipeline-overwrite').checked=stale||$('#pipeline-overwrite').checked;$('#pipeline-overwrite').disabled=stale;
  updatePipelineAction();
  const active=overview.jobs.find(job=>['running','paused','cancelling'].includes(job.status));const latest=active||overview.jobs[0];
  if(latest){state.pipelineJob=latest;renderPipelineJob(latest);}else{state.pipelineJob=null;$('#pipeline-job').classList.add('hidden');renderPipelineStages({});}
}

function updatePipelineAction(){
  const overwrite=$('#pipeline-overwrite').checked;const target=overwrite?state.pipelineAvailable:state.pipelinePending;const targetCard=$('#pipeline-target-count');
  if(targetCard){targetCard.innerHTML=`<b>${target}</b>${overwrite?'再分析対象':'未確定'}`;targetCard.classList.toggle('overwrite',overwrite);}
  $('#pipeline-confirm-text').textContent=overwrite?`既存結果を更新し、全${target}件を再分析することを確認しました`:'未処理PDFをローカルLLMでバッチ処理することを確認しました';
  $('#pipeline-execute').textContent=overwrite?`全${target}件の再分析を開始`:target?`未処理 ${target}件の夜間一括分析を開始`:'すべて処理済み（全件再分析を選択できます）';
  $('#pipeline-execute').disabled=!state.pipelineRunnable||!target||!$('#pipeline-confirm').checked;
}

async function showPipeline(){
  if(!state.dashboard)return;const dialog=$('#pipeline-dialog');if(!dialog.open)dialog.showModal();const select=$('#pipeline-research-select');select.innerHTML='';state.researches.forEach(item=>select.add(new Option(`${item.name}（${item.document_count}件${item.lifecycle?.analysis_stale?'・再分析必要':''}）`,item.id)));state.pipelineResearchId=state.pipelineResearchId&&state.researches.some(item=>item.id===state.pipelineResearchId)?state.pipelineResearchId:state.dashboard.research.id;select.value=state.pipelineResearchId;return loadPipelineResearch(state.pipelineResearchId);
}

async function loadPipelineResearch(researchId){
  state.pipelineResearchId=researchId;$('#pipeline-policy').textContent='状態を確認しています…';$('#pipeline-confirm').checked=false;$('#pipeline-overwrite').checked=false;$('#pipeline-overwrite').disabled=false;
  const id=encodeURIComponent(researchId);const [overview,preflight]=await Promise.all([api(`/api/researches/${id}/pipeline?environment=${state.environment}&compact=true`),api(`/api/researches/${id}/llm-preflight?environment=${state.environment}`)]);const previous=state.pipelineJob?.id;renderPipeline(overview,preflight);const active=overview.jobs.find(job=>['running','paused','cancelling'].includes(job.status));if(active&&active.id!==previous)monitorPipelineJob(active.id,false);
}

function renderPipelineJob(job){
  const jobPanel=$('#pipeline-job');jobPanel.classList.remove('hidden');jobPanel.dataset.status=job.status;
  const progress=job.progress||{};const detail=progress.detail||{};const total=Number(detail.total||0);const completed=Number(detail.completed??(progress.stage==='completed'?total:Math.max(0,Number(detail.current||1)-1)));const percent=Number(detail.percent??(total?completed/total*100:0));
  const statusLabels={running:'実行中',paused:'一時停止',cancelling:'停止処理中',completed:'完了',failed:'バッチ停止',cancelled:'キャンセル'};$('#pipeline-job-state').textContent=statusLabels[job.status]||job.status.toUpperCase();
  const taskLabels={similarity:'① 類似度評価',concept_level:'② 権利範囲の広さ評価',problem_summary:'③ 課題要約',technology_summary:'④ 技術要約',embeddings:'⑤ 文献Embedding',cluster_name:'クラスタ名生成'};
  const stageLabels={initializing:'初期化',preparing:'前処理',company_profile:'自社基準の要約生成',company_embedding:'自社基準のEmbedding',analyzing:'LLM分析開始',shard_started:'永続バッチ開始',shard_completed:'永続バッチ保存・メモリ解放',loading_embeddings:'全Embedding再読込',llm_batch:taskLabels[detail.task]||'LLMバッチ処理',llm_task:taskLabels[detail.task]||'LLM処理',rescue_queued:'高精度モデル救済待ち',rescue_started:'高精度モデル救済開始',rescue_task:taskLabels[detail.task]||'高精度モデル処理',rescue_completed:'高精度モデル救済完了',manual_review_required:'自動救済失敗・手動確認へ移行',document_completed:'1件完了',document_failed:'1件失敗・次の文献へ継続',skipped:'スキップ・次の文献へ継続',cooldown:'冷却中',checkpoint_reused:'分析済み再利用',clustering:'クラスタリング',cluster_naming:'クラスタ名生成',semantic_ordering:'意味順・近接度を確定',completed:'バッチ完了',failed:'バッチ停止'};const stageLabel=stageLabels[progress.stage]||progress.stage||'待機';
  stageLabels.shared_cache_hit='共通LLMキャッシュ再利用';stageLabels.embedding_cache_hit='共通Embeddingキャッシュ再利用';const displayStageLabel=stageLabels[progress.stage]||stageLabel;
  $('#pipeline-job-progress').textContent=[displayStageLabel,detail.patent_id,total?`${completed}/${total}`:''].filter(Boolean).join(' · ');
  const bar=$('#pipeline-progress-bar'),track=bar.parentElement;bar.style.width=`${Math.max(0,Math.min(100,percent))}%`;track.setAttribute('aria-valuenow',String(Math.round(percent)));
  const cooldown=Number(detail.remaining_seconds||0);const attempt=Number(detail.attempt||0);const attemptText=attempt?` · 試行 ${attempt}/${detail.max_attempts||1}`:'';const shardText=Number(detail.shards||0)?` · バッチ ${detail.shard||1}/${detail.shards}`:'';$('#pipeline-progress-label').textContent=cooldown?`GPU冷却中 · 残り ${Math.floor(cooldown/60)}:${String(cooldown%60).padStart(2,'0')} · 処理済み ${completed}/${total}`:total?`${displayStageLabel}${shardText}${attemptText} · 処理済み ${completed}/${total}（${percent.toFixed(1)}%）`:`${displayStageLabel}${shardText}${attemptText}`;
  const processed=Number(detail.processed??completed);const succeeded=Number(detail.succeeded??detail.llm_completed??Math.max(0,processed-Number(detail.failed||0)-Number(detail.skipped||0)));const failed=Number(detail.failed||0);const skipped=Number(detail.skipped||0);const remaining=Math.max(0,total-processed);
  $('#pipeline-live-counts').innerHTML=`<span><b>${processed}</b>処理済み</span><span><b>${succeeded}</b>成功</span><span class="failed"><b>${failed}</b>失敗</span><span><b>${skipped}</b>スキップ</span><span><b>${remaining}</b>残り</span>`;
  renderPipelineTiming(job,progress,detail,total,processed);
  renderPipelineStages(progress);
  const errorText=job.error||detail.error||'';$('#pipeline-job-detail').textContent=errorText?`最終エラー: ${errorText}\n\n${JSON.stringify(progress,null,2)}`:JSON.stringify(progress,null,2);const terminal=['completed','failed','cancelled'].includes(job.status);
  $('#pipeline-pause').disabled=terminal||job.status==='paused';$('#pipeline-resume').disabled=terminal||job.status==='running';$('#pipeline-cancel').disabled=terminal;
}

function formatDuration(seconds){const value=Math.max(0,Math.round(Number(seconds)||0));const hours=Math.floor(value/3600),minutes=Math.floor(value%3600/60),secs=value%60;return hours?`${hours}:${String(minutes).padStart(2,'0')}:${String(secs).padStart(2,'0')}`:`${minutes}:${String(secs).padStart(2,'0')}`;}
function renderPipelineTiming(job,progress,detail,total,processed){
  const timing=progress.timing||{};const liveDelta=job.status==='running'&&progress.updated_at?Math.max(0,(Date.now()-Date.parse(progress.updated_at))/1000):0;const elapsed=Number(timing.elapsed_seconds||0)+liveDelta;const current=Number(timing.current_phase_elapsed_seconds||0)+liveDelta;
  const taskIndex=Number(detail.task_index||0),taskFraction=total&&taskIndex?Math.max(0,Math.min(1,((taskIndex-1)+Math.max(0,Number(detail.current||0))/total)/5)):0;const documentFraction=total?processed/total:0;const fraction=Math.max(taskFraction,documentFraction);const eta=fraction>0&&fraction<1?elapsed/fraction*(1-fraction):0;
  $('#pipeline-timing-summary').innerHTML=`<span><small>総経過</small><b>${formatDuration(elapsed)}</b></span><span><small>現在工程</small><b>${formatDuration(current)}</b></span><span><small>推定残り</small><b>${eta?formatDuration(eta):'—'}</b></span>`;
  const labels={preparation:'前処理',company_profile:'自社基準生成',company_embedding:'自社Embedding',embeddings:'文献Embedding',clustering_and_naming:'クラスタ・命名',semantic_ordering:'意味順確定',cooldown:'冷却',finalization:'結果確定',similarity:'類似度',concept_level:'権利範囲の広さ',problem_summary:'課題要約',technology_summary:'技術要約'};
  $('#pipeline-phase-times').innerHTML=Object.entries(timing.phase_durations_seconds||{}).map(([key,value])=>{const clean=key.replace('generation:','');const liveValue=Number(value)+(timing.current_phase===key?liveDelta:0);return `<span class="${timing.current_phase===key?'active':''}"><b>${escapeHtml(labels[clean]||clean)}</b><small>${formatDuration(liveValue)}</small></span>`;}).join('');
}

function renderPipelineStages(progress){
  const stage=progress.stage||'';const detail=progress.detail||{};
  const phases=[['前処理',['initializing','preparing']],['自社基準',['company_profile']],['文献分析',['analyzing','shard_started','shard_completed','llm_batch','llm_task','rescue_queued','rescue_started','rescue_task','rescue_completed','manual_review_required','document_completed','document_failed','skipped','cooldown','checkpoint_reused']],['自社Embedding',['company_embedding']],['クラスタリング',['loading_embeddings','clustering','cluster_naming']],['意味順・近接度',['semantic_ordering']],['結果確定',['completed']]];
  phases[2][1].push('shared_cache_hit','embedding_cache_hit');
  const effectiveStage=stage==='failed'?(detail.failed_at_stage||stage):stage;const activeIndex=phases.findIndex(([,values])=>values.includes(effectiveStage));
  $('#pipeline-stages').innerHTML=phases.map(([label],index)=>`<span class="${stage==='completed'||index<activeIndex?'done':index===activeIndex?(stage==='failed'?'failed':'active'):''}">${index+1}. ${label}</span>`).join('');
  const tasks=[['similarity','類似度'],['concept_level','権利範囲の広さ'],['problem_summary','課題要約'],['technology_summary','技術要約'],['embeddings','Embedding']];const taskIndex=Number(detail.task_index||0);const documentDone=['document_completed','cooldown'].includes(stage);const documentFailed=stage==='document_failed';
  $('#pipeline-document-stages').innerHTML=tasks.map(([key,label],index)=>{const number=index+1;let cls=documentDone?'done':taskIndex>number?'done':taskIndex===number?'active':'';if(documentFailed&&detail.task===key)cls='failed';return `<span class="${cls}">${number}. ${label}</span>`;}).join('');
}

async function controlPipeline(control){if(!state.pipelineJob)return;state.pipelineJob=await api(`/api/pipeline/jobs/${state.pipelineJob.id}/control`,{method:'POST',body:JSON.stringify({control})});renderPipelineJob(state.pipelineJob);}

async function monitorPipelineJob(jobId,bridgeOwned){
  let lastControl='run';
  while(true){
    const job=await api(`/api/pipeline/jobs/${jobId}`);state.pipelineJob=job;renderPipelineJob(job);
    if(['completed','failed','cancelled'].includes(job.status)){
      if(state.dashboard?.research.id===job.research_id)await loadDashboard(job.research_id);await loadResearches(state.dashboard?.research.id);state.pipelineResearchId=job.research_id;await showPipeline();
      if(bridgeOwned&&job.status==='cancelled')throw new DOMException('pipeline job cancelled','AbortError');
      if(job.status==='failed'){if(bridgeOwned)throw new Error(job.error||'pipeline job failed');toast('バッチが停止しました。画面に最終件数とエラーを保持しています。');}
      return job;
    }
    if(bridgeOwned&&window.PatentViewerBridge?.currentControl){const control=await window.PatentViewerBridge.currentControl();if(control!==lastControl){await controlPipeline(control);lastControl=control;}}
    await new Promise(resolve=>setTimeout(resolve,750));
  }
}

async function runPipelineMode(mode,requested={}){
  const researchId=requested.researchId||state.pipelineResearchId||state.dashboard.research.id;const source=window.PatentViewerBridge?.currentCommandId?'codex':'human';const selectedCooldown=Math.max(0,Math.min(180,Number($('#pipeline-cooldown-seconds').value)||0));const cooldown=requested.cooldownSeconds??selectedCooldown;const overwrite=mode==='execute'?(requested.overwrite??$('#pipeline-overwrite').checked):false;const job=await api(`/api/researches/${encodeURIComponent(researchId)}/pipeline/jobs`,{method:'POST',headers:window.PatentViewerBridge?.authorizationHeaders?.()||{},body:JSON.stringify({environment:state.environment,mode,source,overwrite,confirmation:mode==='execute'?'RUN_LOCAL_LLM':undefined,cooldown_seconds:mode==='execute'?cooldown:0})});state.pipelineJob=job;renderPipelineJob(job);return monitorPipelineJob(job.id,source==='codex');
}

function startPipeline(mode,button){
  button.patentViewerActionPromise=(async()=>{button.disabled=true;try{return await runPipelineMode(mode);}finally{button.disabled=false;}})();
  return button.patentViewerActionPromise;
}

function processPending(button){
  button.patentViewerActionPromise=(async()=>{button.disabled=true;try{
    const request={
      researchId:state.pipelineResearchId||state.dashboard.research.id,
      overwrite:$('#pipeline-overwrite').checked,
      cooldownSeconds:Math.max(0,Math.min(180,Number($('#pipeline-cooldown-seconds').value)||0)),
    };
    await runPipelineMode('prepare',{researchId:request.researchId});
    if(!state.pipelineReady)throw new Error('前処理後もpreflightを通過できませんでした。スキップ理由を確認してください。');
    $('#pipeline-overwrite').checked=request.overwrite;
    $('#pipeline-confirm').checked=true;
    updatePipelineAction();
    return await runPipelineMode('execute',request);
  }finally{updatePipelineAction();}})();
  return button.patentViewerActionPromise;
}

function openPipelineFrom(button){button.patentViewerActionPromise=showPipeline();return button.patentViewerActionPromise;}

function renderLegalStatusPreview(){
  const counts={rights_acquired:0,under_examination:0,published:0};
  (state.dashboard?.aggregates?.source_status_counts||[]).forEach(item=>{
    const source=String(item.source_status||'').trim(),count=Number(item.count||0);
    const category=state.legalStatusDraft.rights_acquired.includes(source)?'rights_acquired':state.legalStatusDraft.under_examination.includes(source)?'under_examination':'published';
    counts[category]+=count;
  });
  $('#legal-status-preview').innerHTML=[
    ['権利化',counts.rights_acquired],['審査中',counts.under_examination],['公開',counts.published],
  ].map(([label,count])=>`<span>${label}<strong>${count}件</strong></span>`).join('');
}

function renderLegalStatusTags(){
  for(const category of ['rights_acquired','under_examination']){
    const editor=$(`.tag-editor[data-category="${category}"]`),chips=editor.querySelector('.tag-chips');chips.innerHTML='';
    state.legalStatusDraft[category].forEach((value,index)=>{
      const chip=document.createElement('span');chip.className='tag-chip';
      const text=document.createElement('span');text.textContent=value;
      const remove=document.createElement('button');remove.type='button';remove.setAttribute('aria-label',`${value}を削除`);remove.textContent='×';
      remove.addEventListener('click',()=>{state.legalStatusDraft[category].splice(index,1);renderLegalStatusTags();});
      chip.append(text,remove);chips.append(chip);
    });
  }
  renderLegalStatusPreview();
}

function addLegalStatusTags(category,raw){
  const values=String(raw||'').split(/[,\n、]+/).map(value=>value.trim()).filter(Boolean);
  for(const value of values)if(!state.legalStatusDraft[category].includes(value))state.legalStatusDraft[category].push(value);
  renderLegalStatusTags();
}

function bindLegalTagInput(inputId,category){
  const input=$(inputId);
  input.addEventListener('keydown',event=>{
    if(event.key==='Enter'||event.key===','){event.preventDefault();addLegalStatusTags(category,input.value);input.value='';}
    else if(event.key==='Backspace'&&!input.value&&state.legalStatusDraft[category].length){state.legalStatusDraft[category].pop();renderLegalStatusTags();}
  });
  input.addEventListener('input',()=>{
    if(/[,\n、]/.test(input.value)){const value=input.value;input.value='';addLegalStatusTags(category,value);}
  });
  input.addEventListener('blur',()=>{if(input.value.trim()){addLegalStatusTags(category,input.value);input.value='';}});
}

function showLegalStatusSettings(){
  if(!state.dashboard)return;
  const rules=state.dashboard.research.legal_status_rules||{};
  state.legalStatusDraft={
    rights_acquired:[...(rules.rights_acquired||[])],
    under_examination:[...(rules.under_examination||[])],
  };
  $('#legal-status-feedback').textContent='';renderLegalStatusTags();$('#legal-status-dialog').showModal();
}

async function saveLegalStatusSettings(){
  const button=$('#legal-status-save'),feedback=$('#legal-status-feedback');
  const duplicate=state.legalStatusDraft.rights_acquired.find(value=>state.legalStatusDraft.under_examination.includes(value));
  if(duplicate){feedback.textContent=`「${duplicate}」は権利化と審査中の両方には登録できません。`;return;}
  button.disabled=true;feedback.textContent='保存しています…';
  try{
    const researchId=state.dashboard.research.id;
    await api(`/api/researches/${encodeURIComponent(researchId)}/legal-status-rules`,{method:'POST',body:JSON.stringify({environment:state.environment,...state.legalStatusDraft})});
    await loadDashboard(researchId);$('#legal-status-dialog').close();toast('法的状態の判定設定を保存しました');
  }catch(error){feedback.textContent=error.message;}finally{button.disabled=false;}
}

async function validateCsvFile(file,target){
  const panel=$(target);panel.className='csv-validation muted';panel.textContent='CSVを検証しています…';
  try{const payload=await csvPayload(file);const result=await api('/api/researches/validate-csv',{method:'POST',body:JSON.stringify({environment:state.environment,...payload})});panel.className='csv-validation valid';panel.textContent=`${result.rows}件 · PDF一致 ${result.matched_pdfs}件 · PDF未発見 ${result.missing_pdfs}件 · 複数候補 ${result.multiple_candidates}件 · 警告 ${(result.warnings||[]).length}件`;return result;}
  catch(error){panel.className='csv-validation invalid';panel.textContent=error.message;throw error;}
}

function selectedManagedResearch(){return state.managedResearches.find(item=>item.id===$('#research-manage-select').value);}

function renderManagedResearch(){
  const select=$('#research-manage-select'),previous=select.value;select.innerHTML='';state.managedResearches.forEach(item=>select.add(new Option(`${item.lifecycle?.status==='archived'?'[アーカイブ] ':''}${item.name}`,item.id)));if(previous&&state.managedResearches.some(item=>item.id===previous))select.value=previous;
  const item=selectedManagedResearch(),summary=$('#research-manage-summary');if(!item){summary.textContent='リサーチがありません';return;}
  const active=(item.csv_history||[]).find(csv=>csv.active),stale=!!item.lifecycle?.analysis_stale;summary.innerHTML=`<strong>${escapeHtml(item.name)}</strong> <span class="research-status ${stale?'stale':''}">${item.lifecycle?.status==='archived'?'アーカイブ済み':stale?'再分析必要':'使用中'}</span><br>${item.document_count}件 · 有効CSV: ${escapeHtml(active?.filename||'なし')}<div class="research-history">${(item.csv_history||[]).map(csv=>`<span class="${csv.active?'active':''}"><b>${escapeHtml(csv.filename)}</b><small>${csv.active?'現在有効':escapeHtml(csv.timestamp)}</small></span>`).join('')}</div>`;
  const archived=item.lifecycle?.status==='archived';$('#research-archive-selected').classList.toggle('hidden',archived);$('#research-restore-selected').classList.toggle('hidden',!archived);$('#research-open-selected').disabled=archived;$('#research-update-submit').disabled=archived;$('#research-update-csv').disabled=archived;
}

async function refreshResearchManager(preferred){
  const data=await api(`/api/researches?environment=${state.environment}&status=all`);state.managedResearches=data.items;renderManagedResearch();if(preferred&&state.managedResearches.some(item=>item.id===preferred)){$('#research-manage-select').value=preferred;renderManagedResearch();}
}

async function showResearchManager(){
  if(state.environment!=='normal'){toast('リサーチ管理はNORMAL環境で使用します');return;}
  const dialog=$('#research-dialog');if(!dialog.open)dialog.showModal();await refreshResearchManager(state.dashboard?.research.id);
}

async function createResearch(event){
  event.preventDefault();const button=$('#research-create-submit'),feedback=$('#research-create-feedback');button.disabled=true;feedback.textContent='作成しています…';
  try{const file=$('#research-create-csv').files[0];await validateCsvFile(file,'#research-create-validation');const payload=await csvPayload(file);const result=await api('/api/researches',{method:'POST',body:JSON.stringify({environment:state.environment,id:$('#research-create-id').value.trim(),name:$('#research-create-name').value.trim(),description:$('#research-create-description').value.trim(),company_technology:$('#research-create-company-tech').value.trim(),...payload})});feedback.textContent=`作成しました: ${result.document_count}件`;$('#research-create-form').reset();$('#research-create-validation').className='csv-validation muted';$('#research-create-validation').textContent='CSVを選択すると事前検証します。';await loadResearches(result.research_id);await refreshResearchManager(result.research_id);toast('リサーチを作成しました');}
  catch(error){feedback.textContent=error.message;}finally{button.disabled=false;}
}

async function uploadResearchCsv(){
  const item=selectedManagedResearch(),button=$('#research-update-submit'),feedback=$('#research-update-feedback');if(!item)return;button.disabled=true;feedback.textContent='検証しています…';
  try{const file=$('#research-update-csv').files[0];const payload=await csvPayload(file);const validated=await api('/api/researches/validate-csv',{method:'POST',body:JSON.stringify({environment:state.environment,...payload})});const result=await api(`/api/researches/${encodeURIComponent(item.id)}/csvs`,{method:'POST',body:JSON.stringify({environment:state.environment,...payload})});const diff=result.diff||{};feedback.textContent=result.already_uploaded?'同じCSVはアップロード済みです':result.active_changed?`最新版へ切替: 追加${diff.added}・継続${diff.continued}・除外${diff.removed}。夜間一括分析で全件再分析が必要です。`:`履歴として保存しました。現在有効なCSVは変更されません。`;feedback.textContent+=` PDF一致${validated.matched_pdfs}・未発見${validated.missing_pdfs}`;$('#research-update-csv').value='';await loadResearches(state.dashboard?.research.id);await refreshResearchManager(item.id);}
  catch(error){feedback.textContent=error.message;}finally{button.disabled=false;}
}

async function setResearchArchived(archived){
  const item=selectedManagedResearch();if(!item)return;if(!confirm(archived?`「${item.name}」をアーカイブしますか？`:`「${item.name}」を使用中へ戻しますか？`))return;await api(`/api/researches/${encodeURIComponent(item.id)}/${archived?'archive':'restore'}`,{method:'POST',body:JSON.stringify({environment:state.environment})});await loadResearches(archived&&state.dashboard?.research.id===item.id?undefined:state.dashboard?.research.id);await refreshResearchManager(item.id);toast(archived?'アーカイブしました':'復元しました');
}

function organizationCounts(){
  return new Map((organizationRegistry().organizations||[]).map(item=>[item.id,Number(item.document_count||0)]));
}
function resetOrganizationGroupForm(){
  state.editingOrganizationGroup=null;$('#organization-group-form').reset();$('#organization-group-id').value='';$('#organization-group-scope').disabled=false;$('#organization-group-delete').classList.add('hidden');$('#organization-group-feedback').textContent='';renderOrganizationGroupManager();
}
function editOrganizationGroup(group){
  state.editingOrganizationGroup=group;$('#organization-group-id').value=group.id;$('#organization-group-name').value=group.name;$('#organization-group-scope').value=group.scope||'common';$('#organization-group-scope').disabled=true;$('#organization-group-note').value=group.note||'';$('#organization-group-verified').checked=!!group.verified;$('#organization-group-delete').classList.remove('hidden');$('#organization-group-feedback').textContent='';renderOrganizationGroupManager();
}
function renderOrganizationGroupManager(){
  const registry=organizationRegistry(),groups=registry.groups||[],selectedIds=new Set(state.editingOrganizationGroup?.member_ids||[]),counts=organizationCounts();
  $('#organization-group-list').innerHTML=groups.length?'':'<div class="organization-empty">登録済みグループはありません</div>';
  groups.slice().sort((a,b)=>a.name.localeCompare(b.name,'ja')).forEach(group=>{const button=document.createElement('button');button.type='button';button.className='organization-group-item';button.classList.toggle('active',state.editingOrganizationGroup?.id===group.id);button.innerHTML=`<strong>${escapeHtml(group.name)}</strong><span class="scope-badge">${group.scope==='common'?'共通':'リサーチ'}</span><small>${(group.member_ids||[]).length}社${group.verified?' · 確認済み':' · 未確認'}</small>`;button.addEventListener('click',()=>editOrganizationGroup(group));$('#organization-group-list').append(button);});
  const organizations=(registry.organizations||[]).slice().sort((a,b)=>(counts.get(b.id)||0)-(counts.get(a.id)||0)||a.name.localeCompare(b.name,'ja'));
  $('#organization-member-list').innerHTML=organizations.map(org=>`<label><input type="checkbox" value="${escapeHtml(org.id)}" ${selectedIds.has(org.id)?'checked':''}><span>${escapeHtml(org.name)} <small>(${counts.get(org.id)||0})</small></span></label>`).join('')||'<div class="organization-empty">企業データがありません</div>';
}
function showOrganizationGroups(){resetOrganizationGroupForm();const dialog=$('#organization-dialog');if(!dialog.open)dialog.showModal();}
async function saveOrganizationGroup(event){
  event.preventDefault();const checked=$$('#organization-member-list input:checked'),registry=organizationRegistry(),names=new Map((registry.organizations||[]).map(item=>[item.id,item.name]));const memberIds=checked.map(input=>input.value);const button=$('#organization-group-form button[type="submit"]');button.disabled=true;
  try{const body={environment:state.environment,action:'save',id:$('#organization-group-id').value||undefined,name:$('#organization-group-name').value,scope:$('#organization-group-scope').value,note:$('#organization-group-note').value,verified:$('#organization-group-verified').checked,member_ids:memberIds,members:memberIds.map(id=>({id,name:names.get(id)||id}))};await api(`/api/researches/${encodeURIComponent(state.dashboard.research.id)}/organization-groups`,{method:'POST',body:JSON.stringify(body)});const researchId=state.dashboard.research.id;await loadDashboard(researchId);renderOrganizationGroupManager();$('#organization-group-feedback').textContent='保存しました';toast('企業グループを保存しました');}
  catch(error){$('#organization-group-feedback').textContent=error.message;}finally{button.disabled=false;}
}
async function deleteOrganizationGroup(){
  const group=state.editingOrganizationGroup;if(!group||!confirm(`「${group.name}」を削除しますか？`))return;const button=$('#organization-group-delete');button.disabled=true;
  try{await api(`/api/researches/${encodeURIComponent(state.dashboard.research.id)}/organization-groups`,{method:'POST',body:JSON.stringify({environment:state.environment,action:'delete',id:group.id,scope:group.scope})});const researchId=state.dashboard.research.id;await loadDashboard(researchId);resetOrganizationGroupForm();toast('企業グループを削除しました');}catch(error){$('#organization-group-feedback').textContent=error.message;}finally{button.disabled=false;}
}

const SIDEBAR_DEFAULT_WIDTH=250,SIDEBAR_MIN_WIDTH=180,SIDEBAR_MAX_WIDTH=500;
function setSidebarWidth(value,persist=true){
  const workspace=$('.workspace'),resizer=$('#sidebar-resizer');
  const viewportLimit=Math.max(SIDEBAR_MIN_WIDTH,window.innerWidth-420);
  const width=Math.round(Math.min(SIDEBAR_MAX_WIDTH,viewportLimit,Math.max(SIDEBAR_MIN_WIDTH,Number(value)||SIDEBAR_DEFAULT_WIDTH)));
  workspace.style.setProperty('--sidebar-width',`${width}px`);resizer.setAttribute('aria-valuenow',String(width));
  if(persist)try{localStorage.setItem('patent-viewer-sidebar-width',String(width));}catch{}
  return width;
}
function initSidebarResizer(){
  const resizer=$('#sidebar-resizer');let dragging=false;
  let saved=SIDEBAR_DEFAULT_WIDTH;try{saved=Number(localStorage.getItem('patent-viewer-sidebar-width'))||SIDEBAR_DEFAULT_WIDTH;}catch{}
  setSidebarWidth(saved,false);
  resizer.addEventListener('pointerdown',event=>{if(window.innerWidth<=720)return;dragging=true;resizer.classList.add('dragging');document.body.classList.add('resizing-sidebar');resizer.setPointerCapture?.(event.pointerId);setSidebarWidth(event.clientX);});
  window.addEventListener('pointermove',event=>{if(dragging)setSidebarWidth(event.clientX);});
  window.addEventListener('pointerup',()=>{if(!dragging)return;dragging=false;resizer.classList.remove('dragging');document.body.classList.remove('resizing-sidebar');});
  resizer.addEventListener('keydown',event=>{const current=Number(resizer.getAttribute('aria-valuenow'))||SIDEBAR_DEFAULT_WIDTH;if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();setSidebarWidth(current+(event.key==='ArrowRight'?10:-10));}else if(event.key==='Home'){event.preventDefault();setSidebarWidth(SIDEBAR_MIN_WIDTH);}else if(event.key==='End'){event.preventDefault();setSidebarWidth(SIDEBAR_MAX_WIDTH);}});
  resizer.addEventListener('dblclick',()=>setSidebarWidth(SIDEBAR_DEFAULT_WIDTH));
  window.addEventListener('resize',()=>setSidebarWidth(Number(resizer.getAttribute('aria-valuenow')),false));
}

function changeYearRange(changed){
  const from=$('#year-from'),to=$('#year-to'),fromYear=Number(from.value),toYear=Number(to.value);
  if(fromYear&&toYear&&fromYear>toYear){if(changed===from)to.value=from.value;else from.value=to.value;}
  applyFilters();
}
function resetFilters(){ $('#year-from').value='';$('#year-to').value='';$('#search-input').value='';$('#technology-cluster-min-size').value='1';state.techClusterMinSize=1;$$('#legal-status-filters input').forEach(x=>x.checked=true);clearCell();applyFilters(); }
function setView(view){
  if(!['threat','technology'].includes(view))return;
  if(state.cellFilter&&state.cellFilter.type!==view)clearCell();
  state.view=view;$$('.tab').forEach(t=>{const active=t.dataset.view===view;t.classList.toggle('active',active);t.setAttribute('aria-pressed',String(active));});
  $('#threat-panel').classList.toggle('hidden',view!=='threat');$('#technology-panel').classList.toggle('hidden',view!=='technology');renderOrganizationRanking();
}
function slug(value){return String(value).normalize('NFKC').replace(/[^A-Za-z0-9_-]+/g,'-').replace(/^-|-$/g,'').slice(0,40)||'none';}
function escapeHtml(value){const d=document.createElement('div');d.textContent=String(value??'');return d.innerHTML;}

function bindEvents(){
  initSidebarResizer();
  $('#research-select').addEventListener('change',e=>loadDashboard(e.target.value));$('#year-from').addEventListener('change',event=>changeYearRange(event.target));$('#year-to').addEventListener('change',event=>changeYearRange(event.target));$('#search-input').addEventListener('input',applyFilters);$$('#legal-status-filters input').forEach(x=>x.addEventListener('change',applyFilters));
  $('#reset-filters').addEventListener('click',resetFilters);$('#clear-cell').addEventListener('click',clearCell);$('#preview-selected').addEventListener('click',()=>{if(state.selected?.pdf_available)openPdf(state.selected);});$('#pdf-close').addEventListener('click',()=>{$('#pdf-frame').src='about:blank';$('#pdf-dialog').close();});$('#save-note').addEventListener('click',saveNote);$('#debug-toggle').addEventListener('click',toggleEnvironment);$('#preflight-button').addEventListener('click',showPreflight);
  $('#patent-list').addEventListener('scroll',event=>{const list=event.currentTarget;if(list.scrollTop+list.clientHeight>=list.scrollHeight-120)loadMoreDocuments();});
  $('#technology-cluster-min-size').addEventListener('change',event=>{state.techClusterMinSize=Math.max(1,Number(event.target.value)||1);if(state.cellFilter?.type==='technology')clearCell();else renderTechnologyMap();});
  $$('[data-trend-mode]').forEach(button=>button.addEventListener('click',()=>setCellTrendMode(button.dataset.trendMode)));
  $$('[data-organization-mode]').forEach(button=>button.addEventListener('click',()=>setOrganizationMode(button.dataset.organizationMode)));$('#organization-clear').addEventListener('click',clearOrganization);$('#organization-groups-manage').addEventListener('click',showOrganizationGroups);$('#organization-group-new').addEventListener('click',resetOrganizationGroupForm);$('#organization-group-form').addEventListener('submit',saveOrganizationGroup);$('#organization-group-delete').addEventListener('click',deleteOrganizationGroup);
  $('#pipeline-button').addEventListener('click',()=>openPipelineFrom($('#pipeline-button')));$('#pipeline-refresh').addEventListener('click',()=>loadPipelineResearch(state.pipelineResearchId));$('#pipeline-research-select').addEventListener('change',event=>loadPipelineResearch(event.target.value));$('#pipeline-confirm').addEventListener('change',updatePipelineAction);$('#pipeline-overwrite').addEventListener('change',()=>{$('#pipeline-confirm').checked=false;updatePipelineAction();});$('#pipeline-prepare').addEventListener('click',()=>startPipeline('prepare',$('#pipeline-prepare')));$('#pipeline-execute').addEventListener('click',()=>processPending($('#pipeline-execute')));$('#pipeline-pause').addEventListener('click',()=>controlPipeline('pause'));$('#pipeline-resume').addEventListener('click',()=>controlPipeline('run'));$('#pipeline-cancel').addEventListener('click',()=>controlPipeline('cancel'));
  $('#research-manage-button').addEventListener('click',showResearchManager);$('#research-create-form').addEventListener('submit',createResearch);$('#research-create-csv').addEventListener('change',event=>{if(event.target.files[0])validateCsvFile(event.target.files[0],'#research-create-validation').catch(()=>{});});$('#research-manage-select').addEventListener('change',renderManagedResearch);$('#research-update-submit').addEventListener('click',uploadResearchCsv);$('#research-archive-selected').addEventListener('click',()=>setResearchArchived(true));$('#research-restore-selected').addEventListener('click',()=>setResearchArchived(false));$('#research-open-selected').addEventListener('click',async()=>{const item=selectedManagedResearch();if(!item)return;$('#research-dialog').close();await loadResearches(item.id);});$('#research-create-name').addEventListener('input',event=>{const id=$('#research-create-id');if(!id.dataset.edited){const ascii=event.target.value.normalize('NFKC').toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,'');id.value=ascii||`research_${new Date().toISOString().replace(/\\D/g,'').slice(0,14)}`;}});$('#research-create-id').addEventListener('input',event=>event.target.dataset.edited='true');
  $('#legal-status-settings').addEventListener('click',showLegalStatusSettings);$('#legal-status-save').addEventListener('click',saveLegalStatusSettings);bindLegalTagInput('#rights-acquired-input','rights_acquired');bindLegalTagInput('#under-examination-input','under_examination');
  $$('[data-close-dialog]').forEach(b=>b.addEventListener('click',()=>document.getElementById(b.dataset.closeDialog).close()));$$('.tab').forEach(t=>t.addEventListener('click',()=>setView(t.dataset.view)));
  $('#bridge-open').addEventListener('click',()=>$('#bridge-panel').classList.add('open'));$('#bridge-close').addEventListener('click',()=>$('#bridge-panel').classList.remove('open'));
  setView(state.view);
}

window.PatentViewer={state,api,applyFilters,openPdf,toast};
bootstrap().catch(error=>{console.error(error);toast(error.message);$('#research-description').textContent=`読み込みエラー: ${error.message}`;});
