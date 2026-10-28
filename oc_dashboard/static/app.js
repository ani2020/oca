
'use strict';

// ── Constants ──────────────────────────────────────────────────────
const NSE_IDX = new Set(['NIFTY','BANKNIFTY','FINNIFTY','MIDCPNIFTY','NIFTYNXT50','SENSEX','NIFTYIT']);
const LB = {
  paper_bgcolor:'transparent',plot_bgcolor:'transparent',
  font:{family:"'JetBrains Mono',monospace",size:10,color:'#7a9cbf'},
  xaxis:{gridcolor:'#1a2840',zerolinecolor:'#243550',tickfont:{size:9}},
  yaxis:{gridcolor:'#1a2840',zerolinecolor:'#243550',tickfont:{size:9}},
  margin:{t:16,r:14,b:40,l:54},
  legend:{bgcolor:'transparent',font:{size:9}},
};
const PC = {displayModeBar:false,responsive:true};
const C = {acc:'#00c8f0',acc2:'#f07000',green:'#10b981',red:'#f43f5e',
           amber:'#f59e0b',pur:'#8b5cf6',muted:'#3d5270'};

let ALL_SYMS = [];
let LOT_SIZES = {};

// ── Utilities ─────────────────────────────────────────────────────
async function api(path){
  const r = await fetch(path);
  if(!r.ok){const t=await r.text();throw new Error(t);}
  return r.json();
}
function fmt(v,d=2){
  if(v==null)return'—';const n=parseFloat(v);if(isNaN(n))return String(v);
  return n.toLocaleString('en-IN',{minimumFractionDigits:d,maximumFractionDigits:d});
}
function fmtL(v){
  if(v==null)return'—';const n=parseFloat(v);if(isNaN(n))return'—';
  if(Math.abs(n)>=1e7)return(n/1e7).toFixed(2)+'Cr';
  if(Math.abs(n)>=1e5)return(n/1e5).toFixed(2)+'L';
  return n.toLocaleString('en-IN');
}
function cls(v){const n=parseFloat(v);if(isNaN(n)||n===0)return'neu';return n>0?'up':'down';}
function sspan(v,d=2){
  const n=parseFloat(v);if(isNaN(n))return'<span class="neu">—</span>';
  const c=n>0?'up':n<0?'down':'neu',p=n>0?'+':'';
  return`<span class="${c}">${p}${fmt(v,d)}</span>`;
}
function pill(sig){
  const m={'Long Build-Up':['pill pill-lb','⬆ LBU'],'Short Build-Up':['pill pill-sb','⬇ SBU'],
           'Short Covering':['pill pill-sc','↩ SC'],'Long Unwinding':['pill pill-lu','↪ LU'],
           'Neutral':['pill pill-n','— NEU']};
  const[c,l]=m[sig]||['pill pill-n',sig];return`<span class="${c}">${l}</span>`;
}
function sentBadge(s){
  if(!s||s==='NA')return'<span class="sent-na">—</span>';
  const bull=s.includes('Bull')||s.includes('Squeeze');
  return`<span class="${bull?'sent-bull':'sent-bear'}">${s}</span>`;
}
function regimeBadge(r){
  if(!r||r==='NA')return'<span class="sent-na">—</span>';
  const cls=r.startsWith('R1')?'regime-r1':r.startsWith('R2')?'regime-r2':r.startsWith('R3')?'regime-r3':'regime-r4';
  return`<span class="regime-pill ${cls}">${r}</span>`;
}
function ivRankBar(rank){
  if(rank==null)return'—';
  const pct=Math.min(100,Math.max(0,rank));
  const col=pct>80?'var(--red)':pct>50?'var(--amber)':'var(--green)';
  return`<div class="iv-rank-bar"><div class="iv-rank-fill" style="width:${pct}%;background:${col}"></div></div> ${fmt(pct,1)}%`;
}
function baqClass(spread, ltp, warnPct, badPct){
  if(!ltp||ltp<=0||spread==null)return'';
  const ratio=spread/ltp*100;
  if(ratio>=badPct) return 'baq-bad';
  if(ratio>=warnPct) return 'baq-warn';
  return 'baq-good';
}

function setLoad(id,msg=''){
  const el=document.getElementById(id);if(!el)return;
  if(el._plotly_initialized){try{Plotly.purge(el)}catch(e){}}
  el._plotly_initialized=false;
  el.innerHTML=`<div class="loading"><div class="spinner"></div>${msg||'Loading…'}</div>`;
}
function setEmpty(id,msg='No data'){
  const el=document.getElementById(id);if(!el)return;
  try{Plotly.purge(el)}catch(e){}
  el._plotly_initialized=false;
  el.innerHTML=`<div class="empty ${msg.startsWith('⚠')?'err':''}">${msg}</div>`;
}
// Convert 'YYYY-MM-DD HH:MM' to 'YYYY-MM-DDTHH:MM' for Plotly range params
function toPlotlyTs(ts){return ts?ts.replace(' ','T'):ts;}

function plot(id,traces,layout,config=PC){
  const el=document.getElementById(id);if(!el)return;
  el.innerHTML='';
  Plotly.newPlot(el,traces,layout,config);
  el._plotly_initialized=true;
}
function populateSel(id,items){
  const el=document.getElementById(id);if(!el)return;
  const prev=el.value;
  el.innerHTML=items.map(i=>`<option value="${i}">${i}</option>`).join('');
  if(items.includes(prev))el.value=prev;
}
function makeTbl(rows,cols){
  if(!rows||!rows.length)return'<div class="empty">No data</div>';
  const heads=cols.map(c=>{
    if(typeof c==='string') return `<th onclick="sortTbl(this)">${c}</th>`;
    const tip = c.help ? ` title="${String(c.help).replace(/"/g,'&quot;')}"` : '';
    return `<th onclick="sortTbl(this)"${tip}>${c.label}</th>`;
  }).join('');
  const keys=cols.map(c=>typeof c==='string'?c:c.key);
  const labels=cols.map(c=>typeof c==='string'?c:c.label);
  const fmts=cols.map(c=>typeof c==='object'&&c.fmt?c.fmt:null);
  const svals=cols.map(c=>typeof c==='object'&&c.sortValue?c.sortValue:null);
  // exportValue override → decoded value for CSV (signals→names, sign→pos/neg, raw nums)
  const exps=cols.map(c=>typeof c==='object'&&c.exportValue?c.exportValue:null);
  const body=rows.map(r=>'<tr>'+keys.map((k,i)=>{
    const v=r[k];const cell=fmts[i]?fmts[i](v,r):(v==null?'—':v);
    const sv=svals[i]?` data-sort="${svals[i](v,r)}"`:'';
    // data-export: decoded value for CSV; default to raw v (not the formatted cell)
    let ev = exps[i] ? exps[i](v,r) : (v==null?'':v);
    ev = String(ev).replace(/"/g,'&quot;');
    return`<td${sv} data-export="${ev}">${cell}</td>`;
  }).join('')+'</tr>').join('');
  // stash column labels on the table for the CSV exporter (header row + order)
  const hdr = labels.map(l=>String(l).replace(/"/g,'&quot;')).join('\u0001');
  return`<table data-cols="${hdr}"><thead><tr>${heads}</tr></thead><tbody>${body}</tbody></table>`;
}

// ── CSV export (current filtered + sorted view, with headers) ──
function exportTableCsv(tableEl, filename){
  if(!tableEl){ return; }
  const colHdr = tableEl.getAttribute('data-cols')||'';
  const headers = colHdr ? colHdr.split('\u0001') : [];
  const esc = s => {
    s = (s==null) ? '' : String(s);
    return /[",\n]/.test(s) ? '"'+s.replace(/"/g,'""')+'"' : s;
  };
  const lines = [];
  if(headers.length) lines.push(headers.map(esc).join(','));
  // only VISIBLE rows (respects ticker filter), in DOM order (respects sort)
  tableEl.querySelectorAll('tbody tr').forEach(tr=>{
    if(tr.style.display === 'none') return;
    const cells = Array.from(tr.children).map(td=>{
      let v = td.getAttribute('data-export');
      if(v==null) v = td.textContent.trim();
      return esc(v);
    });
    lines.push(cells.join(','));
  });
  const csv = '\ufeff' + lines.join('\r\n');   // BOM for Excel unicode (₹)
  const blob = new Blob([csv], {type:'text/csv;charset=utf-8;'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename || 'export.csv';
  document.body.appendChild(a); a.click();
  document.body.removeChild(a); URL.revokeObjectURL(url);
}

// Wire a CSV button: finds the table inside containerId, exports it
function csvBtn(containerId, filenameFn){
  const cont = document.getElementById(containerId);
  const tbl = cont ? cont.querySelector('table') : null;
  if(!tbl){ alert('No table to export'); return; }
  const fn = (typeof filenameFn==='function') ? filenameFn() : (filenameFn||'export.csv');
  exportTableCsv(tbl, fn);
}
function sortTbl(th){
  const tbl=th.closest('table');
  const idx=Array.from(th.parentNode.children).indexOf(th);
  const asc=th.dataset.asc!=='1';th.dataset.asc=asc?'1':'0';
  const rows=Array.from(tbl.querySelectorAll('tbody tr'));
  // Numeric value for a cell, most reliable source first:
  //   1. data-sort     explicit override (e.g. signal severity)
  //   2. data-export   the RAW value makeTbl already stashes for CSV
  //   3. visible text  scaled for Indian magnitude suffixes
  // (3) alone used to be the fallback and it silently dropped the suffix:
  // fmtL renders 181325 as "1.81L", the old strip produced 1.81, and every
  // value above a lakh therefore sorted BELOW every value under one. SPAN
  // MARGIN, LOT PREM and RISK all use fmtL, so all three were wrong.
  const numOf=(td)=>{
    const ds=td.getAttribute('data-sort');
    if(ds!=null){ const n=parseFloat(ds); if(!isNaN(n)) return n; }
    const de=td.getAttribute('data-export');
    if(de!=null && de!==''){ const n=parseFloat(de); if(!isNaN(n)) return n; }
    const t=td.textContent.trim();
    const m=t.replace(/[,\u20b9%+\s]/g,'').match(/^(-?\d*\.?\d+)(Cr|L|K)?$/i);
    if(!m) return NaN;
    const mult={cr:1e7,l:1e5,k:1e3}[(m[2]||'').toLowerCase()]||1;
    return parseFloat(m[1])*mult;
  };
  rows.sort((a,b)=>{
    const ac=a.children[idx], bc=b.children[idx];
    const an=numOf(ac), bn=numOf(bc);
    if(!isNaN(an)&&!isNaN(bn)) return asc?an-bn:bn-an;
    // Non-numeric: fall back to text. Blanks always sort last either way.
    const av=ac.textContent.trim(), bv=bc.textContent.trim();
    if(av==='—'||av==='') return 1;
    if(bv==='—'||bv==='') return -1;
    return asc?av.localeCompare(bv):bv.localeCompare(av);
  });
  rows.forEach(r=>tbl.querySelector('tbody').appendChild(r));
}

// ── Toggle groups ─────────────────────────────────────────────────
function wireToggleGroup(id){
  const grp=document.getElementById(id);if(!grp)return;
  grp.addEventListener('click',e=>{
    const btn=e.target.closest('button');if(!btn)return;
    grp.querySelectorAll('button').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
  });
}
['ovFilterGroup','gexFilterGroup','oiFilterGroup','ivFilterGroup',
 'moverFilterGroup','volShockFilterGroup','ivShockFilterGroup','dsFilterGroup',
 'esViewGroup','volFilterGroup','volGateGroup']
  .forEach(wireToggleGroup);
function getFilter(id){
  return document.querySelector('#'+id+' .active')?.dataset.ft||'all';
}

// ── Subtabs ──────────────────────────────────────────────────────
document.querySelectorAll('.subtab').forEach(tab=>{
  tab.addEventListener('click',()=>{
    if(tab.dataset.stab==='oi-flow') refreshFlowExpiries().catch(()=>{});
    if(tab.dataset.stab==='oi-walls-tab') loadOiWalls();
    const container=tab.closest('.view');
    container.querySelectorAll('.subtab').forEach(t=>t.classList.remove('active'));
    container.querySelectorAll('.subtab-panel').forEach(p=>p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('stab-'+tab.dataset.stab).classList.add('active');
  });
});

// ── Navigation ────────────────────────────────────────────────────
document.querySelectorAll('.nav-btn').forEach(btn=>{
  btn.addEventListener('click',()=>{
    document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
    document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('view-'+btn.dataset.view).classList.add('active');
    // History opened directly via the top nav has no origin → hide BACK.
    // (jumpHistory re-shows it right after its own .click() call.)
    if(btn.dataset.view==='history' && typeof _hsShowBack==='function') _hsShowBack(null);
    if(!btn.dataset.init){
      btn.dataset.init='1';
      const fn=VIEW_INIT[btn.dataset.view];
      if(fn)fn();
    }
  });
});
document.querySelectorAll('.tab').forEach(tab=>{
  tab.addEventListener('click',()=>{
    const view=tab.closest('.view');
    view.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    view.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-'+tab.dataset.tab).classList.add('active');
  });
});

// ── Settings ──────────────────────────────────────────────────────
const SETTINGS_DEFAULTS={
  stgIvMin:0.5,stgIvMax:150,stgIvZscore:2.0,
  stgDsMinOi:0,stgDsMinVol:0,stgBaqWarn:5,stgBaqBad:15,
  stgIvLookback:90,stgGammaRange:5,stgMagnetThr:0.75,stgLambda:0.1,
  stgMinOiChange:10,  // hide OI signals rows where |ce+pe oi change| < this
  stgBktAtm:0.50, stgBktNear:0.30, stgBktFar:0.15, stgBktDeep:0.05,
  stgBktStable:1.0, stgBktCross:5.0,
};
function loadSettings(){
  try{
    const s=JSON.parse(localStorage.getItem('oc_settings')||'{}');
    Object.entries({...SETTINGS_DEFAULTS,...s}).forEach(([k,v])=>{
      const el=document.getElementById(k);if(el)el.value=v;
    });
  }catch(e){}
}
function saveSettings(){
  const s={};
  Object.keys(SETTINGS_DEFAULTS).forEach(k=>{const el=document.getElementById(k);if(el)s[k]=parseFloat(el.value)||0;});
  localStorage.setItem('oc_settings',JSON.stringify(s));
  const msg=document.getElementById('settingsSaved');
  if(msg){msg.style.display='';setTimeout(()=>msg.style.display='none',1500);}
}
function resetSettings(){localStorage.removeItem('oc_settings');loadSettings();}
function getSetting(k){
  try{const s=JSON.parse(localStorage.getItem('oc_settings')||'{}');
    return s[k]!=null?s[k]:SETTINGS_DEFAULTS[k];}catch(e){return SETTINGS_DEFAULTS[k];}
}

// ── ICICI ─────────────────────────────────────────────────────────
function openIciciModal(){document.getElementById('iciciModal').classList.add('open');}
function closeIciciModal(){document.getElementById('iciciModal').classList.remove('open');}
document.getElementById('iciciModal').addEventListener('click',e=>{if(e.target===e.currentTarget)closeIciciModal();});
async function saveIciciToken(){
  const token=document.getElementById('iciciTokenInput').value.trim();
  const msg=document.getElementById('iciciMsg');
  if(!token){msg.textContent='⚠ Token is required';msg.style.color='var(--red)';return;}
  msg.textContent='Connecting…';msg.style.color='var(--muted)';
  try{
    const r=await fetch('/api/icici/configure',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({session_token:token})});
    const d=await r.json();
    if(r.ok){msg.textContent='✓ '+d.status;msg.style.color='var(--green)';
      updateIciciStatus();setTimeout(closeIciciModal,1200);}
    else{msg.textContent='✗ '+(d.detail||'Error');msg.style.color='var(--red)';}
  }catch(e){msg.textContent='✗ '+e.message;msg.style.color='var(--red)';}
}
async function updateIciciStatus(){
  try{
    const d=await api('/api/icici/status');
    const badge=document.getElementById('iciciStatusBadge');
    if(d.configured){badge.textContent='ICICI: ON';badge.classList.add('ok');}
    else{badge.textContent='ICICI: OFF';badge.classList.remove('ok');}
  }catch(e){}
}

// ── Cache helpers ─────────────────────────────────────────────────
async function refreshDbCache(){
  const btn=document.getElementById('cacheRefreshBtn');
  btn.textContent='↻ …';btn.style.color='var(--amber)';
  try{
    await fetch('/api/cache/refresh',{method:'POST'});
    btn.textContent='✓ DB';btn.style.color='var(--green)';
    await initSymbols();
    await loadDsTimestamps();  // refresh option screener timestamp
    loadCascade('plSymbol','plExpiry','plTimestamp').catch(()=>{});  // refresh premium lens timestamp
    loadOverview().catch(()=>{});
  }catch(e){btn.textContent='✗ DB';btn.style.color='var(--red)';}
  setTimeout(()=>{btn.textContent='↻ DB';btn.style.color='';},3000);
}
async function refreshMarginCache(){
  const btn=document.getElementById('marginRefreshBtn');
  btn.textContent='↻ …';btn.style.color='var(--amber)';
  try{
    const r=await fetch('/api/icici/margin/refresh',{method:'POST'});
    const d=await r.json();
    btn.textContent='✓ MARGIN';btn.style.color='var(--green)';
    console.log('Margin cache cleared:',d.cleared,'entries');
  }catch(e){btn.textContent='✗ MARGIN';btn.style.color='var(--red)';}
  setTimeout(()=>{btn.textContent='↻ MARGIN';btn.style.color='';},3000);
}

// ── Cascade helpers ───────────────────────────────────────────────
async function loadCascade(symId,expId,tsId,futureOnly=true){
  const sym=document.getElementById(symId)?.value;if(!sym)return;
  try{
    const expVal=expId?document.getElementById(expId)?.value:'';
    const calls=[
      expId?api(`/api/expiries?symbol=${encodeURIComponent(sym)}&future_only=${futureOnly}`):Promise.resolve([]),
      tsId ?api(`/api/timestamps?symbol=${encodeURIComponent(sym)}${expVal?'&expiry='+encodeURIComponent(expVal):''}`):Promise.resolve([]),
    ];
    const[exps]=await Promise.all(calls.slice(0,1).concat([Promise.resolve([])]));
    if(expId&&exps.length)populateSel(expId,exps);
    // Reload timestamps AFTER expiry is populated so expiry value is current
    if(tsId){
      const selExp=expId?document.getElementById(expId)?.value:'';
      const tsUrl=`/api/timestamps?symbol=${encodeURIComponent(sym)}`+(selExp?`&expiry=${encodeURIComponent(selExp)}`:'');
      try{const tss=await api(tsUrl);if(tss.length)populateSel(tsId,tss);}catch(e){}
    }
  }catch(e){console.error('loadCascade:',symId,e.message);}
}
async function reloadTimestamps(symId,expId,tsId){
  const sym=document.getElementById(symId)?.value;if(!sym||!tsId)return;
  const exp=expId?document.getElementById(expId)?.value:'';
  try{
    const url=`/api/timestamps?symbol=${encodeURIComponent(sym)}`+(exp?`&expiry=${encodeURIComponent(exp)}`:'');
    const tss=await api(url);
    if(tss.length)populateSel(tsId,tss);
  }catch(e){}
}
async function reloadExpiries(prefix){
  const symId=prefix+'Symbol',expId=prefix+'Expiry';
  const sym=document.getElementById(symId)?.value;if(!sym)return;
  const showPast=document.getElementById(prefix+'ExpiryShowPast')?.checked;
  try{
    const exps=await api(`/api/expiries?symbol=${encodeURIComponent(sym)}&future_only=${!showPast}`);
    if(exps.length)populateSel(expId,exps);
  }catch(e){}
}

// ── Symbol init ───────────────────────────────────────────────────
async function initSymbols(){
  try{
    ALL_SYMS=await api('/api/symbols');
    ['gexSymbol','oiSymbol','ivSymbol','trendSymbol','plSymbol'].forEach(id=>populateSel(id,ALL_SYMS));
    const[,lotData]=await Promise.all([
      Promise.all([
        loadCascade('gexSymbol','gexExpiry','gexTimestamp'),
        loadCascade('ivSymbol','ivExpiry','ivTimestamp'),
        loadCascade('trendSymbol','trendExpiry','trendTimestamp'),
    ]),
    // Populate EOD history expiry selector on init using a reliable index
    Promise.resolve().then(async()=>{
      const refSym=(['NIFTY','BANKNIFTY'].find(s=>ALL_SYMS.includes(s)))||ALL_SYMS[0];
      if(refSym){
        const exps=await api(`/api/expiries?symbol=${encodeURIComponent(refSym)}&future_only=false`).catch(()=>[]);
        if(exps.length)populateSel('oiHistExpiry',exps);
      }
    }),
    api('/api/lot_sizes').catch(()=>({})),
    ]);
    LOT_SIZES=lotData||{};
    await loadAtmStrikes();
    await loadDsTimestamps();
  }catch(e){console.error('initSymbols:',e);}
}

// ── Event listeners ───────────────────────────────────────────────
document.getElementById('gexSymbol').addEventListener('change',()=>loadCascade('gexSymbol','gexExpiry','gexTimestamp'));
document.getElementById('gexExpiry').addEventListener('change',()=>reloadTimestamps('gexSymbol','gexExpiry','gexTimestamp'));
document.getElementById('ivSymbol').addEventListener('change',()=>loadCascade('ivSymbol','ivExpiry','ivTimestamp'));
document.getElementById('ivExpiry').addEventListener('change',()=>reloadTimestamps('ivSymbol','ivExpiry','ivTimestamp'));
document.getElementById('trendSymbol').addEventListener('change',async()=>{
  await loadCascade('trendSymbol','trendExpiry',null);
  await loadAtmStrikes();
});
document.getElementById('trendExpiry').addEventListener('change',()=>loadAtmStrikes());
document.getElementById('trendAtmDist').addEventListener('change',()=>{
  const opt=document.getElementById('trendAtmDist').selectedOptions[0];if(!opt)return;
  document.getElementById('trendStrike').value=opt.dataset.strike||'';
  document.getElementById('trendStrikeDisplay').textContent='@ '+opt.dataset.strike;
});
document.getElementById('oiFilterGroup').addEventListener('click',e=>{
  const ft=e.target.dataset.ft;if(!ft)return;
  populateSel('oiSymbol',ALL_SYMS.filter(s=>ft==='index'?NSE_IDX.has(s):ft==='stock'?!NSE_IDX.has(s):true));
});
document.getElementById('oiSymbol')?.addEventListener('change',async()=>{
  refreshFlowExpiries().catch(()=>{});
  // Also refresh EOD history expiry selector
  const sym=document.getElementById('oiSymbol')?.value;
  if(!sym) return;
  try{
    const exps=await api(`/api/expiries?symbol=${encodeURIComponent(sym)}&future_only=false`);
    populateSel('oiHistExpiry',exps);
  }catch(e){}
});
document.getElementById('gexFilterGroup').addEventListener('click',e=>{
  const ft=e.target.dataset.ft;if(!ft)return;
  populateSel('gexSymbol',ALL_SYMS.filter(s=>ft==='index'?NSE_IDX.has(s):ft==='stock'?!NSE_IDX.has(s):true));
  loadCascade('gexSymbol','gexExpiry','gexTimestamp');
});
document.getElementById('ivFilterGroup').addEventListener('click',e=>{
  const ft=e.target.dataset.ft;if(!ft)return;
  populateSel('ivSymbol',ALL_SYMS.filter(s=>ft==='index'?NSE_IDX.has(s):ft==='stock'?!NSE_IDX.has(s):true));
  loadCascade('ivSymbol','ivExpiry','ivTimestamp');
});
document.getElementById('deltaSlider').addEventListener('input',()=>{
  document.getElementById('deltaVal').textContent=document.getElementById('deltaSlider').value;updateDsRangeLabel();
});
document.getElementById('deltaMinSlider').addEventListener('input',()=>{
  document.getElementById('deltaMinVal').textContent=document.getElementById('deltaMinSlider').value;updateDsRangeLabel();
});
document.getElementById('btnRefreshOverview').addEventListener('click',loadOverview);
document.getElementById('ovFilterGroup').addEventListener('click',()=>setTimeout(loadOverview,50));
document.getElementById('btnGex').addEventListener('click',loadGex);
document.getElementById('btnOi').addEventListener('click',loadOi);
document.getElementById('btnVolShock').addEventListener('click',loadVolShock);
document.getElementById('btnIvShock').addEventListener('click',loadIvShock);
document.getElementById('btnIv').addEventListener('click',loadIv);
document.getElementById('btnMovers').addEventListener('click',loadMovers);
document.getElementById('btnTrend').addEventListener('click',loadTrend);
document.getElementById('btnDs').addEventListener('click',loadDeltaScreener);
document.getElementById('btnFetchMargin').addEventListener('click',fetchDsMargin);
document.getElementById('btnStr')?.addEventListener('click',loadStraddleScreener);
wireToggleGroup('strFilterGroup');
document.getElementById('btnSg')?.addEventListener('click',loadStrangleScreener);
wireToggleGroup('sgFilterGroup');
document.getElementById('sgDelta')?.addEventListener('input',e=>_sgSyncDelta(parseInt(e.target.value,10)));
document.querySelectorAll('#sgDeltaGroup button').forEach(b=>
  b.addEventListener('click',()=>_sgSyncDelta(parseInt(b.dataset.dt,10))));

// ── ATM strikes ───────────────────────────────────────────────────
async function loadAtmStrikes(){
  const sym=document.getElementById('trendSymbol')?.value;
  const exp=document.getElementById('trendExpiry')?.value;
  const sel=document.getElementById('trendAtmDist');
  if(!sym||!exp||!sel)return;
  try{
    const rows=await api(`/api/atm_strikes?symbol=${encodeURIComponent(sym)}&expiry=${encodeURIComponent(exp)}`);
    if(!rows.length)return;
    sel.innerHTML=rows.map(r=>`<option value="${r.distance_from_atm}" data-strike="${r.strike_price}">${r.distance_from_atm>=0?'+':''}${r.distance_from_atm} (${r.strike_price})</option>`).join('');
    const atm=[...sel.options].find(o=>parseFloat(o.value)===0)||sel.options[0];
    if(atm){sel.value=atm.value;document.getElementById('trendStrike').value=atm.dataset.strike;
      document.getElementById('trendStrikeDisplay').textContent='@ '+atm.dataset.strike;}
  }catch(e){}
}

// ── DS timestamps ─────────────────────────────────────────────────
async function loadDsTimestamps(){
  if(!ALL_SYMS.length)return;
  // Use NIFTY if available (most timestamps), else first index, else first symbol.
  // This gives timestamps that reflect actual OC import cadence rather than
  // an arbitrary stock that may have fewer snapshots.
  const preferred=['NIFTY','BANKNIFTY','FINNIFTY'];
  const sym=preferred.find(s=>ALL_SYMS.includes(s))||ALL_SYMS.find(s=>NSE_IDX.has(s))||ALL_SYMS[0];
  try{
    const tss=await api(`/api/timestamps?symbol=${encodeURIComponent(sym)}`);
    if(tss.length){
      populateSel('dsTimestamp',tss);
      // Show which symbol the timestamps came from
      const lbl=document.getElementById('dsTimestampSym');
      if(lbl)lbl.textContent=`(${sym})`;
    }
  }catch(e){console.warn('loadDsTimestamps:',e.message);}
}

// ── OVERVIEW ──────────────────────────────────────────────────────
async function loadOverview(){
  setLoad('overviewCards');setLoad('allOiTable');
  loadOverviewMeta();
  try{
    const ft=getFilter('ovFilterGroup');
    const[data,sigs]=await Promise.all([api('/api/overview'),api('/api/oi_signals_all')]);
    const filtered=data.filter(d=>ft==='index'?NSE_IDX.has(d.symbol):ft==='stock'?!NSE_IDX.has(d.symbol):true);
    const cont=document.getElementById('overviewCards');
    if(!filtered.length){cont.innerHTML='<div class="empty">No data — check DB or filter</div>';}
    else{
      cont.innerHTML=filtered.map(d=>{
        const pcr=d.pcr!=null?fmt(d.pcr,3):'—';
        const pc=d.pcr>1.2?'up':d.pcr<0.8?'down':'';
        const rv_iv = d.rv_iv_spread!=null ? `<div class="tc-key">RV-IV</div><div class="tc-val ${d.rv_iv_spread>0?'up':'down'}">${sspan(d.rv_iv_spread,1)}</div>` : '';
        return`<div class="tc" onclick="jumpGex('${d.symbol}')">
          <div class="tc-sym">${d.symbol}</div>
          <div class="tc-spot">${fmt(d.spot,2)}</div>
          <div class="tc-row">
            <div><div class="tc-key">PCR</div><div class="tc-val ${pc}">${pcr}</div></div>
            <div><div class="tc-key">OI-WTD IV</div><div class="tc-val">${d.oi_wtd_iv!=null?fmt(d.oi_wtd_iv,1)+'%':fmt(d.avg_ce_iv,1)+'%'}</div></div>
            <div><div class="tc-key">NET GEX</div><div class="tc-val ${d.net_gex>0?'up':d.net_gex<0?'down':'neu'}">${fmtL(d.net_gex)}</div></div>
          </div>
          <div class="tc-row" style="margin-top:5px">
            <div><div class="tc-key">CE OI</div><div class="tc-val">${fmtL(d.total_ce_oi)}</div></div>
            <div><div class="tc-key">PE OI</div><div class="tc-val">${fmtL(d.total_pe_oi)}</div></div>
            ${rv_iv?'<div>'+rv_iv+'</div>':''}
          </div>
          ${(d.exp_move_straddle>0||d.exp_move_theoretical>0)?`
          <div class='tc-row' style='margin-top:4px;border-top:1px solid var(--border);padding-top:4px'>
            <div><div class='tc-key'>EXP MOVE STR</div><div class='tc-val acc'>${d.exp_move_straddle>0?fmt(d.exp_move_straddle,1):'—'}</div></div>
            <div><div class='tc-key'>EXP MOVE TH</div><div class='tc-val'>${d.exp_move_theoretical>0?fmt(d.exp_move_theoretical,1):'—'}</div></div>
          </div>`:''}
        </div>`;
      }).join('');
    }
    const sigFilt=sigs.filter(d=>ft==='index'?NSE_IDX.has(d.symbol):ft==='stock'?!NSE_IDX.has(d.symbol):true);
    document.getElementById('allOiTable').innerHTML=makeTbl(sigFilt,[
      {key:'symbol',label:'SYMBOL'},
      {key:'ce_oi_chg',label:'CE OI Δ',fmt:v=>sspan(v,0)},
      {key:'pe_oi_chg',label:'PE OI Δ',fmt:v=>sspan(v,0)},
      {key:'avg_ce_ltp_chg',label:'CE LTP Δ',fmt:v=>sspan(v)},
      {key:'avg_pe_ltp_chg',label:'PE LTP Δ',fmt:v=>sspan(v)},
      {key:'total_oi_chg',label:'ABS OI Δ',fmt:v=>fmtL(v)},
    ]);
  }catch(e){setEmpty('overviewCards','⚠ '+e.message);setEmpty('allOiTable','⚠ '+e.message);}
}
function jumpGex(sym){
  document.querySelector('[data-view="gex"]').click();
  setTimeout(()=>{const s=document.getElementById('gexSymbol');
    if([...s.options].some(o=>o.value===sym)){s.value=sym;loadCascade('gexSymbol','gexExpiry','gexTimestamp');}},80);
}

// ── GEX ───────────────────────────────────────────────────────────
// ── Net GEX chart: per-strike bars + dealer gamma profile overlay ──
// The BARS are each strike's net dealer gamma at today's price; where they change
// colour is the OI CROSSOVER (call OI overtakes put OI). The LINE is the dealer
// gamma profile: total dealer gamma re-evaluated at each hypothetical price
// across the whole chain. The GAMMA FLIP is where the LINE crosses zero, not
// where the bars change colour (e.g. MCX 2026-10-01: bars cross ~3252, flip
// 3006, because the 3300-3800 call walls outweigh the 2900-3250 put walls when
// gamma is ~+/-400 points wide).
function _oiCrossover(strikes, net, ref){
  let best=null;
  for(let i=0;i<strikes.length-1;i++){
    const a=net[i], b=net[i+1];
    if(!a || !b || (a>0)===(b>0)) continue;
    const x=strikes[i]+(strikes[i+1]-strikes[i])*(0-a)/(b-a);
    if(ref==null || best==null || Math.abs(x-ref)<Math.abs(best-ref)) best=x;
  }
  return best;
}
function _netGexWithProfile(strikes, netG, gp){
  const tr=[{name:'Net GEX per strike',x:strikes,y:netG,type:'bar',
             marker:{color:netG.map(v=>v>=0?C.green:C.red),opacity:.8},
             hovertemplate:'strike %{x}<br>net %{y:.2f}<extra></extra>'}];
  const prof=(gp&&gp.profile)||[];
  const dg=prof.map(r=>r.dealer_gamma_billions);
  if(prof.length && dg.some(v=>v!=null)){
    tr.push({name:'Dealer γ profile (total at each price)',x:prof.map(r=>r.level),y:dg,
             type:'scatter',mode:'lines',yaxis:'y2',line:{color:C.acc,width:2.5},
             hovertemplate:'price %{x:.0f}<br>dealer γ %{y:.4f}<extra></extra>'});
  }
  return tr;
}
function _netGexLayout(strikes, netG, gp){
  const ref = gp && gp.fut!=null ? gp.fut : (gp && gp.spot);
  const shapes=[], ann=[];
  const vline=(x,col,dash,label,yshift)=>{
    if(x==null || !isFinite(x)) return;
    shapes.push({type:'line',x0:x,x1:x,y0:0,y1:1,yref:'paper',line:{color:col,width:1.8,dash}});
    ann.push({x,y:1,yref:'paper',text:label,showarrow:false,yanchor:'bottom',yshift:yshift||0,
              font:{size:9,color:col}});
  };
  const flip = gp && gp.gamma_flip;
  vline(flip, C.red, 'dot', 'γ flip '+(flip!=null?fmt(flip,0):''), 0);
  const oc=_oiCrossover(strikes, netG, ref);
  vline(oc, C.amber, 'dashdot', 'OI crossover '+(oc!=null?fmt(oc,0):''), 12);
  vline(ref, C.acc2, 'dash', 'FUT '+(ref!=null?fmt(ref,0):''), 24);
  // Symmetric ranges on both axes so the two zero lines coincide.
  const m1=Math.max(1e-9, ...netG.map(v=>Math.abs(v||0)))*1.1;
  const dg=((gp&&gp.profile)||[]).map(r=>r.dealer_gamma_billions).filter(v=>v!=null&&isFinite(v));
  const m2=dg.length ? Math.max(1e-12, ...dg.map(Math.abs))*1.1 : 1;
  return {...LB, shapes, annotations:ann, margin:{...LB.margin,t:48,r:58},
    xaxis:{...LB.xaxis,title:'Strike / price',tickangle:-45},
    yaxis:{...LB.yaxis,title:'Net GEX per strike (₹M)',range:[-m1,m1],zeroline:true},
    yaxis2:{...LB.yaxis,title:'Dealer γ profile',overlaying:'y',side:'right',
            range:[-m2,m2],showgrid:false,zeroline:false},
    legend:{...LB.legend,orientation:'h',y:-0.28}};
}

async function loadGex(){
  const sym=document.getElementById('gexSymbol').value;
  const exp=document.getElementById('gexExpiry').value;
  const ts =document.getElementById('gexTimestamp').value;
  const rng=parseFloat(document.getElementById('gexRange').value)||5;
  if(!sym||!exp){alert('Select symbol and expiry');return;}
  ['gexBarChart','gammaProfileChart','netGexChart'].forEach(id=>setLoad(id));
  {const _gk=document.getElementById('gexKpis');if(_gk)_gk.innerHTML='';
   const _gg=document.getElementById('gexGaKpis');if(_gg)_gg.innerHTML='';
   const _gd=document.getElementById('gexGaDetail');if(_gd)_gd.innerHTML='';}
  try{
    const[gex,gp]=await Promise.all([
      api(`/api/gex?symbol=${encodeURIComponent(sym)}&expiry=${encodeURIComponent(exp)}&timestamp=${encodeURIComponent(ts)}`),
      api(`/api/gamma_profile?symbol=${encodeURIComponent(sym)}&expiry=${encodeURIComponent(exp)}&timestamp=${encodeURIComponent(ts)}&price_range_pct=${rng}`),
    ]);
    const strikes=gex.map(r=>r.strike_price);
    const ceG=gex.map(r=>r.ce_gexv||0),peG=gex.map(r=>r.pe_gexv||0),netG=gex.map(r=>r.net_gexv||0);
    const atm=gex[0]?.atm_strike,spot=gex[0]?.spot;
    const shapes=[
      {type:'line',x0:atm,x1:atm,y0:0,y1:1,yref:'paper',line:{color:C.amber,width:1.5,dash:'dot'}},
      {type:'line',x0:spot,x1:spot,y0:0,y1:1,yref:'paper',line:{color:C.acc,width:1,dash:'dash'}},
    ];
    plot('gexBarChart',[
      {name:'CE GEX',x:strikes,y:ceG,type:'bar',marker:{color:C.green,opacity:.75}},
      {name:'PE GEX',x:strikes,y:peG,type:'bar',marker:{color:C.red,opacity:.75}},
      {name:'Net GEX',x:strikes,y:netG,type:'scatter',mode:'lines',line:{color:C.acc,width:2}},
    ],{...LB,barmode:'relative',shapes,xaxis:{...LB.xaxis,title:'Strike',tickangle:-45},yaxis:{...LB.yaxis,title:'GEX (₹M)'}});
    plot('netGexChart',_netGexWithProfile(strikes,netG,gp),
      _netGexLayout(strikes,netG,gp));
    if(gp?.profile?.length){
      const prof=gp.profile,lvl=prof.map(r=>r.level);
      const cg=prof.map(r=>r.call_gamma_billions),pg=prof.map(r=>r.put_gamma_billions),tg=prof.map(r=>r.total_gamma_billions);
      // True dealer gamma profile Gamma(level): total dealer gamma re-evaluated at
      // each hypothetical price across the whole chain. Its zero IS the flip.
      const dg=prof.map(r=>r.dealer_gamma_billions);
      const hasDg=dg.some(v=>v!=null);
      const pshapes=[], pann=[];
      if(gp.gamma_flip!=null){
        pshapes.push({type:'line',x0:gp.gamma_flip,x1:gp.gamma_flip,y0:0,y1:1,yref:'paper',line:{color:C.red,width:2,dash:'dot'}});
        pann.push({x:gp.gamma_flip,y:1,yref:'paper',text:'γ flip '+fmt(gp.gamma_flip,0),showarrow:false,yanchor:'bottom',font:{size:9,color:C.red}});
      }
      if(gp.fut!=null)pshapes.push({type:'line',x0:gp.fut,x1:gp.fut,y0:0,y1:1,yref:'paper',line:{color:C.acc2,width:1.5,dash:'dash'}});
      if(gp.spot!=null)pshapes.push({type:'line',x0:gp.spot,x1:gp.spot,y0:0,y1:1,yref:'paper',line:{color:C.acc,width:1,dash:'dot'}});
      if(gp.magnet)pshapes.push({type:'rect',x0:gp.magnet.lower,x1:gp.magnet.upper,y0:0,y1:1,yref:'paper',fillcolor:'rgba(0,200,240,.05)',line:{width:0}});
      const ptraces=[
        {name:'Call GEX (strike)',x:lvl,y:cg,type:'scatter',mode:'lines',fill:'tozeroy',line:{color:C.green,width:1},fillcolor:'rgba(16,185,129,.08)'},
        {name:'Put GEX (strike)', x:lvl,y:pg,type:'scatter',mode:'lines',fill:'tozeroy',line:{color:C.red,  width:1},fillcolor:'rgba(244,63,94,.08)'},
        {name:'Net per strike (OI layout)',x:lvl,y:tg,type:'scatter',mode:'lines',line:{color:C.muted||'#888',width:1,dash:'dash'}},
      ];
      if(hasDg) ptraces.push({name:'Dealer γ profile',x:lvl,y:dg,type:'scatter',mode:'lines',line:{color:C.acc,width:2.5},
        hovertemplate:'price %{x:.0f}<br>dealer γ %{y:.4f}<extra>Dealer γ profile</extra>'});
      plot('gammaProfileChart',ptraces,{...LB,shapes:pshapes,annotations:pann,
        xaxis:{...LB.xaxis,title:'Price Level'},yaxis:{...LB.yaxis,title:'GEX (₹B)',zeroline:true}});
      // Regime is the SIGN OF THE DEALER PROFILE AT FUTURES (stored as
      // profile_regime), not "fut above flip": with 2+ zeros, or heavy ITM put OI
      // above price, the profile can be inverted and the flip sit on either side.
      const _ref = gp.fut!=null?gp.fut:gp.spot;
      const _pr = gp.profile_regime||'';
      const regime=_pr.indexOf('negative')>=0?'Negative Gamma':(_pr==='mixed'?'Mixed (near flip)':
                   (_pr?'Positive Gamma':(_ref>=(gp.gamma_flip||0)?'Positive Gamma':'Negative Gamma')));
      const _basis=(gp.fut!=null&&gp.spot!=null)?(gp.fut-gp.spot):null;
      const _dist =(gp.gamma_flip!=null&&_ref!=null)?(_ref-gp.gamma_flip):null;
      const _sgn=v=>(v>=0?'+':'')+fmt(v,1);
      const _gpos=gp.gamma_position;
      document.getElementById('gexKpis').innerHTML=[
        {label:'SPOT',value:fmt(gp.spot),sub:'underlying'},
        {label:'FUT',value:gp.fut!=null?fmt(gp.fut):'—',
         sub:_basis!=null?_sgn(_basis)+' basis':'',subcls:_basis>=0?'up':'down',
         tip:'Futures price. The gamma flip and the regime are measured against '
            +'FUT, not spot — same convention as the exposure screener. When the '
            +'basis is wide, spot can sit the other side of the flip; that is '
            +'expected, not an error.'},
        {label:'GAMMA FLIP',value:gp.gamma_flip!=null?fmt(gp.gamma_flip,0):'—',
         sub:regime+(_dist!=null?' · fut '+_sgn(_dist):''),
         subcls:regime==='Positive Gamma'?'up':(regime==='Negative Gamma'?'down':''),
         tip:'Zero-gamma level: the price at which TOTAL dealer gamma, re-evaluated '
            +'across the whole chain at that price, changes sign (nearest to FUT). It '
            +'is where the Dealer \u03b3 profile line crosses zero \u2014 NOT where the '
            +'per-strike bars change colour (that is the OI crossover, where call OI '
            +'overtakes put OI). Same value as the exposure screener and history.'},
        {label:'γ POS',value:_gpos!=null?(_gpos>=0?'+':'')+fmt(_gpos,2):'—',
         sub:_gpos==null?'':(_gpos>=0.4?'deep long γ':_gpos>=0?'shallow long γ':_gpos>-0.4?'shallow short γ':'deep short γ'),
         subcls:_gpos==null?'':(_gpos>=0?'up':'down'),
         tip:'Dealer gamma at FUT / largest |gamma| on the profile, in [-1, +1]. How '
            +'deep in the regime price sits: lower values have been followed by '
            +'larger next-day moves.'},
        {label:'MAGNET CTR',value:gp.magnet?fmt(gp.magnet.center,0):'—',sub:gp.magnet?fmt(gp.magnet.lower,0)+'–'+fmt(gp.magnet.upper,0):''},
        {label:'STRENGTH',value:gp.magnet?fmt(gp.magnet.strength,4):'—',sub:'₹B'},
      ].map(k=>`<div class="kpi"${k.tip?` title="${k.tip.replace(/"/g,'&quot;')}"`:''}><div class="kpi-label">${k.label}</div><div class="kpi-val acc">${k.value}</div><div class="kpi-sub ${k.subcls||''}">${k.sub}</div></div>`).join('');
    }else setEmpty('gammaProfileChart','No gamma profile data');
    loadGammaAnalysis(sym,exp,ts,rng);
    loadMaxPain(sym,exp,ts);
    loadMmDelta(sym,exp,ts);
    renderGexTable(gex);
  }catch(e){['gexBarChart','gammaProfileChart','netGexChart'].forEach(id=>setEmpty(id,'⚠ '+e.message));}
}

async function loadGammaAnalysis(sym,exp,ts,rng){
  const boxes=document.getElementById('gexGaKpis');
  const detail=document.getElementById('gexGaDetail');
  if(boxes) boxes.innerHTML='<div class="kpi"><div class="kpi-label">GAMMA ANALYSIS</div><div class="kpi-val" style="font-size:11px">loading…</div></div>';
  api(`/api/gamma_analysis?symbol=${encodeURIComponent(sym)}&expiry=${encodeURIComponent(exp)}&timestamp=${encodeURIComponent(ts)}&price_range_pct=${rng}`)
  .then(ga=>{
    const pos=ga.regime==='Positive Gamma';
    // Secondary stat boxes — gamma-analysis values folded in as boxes (#11b)
    const kpi=(lbl,val,cls='')=>`<div class="kpi"><div class="kpi-label">${lbl}</div>
      <div class="kpi-val ${cls}">${val||'—'}</div></div>`;
    if(boxes){
      boxes.innerHTML=[
        kpi('REGIME', ga.regime, pos?'up':'down'),
        kpi('γ@SPOT', fmt(ga.gamma_at_spot,4)),
        kpi('ATM IV', ga.atm_iv_pct!=null?ga.atm_iv_pct+'%':'—'),
        kpi('EXP RANGE', ga.expected_range!=null?'±'+fmt(ga.expected_range,1):'—'),
        kpi('γ-ADJ ATR', ga.gamma_adj_atr!=null?fmt(ga.gamma_adj_atr,1):'—'),
        kpi('TREND', ga.trend, 'acc'),
        kpi('BULL BREAK', ga.bullish_break!=null?fmt(ga.bullish_break,0):'—','up'),
        kpi('BEAR BREAK', ga.bearish_break!=null?fmt(ga.bearish_break,0):'—','down'),
      ].join('');
    }
    // Compact structures + warnings below the boxes
    if(detail){
      const structs=ga.structures?.length?ga.structures.map(s=>`<span class="ga-chip">${s}</span>`).join(''):'';
      const warns=ga.warnings?.length?ga.warnings.map(w=>`<span class="ga-chip warn">⚠ ${w}</span>`).join(''):'';
      detail.innerHTML=(structs||warns)?
        `<div style="display:flex;flex-wrap:wrap;gap:4px;align-items:center">
           ${ga.behavior?`<span style="font-family:var(--mono);font-size:9px;color:var(--muted);margin-right:6px">${ga.behavior}</span>`:''}
           ${structs}${warns}</div>`:'';
    }
  })
  .catch(e=>{ if(boxes) boxes.innerHTML=`<div class="kpi"><div class="kpi-val" style="font-size:10px;color:var(--red)">⚠ ${e.message}</div></div>`; });
}


// ── MAX PAIN ──────────────────────────────────────────────────────
async function loadMaxPain(sym,exp,ts){
  ['maxPainChart','maxPainSeriesChart'].forEach(id=>setLoad(id));
  document.getElementById('maxPainKpis').innerHTML='';
  try{
    const[mp,mps]=await Promise.all([
      api(`/api/max_pain?symbol=${encodeURIComponent(sym)}&expiry=${encodeURIComponent(exp)}&timestamp=${encodeURIComponent(ts)}`),
      api(`/api/max_pain_series?symbol=${encodeURIComponent(sym)}&expiry=${encodeURIComponent(exp)}`),
    ]);
    // KPIs
    document.getElementById('maxPainKpis').innerHTML=[
      {label:'MAX PAIN',value:fmt(mp.max_pain_strike,0),sub:'optimal expiry price for writers'},
      {label:'SPOT',value:fmt(mp.spot,2),sub:''},
      {label:'DISTANCE',value:fmt(mp.distance_pts,0)+' pts',sub:fmt(mp.distance_pct,2)+'% from spot'},
    ].map(k=>`<div class="kpi"><div class="kpi-label">${k.label}</div><div class="kpi-val acc">${k.value}</div><div class="kpi-sub">${k.sub}</div></div>`).join('');

    // Pain curve chart
    const prices=mp.pain_curve.map(r=>r.price);
    const totalPain=mp.pain_curve.map(r=>r.total_pain);
    const mpLine=Array(prices.length).fill(null);
    const mpIdx=prices.indexOf(mp.max_pain_strike);
    if(mpIdx>=0)mpLine[mpIdx]=totalPain[mpIdx];
    plot('maxPainChart',[
      {name:'Call Pain',x:prices,y:mp.pain_curve.map(r=>r.call_pain),type:'bar',marker:{color:C.green,opacity:.6}},
      {name:'Put Pain', x:prices,y:mp.pain_curve.map(r=>r.put_pain), type:'bar',marker:{color:C.red,  opacity:.6}},
      {name:'Total',    x:prices,y:totalPain,type:'scatter',mode:'lines',line:{color:C.acc,width:2}},
    ],{...LB,barmode:'stack',
      shapes:[{type:'line',x0:mp.max_pain_strike,x1:mp.max_pain_strike,y0:0,y1:1,yref:'paper',line:{color:C.amber,width:2,dash:'dot'}},
              {type:'line',x0:mp.spot,x1:mp.spot,y0:0,y1:1,yref:'paper',line:{color:C.acc,width:1,dash:'dash'}}],
      xaxis:{...LB.xaxis,title:'Strike'},yaxis:{...LB.yaxis,title:'Intrinsic Loss (₹M)'}});

    // Time series
    if(mps.series?.length){
      plot('maxPainSeriesChart',[
        {name:'Max Pain',x:mps.series.map(r=>r.timestamp),y:mps.series.map(r=>r.max_pain_strike),
          type:'scatter',mode:'lines+markers',line:{color:C.amber,width:2},marker:{size:4}},
        {name:'Spot',x:mps.series.map(r=>r.timestamp),y:mps.series.map(r=>r.spot),
          type:'scatter',mode:'lines',line:{color:C.acc,width:1.5,dash:'dash'}},
      ],{...LB,xaxis:{...LB.xaxis,title:'Timestamp'},yaxis:{...LB.yaxis,title:'Strike / Price'}});
    }else setEmpty('maxPainSeriesChart','Only one timestamp available');
  }catch(e){['maxPainChart','maxPainSeriesChart'].forEach(id=>setEmpty(id,'⚠ '+e.message));}
}

// ── MM DELTA ──────────────────────────────────────────────────────
async function loadMmDelta(sym,exp,ts){
  ['mmDeltaChart','vannaChart'].forEach(id=>setLoad(id));
  document.getElementById('mmDeltaKpis').innerHTML='';
  document.getElementById('mmDeltaTable').innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  try{
    const d=await api(`/api/delta_oi?symbol=${encodeURIComponent(sym)}&expiry=${encodeURIComponent(exp)}&timestamp=${encodeURIComponent(ts)}`);
    document.getElementById('mmDeltaKpis').innerHTML=[
      {label:'NET MM DELTA',value:fmt(d.net_delta_oi,4),sub:d.interpretation,subcls:d.net_delta_oi<-0.5?'down':d.net_delta_oi>0.5?'up':'neu'},
      {label:'CE DELTA OI',value:fmt(d.ce_delta_oi,4),sub:'₹M lots',subcls:'up'},
      {label:'PE DELTA OI',value:fmt(d.pe_delta_oi,4),sub:'₹M lots',subcls:'down'},
      {label:'NET FLOW',value:fmt(d.net_flow,2),sub:'net_flow',subcls:d.net_flow>0?'up':'down'},
      {label:'NET VANNA',value:fmt(d.net_vanna_ex,4),sub:'vanna exposure'},
      {label:'NET CHARM',value:fmt(d.net_charm_ex,4),sub:'charm exposure'},
    ].map(k=>`<div class="kpi"><div class="kpi-label">${k.label}</div><div class="kpi-val ${k.subcls||'acc'}">${k.value}</div><div class="kpi-sub">${k.sub||''}</div></div>`).join('');
    const strikes=d.rows.map(r=>r.strike_price);
    const netDoi=d.rows.map(r=>r.net_delta_oi);
    plot('mmDeltaChart',[{
      name:'Net MM Delta OI',x:strikes,y:netDoi,type:'bar',
      marker:{color:netDoi.map(v=>v>=0?C.green:C.red),opacity:.8},
    }],{...LB,xaxis:{...LB.xaxis,title:'Strike',tickangle:-45},yaxis:{...LB.yaxis,title:'Net Delta OI (₹M)'}});
    plot('vannaChart',[
      {name:'CE Delta OI',x:strikes,y:d.rows.map(r=>r.ce_delta_oi),type:'bar',marker:{color:C.green,opacity:.7}},
      {name:'PE Delta OI',x:strikes,y:d.rows.map(r=>r.pe_delta_oi),type:'bar',marker:{color:C.red,opacity:.7}},
    ],{...LB,barmode:'group',xaxis:{...LB.xaxis,title:'Strike',tickangle:-45},yaxis:{...LB.yaxis,title:'Delta OI (₹M)'}});
    document.getElementById('mmDeltaTable').innerHTML=makeTbl(d.rows,[
      {key:'strike_price',label:'STRIKE'},
      {key:'ce_delta',label:'CE Δ',fmt:v=>fmt(v,3)},
      {key:'ce_nd2',label:'CE P(ITM)',fmt:v=>v>0?fmt(v,3):'—'},
      {key:'pe_delta',label:'PE Δ',fmt:v=>fmt(v,3)},
      {key:'pe_nd2',label:'PE P(ITM)',fmt:v=>v>0?fmt(v,3):'—'},
      {key:'ce_oi',label:'CE OI',fmt:v=>fmtL(v)},
      {key:'pe_oi',label:'PE OI',fmt:v=>fmtL(v)},
      {key:'ce_delta_oi',label:'CE ΔOI ₹M',fmt:v=>fmt(v,3)},
      {key:'pe_delta_oi',label:'PE ΔOI ₹M',fmt:v=>fmt(v,3)},
      {key:'net_delta_oi',label:'NET ΔOI ₹M',fmt:v=>sspan(v,3)},
    ]);
  }catch(e){['mmDeltaChart','vannaChart'].forEach(id=>setEmpty(id,'⚠ '+e.message));
    document.getElementById('mmDeltaTable').innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;}
}

// ── OI ────────────────────────────────────────────────────────────
async function loadOi(){
  const sym=document.getElementById('oiSymbol').value;if(!sym){alert('Select a symbol');return;}
  const minOiChg=getSetting('stgMinOiChange')||1;
  ['ceOiChart','peOiChart'].forEach(id=>setLoad(id));
  document.getElementById('oiSignalTable').innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  try{
    const data=await api(`/api/oi_change?symbol=${encodeURIComponent(sym)}&min_oi_change=${minOiChg}`);
    if(!data.length){
      ['ceOiChart','peOiChart'].forEach(id=>setEmpty(id,'No significant OI changes'));
      document.getElementById('oiSignalTable').innerHTML='<div class="empty">No significant OI changes above threshold</div>';
      return;
    }
    // Sort by strike for charts
    const chartData=[...data].sort((a,b)=>a.strike_price-b.strike_price);
    const strikes=chartData.map(r=>r.strike_price);
    const ceChg=chartData.map(r=>r.ce_oi_chg||0);
    const peChg=chartData.map(r=>r.pe_oi_chg||0);
    const ax={...LB,
      xaxis:{...LB.xaxis,title:'Strike',tickangle:-45},
      yaxis:{...LB.yaxis,title:'OI Change (NSE intraday delta)'},
    };
    plot('ceOiChart',[{name:'CE OI Δ',x:strikes,y:ceChg,type:'bar',
      marker:{color:ceChg.map(v=>v>=0?C.green:C.red),opacity:.8}}],ax);
    plot('peOiChart',[{name:'PE OI Δ',x:strikes,y:peChg,type:'bar',
      marker:{color:peChg.map(v=>v>=0?C.green:C.red),opacity:.8}}],ax);
    // Table sorted by magnitude (already sorted server-side)
    document.getElementById('oiSignalTable').innerHTML=makeTbl(data,[
      {key:'strike_price',label:'STRIKE'},{key:'expiry',label:'EXPIRY'},
      {key:'timestamp',label:'TIMESTAMP'},
      {key:'ce_oi_chg',label:'CE OI Δ',fmt:v=>sspan(v,0)},
      {key:'ce_oi',label:'CE OI',fmt:v=>fmtL(v)},
      {key:'ce_vol_oi',label:'CE V/OI',fmt:v=>v!=null?fmt(v,3):'—'},
      {key:'ce_prem_oi_chg',label:'CE ₹FLOW',fmt:v=>v!=null?sspan(v,0):'—'},
      {key:'ce_signal',label:'CE SIG',fmt:v=>pill(v)},
      {key:'pe_oi_chg',label:'PE OI Δ',fmt:v=>sspan(v,0)},
      {key:'pe_oi',label:'PE OI',fmt:v=>fmtL(v)},
      {key:'pe_vol_oi',label:'PE V/OI',fmt:v=>v!=null?fmt(v,3):'—'},
      {key:'pe_prem_oi_chg',label:'PE ₹FLOW',fmt:v=>v!=null?sspan(v,0):'—'},
      {key:'pe_signal',label:'PE SIG',fmt:v=>pill(v)},
    ]);
  }catch(e){['ceOiChart','peOiChart'].forEach(id=>setEmpty(id,'⚠ '+e.message));
    document.getElementById('oiSignalTable').innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;}
}

// ── SHOCKERS ──────────────────────────────────────────────────────
async function loadVolShock(){
  const n=document.getElementById('volShockerN').value||30;
  const ft=getFilter('volShockFilterGroup');
  setLoad('volShockChart');
  document.getElementById('volShockTable').innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  try{
    const data=await api(`/api/volume_shockers?top_n=${n}&filter_type=${ft}`);
    if(!data.length){setEmpty('volShockChart','No volume spikes detected');
      document.getElementById('volShockTable').innerHTML='<div class="empty">No data</div>';return;}
    plot('volShockChart',[{name:'Vol Δ',x:data.map(r=>r.vol_delta||0),y:data.map(r=>`${r.symbol} ${r.strike_price}`),
      type:'bar',orientation:'h',marker:{color:C.acc2,opacity:.8}}],
      {...LB,yaxis:{...LB.yaxis,automargin:true},xaxis:{...LB.xaxis,title:'Volume Change'},
       height:Math.max(280,data.length*22),margin:{...LB.margin,l:140}});
    document.getElementById('volShockTable').innerHTML=makeTbl(data,[
      {key:'symbol',label:'SYMBOL'},{key:'strike_price',label:'STRIKE'},{key:'expiry',label:'EXPIRY'},
      {key:'vol_now',label:'VOL NOW',fmt:v=>fmtL(v)},{key:'vol_prev',label:'VOL PREV',fmt:v=>fmtL(v)},
      {key:'vol_delta',label:'DELTA',fmt:v=>sspan(v,0)},{key:'vol_pct_chg',label:'% CHG',fmt:v=>sspan(v,1)},
    ]);
  }catch(e){setEmpty('volShockChart','⚠ '+e.message);document.getElementById('volShockTable').innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;}
}

async function loadIvShock(){
  const n=document.getElementById('ivShockerN').value||30;
  const ft=getFilter('ivShockFilterGroup');
  setLoad('ivShockChart');
  document.getElementById('ivShockTable').innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  try{
    const data=await api(`/api/iv_shockers?top_n=${n}&filter_type=${ft}`);
    if(!data.length){setEmpty('ivShockChart','No IV changes detected');
      document.getElementById('ivShockTable').innerHTML='<div class="empty">No data</div>';return;}
    const ivd=data.map(r=>r.iv_delta||0);
    plot('ivShockChart',[{name:'IV Δ',x:ivd,y:data.map(r=>`${r.symbol} ${r.strike_price}`),
      type:'bar',orientation:'h',marker:{color:ivd.map(v=>v>=0?C.red:C.green),opacity:.8}}],
      {...LB,yaxis:{...LB.yaxis,automargin:true},xaxis:{...LB.xaxis,title:'IV Change (%)'},
       height:Math.max(280,data.length*22),margin:{...LB.margin,l:140}});
    document.getElementById('ivShockTable').innerHTML=makeTbl(data,[
      {key:'symbol',label:'SYMBOL'},{key:'strike_price',label:'STRIKE'},{key:'expiry',label:'EXPIRY'},
      {key:'iv_now',label:'IV NOW',fmt:v=>fmt(v,2)+'%'},{key:'iv_prev',label:'IV PREV',fmt:v=>fmt(v,2)+'%'},
      {key:'iv_delta',label:'Δ IV',fmt:v=>sspan(v)},
    ]);
  }catch(e){setEmpty('ivShockChart','⚠ '+e.message);document.getElementById('ivShockTable').innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;}
}

// ── IV ANALYSIS ───────────────────────────────────────────────────
async function loadIv(){
  const sym=document.getElementById('ivSymbol').value;
  const exp=document.getElementById('ivExpiry').value;
  const ts =document.getElementById('ivTimestamp').value;
  if(!sym||!exp){alert('Select symbol and expiry');return;}
  // Load all three sub-panels in parallel
  await Promise.all([
    loadIvSmile(sym,exp,ts),
    loadTermStructure(sym,ts),
    loadPcSkew(sym,exp,ts),
  ]);
}

async function loadIvSmile(sym,exp,ts){
  ['ceSmileChart','peSmileChart'].forEach(id=>setLoad(id));
  document.getElementById('ivRankKpis').innerHTML='';
  document.getElementById('ivAnomalyTable').innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  try{
    const lookback=getSetting('stgIvLookback');
    const[data,ivr]=await Promise.all([
      api(`/api/iv_smile?symbol=${encodeURIComponent(sym)}&expiry=${encodeURIComponent(exp)}&timestamp=${encodeURIComponent(ts)}`),
      api(`/api/iv_rank?symbol=${encodeURIComponent(sym)}&expiry=${encodeURIComponent(exp)}&timestamp=${encodeURIComponent(ts)}&lookback_days=${lookback}`).catch(()=>null),
    ]);
    // IV Rank KPIs
    if(ivr){
      // iv_rank is built on a constant-maturity series from exposure_eod.
      // `method` is 'cm30' (rank0/rank1 interpolated to 30d) or 'band' (rank0
      // restricted to a DTE window when rank-1 is unavailable). When the series
      // is too thin to rank honestly the backend sends null — say why rather
      // than showing a blank bar.
      const thin = ivr.iv_rank==null && ivr.hist_count<(ivr.min_samples||20);
      const rankSub = thin
        ? `only ${ivr.hist_count} samples (need ${ivr.min_samples||20})`
        : `${ivr.hist_count} samples / ${lookback}d · ${ivr.method||'—'}`;
      document.getElementById('ivRankKpis').innerHTML=[
        {label:'ATM IV',value:fmt(ivr.atm_iv,2)+'%',sub:'live intraday'},
        {label:'IV RANK',value:thin?'—':ivRankBar(ivr.iv_rank),sub:rankSub},
        {label:'IV PCTILE',value:ivr.iv_pctile!=null?fmt(ivr.iv_pctile,1)+'%':'—',
         sub:'% of days cheaper'},
        {label:'RANKED IV',value:ivr.ranked_iv!=null?fmt(ivr.ranked_iv,2)+'%':'—',
         sub:ivr.iv_min!=null?`range ${fmt(ivr.iv_min,1)}–${fmt(ivr.iv_max,1)}`:''},
        {label:'REALISED VOL',value:fmt(ivr.rv,2)+'%',sub:'m_volatility (annualised)'},
        {label:'RV–IV SPREAD',value:sspan(ivr.rv_iv_spread,2),sub:ivr.rv_iv_spread>0?'options cheap':'options expensive'},
      ].map(k=>`<div class="kpi"><div class="kpi-label">${k.label}</div><div class="kpi-val acc">${k.value}</div><div class="kpi-sub">${k.sub}</div></div>`).join('');
    }
    const strikes=data.map(r=>r.strike_price),spot=data[0]?.spot||0;
    const spotShapes=[{type:'line',x0:spot,x1:spot,y0:0,y1:1,yref:'paper',line:{color:C.amber,width:1,dash:'dash'}}];
    function renderSmile(elId,ivKey,fitKey,anomKey,lc){
      const iv=data.map(r=>r[ivKey]>0?r[ivKey]:null);
      const fit=data.map(r=>r[fitKey]);
      const aS=strikes.filter((_,i)=>data[i][anomKey]);
      const aIv=data.filter((_,i)=>data[i][anomKey]).map(r=>r[ivKey]);
      plot(elId,[
        {name:'IV',x:strikes,y:iv,type:'scatter',mode:'markers',marker:{color:C.muted,size:5,opacity:.9}},
        {name:'Spline',x:strikes,y:fit,type:'scatter',mode:'lines',line:{color:lc,width:2}},
        {name:'Anomaly',x:aS,y:aIv,type:'scatter',mode:'markers',marker:{color:C.acc2,size:10,symbol:'x',line:{width:2}}},
      ],{...LB,shapes:spotShapes,xaxis:{...LB.xaxis,title:'Strike'},yaxis:{...LB.yaxis,title:'IV (%)'}});
    }
    renderSmile('ceSmileChart','ce_iv','ce_iv_fit','ce_anomaly',C.green);
    renderSmile('peSmileChart','pe_iv','pe_iv_fit','pe_anomaly',C.red);
    const anomRows=data.filter(r=>r.ce_anomaly||r.pe_anomaly);
    document.getElementById('ivAnomalyTable').innerHTML=makeTbl(anomRows,[
      {key:'strike_price',label:'STRIKE'},
      {key:'ce_iv',label:'CE IV',fmt:v=>fmt(v,2)+'%'},{key:'ce_iv_fit',label:'CE FIT',fmt:v=>v!=null?fmt(v,2)+'%':'—'},
      {key:'ce_zscore',label:'CE Z',fmt:v=>sspan(v)},
      {key:'pe_iv',label:'PE IV',fmt:v=>fmt(v,2)+'%'},{key:'pe_iv_fit',label:'PE FIT',fmt:v=>v!=null?fmt(v,2)+'%':'—'},
      {key:'pe_zscore',label:'PE Z',fmt:v=>sspan(v)},
    ]);
  }catch(e){['ceSmileChart','peSmileChart'].forEach(id=>setEmpty(id,'⚠ '+e.message));}
}

async function loadTermStructure(sym,ts){
  setLoad('termStructureChart');
  document.getElementById('termKpis').innerHTML='';
  document.getElementById('termTable').innerHTML='';
  try{
    const d=await api(`/api/iv_term_structure?symbol=${encodeURIComponent(sym)}&timestamp=${encodeURIComponent(ts)}`);
    const rows=d.rows;
    if(!rows?.length){setEmpty('termStructureChart','No term structure data');return;}
    const exps=rows.map(r=>r.expiry);
    const dte=rows.map(r=>r.dte);
    // Contango/backwardation: compare slope
    const isContango=rows.length>1&&rows[rows.length-1].mid_iv>rows[0].mid_iv;
    document.getElementById('termKpis').innerHTML=[
      {label:'NEAR ATM IV',value:fmt(rows[0]?.mid_iv,2)+'%',sub:rows[0]?.expiry||''},
      {label:'FAR ATM IV',value:fmt(rows[rows.length-1]?.mid_iv,2)+'%',sub:rows[rows.length-1]?.expiry||''},
      {label:'STRUCTURE',value:isContango?'CONTANGO':'BACKWARDATION',
       sub:isContango?'Far IV > Near IV (normal)':'Near IV > Far IV (event risk)',
       subcls:isContango?'up':'down'},
    ].map(k=>`<div class="kpi"><div class="kpi-label">${k.label}</div><div class="kpi-val acc">${k.value}</div><div class="kpi-sub ${k.subcls||''}">${k.sub}</div></div>`).join('');
    plot('termStructureChart',[
      {name:'CE ATM IV',x:exps,y:rows.map(r=>r.ce_iv),type:'scatter',mode:'lines+markers',
        line:{color:C.green,width:2},marker:{size:6}},
      {name:'PE ATM IV',x:exps,y:rows.map(r=>r.pe_iv),type:'scatter',mode:'lines+markers',
        line:{color:C.red,  width:2},marker:{size:6}},
      {name:'Mid IV',   x:exps,y:rows.map(r=>r.mid_iv),type:'scatter',mode:'lines',
        line:{color:C.acc,  width:1.5,dash:'dot'}},
    ],{...LB,xaxis:{...LB.xaxis,title:'Expiry',tickangle:-30},yaxis:{...LB.yaxis,title:'ATM IV (%)'}});
    document.getElementById('termTable').innerHTML=makeTbl(rows,[
      {key:'expiry',label:'EXPIRY'},{key:'dte',label:'DTE',fmt:v=>fmt(v,0)},
      {key:'ce_iv',label:'CE ATM IV',fmt:v=>fmt(v,2)+'%'},
      {key:'pe_iv',label:'PE ATM IV',fmt:v=>fmt(v,2)+'%'},
      {key:'mid_iv',label:'MID IV',fmt:v=>fmt(v,2)+'%'},
      {key:'skew',label:'SKEW (PE-CE)',fmt:v=>sspan(v,2)},
    ]);
  }catch(e){setEmpty('termStructureChart','⚠ '+e.message);}
}

async function loadPcSkew(sym,exp,ts){
  setLoad('rrSeriesChart');
  document.getElementById('skewKpis').innerHTML='';
  document.getElementById('skewSignalCard').innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  try{
    const d=await api(`/api/pc_skew?symbol=${encodeURIComponent(sym)}&expiry=${encodeURIComponent(exp)}&timestamp=${encodeURIComponent(ts)}`);
    document.getElementById('skewKpis').innerHTML=[
      {label:'25Δ RISK REVERSAL',value:d.rr_25d!=null?sspan(d.rr_25d):'-',sub:'CE IV – PE IV at 25Δ · neg = puts bid'},
      {label:'25Δ PUT IV',value:d.pe_25d_iv!=null?fmt(d.pe_25d_iv,2)+'%':'—',sub:d.pe_25d_strike?'@ '+d.pe_25d_strike:''},
      {label:'25Δ CALL IV',value:d.ce_25d_iv!=null?fmt(d.ce_25d_iv,2)+'%':'—',sub:d.ce_25d_strike?'@ '+d.ce_25d_strike:''},
      {label:'LEFT WING SLOPE',value:d.left_wing_slope!=null?fmt(d.left_wing_slope,4):'—',sub:'put wing · IV pts per 1% spot'},
      {label:'RIGHT WING SLOPE',value:d.right_wing_slope!=null?fmt(d.right_wing_slope,4):'—',sub:'call wing · IV pts per 1% spot'},
      {label:'SKEW ASYMMETRY',value:d.skew_asymmetry!=null?sspan(d.skew_asymmetry,4):'—',sub:d.skew_asymmetry>0?'put skew dominant':'call skew dominant'},
    ].map(k=>`<div class="kpi"><div class="kpi-label">${k.label}</div><div class="kpi-val acc">${k.value}</div><div class="kpi-sub">${k.sub||''}</div></div>`).join('');
    // Signal card
    document.getElementById('skewSignalCard').innerHTML=`
      <div style="display:flex;flex-direction:column;gap:10px">
        <div><div class="ga-label">RISK REVERSAL (ATM)</div>
          <div class="kpi-val acc" style="font-size:24px">${fmt(d.riskreversal,4)}</div></div>
        <div><div class="ga-label">SENTIMENT</div>${sentBadge(d.sentiment)}</div>
        <div><div class="ga-label">REGIME</div>${regimeBadge(d.regime)}</div>
        <div><div class="ga-label">ATM STRIKE</div>
          <span style="font-family:var(--mono);font-size:13px;color:var(--acc)">${fmt(d.atm_strike,0)}</span></div>
      </div>`;
    // RR time series
    if(d.rr_series?.length){
      const rrTs=d.rr_series.map(r=>r.ts);
      const rrVals=d.rr_series.map(r=>r.riskreversal);
      // Set x-axis range to just the data's own min/max — avoids year-spanning axis
      // when DB contains historical rows with old timestamps
      const tsMin=rrTs[0], tsMax=rrTs[rrTs.length-1];
      plot('rrSeriesChart',[
        {name:'Risk Reversal',x:rrTs,y:rrVals,
          type:'scatter',mode:'lines+markers',
          line:{color:C.pur,width:2},marker:{size:4},
          fill:'tozeroy',fillcolor:'rgba(139,92,246,.08)'},
      ],{...LB,
        shapes:[{type:'line',x0:tsMin,x1:tsMax,
          y0:0,y1:0,line:{color:C.muted,width:1,dash:'dot'}}],
        xaxis:{...LB.xaxis,title:'Time',
               range:[tsMin.replace(' ','T'), tsMax.replace(' ','T')],
               tickformat:'%H:%M',tickangle:-30},
        yaxis:{...LB.yaxis,title:'Risk Reversal (PE IV – CE IV)'}});
    }else setEmpty('rrSeriesChart','Only one timestamp available');
  }catch(e){setEmpty('rrSeriesChart','⚠ '+e.message);
    document.getElementById('skewSignalCard').innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;}
}

// ── MOVERS ────────────────────────────────────────────────────────
async function loadMovers(){
  const side=document.getElementById('moverSide').value;
  const n=document.getElementById('moverN').value||20;
  const ft=getFilter('moverFilterGroup');
  ['gainersChart','losersChart'].forEach(id=>setLoad(id));
  ['gainersTable','losersTable'].forEach(id=>{document.getElementById(id).innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';});
  try{
    const d=await api(`/api/top_movers?side=${side}&top_n=${n}&filter_type=${ft}`);
    const cols=[{key:'symbol',label:'SYMBOL'},{key:'strike_price',label:'STRIKE'},{key:'expiry',label:'EXPIRY'},
      {key:'ltp_now',label:'LTP',fmt:v=>fmt(v)},{key:'ltp_chg',label:'Δ LTP',fmt:v=>sspan(v)},{key:'ltp_pct_chg',label:'% CHG',fmt:v=>sspan(v,1)}];
    function moverChart(elId,rows,isGain){
      plot(elId,[{name:isGain?'Gain':'Loss',x:rows.map(r=>`${r.symbol} ${r.strike_price}`),y:rows.map(r=>r.ltp_chg||0),
        type:'bar',marker:{color:isGain?C.green:C.red,opacity:.8}}],
        {...LB,xaxis:{...LB.xaxis,tickangle:-45,automargin:true},yaxis:{...LB.yaxis,title:'LTP Change'}});
    }
    moverChart('gainersChart',d.gainers,true);moverChart('losersChart',d.losers,false);
    document.getElementById('gainersTable').innerHTML=makeTbl(d.gainers,cols);
    document.getElementById('losersTable').innerHTML=makeTbl(d.losers,cols);
  }catch(e){['gainersChart','losersChart'].forEach(id=>setEmpty(id,'⚠ '+e.message));}
}

// ── STRIKE TREND ──────────────────────────────────────────────────


// ── DELTA SCREENER ────────────────────────────────────────────────
let _dsCurrentRows=[];
let _dsSessionLoaded=false;  // true only after LOAD button clicked this session
function updateDsRangeLabel(){
  const hi=document.getElementById('deltaSlider').value;
  const lo=document.getElementById('deltaMinSlider').value;
  const lbl=document.getElementById('dsRangeLabel');
  if(lbl)lbl.textContent=lo+'–'+hi;
}
async function loadDeltaScreener(){
  const ts=document.getElementById('dsTimestamp')?.value||'';
  const tgtDlt=document.getElementById('deltaSlider').value;
  const minDlt=document.getElementById('deltaMinSlider').value;
  const ft=getFilter('dsFilterGroup');
  // Liquidity floors and expiry are now applied SERVER-side, per leg. They used
  // to be a client-side re-filter of already-fetched rows using the settings
  // values, which meant illiquid legs were transferred and then hidden — and
  // the SQL gate was a row-level OR, so a dead call rode along with a liquid put.
  const expiry=document.getElementById('dsExpiry')?.value||'all';
  const minOi=Math.max(0,parseFloat(document.getElementById('dsMinOi')?.value)||0);
  const minVol=Math.max(0,parseFloat(document.getElementById('dsMinVol')?.value)||0);
  updateDsRangeLabel();
  document.getElementById('dsTable').innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  document.getElementById('dsKpis').innerHTML='';
  try{
    const tsParam=ts?`&timestamp=${encodeURIComponent(ts)}`:'';
    const expParam=(expiry&&expiry!=='all')?`&expiry=${encodeURIComponent(expiry)}`:'';
    const res=await api(`/api/delta_screener?target_delta=${tgtDlt}&min_delta=${minDlt}&filter_type=${ft}`
      +`&min_oi=${minOi}&min_volume=${minVol}${expParam}${tsParam}`);
    _dsCurrentRows=res.rows||[];
    _dsSessionLoaded=true;
    const visible=_dsCurrentRows;
    const tgtN=parseFloat(tgtDlt)/100;
    const ceRows=visible.filter(r=>r.option_type==='CE');
    const peRows=visible.filter(r=>r.option_type==='PE');
    const bestCe=ceRows.length?ceRows.reduce((p,c)=>Math.abs(c.delta-tgtN)<Math.abs(p.delta-tgtN)?c:p):null;
    const bestPe=peRows.length?peRows.reduce((p,c)=>Math.abs(c.delta-tgtN)<Math.abs(p.delta-tgtN)?c:p):null;
    document.getElementById('dsKpis').innerHTML=[
      {label:'CE ROWS',value:ceRows.length,sub:'in delta range'},
      {label:'PE ROWS',value:peRows.length,sub:'in delta range'},
      bestCe?{label:'BEST CE',value:fmt(bestCe.ltp),sub:bestCe.symbol+' '+bestCe.strike_price+' Δ'+fmt(bestCe.delta,3)}:null,
      bestPe?{label:'BEST PE',value:fmt(bestPe.ltp),sub:bestPe.symbol+' '+bestPe.strike_price+' Δ'+fmt(bestPe.delta,3)}:null,
    ].filter(Boolean).map(k=>`<div class="kpi"><div class="kpi-label">${k.label}</div><div class="kpi-val acc">${k.value}</div><div class="kpi-sub">${k.sub}</div></div>`).join('');
    renderDsTable(visible);
  }catch(e){document.getElementById('dsTable').innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;}
}
function renderDsTable(rows){
  if(!rows||!rows.length){document.getElementById('dsTable').innerHTML='<div class="empty">No options in delta range</div>';return;}
  const warnPct=getSetting('stgBaqWarn'),badPct=getSetting('stgBaqBad');
  document.getElementById('dsTable').innerHTML=makeTbl(rows,[
    {key:'option_type',label:'TYPE',fmt:v=>`<span class="${v==='CE'?'up':'down'}">${v}</span>`},
    {key:'symbol',label:'SYMBOL'},{key:'expiry',label:'EXPIRY'},{key:'strike_price',label:'STRIKE'},
    {key:'delta',label:'Δ',fmt:v=>fmt(v,3)},
    {key:'ltp',label:'LTP',fmt:(v,r)=>{
      const bc=r.bid_ask_spread!=null?baqClass(r.bid_ask_spread,v,warnPct,badPct):'';
      return`<span class="${bc}">${fmt(v,2)}</span>`;}},
    {key:'iv',label:'IV%',fmt:v=>fmt(v,1)+'%'},
    {key:'gamma',label:'γ',fmt:v=>fmt(v,5)},
    {key:'theta',label:'Θ',fmt:v=>fmt(v,3)},
    {key:'gexv',label:'GEXv',fmt:v=>fmtL(v)},
    {key:'net_gexv',label:'NetGEXv',fmt:v=>fmtL(v)},
    {key:'oi',label:'OI',fmt:v=>fmtL(v)},
    {key:'volume',label:'VOL',fmt:v=>fmtL(v)},
    {key:'lot_size',label:'LOT'},
    {key:'premium_per_lot',label:'LOT PREM ₹',fmt:v=>fmtL(v)},
    {key:'prem_strike_pct',label:'PREM/K %',fmt:v=>v!=null?fmt(v,3)+'%':'—'},
    {key:'margin',label:'SPAN MARGIN ₹',fmt:v=>v!=null&&v>0?fmtL(v):'—'},
    {key:'return_on_margin',label:'RoM%',fmt:v=>v!=null?sspan(v,2):'—'},
    {key:'risk_indicator',label:'RISK NOTL',fmt:v=>fmtL(v)},
  ]);
}
async function fetchDsMargin(){
  // Guard: only run if delta screener was loaded in this browser session
  if(!_dsSessionLoaded){
    alert('Load the delta screener first (click LOAD), then fetch margin.');
    return;
  }
  const status=await api('/api/icici/status').catch(()=>({configured:false}));
  if(!status.configured){alert('ICICI not configured. Click the ICICI badge in the topbar.');return;}
  const btn=document.getElementById('btnFetchMargin');
  btn.textContent='FETCHING… 0/?';btn.disabled=true;

  // Build the request payload — only rows that need fetching (no valid margin yet)
  const toFetch=_dsCurrentRows.filter(r=>r.ltp&&r.ltp>0&&r.margin==null);
  if(!toFetch.length){btn.textContent='FETCH MARGIN (ICICI)';btn.disabled=false;return;}

  // Index rows by their position in _dsCurrentRows for fast update
  const rowMap=new Map(_dsCurrentRows.map((r,i)=>[i,r]));
  const payload=toFetch.map(r=>({
    symbol:r.symbol, strike:r.strike_price,
    expiry:(r.expiry||'').substring(0,10),
    option_type:r.option_type==='CE'?'call':'put',
    ltp:r.ltp, qty:r.lot_size,
  }));

  // Use EventSource-style fetch with ReadableStream to consume SSE
  let done=0,total=toFetch.length;
  btn.textContent=`FETCHING… 0/${total}`;

  try{
    const resp=await fetch('/api/icici/margin/batch',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({rows:payload}),
    });
    if(!resp.ok){throw new Error(await resp.text());}

    const reader=resp.body.getReader();
    const decoder=new TextDecoder();
    let buf='';

    while(true){
      const{done:streamDone,value}=await reader.read();
      if(streamDone)break;
      buf+=decoder.decode(value,{stream:true});
      // SSE events are separated by double newlines
      const events=buf.split('\n\n');
      buf=events.pop();  // keep incomplete last chunk in buffer

      for(const ev of events){
        const line=ev.trim();
        if(!line.startsWith('data:'))continue;
        let msg;
        try{msg=JSON.parse(line.slice(5).trim());}catch(e){continue;}

        if(msg.type==='progress'){
          btn.textContent=`FETCHING… ${msg.done}/${msg.total}`;

        }else if(msg.type==='result'){
          // Find the matching row by index in toFetch (msg.idx)
          const r=toFetch[msg.idx];
          if(r){
            r.margin=msg.span_margin!=null?msg.span_margin:msg.isec_margin;
            r.return_on_margin=r.margin>0
              ?parseFloat(((r.premium_per_lot/r.margin)*100).toFixed(2)):null;
          }
          done++;
          btn.textContent=`FETCHING… ${done}/${total}`;
          // Re-render table progressively so user sees results arriving
          const minOi=getSetting('stgDsMinOi'),minVol=getSetting('stgDsMinVol');
          renderDsTable(_dsCurrentRows.filter(r=>r.oi>=minOi&&r.volume>=minVol));

        }else if(msg.type==='done'){
          console.log(`Margin batch: ${msg.total} rows, ${msg.from_cache} from cache, ${msg.fetched} fetched`);

        }else if(msg.type==='error'){
          console.warn(`Margin row ${msg.idx}:`,msg.message);
        }
      }
    }
  }catch(e){
    console.error('Margin batch failed:',e);
  }

  btn.textContent='FETCH MARGIN (ICICI)';btn.disabled=false;
  // Final render
  const minOi=getSetting('stgDsMinOi'),minVol=getSetting('stgDsMinVol');
  renderDsTable(_dsCurrentRows.filter(r=>r.oi>=minOi&&r.volume>=minVol));
}

// ── View init map ─────────────────────────────────────────────────
// ── VIEW_INIT ─────────────────────────────────────────
// Wire expiry DTE badges
document.getElementById('gexExpiry').addEventListener('change',()=>updateDteBadge('gexExpiry','gexExpiryDteBadge'));
document.getElementById('ivExpiry').addEventListener('change',()=>updateDteBadge('ivExpiry','ivExpiryDteBadge'));
document.getElementById('trendExpiry').addEventListener('change',()=>{loadAtmStrikes();updateDteBadge('trendExpiry','trendExpiryDteBadge');});

const VIEW_INIT={
  expscreen:()=>loadExposureScreener(),
  playbook:()=>initPlaybook(), // new
  history:()=>{ return initHistory(); },
  gex:()=>loadCascade('gexSymbol','gexExpiry','gexTimestamp'),
  oi:()=>{},
  iv:()=>loadCascade('ivSymbol','ivExpiry','ivTimestamp'),
  trend:()=>loadCascade('trendSymbol','trendExpiry',null).then(()=>loadAtmStrikes()),
  delta:()=>{ const t=document.querySelector('#screenerPageTabs .pagetab[data-ptab="sp-delta"]');
              if(t)t.dataset.init='1'; loadDsTimestamps(); },
  settings:()=>loadSettings(),
  vix:()=>loadVix(),
  walls:()=>loadOiWalls(),
  market:()=>loadMarketInfo(),
  rvfcst:()=>loadRvForecast(),
  vrp:()=>loadVrp(),
  wheel:()=>loadWheel(),
  premium:()=>initPremium(),
};

// ── #10: Screener mid-level page-tabs (Delta/Premium | Divergence | Shockers | Movers) ──
// Each panel inits lazily the first time its tab is shown.
const PTAB_INIT = {
  'sp-delta':      ()=>loadDsTimestamps(),
  'sp-divergence': ()=>{ if(typeof loadDivergence==='function') loadDivergence(); },
  'sp-shockers':   ()=>{ loadVolShock(); loadIvShock(); },
  'sp-movers':     ()=>loadMovers(),
  'sp-vol':        ()=>loadVolScreener(),
};
document.querySelectorAll('#screenerPageTabs .pagetab').forEach(tab=>{
  tab.addEventListener('click',()=>{
    const bar=tab.closest('.pagetab-bar');
    const view=tab.closest('.view');
    bar.querySelectorAll('.pagetab').forEach(t=>t.classList.remove('active'));
    view.querySelectorAll('.pagetab-panel').forEach(p=>p.classList.remove('active'));
    tab.classList.add('active');
    const panel=document.getElementById(tab.dataset.ptab);
    panel.classList.add('active');
    if(!tab.dataset.init){
      tab.dataset.init='1';
      const fn=PTAB_INIT[tab.dataset.ptab];
      if(fn)fn();
    }
  });
});


// ── bfcache guard ────────────────────────────────────────────────
// When Chrome/Edge restores the page from back-forward cache, persisted=true.
// Reset session state so stale JS data can't trigger API calls.
window.addEventListener('pageshow', e=>{
  if(e.persisted){
    _dsCurrentRows=[];
    _dsSessionLoaded=false;
    console.log('bfcache restore detected — session state reset');
  }
});


// VIX and OI Walls button listeners
document.getElementById('btnVix')?.addEventListener('click', loadVix);
document.getElementById('btnWalls')?.addEventListener('click', loadOiWalls);
wireToggleGroup('wallsFilterGroup');
document.getElementById('wallsFilterGroup')?.addEventListener('click', ()=>setTimeout(loadOiWalls,50));
// Editing the shelf fraction reloads immediately (matches other live controls)
document.getElementById('wallsFracInput')?.addEventListener('change', loadOiWalls);
document.getElementById('wallsExpirySelect')?.addEventListener('change', loadOiWalls);
// Wire trendTimestamp cascade
document.getElementById('trendExpiry').addEventListener('change',()=>{
  loadAtmStrikes();
  // Also refresh timestamps for trend panel
  const sym=document.getElementById('trendSymbol').value;
  const exp=document.getElementById('trendExpiry').value;
  if(sym&&exp) api(`/api/timestamps?symbol=${encodeURIComponent(sym)}`)
    .then(tss=>{if(tss.length)populateSel('trendTimestamp',tss);}).catch(()=>{});
});


// ══════════════════════════════════════════════════════════════════
// ITEM 11: Market Info
// ══════════════════════════════════════════════════════════════════
document.getElementById('btnMarket')?.addEventListener('click', loadMarketInfo);

async function loadMarketInfo(){
  const sym = document.getElementById('marketSymbol')?.value.trim().toUpperCase()||'';
  const symParam = sym ? `?symbol=${encodeURIComponent(sym)}` : '';
  await Promise.all([
    loadMarketStatus(),
    loadBlockDeals(),
    loadCorpActions(symParam),
    loadAnnouncements(symParam),
    loadBoardMeetings(symParam),
  ]);
}

async function loadMarketStatus(){
  const el=document.getElementById('marketStatusCard');
  el.innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  try{
    const d=await api('/api/market/status');
    const stClass=d.market_open?'ms-open':d.pre_open?'ms-pre':'ms-closed';
    const stText=d.market_open?'OPEN':d.pre_open?'PRE-OPEN':'CLOSED';
    const segs=(d.segments||[]).map(s=>`
      <div style="display:flex;justify-content:space-between;padding:5px 8px;
        border-bottom:1px solid var(--border);font-family:var(--mono);font-size:11px">
        <span style="color:var(--acc)">${s.market||s.exchange||'?'}</span>
        <span class="market-status ${s.marketStatus?.toLowerCase()==='open'?'ms-open':'ms-closed'}">
          ${s.marketStatus||'?'}
        </span>
      </div>`).join('');
    el.innerHTML=`
      <div class="card">
        <div style="display:flex;align-items:center;gap:16px;padding:12px">
          <div class="kpi-val acc" style="font-size:32px">${stText}</div>
          <span class="market-status ${stClass}" style="font-size:11px">${d.message||''}</span>
        </div>
        ${segs}
      </div>`;
  }catch(e){el.innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;}
}

async function loadBlockDeals(){
  const el=document.getElementById('blockDealsTable');
  el.innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  try{
    const data=await api('/api/market/block_deals');
    if(!data.length){el.innerHTML='<div class="empty">No block deals today</div>';return;}
    el.innerHTML=makeTbl(data,[
      {key:'symbol',label:'SYMBOL'},
      {key:'deal_date',label:'DATE'},
      {key:'client_name',label:'CLIENT'},
      {key:'buy_sell',label:'B/S',fmt:v=>v?`<span class="${v==='B'||v?.startsWith('B')?'up':'down'}">${v}</span>`:'—'},
      {key:'quantity',label:'QTY',fmt:v=>fmtL(v)},
      {key:'price',label:'PRICE',fmt:v=>fmt(v,2)},
      {key:'exchange',label:'EXCH'},
    ]);
  }catch(e){el.innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;}
}

async function loadCorpActions(symParam){
  const el=document.getElementById('corpActionsTable');
  el.innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  try{
    const data=await api('/api/market/corp_actions'+symParam);
    if(!data.length){el.innerHTML='<div class="empty">No corporate actions</div>';return;}
    el.innerHTML=makeTbl(data,[
      {key:'symbol',label:'SYMBOL'},
      {key:'company',label:'COMPANY'},
      {key:'ex_date',label:'EX DATE'},
      {key:'record_date',label:'RECORD DATE'},
      {key:'purpose',label:'PURPOSE'},
      {key:'face_value',label:'FACE VALUE'},
    ]);
  }catch(e){el.innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;}
}

async function loadAnnouncements(symParam){
  const el=document.getElementById('announcementsTable');
  el.innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  try{
    const data=await api('/api/market/announcements'+symParam);
    if(!data.length){el.innerHTML='<div class="empty">No announcements</div>';return;}
    el.innerHTML=makeTbl(data,[
      {key:'symbol',label:'SYMBOL'},
      {key:'company',label:'COMPANY'},
      {key:'ann_date',label:'DATE'},
      {key:'subject',label:'SUBJECT'},
      {key:'attachment',label:'FILE',fmt:v=>v?`<a href="https://nsearchives.nseindia.com${v}" target="_blank" style="color:var(--acc)">↗</a>`:'—'},
    ]);
  }catch(e){el.innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;}
}

async function loadBoardMeetings(symParam){
  const el=document.getElementById('boardMeetingsTable');
  el.innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  try{
    const data=await api('/api/market/board_meetings'+symParam);
    if(!data.length){el.innerHTML='<div class="empty">No upcoming board meetings</div>';return;}
    el.innerHTML=makeTbl(data,[
      {key:'symbol',label:'SYMBOL'},
      {key:'company',label:'COMPANY'},
      {key:'meeting_date',label:'DATE'},
      {key:'purpose',label:'PURPOSE'},
    ]);
  }catch(e){el.innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;}
}


// ══════════════════════════════════════════════════════════════════
// Toggle groups for new panels
// ══════════════════════════════════════════════════════════════════
['divModeGroup','divFilterGroup','oiHistViewGroup'].forEach(id=>{
  const grp=document.getElementById(id);if(!grp)return;
  grp.addEventListener('click',e=>{
    const btn=e.target.closest('button');if(!btn)return;
    grp.querySelectorAll('button').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
  });
});
wireToggleGroup('oiHistViewGroup');

// ══════════════════════════════════════════════════════════════════
// VIEW_INIT additions
// ══════════════════════════════════════════════════════════════════
VIEW_INIT['divergence'] = ()=>{};
// ── ATM short straddle ────────────────────────────────────────────
// One row per SYMBOL (a straddle is a position, not a leg), so this is its own
// page tab rather than more columns on the delta grid.
let _strRows=[];
async function loadStraddleScreener(){
  const ft=getFilter('strFilterGroup');
  const rank=document.getElementById('strRank')?.value||'0';
  const minOi=Math.max(0,parseFloat(document.getElementById('strMinOi')?.value)||0);
  document.getElementById('strTable').innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  document.getElementById('strKpis').innerHTML='';
  try{
    const res=await api(`/api/straddle_screener?filter_type=${ft}&expiry_rank=${rank}&min_oi=${minOi}`);
    _strRows=res.rows||[];
    if(!_strRows.length){
      document.getElementById('strTable').innerHTML='<div class="empty">No straddles match</div>';
      return;
    }
    const rich=_strRows.filter(r=>r.be_vs_hv!=null);
    const med=arr=>{if(!arr.length)return null;const s=[...arr].sort((a,b)=>a-b);return s[Math.floor(s.length/2)];};
    document.getElementById('strKpis').innerHTML=[
      {label:'SYMBOLS',value:_strRows.length,sub:rank==='0'?'nearest expiry':'next expiry'},
      {label:'MEDIAN BE/HV',value:rich.length?fmt(med(rich.map(r=>r.be_vs_hv)),3):'—',
       sub:'>1 = paid above realised'},
      {label:'RICH (BE/HV>1.1)',value:rich.filter(r=>r.be_vs_hv>1.1).length,sub:`of ${rich.length} with hv20`},
      {label:'MEDIAN BE WIDTH',value:fmt(med(_strRows.map(r=>r.be_width_pct).filter(v=>v!=null)),2)+'%',sub:'± of futures price'},
      {label:'MEDIAN RoM',value:fmt(med(_strRows.map(r=>r.return_on_margin).filter(v=>v!=null)),2)+'%',sub:'SPAN-netted margin'},
    ].map(k=>`<div class="kpi"><div class="kpi-label">${k.label}</div><div class="kpi-val acc">${k.value}</div><div class="kpi-sub">${k.sub}</div></div>`).join('');
    renderStrTable();
  }catch(e){
    document.getElementById('strTable').innerHTML=`<div class="empty">Error: ${e.message}</div>`;
  }
}

// Straddle detail popover — mirrors the exposure screener's ⊕ mechanism
// (shared .wall-tip-pop element, same show/hide lifecycle).
// The grid stays at 13 columns; everything that is context rather than a
// ranking key lives in here.
function _strDetailTip(r){
  const row=(lbl,val)=>`<div style="display:flex;gap:10px;margin:2px 0">
    <span style="color:var(--muted);min-width:94px;font-size:10px">${lbl}</span>
    <span style="font-size:11px">${val}</span></div>`;
  const m=r.iv_rank_method;
  const rankTxt = r.iv_rank!=null
    ? `${fmt(r.iv_rank,1)}<span style="color:var(--muted);font-size:9px"> · ${m||'?'} · n=${r.iv_rank_n||0}</span>`
      + (m==='band'?' <span style="color:var(--amber);font-size:9px">(band: DTE-sensitive)</span>':'')
    : '<span style="color:var(--muted)">— too few samples</span>';
  return `<div style="font-family:var(--mono);min-width:250px;max-width:310px">
    <div style="font-weight:600;margin-bottom:5px;color:var(--acc)">${r.symbol}
      <span style="color:var(--muted);font-weight:400">· ATM straddle</span></div>
    ${row('Breakeven', `${fmt(r.be_lo,0)} – ${fmt(r.be_hi,0)}`
        +` <span style="color:var(--muted)">±${fmt(r.straddle_prem,1)}</span>`)}
    ${row('Legs', `CE ${fmt(r.ce_ltp,2)} + PE ${fmt(r.pe_ltp,2)}`)}
    ${row('Prem/lot', `₹${fmtL(r.straddle_prem_lot)} · lot ${r.lot_size}`)}
    ${row('Margin', r.margin!=null?`₹${fmtL(r.margin)} <span style="color:var(--muted);font-size:9px">SPAN-netted</span>`:'—')}
    <div style="border-top:1px solid var(--bd);margin:5px 0"></div>
    ${row('IV rank', rankTxt)}
    ${row('VRP', r.vrp!=null?`${r.vrp>0?'+':''}${fmt(r.vrp,2)} <span style="color:var(--muted);font-size:9px">IV − HV20</span>`:'—')}
    ${row('EM (1 day)', emBandsHtml(r.em_1d, null, 1))}
    ${row('OI', `CE ${fmtL(r.ce_oi)} · PE ${fmtL(r.pe_oi)}`)}
    <div style="color:var(--muted);font-size:9px;margin-top:5px;line-height:1.35">
      BE/HV compares premium to the move 20d realised vol implies over the same
      DTE. Caveat: realised vol mean-reverts hard — the quietest quintile of
      trailing HV was followed by 1.47× that level. A high BE/HV driven by a
      depressed HV20 is partly that effect, not mispricing.</div>
  </div>`;
}
function _showStrTip(trigger){
  const idx=parseInt(trigger.dataset.stridx,10);
  const r=_strRows[idx]; if(!r) return;
  const el=_ensureWallTipEl();
  el.innerHTML=_strDetailTip(r);
  el.style.display='block';
  const tr=trigger.getBoundingClientRect();
  const pw=el.offsetWidth, ph=el.offsetHeight, vw=window.innerWidth, vh=window.innerHeight;
  let left=tr.right-pw; if(left<8) left=8; if(left+pw>vw-8) left=vw-8-pw;
  let top=tr.bottom+6; if(top+ph>vh-8) top=tr.top-ph-6; if(top<8) top=8;
  el.style.left=left+'px'; el.style.top=top+'px';
}
function _wireStrTips(){
  const tbl=document.querySelector('#strTable table');
  if(!tbl) return;
  tbl.querySelectorAll('.wall-tip-trigger[data-stridx]').forEach(t=>{
    t.addEventListener('mouseenter',()=>{ t._over=true;
      const el=_ensureWallTipEl(); el._triggerOver=true; _showStrTip(t); });
    t.addEventListener('mouseleave',()=>{ t._over=false;
      const el=document.getElementById('wallTipPop'); if(el) el._triggerOver=false;
      _hideWallTip(); });
    t.addEventListener('focus',()=>_showStrTip(t));
    t.addEventListener('blur',()=>_hideWallTip());
  });
}

function renderStrTable(){
  const cols=[
    {key:'symbol',label:'SYMBOL'},
    {key:'expiry',label:'EXPIRY'},
    {key:'dte',label:'DTE',fmt:v=>fmt(v,0)},
    {key:'fut_price',label:'FUT',fmt:v=>fmt(v,1)},
    {key:'atm_strike',label:'ATM K',fmt:v=>fmt(v,0)},
    {key:'straddle_prem',label:'STRADDLE',fmt:v=>fmt(v,2)},
    {key:'be_width_pct',label:'BE ±%',fmt:v=>v!=null?fmt(v,2)+'%':'—'},
    {key:'atm_iv',label:'IV',fmt:v=>fmt(v,1)},
    {key:'iv_change',label:'IV CHG',fmt:v=>v!=null?sspan(v,2):'—'},
    {key:'iv_pctile',label:'IV %ILE',fmt:v=>v!=null?fmt(v,0):'—',
     help:'Share of the last 90 days with a lower constant-maturity IV. Shown instead of IV Rank: with a 90d window the min/max range can be set by one spike, so the percentile is the more robust read.'},
    {key:'hv20',label:'HV20',fmt:v=>v!=null?fmt(v,1):'—'},
    {key:'be_vs_hv',label:'BE/HV',
     fmt:v=>v==null?'—':`<span class="${v>=1.1?'up':v<0.9?'down':'neu'}">${fmt(v,3)}</span>`,
     help:'Straddle premium / move implied by 20d realised vol over the same DTE. >1 = paid above realised movement.'},
    {key:'return_on_margin',label:'RoM%',fmt:v=>v!=null?sspan(v,2):'—'},
    {key:'_detail',label:'',sortable:false,
     fmt:(v,r)=>`<span class="wall-tip-trigger" tabindex="0" data-stridx="${r._idx}">⊕</span>`},
  ];
  // makeTbl passes (value, row) only — no index — so stamp a stable one.
  // It must survive the DOM re-sort, hence stored on the row object rather
  // than derived from position at render time.
  _strRows.forEach((r,i)=>{ r._idx=i; });
  document.getElementById('strTable').innerHTML=makeTbl(_strRows,cols);
  _wireStrTips();
}

// ── Model-ranked straddle (EV) ────────────────────────────────────
// Reads the NIGHTLY PREDICTION LOG, never a live recompute. If this page
// recomputed on load it would show one ranking while vol_prediction stored
// another, and every later comparison would grade the model against numbers it
// never emitted. Intraday refresh, when it comes, must write to the log too.
let _volRows=[];

// Index symbols have no single-stock analogue for several fields and are a
// different animal for a short straddle; the filter mirrors the other tabs.
const _VOL_INDEX = new Set(['NIFTY','BANKNIFTY','FINNIFTY','MIDCPNIFTY','NIFTYNXT50']);

async function loadVolScreener(){
  const rank = document.getElementById('volRank')?.value || '0';
  const gated = getFilter('volGateGroup') !== 'all';
  document.getElementById('volTable').innerHTML =
    '<div class="loading"><div class="spinner"></div>Loading…</div>';
  document.getElementById('volKpis').innerHTML='';
  try{
    const res = await api(`/api/vol_screener/straddle?expiry_rank=${rank}`
                          +`&gated_only=${gated}&limit=400`);
    const asofEl=document.getElementById('volAsof');
    if(asofEl) asofEl.textContent = res.asof ? `as of ${res.asof}` : '';
    _volRows = res.rows || [];
    if(!_volRows.length){
      const ok = res.n_total ?? null;
      document.getElementById('volTable').innerHTML = ok
        ? `<div class="empty">${ok} candidate(s) logged for ${res.asof}, but 0 `
          +`passed gates (see the reasons below). Flip SHOW to `
          +`"ALL + REJECTS" to see why.</div>`
        : '<div class="empty">No predictions logged for this day. '
          +'Run build_vol_predictions.py predict.</div>';
      loadVolGateSummary(rank);
      return;
    }
    _renderVolKpis(res);
    renderVolTable();
    loadVolCompare();
  }catch(e){
    document.getElementById('volTable').innerHTML =
      `<div class="empty">Error: ${e.message}</div>`;
  }
}

async function loadVolGateSummary(rank){
  // Called when the gated view comes back empty, so the person sees WHY
  // rather than a bare "no data" that looks identical to the log not having
  // run at all. Measured on 2026-09-22: 158/160 rejected for dte=7, once the
  // nearest expiry rolled below --min-dte 10 -- correct gate behaviour, but
  // "no predictions logged" would have read as a broken pipeline instead.
  try{
    const g = await api(`/api/vol_screener/gate_summary?expiry_rank=${rank}`);
    const el = document.getElementById('volTable');
    if(!el || !g.reasons?.length) return;
    const rows = g.reasons.map(r=>
      `<div>${r.n}× &nbsp;<span style="color:var(--muted)">${r.gate_reasons}</span></div>`
    ).join('');
    el.innerHTML += `<div style="padding:8px 10px;font-family:var(--mono);`
      +`font-size:10px;color:var(--text)">${g.passed}/${g.total} passed. `
      +`Rejection reasons:</div>`
      +`<div style="padding:0 10px 10px;font-family:var(--mono);font-size:10px">${rows}</div>`;
  }catch(e){ /* best-effort */ }
}

function _volFiltered(){
  const ft = getFilter('volFilterGroup');
  if(ft==='index') return _volRows.filter(r=>_VOL_INDEX.has(r.symbol));
  if(ft==='stock') return _volRows.filter(r=>!_VOL_INDEX.has(r.symbol));
  return _volRows;
}

function _renderVolKpis(res){
  const rows=_volFiltered();
  const med=a=>{const s=a.filter(v=>v!=null).sort((x,y)=>x-y);
                return s.length?s[Math.floor(s.length/2)]:null;};
  const m=res.model||{};
  const kpis=[
    {label:'CANDIDATES', value:rows.length,
     sub:(res.gated_only?'passed gates':'incl. rejects')},
    {label:'MEDIAN EV/MARGIN',
     value:fmt(100*med(rows.map(r=>r.ev_per_margin)),2)+'%',
     sub:'simulated, net of spread'},
    {label:'MEDIAN SPREAD',
     value:fmt(100*med(rows.map(r=>r.spread_frac)),2)+'%',
     sub:'of credit, modelled'},
    {label:'MEDIAN CVaR5',
     value:fmt(100*med(rows.map(r=>r.sim_cvar05_per_margin)),1)+'%',
     sub:'of margin, worst 5%'},
    {label:'BACKTEST IC', value:m.backtest_ic!=null?fmt(m.backtest_ic,3):'—',
     sub:`t ${m.backtest_t??'—'} · ${m.backtest_expiries??'—'} expiries`},
  ];
  // Month-level premium gate: WHETHER to sell premium this expiry at all, not
  // which names. Universe median iv/rv_forecast over all candidates. Below 1.05
  // the month lost money in both regimes (2021-26 backtest); see straddle_core.
  const u=res.universe||{};
  const gateTile = u.median_iv_over_rv==null
    ? {label:'PREMIUM GATE', value:'—', sub:'not computed yet', cls:''}
    : {label:'PREMIUM GATE', value:fmt(u.median_iv_over_rv,3),
       sub: u.pass ? `≥ ${fmt(u.threshold,2)} · PASS — trade` : `< ${fmt(u.threshold,2)} · FAIL — skip / reduce`,
       cls: u.pass ? 'up' : 'down',
       tip:'Universe median of IV / RV forecast at entry, all candidates. When the '
          +'market as a whole is not paying a premium over recent realised vol, '
          +'short premium has lost money: below 1.05 the backtest month averaged '
          +'+0.1% of margin before Nov 2024 and -2.7% after; at or above, +3.0% and '
          +'+1.0%. Small samples: validate prospectively. Stock-level GATE ticks '
          +'below are independent of this; a trade needs both.'};
  kpis.unshift(gateTile);
  document.getElementById('volKpis').innerHTML = kpis.map(k=>
    `<div class="kpi"${k.tip?` title="${k.tip.replace(/"/g,'&quot;')}"`:''}><div class="kpi-label">${k.label}</div>`
    +`<div class="kpi-val ${k.cls||'acc'}">${k.value}</div>`
    +`<div class="kpi-sub ${k.cls||''}">${k.sub}</div></div>`).join('');
}

function renderVolTable(){
  const rows=_volFiltered();
  const cols=[
    {key:'score_rank',label:'#',fmt:v=>v==null?'—':fmt(v,0)},
    {key:'symbol',label:'SYMBOL'},
    {key:'expiry',label:'EXPIRY'},
    {key:'dte',label:'DTE',fmt:v=>fmt(v,0)},
    {key:'fut_price',label:'FUT',fmt:v=>fmt(v,1)},
    {key:'atm_strike',label:'ATM K',fmt:v=>fmt(v,0)},
    {key:'gross_credit',label:'STRADDLE',fmt:v=>fmt(v,2)},
    {key:'spread_frac',label:'SPRD%',fmt:v=>v==null?'—':fmt(100*v,2)+'%',
     sortValue:v=>v==null?-1:v,
     help:'Modelled entry half-spread as a share of gross credit. Calibrated on 37,868 real ATM quotes by volume quintile x DTE band; options_eod carries no bid/ask, so every backtest before this filled at mid and overstated returns by 35-84%.'},
    {key:'score',label:'SCORE',fmt:v=>v==null?'—':`<b>${fmt(v,1)}</b>`,
     help:'Equal-weighted percentile rank of EV/margin and prior path efficiency. Backtest IC 0.131 (t 8.2) over 71 expiries.'},
    {key:'ev_per_margin',label:'EV/MGN',
     fmt:v=>v==null?'—':sspan(100*v,2),
     help:'Simulated expected P&L per rupee of margin, from filtered historical simulation on CA-corrected returns, net of modelled spread. IC 0.125 standalone — carries most of the signal.'},
    {key:'trend_eff',label:'TREND',fmt:v=>v==null?'—':fmt(v,3),
     help:'Prior 20-day path efficiency: |sum of returns| / sum of |returns|. HIGHER is better — efficiency mean-reverts, so a name that just trended hard tends to consolidate. Shipping this inverted cost the first seven expiries.'},
    {key:'sim_cvar05_per_margin',label:'CVaR5',
     fmt:v=>v==null?'—':`<span class="down">${fmt(100*v,0)}%</span>`,
     help:'Mean of the worst 5% of simulated outcomes, per rupee of margin. Used as a gate, not a ranking term — it correlates 0.96 with EV.'},
    {key:'sim_pop',label:'POP',fmt:v=>v==null?'—':fmt(100*v,0)+'%',
     help:'Simulated probability of profit. DIAGNOSTIC ONLY, never ranked: for an ATM straddle it is ~57.5% structurally and RISES with fat tails, so it is highest exactly where losses are worst.'},
    {key:'atm_iv',label:'IV',fmt:v=>v==null?'—':fmt(v,1)},
    {key:'hv20',label:'HV20',fmt:v=>v==null?'—':fmt(v,1)},
    {key:'legacy_rom',label:'RoM%',fmt:v=>v==null?'—':sspan(v,2)},
    {key:'legacy_rom_rank',label:'RoM #',fmt:v=>v==null?'—':fmt(v,0)},
    {key:'rank_delta_vs_rom',label:'Δ RANK',
     fmt:v=>v==null?'—':`<span class="${v>0?'up':v<0?'down':'neu'}">${v>0?'+':''}${fmt(v,0)}</span>`,
     sortValue:v=>v==null?0:v,
     help:'Legacy RoM rank minus model rank. Positive = the model likes this name far more than the RoM sort does. On the first live day the two correlated -0.06 and shared only 2 of the top 20, so most rows disagree — which is what makes running both worth the effort.'},
    {key:'gate_reasons',label:'GATE',
     // Stock-level gates (events, volume, CVaR) decide the tick; the month-level
     // premium gate is shown beside it, so a clean name in a failing month reads
     // "✓ month ✕" instead of disappearing.
     fmt:(v,r)=>{
       const stock = r.gate_pass?'<span class="up">✓</span>'
         :`<span class="down" title="${String(v||'').replace(/"/g,'&quot;')}">✕</span>`;
       const month = r.universe_gate===false
         ? ` <span class="down" title="Month-level premium gate failed: universe median IV/RV forecast ${r.universe_vrp_median!=null?fmt(r.universe_vrp_median,3):''} below threshold">month ✕</span>`
         : '';
       return stock+month;
     },
     sortValue:(v,r)=>(r.gate_pass?1:0)+(r.universe_gate===false?0:1),
     exportValue:(v,r)=>(r.gate_pass?'pass':(v||'reject'))+(r.universe_gate===false?' | month fail':'')},
  ];
  document.getElementById('volTable').innerHTML = makeTbl(rows,cols);
  const s=document.getElementById('volSearch');
  if(s && s.value) filterTableBySymbol('volTable', s.value);
}

async function loadVolCompare(){
  const el=document.getElementById('volCompare');
  if(!el) return;
  try{
    const res=await api('/api/vol_screener/compare');
    if(!res.ready){
      // Say this plainly rather than hiding the panel. An absent page implies
      // nothing is being measured; this says the opposite -- it IS being
      // measured, and it is too early to read.
      el.innerHTML=`<div style="padding:10px;font-family:var(--mono);font-size:10px;`
        +`color:var(--muted);line-height:1.5">`
        +`<b>Accumulating.</b> ${res.n||0} resolved rows across ${res.periods||0} `
        +`expiries.<br>Needs roughly 20–30 resolved expiries — about two years at `
        +`monthly cadence — before the per-expiry IC standard error is small `
        +`enough to separate the models. An early lead here means nothing.`
        +`</div>`;
      return;
    }
    const cols=[
      {key:'ranker',label:'RANKER',
       fmt:(v,r)=>r.is_model?`<b>${v}</b>`:v},
      {key:'ic',label:'IC',fmt:v=>sspan(v,4)},
      {key:'t',label:'t',fmt:v=>v==null?'—':fmt(v,2)},
      {key:'positive',label:'POS',fmt:(v,r)=>`${v}/${r.periods}`,
       sortValue:(v,r)=>r.periods?v/r.periods:0},
      {key:'periods',label:'EXPIRIES',fmt:v=>fmt(v,0)},
    ];
    el.innerHTML=`<div style="font-size:9px;color:var(--muted);padding:0 10px 6px">`
      +`Same candidates, same expiries — only the ranking rule differs. `
      +`Bold rows are the new model.</div>`
      +makeTbl(res.rankers||[],cols);
  }catch(e){
    el.innerHTML=`<div class="empty">Error: ${e.message}</div>`;
  }
}

document.getElementById('btnVol')?.addEventListener('click',loadVolScreener);
document.getElementById('btnVolGuide')?.addEventListener('click',()=>{
  const g=document.getElementById('volGuide');
  if(!g) return;
  if(!g.dataset.built){ g.innerHTML=_volGuideHtml(); g.dataset.built='1'; }
  g.style.display = g.style.display==='none' ? '' : 'none';
});

function _volGuideHtml(){
  const sec=(title,body)=>`<b style="color:var(--text)">${title}</b><br>${body}<br><br>`;
  return sec('WHAT THIS PAGE IS',
    `A second, independent ranking for the same ATM straddles as the `
    +`<b>STRADDLE</b> tab, scored by a model instead of by sorting one column. `
    +`It reads last night's frozen prediction log (<code>vol_prediction</code>), `
    +`not a live recalculation — what you see here is exactly what will later `
    +`be graded, so the two can never quietly drift apart. The model has 71 `
    +`expiries of backtest and forward evidence only from 2026-09-18, so treat `
    +`it as a second opinion to weigh against the older tab, not a replacement `
    +`for it — that is also why both are kept on the dashboard side by side.`)

  + sec('THE MODEL, IN ONE LINE',
    `Equal-weighted rank of two numbers — <b>EV/MGN</b> (does the simulator `
    +`expect this trade to make money) and <b>TREND</b> (was the stock's prior `
    +`month choppy or trending). Backtest IC 0.131, t = 8.2, over 71 monthly `
    +`expiries with corporate-action-corrected returns and a real bid-ask `
    +`spread charged — net RoM 2.21% after those costs. Ten other candidate `
    +`signals (VRP, gamma regime, dealer positioning, compression duration, `
    +`liquidity ratios…) were tried and measured to be worthless or actively `
    +`backwards, so they were cut rather than diluting the two that work.`)

  + sec('COLUMN REFERENCE',
    [['#','Rank by SCORE, best first. This is the order to work down.'],
     ['SCORE','The composite — average percentile of EV/MGN and TREND. Higher is better. Hover a row\'s other cells for the full breakdown.'],
     ['EV/MGN','Simulated expected P&amp;L per rupee of margin, on corporate-action-corrected historical paths, net of modelled spread. Does most of the work alone (standalone IC 0.125) — treat as "is this trade good", with TREND as a tiebreaker.'],
     ['TREND','Prior 20-day path efficiency. HIGHER is better, which is the non-obvious part: a name that trended hard for a month tends to go quiet next, so recent trendiness argues FOR a straddle, not against one.'],
     ['SPRD%','Modelled entry cost, as a share of the credit you collect. Calibrated on 37,868 real quotes by volume and DTE — thin chains near 15–20%, deep ones under 1%. Before this existed, every backtest filled at mid and overstated returns by up to 84%.'],
     ['CVaR5','Average of the worst 5% of simulated outcomes. A risk GATE, not a ranking input — it correlates 0.96 with EV/MGN so ranking on both would double-count the same thing.'],
     ['POP','Simulated probability of profit. Shown for reference ONLY — never use this to pick between rows. For an ATM straddle it sits near 57.5% by construction and actually RISES as tails get fatter, so a high POP can flag the worse trade.'],
     ['IV / HV20','Current implied vol and 20-day realised vol of the stock, for context against the legacy BE/HV read.'],
     ['RoM% / RoM #','The legacy tab\'s return-on-margin figure and its rank — the other opinion, shown alongside rather than hidden.'],
     ['Δ RANK','Legacy RoM rank minus this model\'s rank. Positive = this model rates the name much higher than the old sort does. On the first live day the two agreed on almost nothing — rank correlation −0.06, only 2 of the top 20 names shared — so most rows will show a large Δ. That disagreement is the point of running both: if they always agreed there would be nothing to learn by comparing them.'],
     ['GATE','✓ = this candidate is tradeable as scored. ✕ = rejected; hover for why (usually spread/credit, volume floor, or CVaR). Flip SHOW to "ALL + REJECTS" to audit what is being filtered out — a gate you cannot see the rejects of cannot be checked.'],
    ].map(([k,v])=>`<b style="color:var(--acc)">${k}:</b> ${v}`).join('<br>'))

  + sec('HOW TO USE IT',
    `1) Start at the top of <b>#</b> and work down — don't cherry-pick a row `
    +`from the middle because one number looks good; the SCORE already `
    +`combined what matters.<br>`
    +`2) Check <b>GATE</b> is ✓ and glance at <b>SPRD%</b> — a name can rank `
    +`well and still be expensive to actually trade.<br>`
    +`3) Compare <b>Δ RANK</b>: a large positive value means this model likes `
    +`a name the old tab doesn't — worth a second look before sizing up on `
    +`it alone, since it has less of a track record. A large negative value `
    +`(old tab likes it, this one doesn't) is worth understanding too, not `
    +`dismissing.<br>`
    +`4) Ignore <b>POP</b> when choosing — it is there for completeness, not `
    +`for ranking.<br>`
    +`5) Check <b>MODEL TRACKING</b> below before trusting either ranking `
    +`blindly — while it reads "Accumulating" there isn't yet enough forward `
    +`evidence to say which model is actually better live.`)

  + sec('CONFIRM ELSEWHERE ON THE DASHBOARD',
    [['STRADDLE tab','The other opinion — BE/HV, VRP, IV percentile. If both tabs like the same name, that is a stronger signal than either alone.'],
     ['IV ANALYSIS','Check where current IV sits in its own history before selling premium — a good EV/MGN score on IV that is already cheap is a different trade than the same score on IV that is rich.'],
     ['GEX &amp; GAMMA','A short straddle wants the dealer positioned to dampen moves. Check the gamma regime for the strike — EV/MGN alone knows nothing about dealer hedging flows.'],
     ['OI WALLS','Confirm the ATM strike isn\'t sitting right on top of a wall that could void the day\'s realised move behaviour either way.'],
     ['DIVERGENCE / SHOCKERS','A name mid a fresh IV or OI shock is not the quiet, mean-reverting setup TREND is measuring — the backtest\'s history for that name may not apply this week.'],
    ].map(([k,v])=>`<b style="color:var(--acc)">${k}:</b> ${v}`).join('<br>'));
}

['volFilterGroup','volGateGroup'].forEach(id=>{
  document.getElementById(id)?.addEventListener('click',e=>{
    if(e.target.tagName!=='BUTTON') return;
    // The gate toggle changes what the SERVER returns; the index/stock toggle
    // only re-slices rows already in hand, so it must not re-fetch.
    if(id==='volGateGroup') loadVolScreener();
    else { _renderVolKpis({gated_only:getFilter('volGateGroup')!=='all',model:{}});
           renderVolTable(); }
  });
});

// ── Short strangle (delta-selected) ─────────────────────────────────
let _sgRows=[];
function _sgDelta(){ return parseInt(document.getElementById('sgDelta')?.value||'20',10); }
function _sgSyncDelta(v){
  const el=document.getElementById('sgDelta'); if(el) el.value=v;
  ['sgDeltaLabel','sgDeltaBadge'].forEach(id=>{
    const n=document.getElementById(id); if(n) n.textContent=v+'\u0394';
  });
  // Presets and slider are two views of one value — keep the preset highlight
  // honest when the slider lands off a preset.
  document.querySelectorAll('#sgDeltaGroup button').forEach(b=>
    b.classList.toggle('active', parseInt(b.dataset.dt,10)===v));
}
async function loadStrangleScreener(){
  const ft=getFilter('sgFilterGroup');
  const rank=document.getElementById('sgRank')?.value||'0';
  const minOi=Math.max(0,parseFloat(document.getElementById('sgMinOi')?.value)||0);
  const tgt=_sgDelta();
  document.getElementById('sgTable').innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  document.getElementById('sgKpis').innerHTML='';
  try{
    const res=await api(`/api/strangle_screener?filter_type=${ft}&expiry_rank=${rank}`
      +`&target_delta=${tgt}&min_oi=${minOi}`);
    _sgRows=res.rows||[];
    if(!_sgRows.length){
      document.getElementById('sgTable').innerHTML='<div class="empty">No strangles match — try a higher delta or lower MIN OI</div>';
      return;
    }
    const med=arr=>{if(!arr.length)return null;const s=[...arr].sort((a,b)=>a-b);return s[Math.floor(s.length/2)];};
    const holds=_sgRows.map(r=>r.p_hold).filter(v=>v!=null);
    const zs=_sgRows.map(r=>r.z_min).filter(v=>v!=null);
    document.getElementById('sgKpis').innerHTML=[
      {label:'SYMBOLS',value:_sgRows.length,sub:`${tgt}Δ · ${rank==='0'?'nearest':'next'} expiry`},
      {label:'MEDIAN Z MIN',value:zs.length?fmt(med(zs),2):'—',sub:'narrower side, in σ'},
      {label:'MEDIAN P(HOLD)',value:holds.length?fmt(med(holds),1)+'%':'—',sub:'realised · empirical'},
      {label:'MEDIAN EDGE',value:(()=>{const e=_sgRows.map(r=>r.edge).filter(v=>v!=null);return e.length?sspan(med(e),1)+'pp':'—';})(),
       sub:'P(hold) − P(otm)'},
      {label:'MEDIAN RoM',value:fmt(med(_sgRows.map(r=>r.return_on_margin).filter(v=>v!=null)),2)+'%',sub:'SPAN-netted'},
    ].map(k=>`<div class="kpi"><div class="kpi-label">${k.label}</div><div class="kpi-val acc">${k.value}</div><div class="kpi-sub">${k.sub}</div></div>`).join('');
    renderSgTable();
  }catch(e){
    document.getElementById('sgTable').innerHTML=`<div class="empty">Error: ${e.message}</div>`;
  }
}

function _sgDetailTip(r){
  const row=(lbl,val)=>`<div style="display:flex;gap:10px;margin:2px 0">
    <span style="color:var(--muted);min-width:94px;font-size:10px">${lbl}</span>
    <span style="font-size:11px">${val}</span></div>`;
  const bandTxt={1:'very quiet',2:'quiet',3:'normal',4:'active',5:'volatile'}[r.hv_band]||'—';
  const m=r.iv_rank_method;
  return `<div style="font-family:var(--mono);min-width:250px;max-width:315px">
    <div style="font-weight:600;margin-bottom:5px;color:var(--acc)">${r.symbol}
      <span style="color:var(--muted);font-weight:400">· ${fmt(r.pe_delta,0)}Δ/${fmt(r.ce_delta,0)}Δ strangle</span></div>
    ${row('Strikes', `${fmt(r.pe_strike,0)} P / ${fmt(r.ce_strike,0)} C`
        +` <span style="color:var(--muted)">width ${fmt(r.width_pct,1)}%</span>`)}
    ${row('Breakeven', `${fmt(r.be_lo,0)} – ${fmt(r.be_hi,0)}`)}
    ${row('Z each side', r.z_lo!=null?`put ${fmt(r.z_lo,2)}σ · call ${fmt(r.z_hi,2)}σ`:'—')}
    ${row('P(hold) vs P(otm)', (r.p_hold!=null?fmt(r.p_hold,1)+'%':'—')
        +' vs '+(r.p_otm!=null?fmt(r.p_otm,1)+'%':'—')
        +(r.edge!=null?` <span class="${r.edge>0?'up':'down'}">${r.edge>0?'+':''}${fmt(r.edge,1)}pp</span>`:''))}
    ${row('Legs', `PE ${fmt(r.pe_ltp,2)} + CE ${fmt(r.ce_ltp,2)}`)}
    ${row('Prem/lot', `₹${fmtL(r.strangle_prem_lot)} · lot ${r.lot_size}`)}
    ${row('Margin', r.margin!=null?`₹${fmtL(r.margin)} <span style="color:var(--muted);font-size:9px">SPAN-netted</span>`:'—')}
    <div style="border-top:1px solid var(--bd);margin:5px 0"></div>
    ${row('Vol band', `${bandTxt} <span style="color:var(--muted);font-size:9px">hv20 ${fmt(r.hv20,1)}</span>`)}
    ${row('IV rank', r.iv_rank!=null
        ? `${fmt(r.iv_rank,1)}<span style="color:var(--muted);font-size:9px"> · ${m||'?'} · n=${r.iv_rank_n||0}</span>`
          +(m==='band'?' <span style="color:var(--amber);font-size:9px">(DTE-sensitive)</span>':'')
        : '<span style="color:var(--muted)">— too few samples</span>')}
    ${row('EM (1 day)', emBandsHtml(r.em_1d, null, 1))}
    ${row('OI', `PE ${fmtL(r.pe_oi)} · CE ${fmtL(r.ce_oi)}`)}
    <div style="color:var(--muted);font-size:9px;margin-top:5px;line-height:1.35">
      P(hold) is what the stock has DONE — measured, fat-tailed, read from this
      row's vol band. P(otm) is what the market is PRICING — lognormal on each
      side's IV. They disagree for two reasons at once: realised vs implied vol,
      and measured vs assumed tails (the latter is ~−5pp at 2σ). So EDGE ranks
      well but is not a probability you can size against.</div>
  </div>`;
}
function _showSgTip(trigger){
  const idx=parseInt(trigger.dataset.sgidx,10);
  const r=_sgRows[idx]; if(!r) return;
  const el=_ensureWallTipEl();
  el.innerHTML=_sgDetailTip(r);
  el.style.display='block';
  const tr=trigger.getBoundingClientRect();
  const pw=el.offsetWidth, ph=el.offsetHeight, vw=window.innerWidth, vh=window.innerHeight;
  let left=tr.right-pw; if(left<8) left=8; if(left+pw>vw-8) left=vw-8-pw;
  let top=tr.bottom+6; if(top+ph>vh-8) top=tr.top-ph-6; if(top<8) top=8;
  el.style.left=left+'px'; el.style.top=top+'px';
}
function _wireSgTips(){
  const tbl=document.querySelector('#sgTable table');
  if(!tbl) return;
  tbl.querySelectorAll('.wall-tip-trigger[data-sgidx]').forEach(t=>{
    t.addEventListener('mouseenter',()=>{ t._over=true;
      const el=_ensureWallTipEl(); el._triggerOver=true; _showSgTip(t); });
    t.addEventListener('mouseleave',()=>{ t._over=false;
      const el=document.getElementById('wallTipPop'); if(el) el._triggerOver=false;
      _hideWallTip(); });
    t.addEventListener('focus',()=>_showSgTip(t));
    t.addEventListener('blur',()=>_hideWallTip());
  });
}

function renderSgTable(){
  const cols=[
    {key:'symbol',label:'SYMBOL'},
    {key:'expiry',label:'EXPIRY'},
    {key:'dte',label:'DTE',fmt:v=>fmt(v,0)},
    {key:'fut_price',label:'FUT',fmt:v=>fmt(v,1)},
    {key:'pe_strike',label:'PUT K',fmt:v=>fmt(v,0)},
    {key:'ce_strike',label:'CALL K',fmt:v=>fmt(v,0)},
    {key:'strangle_prem',label:'PREMIUM',fmt:v=>fmt(v,2)},
    {key:'width_pct',label:'WIDTH%',fmt:v=>v!=null?fmt(v,1)+'%':'—'},
    {key:'z_min',label:'Z MIN',fmt:v=>v==null?'—':
      `<span class="${v>=2.2?'up':v<1.6?'down':'neu'}">${fmt(v,2)}</span>`,
     help:'Breakeven distance on the NARROWER side, in units of realised vol over the remaining DTE. The side that breaks first.'},
    {key:'p_hold',label:'P(HOLD)',fmt:v=>v==null?'—':
      `<span class="${v>=92?'up':v<85?'down':'neu'}">${fmt(v,1)}%</span>`,
     help:'REALISED view: empirical probability both breakevens survive, from 725k observations, conditioned on the trailing vol band. Fat-tailed — a normal overstates by ~5pp at 2 sigma.'},
    {key:'p_otm',label:'P(OTM)',fmt:v=>v==null?'—':fmt(v,1)+'%',
     help:'IMPLIED view: P(finish between the breakevens) under Black-76 using each side\'s own IV. This is the number delta intuition gives you.'},
    {key:'edge',label:'EDGE',fmt:v=>v==null?'—':sspan(v,1),
     help:'P(HOLD) minus P(OTM), in percentage points. Positive = realised movement says the band is safer than the market is pricing it. CAVEAT: the two differ in vol source AND distribution, so part of this gap is fat tails (~-5pp at 2 sigma), not premium. Use it to rank, not to size.'},
    {key:'atm_iv',label:'IV',fmt:v=>fmt(v,1)},
    {key:'iv_change',label:'IV CHG',fmt:v=>v!=null?sspan(v,2):'—'},
    {key:'iv_pctile',label:'IV %ILE',fmt:v=>v!=null?fmt(v,0):'—'},
    {key:'hv20',label:'HV20',fmt:v=>v!=null?fmt(v,1):'—'},
    {key:'return_on_margin',label:'RoM%',fmt:v=>v!=null?sspan(v,2):'—'},
    {key:'_detail',label:'',sortable:false,
     fmt:(v,r)=>`<span class="wall-tip-trigger" tabindex="0" data-sgidx="${r._idx}">⊕</span>`},
  ];
  _sgRows.forEach((r,i)=>{ r._idx=i; });
  document.getElementById('sgTable').innerHTML=makeTbl(_sgRows,cols);
  _wireSgTips();
}

VIEW_INIT['delta'] = ()=>{
  loadDsTimestamps();  // populates dsTimestamp using best available symbol
  loadDsExpiries();    // cross-symbol live expiry list
  // Seed the liquidity floors from Settings so the saved defaults still apply,
  // but they are editable per-load and are sent to the API rather than being
  // used to hide rows after the fact.
  const oiEl=document.getElementById('dsMinOi'), volEl=document.getElementById('dsMinVol');
  if(oiEl && !oiEl.dataset.seeded){ oiEl.value=getSetting('stgDsMinOi')||0; oiEl.dataset.seeded='1'; }
  if(volEl && !volEl.dataset.seeded){ volEl.value=getSetting('stgDsMinVol')||0; volEl.dataset.seeded='1'; }
  // For premium lens: if plSymbol already has a selection, cascade it;
  // otherwise it will populate once user picks a symbol.
  const plSym=document.getElementById('plSymbol')?.value;
  if(plSym) loadCascade('plSymbol','plExpiry','plTimestamp');
};

// Cross-symbol expiry list for the delta screener. Reuses the OI-walls endpoint
// (distinct live expiries across ALL symbols) rather than /api/expiries, which
// is per-symbol and so cannot serve a screener that spans the whole universe.
async function loadDsExpiries(){
  const sel=document.getElementById('dsExpiry');
  if(!sel) return;
  try{
    const exps=await api('/api/oi_walls_expiries');
    const list=(exps||[]).map(e=>(typeof e==='string')?e:(e.expiry||''))
                         .filter(Boolean);
    const prev=sel.value;
    sel.innerHTML='<option value="all">ALL EXPIRIES</option>'
      +list.map(e=>`<option value="${e}">${e}</option>`).join('');
    if(prev && [...sel.options].some(o=>o.value===prev)) sel.value=prev;
  }catch(e){/* leave ALL EXPIRIES */}
}

// ══════════════════════════════════════════════════════════════════
// Button listeners
// ══════════════════════════════════════════════════════════════════
document.getElementById('btnDiv')?.addEventListener('click', loadDivergence);
document.getElementById('btnPl')?.addEventListener('click', loadPremiumLens);
document.getElementById('btnOiHist')?.addEventListener('click', loadOiHistory);
document.getElementById('plSymbol')?.addEventListener('change',()=>
  loadCascade('plSymbol','plExpiry','plTimestamp'));
document.getElementById('plExpiry')?.addEventListener('change',()=>
  reloadTimestamps('plSymbol','plExpiry','plTimestamp'));

// Populate plSymbol on init
document.addEventListener('DOMContentLoaded',()=>{
  // Wire after ALL_SYMS populated
});

// ══════════════════════════════════════════════════════════════════
// Item 5: Divergence
// ══════════════════════════════════════════════════════════════════
const DIV_SIGNALS = {
  BULL_HEDGE:   {label:'⬆ BULL HEDGE',   cls:'down',  desc:'Spot UP + PE premium UP — bearish hedge on rally'},
  BEAR_SQUEEZE: {label:'⬇ BEAR SQUEEZE', cls:'up',    desc:'Spot DOWN + CE premium UP — potential short squeeze'},
  IV_SPIKE:     {label:'⚡ IV SPIKE',     cls:'amber', desc:'Spot FLAT + IV spike — event anticipation'},
  SMART_SELL:   {label:'🔴 SMART SELL',  cls:'down',  desc:'Spot UP + CE premium DOWN — call sellers on rally (ceiling)'},
  SMART_BUY:    {label:'🟢 SMART BUY',   cls:'up',    desc:'Spot DOWN + PE premium DOWN — put sellers on dip (floor)'},
};

async function loadDivergence(){
  const mode  = document.querySelector('#divModeGroup .active')?.dataset.dm||'snapshot';
  const ft    = getFilter('divFilterGroup');
  const minSp = document.getElementById('divMinSpot')?.value||0.03;
  const minPr = document.getElementById('divMinPrem')?.value||0.5;
  document.getElementById('divTable').innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  try{
    const data=await api(`/api/divergence?mode=${mode}&filter_type=${ft}&min_spot_chg=${minSp}&min_prem_chg=${minPr}`);
    if(!data.length){
      document.getElementById('divTable').innerHTML='<div class="empty">No divergences detected with current thresholds</div>';
      return;
    }
    document.getElementById('divTable').innerHTML=makeTbl(data,[
      {key:'symbol',label:'SYMBOL'},
      {key:'signal',label:'SIGNAL',fmt:v=>{
        const s=DIV_SIGNALS[v]||{label:v,cls:'neu'};
        return`<span class="${s.cls}" title="${s.desc}">${s.label}</span>`;}},
      {key:'magnitude',label:'MAGNITUDE',fmt:v=>fmt(v,3)},
      {key:'spot_pct', label:'SPOT %',   fmt:v=>sspan(v,3)},
      {key:'ce_pct',   label:'CE PREM%', fmt:v=>sspan(v,3)},
      {key:'pe_pct',   label:'PE PREM%', fmt:v=>sspan(v,3)},
      {key:'ce_iv_chg',label:'CE IV Δ',  fmt:v=>sspan(v,2)},
      {key:'pe_iv_chg',label:'PE IV Δ',  fmt:v=>sspan(v,2)},
      {key:'spot_now', label:'SPOT',     fmt:v=>fmt(v,2)},
      {key:'net_flow', label:'NET FLOW', fmt:v=>sspan(v,2)},
      {key:'ts_now',   label:'TS NOW'},
      {key:'ts_ref',   label:'TS REF'},
    ]);
  }catch(e){
    document.getElementById('divTable').innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;
  }
}

// ══════════════════════════════════════════════════════════════════
// Item 8: Premium Lens
// ══════════════════════════════════════════════════════════════════
async function loadPremiumLens(){
  const sym     = document.getElementById('plSymbol')?.value;
  const exp     = document.getElementById('plExpiry')?.value;
  const ts      = document.getElementById('plTimestamp')?.value||'';
  const minOi   = document.getElementById('plMinOi')?.value||0;
  const minRatio= parseFloat(document.getElementById('plMinRatio')?.value)||0.85;
  const maxRatio= parseFloat(document.getElementById('plMaxRatio')?.value)||1.15;
  if(!sym||!exp){alert('Select symbol and expiry');return;}
  document.getElementById('plTable').innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  try{
    const tsParam=ts?`&timestamp=${encodeURIComponent(ts)}`:'';
    const d=await api(`/api/premium_lens?symbol=${encodeURIComponent(sym)}&expiry=${encodeURIComponent(exp)}${tsParam}&min_oi=${minOi}&min_ratio=${minRatio}&max_ratio=${maxRatio}`);
    const rows=d.rows||[];
    if(!rows.length){
      document.getElementById('plTable').innerHTML='<div class="empty">All options within normal price ratio range</div>';
      return;
    }
    const ltsSBadge=(s)=>s==='premium'?'<span class="ltp-s-prem">PREM</span>':s==='discount'?'<span class="ltp-s-disc">DISC</span>':'—';
    const ratioCls=(r)=>{
      if(r==null)return'';
      if(r>maxRatio)return'down';   // expensive
      if(r<minRatio)return'up';     // cheap
      return'neu';
    };
    document.getElementById('plTable').innerHTML=makeTbl(rows,[
      {key:'strike_price',label:'STRIKE'},
      {key:'distance_from_atm',label:'ATM DIST'},
      {key:'dte',label:'DTE',fmt:v=>fmt(v,0)+'d'},
      {key:'ce_ltp',label:'CE LTP',fmt:v=>fmt(v,2)},
      {key:'ce_tprice',label:'CE THEO',fmt:v=>fmt(v,2)},
      {key:'ce_ratio',label:'CE RATIO',fmt:(v,r)=>`<span class="${ratioCls(v)}">${v!=null?fmt(v,3):'—'}</span>`},
      {key:'ce_diff_pct',label:'CE DIFF%',fmt:v=>sspan(v,2)},
      {key:'ce_ltp_s',label:'CE',fmt:v=>ltsSBadge(v)},
      {key:'pe_ltp',label:'PE LTP',fmt:v=>fmt(v,2)},
      {key:'pe_tprice',label:'PE THEO',fmt:v=>fmt(v,2)},
      {key:'pe_ratio',label:'PE RATIO',fmt:(v,r)=>`<span class="${ratioCls(v)}">${v!=null?fmt(v,3):'—'}</span>`},
      {key:'pe_diff_pct',label:'PE DIFF%',fmt:v=>sspan(v,2)},
      {key:'pe_ltp_s',label:'PE',fmt:v=>ltsSBadge(v)},
      {key:'ce_iv',label:'CE IV%',fmt:v=>fmt(v,1)+'%'},
      {key:'pe_iv',label:'PE IV%',fmt:v=>fmt(v,1)+'%'},
      {key:'ce_oi',label:'CE OI',fmt:v=>fmtL(v)},
      {key:'pe_oi',label:'PE OI',fmt:v=>fmtL(v)},
    ]);
  }catch(e){
    document.getElementById('plTable').innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;
  }
}

// ══════════════════════════════════════════════════════════════════
// Item 9b: EOD History Heatmap
// ══════════════════════════════════════════════════════════════════
async function loadOiHistory(){
  const sym  = document.getElementById('oiSymbol')?.value;
  const days = document.getElementById('oiHistDays')?.value||1;
  const view = document.querySelector('#oiHistViewGroup .active')?.dataset.hv||'oi';
  const exp  = document.getElementById('oiHistExpiry')?.value;
  if(!sym){alert('Select a symbol in the OI Signals filter bar');return;}
  if(!exp){alert('Select an expiry');return;}
  setLoad('oiHistChart');
  try{
    const endpoint=view==='iv'?'iv_history':'oi_history';
    const d=await api(`/api/${endpoint}?symbol=${encodeURIComponent(sym)}&expiry=${encodeURIComponent(exp)}&days=${days}`);
    if(!d.dates?.length||!d.strikes?.length){
      setEmpty('oiHistChart','Not enough historical data — try days=1 for intraday');return;}

    // Build heatmap z-matrix
    const rowsMap={};
    (d.rows||[]).forEach(r=>{
      const k=r.strike_price;
      if(!rowsMap[k])rowsMap[k]={};
      rowsMap[k][r.date]= view==='iv'
        ?(r.iv_chg!=null?r.iv_chg:r.avg_iv)
        :(r.ce_oi_chg!=null?r.ce_oi_chg:r.ce_oi);
    });
    const z=d.strikes.map(s=>d.dates.map(dt=>rowsMap[s]?.[dt]??null));
    const colorscale=view==='iv'
      ?[['0','#f43f5e'],['0.5','#1e2d47'],['1','#10b981']]
      :[['0','#f43f5e'],['0.5','#1e2d47'],['1','#10b981']];
    plot('oiHistChart',[{
      type:'heatmap',
      x:d.dates,
      y:d.strikes.map(s=>s.toString()),
      z,
      colorscale,
      zmid:0,
      colorbar:{title:view==='iv'?'IV Δ':'OI Δ',thickness:12},
    }],{...LB,
      xaxis:{...LB.xaxis,title:'Date'},
      yaxis:{...LB.yaxis,title:'Strike'},
      height:Math.max(300,d.strikes.length*18),
    });
  }catch(e){setEmpty('oiHistChart','⚠ '+e.message);}
}

// ══════════════════════════════════════════════════════════════════
// Item 7: IV intraday trend on overview cards
// (Loaded alongside overview, shown as small sparkline indicator)
// ══════════════════════════════════════════════════════════════════
let _ivTrend={};

async function loadIvIntradayTrend(ft){
  try{
    const data=await api(`/api/iv_intraday_trend?filter_type=${ft}`);
    _ivTrend={};
    data.forEach(r=>{_ivTrend[r.symbol]=r;});
  }catch(e){console.warn('IV trend failed:',e.message);}
}

// Patch loadOverview to also fetch IV trend
const _origLoadOverview=loadOverview;
loadOverview=async function(){
  const ft=getFilter('ovFilterGroup');
  await Promise.all([_origLoadOverview(), loadIvIntradayTrend(ft)]);
  // Inject IV trend badge into each card
  Object.entries(_ivTrend).forEach(([sym,t])=>{
    const cards=document.querySelectorAll('.tc');
    cards.forEach(card=>{
      if(card.querySelector('.tc-sym')?.textContent===sym){
        const existing=card.querySelector('.iv-trend-badge');
        if(existing)existing.remove();
        const badge=document.createElement('div');
        badge.className='tc-row iv-trend-badge';
        badge.style.marginTop='4px';
        const dir=t.iv_chg>0?'⬆':'⬇';
        const cls=t.iv_chg>0?'down':'up';  // rising IV = bearish for premium sellers
        badge.innerHTML=`<div><div class="tc-key">IV TREND</div>
          <div class="tc-val ${cls}">${dir} ${fmt(t.iv_chg,2)}% (${fmt(t.iv_chg_pct,1)}%)</div></div>
          <div><div class="tc-key">IV NOW</div><div class="tc-val">${fmt(t.iv_now,1)}%</div></div>`;
        card.appendChild(badge);
      }
    });
  });
};

function toggleDivLegend(){
  const el=document.getElementById('divLegend');
  if(el)el.style.display=el.style.display==='none'?'':'none';
}

// ── Table utilities ─────────────────────────────────────────────
function filterTableBySymbol(tableContainerId, query){
  const container=document.getElementById(tableContainerId);
  if(!container)return;
  const q=query.trim().toUpperCase();
  // Find the SYMBOL column from the header rather than assuming cell 0. It IS
  // cell 0 on OI Walls, but the delta screener leads with TYPE (CE/PE), so a
  // hardcoded 0 matched every keystroke against "CE"/"PE" and blanked the grid.
  let idx=0;
  const heads=container.querySelectorAll('thead th');
  for(let i=0;i<heads.length;i++){
    if((heads[i].textContent||'').trim().toUpperCase()==='SYMBOL'){ idx=i; break; }
  }
  const rows=container.querySelectorAll('tbody tr');
  rows.forEach(row=>{
    const sym=row.cells[idx]?.textContent.trim().toUpperCase()||'';
    row.style.display=(!q||sym.includes(q))?'':'none';
  });
}

function filterCardsBySymbol(containerId, query){
  const q = (query||'').trim().toUpperCase();
  const cont = document.getElementById(containerId);
  if(!cont) return;
  cont.querySelectorAll('.tc').forEach(card=>{
    const sym = (card.querySelector('.tc-sym')?.textContent||'').toUpperCase();
    card.style.display = (!q || sym.includes(q)) ? '' : 'none';
  });
}

function jumpOiSignals(sym){
  // Switch to OI Signals tab and pre-select the symbol
  document.querySelector('[data-view="oi"]').click();
  setTimeout(()=>{
    const sel=document.getElementById('oiSymbol');
    if(sel&&[...sel.options].some(o=>o.value===sym)){
      sel.value=sym;
      document.getElementById('btnOi')?.click();
    }
  },80);
}


// ══════════════════════════════════════════════════════════════════
// OI Flow Bucket Analysis
// ══════════════════════════════════════════════════════════════════
const BUCKET_NAMES  = ['DEEP_ITM','ATM','NEAR_OTM','FAR_OTM','DEEP_OTM'];
const BUCKET_LABELS = {
  DEEP_ITM:'DEEP ITM (|δ|≥0.5)',
  ATM:'ATM (|δ| 0.3-0.5)',
  NEAR_OTM:'NEAR OTM (|δ| 0.15-0.3)',
  FAR_OTM:'FAR OTM (|δ| 0.05-0.15)',
  DEEP_OTM:'DEEP OTM (|δ|<0.05)',
};
const BUCKET_COLORS = {
  DEEP_ITM: C.acc2,
  ATM:      C.acc,
  NEAR_OTM: C.green,
  FAR_OTM:  C.pur,
  DEEP_OTM: C.muted,
};
const DTE_REGIME_STYLE = {
  expiry: {bg:'rgba(244,63,94,.12)',color:'var(--red)',  label:'⚠ EXPIRY DAY — delta unstable, interpret with caution'},
  short:  {bg:'rgba(245,158,11,.10)',color:'var(--amber)',label:'SHORT TERM (1–7 DTE) — gamma dominant'},
  medium: {bg:'rgba(0,200,240,.06)', color:'var(--acc)',  label:'MEDIUM TERM (8–21 DTE)'},
  long:   {bg:'rgba(139,92,246,.08)',color:'var(--pur)',  label:'LONG TERM (>21 DTE) — vega dominant'},
};
const FLOW_TYPE_COLORS = {
  'Speculative':'var(--acc2)',
  'Hedging':'var(--green)',
  'Squeeze Setup':'var(--amber)',
  'Panic Flow':'var(--red)',
  'Dealer Positioning':'var(--muted)',
  'Mixed':'var(--text)',
};

// Wire toggle group
wireToggleGroup('flowViewGroup');
document.getElementById('flowViewGroup')?.addEventListener('click',()=>{
  if(_flowLastData) renderFlowCharts(_flowLastData);
});

// Set default dates to today
(()=>{
  const today=new Date().toLocaleDateString('sv-SE'); // YYYY-MM-DD
  const df=document.getElementById('flowDateFrom');
  const dt=document.getElementById('flowDateTo');
  if(df&&!df.value) df.value=today;
  if(dt&&!dt.value) dt.value=today;
})();

document.getElementById('btnFlowToday')?.addEventListener('click',()=>{
  const today=new Date().toLocaleDateString('sv-SE');
  document.getElementById('flowDateFrom').value=today;
  document.getElementById('flowDateTo').value=today;
});
document.getElementById('btnFlowLoad')?.addEventListener('click', loadFlowBuckets);
document.getElementById('oiSymbol')?.addEventListener('change', async()=>{
  const sym=document.getElementById('flowSymbol').value;
  if(!sym) return;
  const showPast=document.getElementById('flowExpiryShowPast')?.checked;
  try{
    const exps=await api(`/api/expiries?symbol=${encodeURIComponent(sym)}&future_only=${!showPast}`);
    const sel=document.getElementById('flowExpiry');
    sel.innerHTML='<option value="all">ALL EXPIRIES</option>';
    exps.forEach(e=>{sel.innerHTML+=`<option value="${e}">${e}</option>`;});
  }catch(e){}
});

let _flowLastData=null;

async function loadFlowBuckets(){
  const sym    = document.getElementById('oiSymbol')?.value;
  const expiry = document.getElementById('flowExpiry')?.value||'all';
  const dFrom  = document.getElementById('flowDateFrom')?.value;
  const dTo    = document.getElementById('flowDateTo')?.value;
  const minVol = document.getElementById('flowMinVol')?.value||0;
  if(!sym){alert('Select a symbol');return;}

  const bAtm   = getSetting('stgBktAtm');
  const bNear  = getSetting('stgBktNear');
  const bFar   = getSetting('stgBktFar');
  const bDeep  = getSetting('stgBktDeep');
  const stable = getSetting('stgBktStable');
  const cross  = getSetting('stgBktCross');
  const minOi  = getSetting('stgDsMinOi')||0;
  const maxBaq = getSetting('stgBaqBad')||15;

  setLoad('flowMainChart');
  ['flowVelChart','flowImbalChart'].forEach(id=>setLoad(id));
  document.getElementById('flowSignalTable').innerHTML=
    '<div class="loading"><div class="spinner"></div>Computing…</div>';
  document.getElementById('flowMigTable').innerHTML='';
  document.getElementById('flowDteBanner').style.display='none';
  document.getElementById('flowCrossSignal').style.display='none';

  try{
    const params=new URLSearchParams({
      symbol:sym, expiry, date_from:dFrom||'', date_to:dTo||'',
      min_oi:minOi, min_volume:minVol, max_baq_pct:maxBaq,
      b_atm:bAtm, b_near:bNear, b_far:bFar, b_deep:bDeep,
      stable_pct:stable, cross_pct:cross,
    });
    const d=await api(`/api/oi_flow_buckets?${params}`);
    _flowLastData=d;
    renderFlowCharts(d);
  }catch(e){
    setEmpty('flowMainChart','⚠ '+e.message);
  }
}

function renderFlowCharts(d){
  if(!d||!d.buckets) return;

  // ── DTE regime banner ──
  const reg=DTE_REGIME_STYLE[d.dte_regime]||DTE_REGIME_STYLE.medium;
  const banner=document.getElementById('flowDteBanner');
  banner.style.display='';
  banner.style.background=reg.bg;
  banner.style.color=reg.color;
  banner.textContent=`DTE: ${d.dte} — ${reg.label}`;

  // ── Crossing signal ──
  const cs=d.crossing_signal;
  const csEl=document.getElementById('flowCrossSignal');
  if(cs&&cs.fired){
    csEl.style.display='';
    csEl.textContent='⚡ CROSSING SIGNAL: '+cs.description;
  } else {
    csEl.style.display='none';
  }

  const view=document.querySelector('#flowViewGroup .active')?.dataset.fv||'oi';

  // ── Main chart ──
  const traces=[];
  const anyTs=d.timestamps||[];

  BUCKET_NAMES.forEach(bname=>{
    const b=d.buckets[bname];
    if(!b||!b.timestamps?.length) return;
    const col=BUCKET_COLORS[bname];
    const lbl=BUCKET_LABELS[bname];

    let yce, ype, yname;
    if(view==='gex'){
      // Scale GEX to same visual range as OI by using Y2 axis
      yce=b.ce_gexv; ype=b.pe_gexv; yname='GEX ₹M';
    } else if(view==='velocity'){
      yce=b.ce_velocity; ype=b.pe_velocity; yname='Velocity';
    } else {
      yce=b.ce_cum_flow; ype=b.pe_cum_flow; yname='Cumulative OI';
    }

    // CE solid line
    traces.push({
      name:`CE ${lbl}`,
      x:b.timestamps, y:yce,
      type:'scatter',mode:'lines',
      line:{color:col,width:2,dash:'solid'},
      legendgroup:bname,
    });
    // PE dashed line
    traces.push({
      name:`PE ${lbl}`,
      x:b.timestamps, y:ype,
      type:'scatter',mode:'lines',
      line:{color:col,width:1.5,dash:'dash'},
      legendgroup:bname,
      showlegend:false,
    });

    // Red fill where CE cum flow goes negative
    if(view==='oi'&&yce){
      const negX=[], negY=[];
      b.timestamps.forEach((t,i)=>{
        if((yce[i]||0)<0){negX.push(t);negY.push(yce[i]);}
      });
      if(negX.length){
        traces.push({
          name:'CE exits',x:negX,y:negY,type:'scatter',mode:'none',
          fill:'tozeroy',fillcolor:'rgba(244,63,94,.15)',
          showlegend:false,legendgroup:bname,hoverinfo:'skip',
        });
      }
    }
  });

  // PCR for ATM and NEAR_OTM on Y2
  ['ATM','NEAR_OTM'].forEach(bname=>{
    const b=d.buckets[bname];
    if(!b?.pcr?.length) return;
    traces.push({
      name:`PCR ${bname}`,
      x:b.timestamps, y:b.pcr,
      type:'scatter',mode:'lines+markers',
      line:{color:BUCKET_COLORS[bname],width:1,dash:'dot'},
      marker:{size:3},
      yaxis:'y2',
      legendgroup:bname+'pcr',
    });
  });

  plot('flowMainChart', traces, {
    ...LB,
    xaxis:{...LB.xaxis,title:'Time',tickangle:-30},
    yaxis:{...LB.yaxis,title:view==='gex'?'GEX ₹M':view==='velocity'?'Velocity':'Cumulative OI'},
    yaxis2:{overlaying:'y',side:'right',title:'PCR',
      gridcolor:'transparent',tickfont:{size:9},range:[0,3]},
    legend:{...LB.legend,orientation:'h',y:-0.2},
    height:480,
  });

  // ── Velocity / Acceleration chart ──
  const velTraces=[];
  BUCKET_NAMES.forEach(bname=>{
    const b=d.buckets[bname];
    if(!b?.ce_velocity?.length) return;
    velTraces.push({
      name:`Vel ${BUCKET_LABELS[bname]}`,
      x:b.timestamps,y:b.ce_velocity,
      type:'bar',marker:{color:BUCKET_COLORS[bname],opacity:.7},
      legendgroup:'v'+bname,showlegend:true,
    });
    velTraces.push({
      name:`Acc ${BUCKET_LABELS[bname]}`,
      x:b.timestamps,y:b.ce_acceleration,
      type:'scatter',mode:'lines+markers',
      line:{color:BUCKET_COLORS[bname],width:1.5,dash:'dot'},
      marker:{size:3,symbol:'circle'},
      yaxis:'y2',legendgroup:'v'+bname,showlegend:true,
    });
  });
  if(velTraces.length){
    plot('flowVelChart',velTraces,{
      ...LB,barmode:'group',
      xaxis:{...LB.xaxis,title:'Time',tickangle:-30},
      yaxis:{...LB.yaxis,title:'CE Velocity (bars)'},
      yaxis2:{overlaying:'y',side:'right',title:'CE Accel (dots)',
        gridcolor:'transparent',tickfont:{size:9}},
      legend:{...LB.legend,orientation:'h',y:-0.35,font:{size:8}},
      height:260,
      margin:{...LB.margin,b:80},
    });
  }

  // ── Buy/sell imbalance bars ──
  const imbTraces=BUCKET_NAMES.map(bname=>{
    const b=d.buckets[bname];
    const lastCe=b?.ce_tbq?.[b.ce_tbq.length-1]||0;
    const lastPe=b?.pe_tbq?.[b.pe_tbq.length-1]||0;
    const imb=lastCe-lastPe;
    return {bname,imb,col:BUCKET_COLORS[bname]};
  });
  plot('flowImbalChart',[{
    x:imbTraces.map(t=>BUCKET_LABELS[t.bname]),
    y:imbTraces.map(t=>t.imb),
    type:'bar',
    marker:{color:imbTraces.map(t=>t.imb>=0?C.green:C.red),opacity:.8},
    text:imbTraces.map(t=>t.imb>=0?'BUYER DOM':'SELLER DOM'),
    textposition:'outside',
    textfont:{size:8,family:"'JetBrains Mono',monospace"},
  }],{
    ...LB,
    xaxis:{...LB.xaxis,tickangle:-20,automargin:true},
    yaxis:{...LB.yaxis,title:'CE TBQ – PE TBQ'},
    shapes:[{type:'line',x0:-0.5,x1:4.5,y0:0,y1:0,
      line:{color:C.muted,width:1,dash:'dot'}}],
    height:220,
  });

  // ── Signal table ──
  const sigRows=BUCKET_NAMES.map(bname=>{
    const s=d.flow_signals?.[bname]||{};
    return {
      bucket:    BUCKET_LABELS[bname],
      ce_signal: s.ce_signal||'—',
      pe_signal: s.pe_signal||'—',
      flow_type: s.flow_type||'—',
      buy_imb:   s.buy_imbalance!=null?s.buy_imbalance:null,
      ce_cum:    s.ce_cum_flow!=null?s.ce_cum_flow:null,
      pe_cum:    s.pe_cum_flow!=null?s.pe_cum_flow:null,
      net_cum:   s.net_cum_flow!=null?s.net_cum_flow:null,
      pcr:       s.pcr!=null?s.pcr:null,
      n_strikes: s.strike_count||0,
      migrated:  s.migrated_count||0,
      min_strike: s.min_strike??null,
      max_strike: s.max_strike??null,
    };
  });
  document.getElementById('flowSignalTable').innerHTML=makeTbl(sigRows,[
    {key:'bucket',    label:'BUCKET'},
    {key:'ce_signal', label:'CE SIGNAL', fmt:v=>pill(v)},
    {key:'pe_signal', label:'PE SIGNAL', fmt:v=>pill(v)},
    {key:'flow_type', label:'FLOW TYPE', fmt:v=>{
      const col=FLOW_TYPE_COLORS[v]||'var(--text)';
      return`<span style="font-family:var(--mono);font-size:9px;font-weight:600;color:${col}">${v}</span>`;}},
    {key:'ce_cum',    label:'CE CUM OI', fmt:v=>v!=null?sspan(v,0):'—'},
    {key:'pe_cum',    label:'PE CUM OI', fmt:v=>v!=null?sspan(v,0):'—'},
    {key:'net_cum',   label:'NET FLOW',  fmt:v=>v!=null?sspan(v,0):'—'},
    {key:'pcr',       label:'PCR',       fmt:v=>v!=null?fmt(v,3):'—'},
    {key:'buy_imb',   label:'BUY IMB',   fmt:v=>v!=null?sspan(v,3):'—'},
    {key:'min_strike', label:'MIN STRIKE', fmt:v=>v!=null?fmt(v,0):'—'},
    {key:'max_strike', label:'MAX STRIKE', fmt:v=>v!=null?fmt(v,0):'—'},
    {key:'n_strikes', label:'N STRIKES'},
    {key:'migrated',  label:'MIGRATED',  fmt:v=>v>0?`<span class="down">${v}</span>`:v},
  ]);

  // ── Migrated strikes table ──
  const migEl=document.getElementById('flowMigTable');
  if(d.migrated_strikes?.length){
    migEl.innerHTML=makeTbl(d.migrated_strikes,[
      // "SIDE" column — shows which side actually crossed a bucket boundary
      {key:'ce_migrated', label:'SIDE',
        fmt:(v,r)=>{
          const ce=r.ce_migrated, pe=r.pe_migrated;
          if(ce&&pe) return'<span class="down">CE+PE</span>';
          if(ce)     return'<span class="down">CE</span>';
          if(pe)     return'<span class="down">PE</span>';
          return'—';
        }},
      {key:'strike',         label:'STRIKE'},
      // CE side — dim the whole CE section if CE did NOT migrate
      {key:'ce_delta',       label:'CE |δ|',
        fmt:(v,r)=>`<span style="opacity:${r.ce_migrated?1:0.45}">${fmt(v,3)}</span>`},
      {key:'ce_bucket_open', label:'CE OPEN',
        fmt:(v,r)=>r.ce_migrated
          ?`<span style="color:var(--muted)">${v}</span>`
          :`<span style="opacity:0.45">${v}</span>`},
      {key:'ce_bucket_cur',  label:'CE NOW',
        fmt:(v,r)=>r.ce_migrated
          ?`<span style="color:var(--amber)">→ ${v}</span>`
          :`<span style="color:var(--green);opacity:0.45">${v}</span>`},
      {key:'ce_oi',          label:'CE OI',   fmt:v=>fmtL(v)},
      {key:'ce_oi_chg',      label:'CE ΔOI',  fmt:v=>sspan(v,0)},
      // PE side — dim if PE did NOT migrate
      {key:'pe_delta',       label:'PE |δ|',
        fmt:(v,r)=>`<span style="opacity:${r.pe_migrated?1:0.45}">${fmt(v,3)}</span>`},
      {key:'pe_bucket_open', label:'PE OPEN',
        fmt:(v,r)=>r.pe_migrated
          ?`<span style="color:var(--muted)">${v}</span>`
          :`<span style="opacity:0.45">${v}</span>`},
      {key:'pe_bucket_cur',  label:'PE NOW',
        fmt:(v,r)=>r.pe_migrated
          ?`<span style="color:var(--amber)">→ ${v}</span>`
          :`<span style="color:var(--green);opacity:0.45">${v}</span>`},
      {key:'pe_oi',          label:'PE OI',   fmt:v=>fmtL(v)},
      {key:'pe_oi_chg',      label:'PE ΔOI',  fmt:v=>sspan(v,0)},
    ]);
  } else {
    migEl.innerHTML='<div class="empty">No migrations — all strikes in opening bucket</div>';
  }
}

// Populate flowSymbol on init

async function refreshFlowExpiries(){
  const sym=document.getElementById('oiSymbol')?.value;
  if(!sym) return;
  const showPast=document.getElementById('flowExpiryShowPast')?.checked;
  try{
    const exps=await api(`/api/expiries?symbol=${encodeURIComponent(sym)}&future_only=${!showPast}`);
    const sel=document.getElementById('flowExpiry');
    if(!sel) return;
    sel.innerHTML='<option value="all">ALL EXPIRIES</option>';
    exps.forEach(e=>{sel.innerHTML+=`<option value="${e}">${e}</option>`;});
  }catch(e){console.warn('refreshFlowExpiries:',e.message);}
}


// ══════════════════════════════════════════════════════════════════
// Smart Money Flow — Premium-weighted OI clustering
// ══════════════════════════════════════════════════════════════════

// Colour palette for cluster tracks (up to 8 CE + 8 PE clusters)
const SM_CE_COLORS = ['#00c8f0','#10b981','#f59e0b','#8b5cf6','#f43f5e','#06b6d4','#84cc16','#fb923c'];
const SM_PE_COLORS = ['#f43f5e','#f59e0b','#10b981','#00c8f0','#8b5cf6','#fb923c','#84cc16','#06b6d4'];
const SM_STATUS_STYLE = {
  TRACKED:  {dash:'solid',  opacity:1.0},
  EMERGING: {dash:'dot',    opacity:0.8},
  DISSOLVED:{dash:'dashdot',opacity:0.4},
};

// ── Init ──────────────────────────────────────────────────────────
(()=>{
  const today=new Date().toLocaleDateString('sv-SE');
  const d5=new Date(Date.now()-5*86400000).toLocaleDateString('sv-SE');
  ['smDateFrom'].forEach(id=>{const el=document.getElementById(id);if(el&&!el.value)el.value=d5;});
  ['smDateTo','smIntradayDate'].forEach(id=>{const el=document.getElementById(id);if(el&&!el.value)el.value=today;});
})();

wireToggleGroup('smModeGroup');
document.getElementById('smModeGroup')?.addEventListener('click',()=>{
  const mode=document.querySelector('#smModeGroup .active')?.dataset.sm||'multiday';
  document.getElementById('smMultiDayCtrl').style.display=mode==='multiday'?'flex':'none';
  document.getElementById('smIntraDayCtrl').style.display=mode==='intraday'?'flex':'none';
});
document.getElementById('btnSmLoad')?.addEventListener('click', loadSmartMoney);

// Populate smExpiry when oiSymbol changes (shared top-bar ticker)
async function refreshSmExpiries(){
  const sym=document.getElementById('oiSymbol')?.value;
  if(!sym) return;
  const showPast=document.getElementById('smExpiryPast')?.checked;
  try{
    const exps=await api(`/api/expiries?symbol=${encodeURIComponent(sym)}&future_only=${!showPast}`);
    const sel=document.getElementById('smExpiry');
    if(!sel) return;
    sel.innerHTML='<option value="all">ALL EXPIRIES</option>';
    exps.forEach(e=>{sel.innerHTML+=`<option value="${e}">${e}</option>`;});
  }catch(e){}
}

// Wire to subtab open and oiSymbol change
document.querySelectorAll('.subtab').forEach(tab=>{
  if(tab.dataset.stab==='oi-smart')
    tab.addEventListener('click',()=>refreshSmExpiries().catch(()=>{}));
});

let _smLastData=null;

async function loadSmartMoney(){
  const sym     = document.getElementById('oiSymbol')?.value;
  const expiry  = document.getElementById('smExpiry')?.value||'all';
  const mode    = document.querySelector('#smModeGroup .active')?.dataset.sm||'multiday';
  const dFrom   = document.getElementById('smDateFrom')?.value||'';
  const dTo     = document.getElementById('smDateTo')?.value||'';
  const iDate   = document.getElementById('smIntradayDate')?.value||'';
  const minVol  = document.getElementById('smMinVol')?.value||100;
  const smooth  = document.getElementById('smSmoothing')?.value||3;
  const minProm = document.getElementById('smMinProm')?.value||10;
  const minOi   = getSetting('stgDsMinOi')||0;
  const maxBaq  = getSetting('stgBaqBad')||15;

  if(!sym){alert('Select a symbol in the OI Signals filter bar');return;}

  ['smMigChart','smPremFlowChart','smOiVolChart'].forEach(id=>setLoad(id));
  document.getElementById('smClusterTable').innerHTML=
    '<div class="loading"><div class="spinner"></div>Clustering…</div>';

  try{
    const params=new URLSearchParams({
      symbol:sym, expiry, mode,
      date_from:dFrom, date_to:dTo,
      intraday_date:iDate,
      min_oi:minOi, min_volume:minVol,
      max_baq_pct:maxBaq,
      smoothing:smooth, min_prom_pct:minProm,
    });
    const d=await api(`/api/smart_money_flow?${params}`);
    _smLastData=d;
    renderSmartMoney(d);
  }catch(e){
    ['smMigChart','smPremFlowChart','smOiVolChart'].forEach(id=>setEmpty(id,'⚠ '+e.message));
    document.getElementById('smClusterTable').innerHTML=
      `<div class="empty err">⚠ ${e.message}</div>`;
  }
}

function renderSmartMoney(d){
  if(!d) return;
  const periods = d.periods||[];
  const spots   = d.spots||{};
  const ceTracks= d.ce_tracks||[];
  const peTracks= d.pe_tracks||[];

  // ── Migration chart: strike vs period, one line per track ──────────
  const migTraces=[];

  // Spot price reference line
  if(periods.length){
    migTraces.push({
      name:'Spot',
      x:periods,
      y:periods.map(p=>spots[p]||null),
      type:'scatter',mode:'lines',
      line:{color:'rgba(255,255,255,0.3)',width:1,dash:'dot'},
      showlegend:true,
    });
  }

  ceTracks.forEach((t,i)=>{
    if(!t.points?.length) return;
    const sty=SM_STATUS_STYLE[t.status]||SM_STATUS_STYLE.TRACKED;
    const col=SM_CE_COLORS[i%SM_CE_COLORS.length];
    // Line of center_strike over periods
    migTraces.push({
      name:`CE #${i+1} (${t.status})`,
      x:t.points.map(p=>p.period),
      y:t.points.map(p=>p.center_strike),
      type:'scatter',mode:'lines+markers',
      line:{color:col,width:3*sty.opacity,dash:sty.dash},
      marker:{size:t.points.map(p=>Math.max(4,Math.min(20,
        Math.sqrt(Math.abs(p.tv_weight||0))/500))),
        color:col,opacity:sty.opacity},
      text:t.points.map(p=>
        `δ: ${fmt(p.avg_delta,3)}<br>IV: ${fmt(p.avg_iv,1)}%<br>`+
        `Prem OI: ${fmtL(p.premium_oi)}<br>TV wt: ${fmtL(p.tv_weight)}<br>`+
        `Conc: ${fmt(p.concentration_ratio,2)}<br>`+
        `PCR: ${p.pcr!=null?fmt(p.pcr,3):'—'}`),
      hovertemplate:'%{text}<extra>CE #'+(i+1)+'</extra>',
      legendgroup:'ce'+(i+1),
    });
    // Min-max range band
    migTraces.push({
      name:`CE #${i+1} range`,
      x:[...t.points.map(p=>p.period),...t.points.map(p=>p.period).reverse()],
      y:[...t.points.map(p=>p.max_strike),...t.points.map(p=>p.min_strike).reverse()],
      type:'scatter',mode:'none',fill:'toself',
      fillcolor:`rgba(${parseInt(col.slice(1,3),16)},${parseInt(col.slice(3,5),16)},${parseInt(col.slice(5,7),16)},0.08)`,
      showlegend:false,hoverinfo:'skip',legendgroup:'ce'+(i+1),
    });
  });

  peTracks.forEach((t,i)=>{
    if(!t.points?.length) return;
    const sty=SM_STATUS_STYLE[t.status]||SM_STATUS_STYLE.TRACKED;
    const col=SM_PE_COLORS[i%SM_PE_COLORS.length];
    migTraces.push({
      name:`PE #${i+1} (${t.status})`,
      x:t.points.map(p=>p.period),
      y:t.points.map(p=>p.center_strike),
      type:'scatter',mode:'lines+markers',
      line:{color:col,width:2*sty.opacity,dash:'dash'},
      marker:{size:t.points.map(p=>Math.max(4,Math.min(20,
        Math.sqrt(Math.abs(p.tv_weight||0))/500))),
        color:col,symbol:'diamond',opacity:sty.opacity},
      text:t.points.map(p=>
        `δ: ${fmt(p.avg_delta,3)}<br>IV: ${fmt(p.avg_iv,1)}%<br>`+
        `Prem OI: ${fmtL(p.premium_oi)}<br>TV wt: ${fmtL(p.tv_weight)}<br>`+
        `Conc: ${fmt(p.concentration_ratio,2)}<br>`+
        `PCR: ${p.pcr!=null?fmt(p.pcr,3):'—'}`),
      hovertemplate:'%{text}<extra>PE #'+(i+1)+'</extra>',
      legendgroup:'pe'+(i+1),
    });
  });

  // Shared y-axis range for both CE and PE charts
  const allY=[...(ceTracks||[]),...(peTracks||[])]
    .flatMap(t=>t.points?.map(p=>p.center_strike)||[]).filter(Boolean);
  const sVals=Object.values(d.spots||{}).filter(Boolean);
  const yAll=[...allY,...sVals];
  const yRange=yAll.length?[Math.min(...yAll)*0.995,Math.max(...yAll)*1.005]:undefined;
  const mBase={...LB,
    xaxis:{...LB.xaxis,title:d.mode==='intraday'?'Time':'Date',tickangle:-30},
    yaxis:{...LB.yaxis,title:'Strike',...(yRange?{range:yRange}:{autorange:true})},
    legend:{...LB.legend,orientation:'h',y:-0.3,font:{size:8}},height:340};
  const ceT=migTraces.filter(t=>t.name?.startsWith('CE')||t.name==='Spot');
  const peT=migTraces.filter(t=>t.name?.startsWith('PE')||t.name==='Spot');
  plot('smMigChartCE',ceT.length?ceT:[{x:[],y:[],type:'scatter',name:'No CE clusters'}],{...mBase});
  plot('smMigChartPE',peT.length?peT:[{x:[],y:[],type:'scatter',name:'No PE clusters'}],{...mBase});

  // ── Premium flow bar chart (latest period only) ────────────────────
  const latestP=periods[periods.length-1];
  const pfData=[];
  [...ceTracks.map((t,i)=>({t,i,side:'CE',cols:SM_CE_COLORS})),
   ...peTracks.map((t,i)=>({t,i,side:'PE',cols:SM_PE_COLORS}))].forEach(({t,i,side,cols})=>{
    const pt=t.points.find(p=>p.period===latestP);
    if(!pt) return;
    pfData.push({
      label:`${side} #${i+1}\n${fmt(pt.center_strike,0)}`,
      val:pt.prem_flow||0,
      col:side==='CE'?SM_CE_COLORS[i%8]:SM_PE_COLORS[i%8],
    });
  });

  if(pfData.length){
    plot('smPremFlowChart',[{
      x:pfData.map(r=>r.label),
      y:pfData.map(r=>r.val),
      type:'bar',
      marker:{color:pfData.map(r=>r.val>=0?C.green:C.red),opacity:.8},
      text:pfData.map(r=>`₹${fmtL(Math.abs(r.val))}`),
      textposition:'outside',textfont:{size:8,family:"'JetBrains Mono',monospace"},
    }],{...LB,
      xaxis:{...LB.xaxis,tickangle:-20,automargin:true},
      yaxis:{...LB.yaxis,title:'Premium Flow (ltp × ΔOI)'},
      shapes:[{type:'line',x0:-0.5,x1:pfData.length-0.5,y0:0,y1:0,
        line:{color:C.muted,width:1,dash:'dot'}}],
      height:250,
    });
  }

  // ── OI/Vol ratio heatmap across periods ───────────────────────────
  const ovLabels=[
    ...ceTracks.map((t,i)=>`CE #${i+1} ${fmt((t.points[0]||{}).center_strike||0,0)}`),
    ...peTracks.map((t,i)=>`PE #${i+1} ${fmt((t.points[0]||{}).center_strike||0,0)}`),
  ];
  const ovZ=[];
  ceTracks.forEach(t=>{
    ovZ.push(periods.map(p=>{
      const pt=t.points.find(q=>q.period===p);
      return pt?.oi_vol_ratio??null;
    }));
  });
  peTracks.forEach(t=>{
    ovZ.push(periods.map(p=>{
      const pt=t.points.find(q=>q.period===p);
      return pt?.oi_vol_ratio??null;
    }));
  });

  if(ovZ.length){
    plot('smOiVolChart',[{
      type:'heatmap',
      x:periods, y:ovLabels, z:ovZ,
      colorscale:[['0','#f43f5e'],['0.5','#1e2d47'],['1','#10b981']],
      zmid:0,
      colorbar:{title:'OI/Vol',thickness:12},
      hovertemplate:'%{y}<br>%{x}<br>OI/Vol: %{z:.3f}<extra></extra>',
    }],{...LB,
      xaxis:{...LB.xaxis,title:'Period',tickangle:-30},
      yaxis:{...LB.yaxis,automargin:true},
      height:Math.max(200,(ovLabels.length)*40+80),
    });
  }

  // ── Cluster detail table — latest period ──────────────────────────
  const rows=[];
  const addRows=(tracks,side,cols)=>{
    tracks.forEach((t,i)=>{
      const pt=t.points.find(p=>p.period===latestP);
      if(!pt) return;
      rows.push({
        side, id:`#${i+1}`, status:t.status,
        peak_strike:pt.peak_strike, center:pt.center_strike, range:`${fmt(pt.min_strike,0)}–${fmt(pt.max_strike,0)}`,
        prem_oi:pt.premium_oi, tv_weight:pt.tv_weight,
        conc:pt.concentration_ratio,
        avg_delta:pt.avg_delta, avg_iv:pt.avg_iv,
        prem_flow:pt.prem_flow,
        oi_vol:pt.oi_vol_ratio,
        gamma_adj:pt.gamma_adj_oi,
        pcr:pt.pcr,
        buy_imb:pt.buy_imbalance,
        velocity:pt.velocity,
        acceleration:pt.acceleration,
        n_strikes:pt.member_count,
        days_tracked:t.points.length,
      });
    });
  };
  addRows(ceTracks,'CE',SM_CE_COLORS);
  addRows(peTracks,'PE',SM_PE_COLORS);

  document.getElementById('smClusterTable').innerHTML=makeTbl(rows,[
    {key:'side',        label:'SIDE',   fmt:v=>`<span class="${v==='CE'?'up':'down'}">${v}</span>`},
    {key:'id',          label:'CLUSTER'},
    {key:'status',      label:'STATUS', fmt:v=>{
      const c={TRACKED:'var(--green)',EMERGING:'var(--amber)',DISSOLVED:'var(--muted)'};
      const col=c[v]||'var(--text)';
      return`<span style="color:${col};font-family:var(--mono);font-size:9px">${v}</span>`;}},
    {key:'peak_strike', label:'PEAK STR',fmt:v=>fmt(v,0)},
    {key:'center',      label:'CENTROID', fmt:v=>fmt(v,1)},
    {key:'range',       label:'RANGE'},
    {key:'prem_oi',     label:'PREM OI', fmt:v=>fmtL(v)},
    {key:'tv_weight',   label:'TV WEIGHT',fmt:v=>fmtL(v)},
    {key:'conc',        label:'CONC',    fmt:v=>v!=null?fmt(v,3):'—'},
    {key:'avg_delta',   label:'AVG |δ|', fmt:v=>fmt(v,3)},
    {key:'avg_iv',      label:'AVG IV%', fmt:v=>fmt(v,1)+'%'},
    {key:'prem_flow',   label:'PREM FLOW', fmt:v=>sspan(v,0)},
    {key:'oi_vol',      label:'OI/VOL',  fmt:v=>v!=null?fmt(v,3):'—'},
    {key:'gamma_adj',   label:'γ-ADJ OI', fmt:v=>v!=null?sspan(v,2):'—'},
    {key:'pcr',         label:'PCR',     fmt:v=>v!=null?fmt(v,3):'—'},
    {key:'buy_imb',     label:'BUY IMB', fmt:v=>v!=null?sspan(v,3):'—'},
    {key:'velocity',    label:'VEL',     fmt:v=>sspan(v,2)},
    {key:'acceleration',label:'ACCEL',   fmt:v=>sspan(v,2)},
    {key:'n_strikes',   label:'N STR'},
    {key:'days_tracked',label:'PERIODS'},
  ]);
}


// ── OI Signals column filter ─────────────────────────────────────
function filterOiSignals(){
  const expiry = document.getElementById('oiSigFilterExpiry')?.value.trim().toUpperCase()||'';
  const strike = document.getElementById('oiSigFilterStrike')?.value.trim()||'';
  const ceSig  = document.getElementById('oiSigFilterCeSig')?.value.trim().toUpperCase()||'';
  const peSig  = document.getElementById('oiSigFilterPeSig')?.value.trim().toUpperCase()||'';
  const tbl    = document.querySelector('#oiSignalTable table');
  if(!tbl) return;
  // Column indices: 0=timestamp,1=strike,2=expiry,3=ce_oi_chg,4=ce_oi,5=ce_vol,6=ce_prem,7=ce_sig...
  // Find by header text
  const headers=[...tbl.querySelectorAll('th')].map(h=>h.textContent.trim());
  const colIdx = name => headers.findIndex(h=>h.includes(name));
  const iStrike=colIdx('STRIKE'), iExpiry=colIdx('EXPIRY');
  const iCeSig=colIdx('CE SIG'), iPeSig=colIdx('PE SIG');
  tbl.querySelectorAll('tbody tr').forEach(row=>{
    const cells=[...row.cells];
    const matchExpiry = !expiry || (cells[iExpiry]?.textContent||'').toUpperCase().includes(expiry);
    const matchStrike = !strike || (cells[iStrike]?.textContent||'').includes(strike);
    const matchCeSig  = !ceSig  || (cells[iCeSig]?.textContent||'').toUpperCase().includes(ceSig);
    const matchPeSig  = !peSig  || (cells[iPeSig]?.textContent||'').toUpperCase().includes(peSig);
    row.style.display=(matchExpiry&&matchStrike&&matchCeSig&&matchPeSig)?'':'none';
  });
}
function clearOiSigFilters(){
  ['oiSigFilterExpiry','oiSigFilterStrike','oiSigFilterCeSig','oiSigFilterPeSig']
    .forEach(id=>{const el=document.getElementById(id);if(el)el.value='';});
  filterOiSignals();
}


// ══════════════════════════════════════════════════════════════════
// Exposure Flow — Gamma, Delta, Vanna layers
// ══════════════════════════════════════════════════════════════════

(()=>{
  const today=new Date().toLocaleDateString('sv-SE');
  const d5=new Date(Date.now()-5*86400000).toLocaleDateString('sv-SE');
  const ef=document.getElementById('expDateFrom');
  const et=document.getElementById('expDateTo');
  const ei=document.getElementById('expIntraDate');
  if(ef&&!ef.value) ef.value=d5;
  if(et&&!et.value) et.value=today;
  if(ei&&!ei.value) ei.value=today;
})();

wireToggleGroup('expModeGroup');
document.getElementById('expModeGroup')?.addEventListener('click',()=>{
  const mode=document.querySelector('#expModeGroup .active')?.dataset.em||'multiday';
  document.getElementById('expMultiCtrl').style.display=mode==='multiday'?'flex':'none';
  document.getElementById('expIntraCtrl').style.display=mode==='intraday'?'flex':'none';
});
document.getElementById('btnExpLoad')?.addEventListener('click', loadExposureFlow);

// Populate expExpiry when OI EXPOSURE subtab opens
document.querySelectorAll('.subtab').forEach(tab=>{
  if(tab.dataset.stab==='oi-exposure')
    tab.addEventListener('click', async()=>{
      const sym=document.getElementById('oiSymbol')?.value;
      if(!sym) return;
      try{
        const showPast=document.getElementById('expExpiryPast')?.checked;
        const exps=await api(`/api/expiries?symbol=${encodeURIComponent(sym)}&future_only=${!showPast}`);
        const sel=document.getElementById('expExpiry');
        if(!sel) return;
        sel.innerHTML='<option value="all">ALL EXPIRIES</option>';
        exps.forEach(e=>{sel.innerHTML+=`<option value="${e}">${e}</option>`;});
      }catch(e){}
    });
});

async function loadExposureFlow(){
  const sym    = document.getElementById('oiSymbol')?.value;
  const expiry = document.getElementById('expExpiry')?.value||'all';
  const mode   = document.querySelector('#expModeGroup .active')?.dataset.em||'multiday';
  const dFrom  = document.getElementById('expDateFrom')?.value||'';
  const dTo    = document.getElementById('expDateTo')?.value||'';
  const iDate  = document.getElementById('expIntraDate')?.value||'';
  const minVol = document.getElementById('expMinVol')?.value||100;

  if(!sym){alert('Select a symbol in OI Signals filter bar');return;}

  ['expFlipChart','expGexProfile','expDeltaChart','expVannaChart'].forEach(id=>setLoad(id));
  document.getElementById('expSummaryTable').innerHTML=
    '<div class="loading"><div class="spinner"></div>Computing…</div>';
  document.getElementById('expDivSignals').style.display='none';

  try{
    const params=new URLSearchParams({
      symbol:sym, expiry, mode,
      date_from:dFrom, date_to:dTo,
      intraday_date:iDate, min_volume:minVol,
    });
    const d=await api(`/api/exposure_flow?${params}`);
    renderExposureFlow(d);
  }catch(e){
    ['expFlipChart','expGexProfile','expDeltaChart','expVannaChart'].forEach(id=>
      setEmpty(id,'⚠ '+e.message));
    document.getElementById('expSummaryTable').innerHTML=
      `<div class="empty err">⚠ ${e.message}</div>`;
  }
}

function renderExposureFlow(d){
  if(!d) return;
  const P = d.periods||[];
  const G = d.gamma||{};
  const D = d.directional||{};
  const V = d.vanna||{};
  const xLabel = d.mode==='intraday'?'Time':'Date';

  // ── Divergence signals ──────────────────────────────────────
  const divEl=document.getElementById('expDivSignals');
  const divs=d.divergence||{};
  const divPills=Object.entries(divs)
    .filter(([k,v])=>v)
    .map(([k,v])=>{
      const colors={fragile_pin:'var(--amber)',coiled_spring:'var(--amber)',
        gamma_shift:'var(--red)',bullish_accumulation:'var(--green)',
        bearish_distribution:'var(--red)'};
      const col=colors[v.type]||'var(--muted)';
      return`<span style="display:inline-block;padding:4px 10px;margin:2px;border-radius:4px;
        border:1px solid ${col};color:${col};font-family:var(--mono);font-size:10px">
        ⚡ ${v.label}</span>`;
    });
  if(divPills.length){
    divPills.push('<span style="cursor:pointer;font-size:9px;padding:2px 8px;'
      +'border:1px solid var(--muted);color:var(--muted);border-radius:3px;margin-left:6px"'
      +' onclick="this.parentElement.nextElementSibling.style.display='
      +'this.parentElement.nextElementSibling.style.display===\'none\'?\'\':\'none\'">ⓘ guide</span>');
    divEl.innerHTML=divPills.join('');
    divEl.style.display='';
  } else {
    divEl.style.display='none';
    document.getElementById('expDivHelp').style.display='none';
  }

  // ── Gamma flip migration chart ──────────────────────────────
  const flipTraces=[];
  // Spot line
  flipTraces.push({
    name:'Spot', x:P, y:P.map(p=>G[p]?.spot||null),
    type:'scatter',mode:'lines',
    line:{color:'rgba(255,255,255,0.3)',width:1,dash:'dot'},
  });
  // Flip point line
  flipTraces.push({
    name:'Gamma Flip', x:P, y:P.map(p=>G[p]?.flip_strike||null),
    type:'scatter',mode:'lines+markers',
    line:{color:C.acc,width:2.5},marker:{size:6,color:C.acc},
  });
  // Peak +γ
  flipTraces.push({
    name:'Peak +γ (pin)', x:P, y:P.map(p=>G[p]?.peak_pos?.strike||null),
    type:'scatter',mode:'markers',
    marker:{size:8,color:C.green,symbol:'triangle-up'},
  });
  // Peak -γ
  flipTraces.push({
    name:'Peak -γ (amplify)', x:P, y:P.map(p=>G[p]?.peak_neg?.strike||null),
    type:'scatter',mode:'markers',
    marker:{size:8,color:C.red,symbol:'triangle-down'},
  });
  plot('expFlipChart', flipTraces, {...LB,
    xaxis:{...LB.xaxis,title:xLabel,tickangle:-30},
    yaxis:{...LB.yaxis,title:'Strike'},
    legend:{...LB.legend,orientation:'h',y:-0.25,font:{size:9}},
    height:320,
  });

  // ── GEX profile (latest vs previous) ───────────────────────
  const profiles=d.gex_profiles||{};
  const profPeriods=Object.keys(profiles).sort();
  const gexTraces=[];
  profPeriods.forEach((p,i)=>{
    const prof=profiles[p];
    const isCurrent = i===profPeriods.length-1;
    gexTraces.push({
      name:isCurrent?`GEX ${p} (current)`:`GEX ${p} (prev)`,
      x:prof.map(r=>r.strike),
      y:prof.map(r=>r.net_gex),
      type:'bar',
      marker:{color:isCurrent?C.acc:'rgba(139,92,246,0.5)',opacity:isCurrent?0.85:0.55},
    });
  });
  // Zero line
  if(gexTraces.length){
    plot('expGexProfile', gexTraces, {...LB,
      barmode:'overlay',
      xaxis:{...LB.xaxis,title:'Strike'},
      yaxis:{...LB.yaxis,title:'Net GEX ₹M'},
      shapes:[{type:'line',x0:gexTraces[0]?.x?.[0],x1:gexTraces[0]?.x?.slice(-1)[0],
        y0:0,y1:0,line:{color:C.muted,width:1,dash:'dot'}}],
      legend:{...LB.legend,orientation:'h',y:-0.2},
      height:320,
    });
  }

  // ── Directional delta flow chart ────────────────────────────
  const deltaTraces=[];
  deltaTraces.push({
    name:'CE Δ-Flow', x:P, y:P.map(p=>D[p]?.ce_delta_flow||0),
    type:'scatter',mode:'lines+markers',
    line:{color:C.green,width:1.5},marker:{size:4},
  });
  deltaTraces.push({
    name:'PE Δ-Flow', x:P, y:P.map(p=>D[p]?.pe_delta_flow||0),
    type:'scatter',mode:'lines+markers',
    line:{color:C.red,width:1.5,dash:'dash'},marker:{size:4},
  });
  deltaTraces.push({
    name:'Net Δ-Flow', x:P, y:P.map(p=>D[p]?.net_delta_flow||0),
    type:'scatter',mode:'lines+markers',
    line:{color:C.acc,width:3},marker:{size:5},
  });
  // Rotation peak as Y2
  deltaTraces.push({
    name:'Rotation Peak', x:P, y:P.map(p=>D[p]?.rotation_peak||null),
    type:'scatter',mode:'markers',
    marker:{size:10,color:C.pur,symbol:'star'},
    yaxis:'y2',
  });
  plot('expDeltaChart', deltaTraces, {...LB,
    xaxis:{...LB.xaxis,title:xLabel,tickangle:-30},
    yaxis:{...LB.yaxis,title:'Delta-Adjusted OI Flow'},
    yaxis2:{overlaying:'y',side:'right',title:'Rotation Strike',
      gridcolor:'transparent',tickfont:{size:9}},
    shapes:[{type:'line',x0:P[0],x1:P[P.length-1],y0:0,y1:0,
      line:{color:C.muted,width:1,dash:'dot'}}],
    legend:{...LB.legend,orientation:'h',y:-0.25,font:{size:9}},
    height:280,
  });

  // ── Vanna flow chart ────────────────────────────────────────
  const vannaTraces=[];
  vannaTraces.push({
    name:'Net Vanna', x:P, y:P.map(p=>V[p]?.net_vanna||0),
    type:'bar', marker:{color:P.map(p=>(V[p]?.net_vanna||0)>=0?C.pur:C.amber),opacity:0.7},
  });
  vannaTraces.push({
    name:'Peak Vanna Strike', x:P,
    y:P.map(p=>V[p]?.peak_vanna?.strike||null),
    type:'scatter',mode:'markers',
    marker:{size:7,color:C.pur,symbol:'diamond'},
    yaxis:'y2',
  });
  plot('expVannaChart', vannaTraces, {...LB,
    xaxis:{...LB.xaxis,title:xLabel,tickangle:-30},
    yaxis:{...LB.yaxis,title:'Net Vanna Exposure'},
    yaxis2:{overlaying:'y',side:'right',title:'Peak Strike',
      gridcolor:'transparent',tickfont:{size:9}},
    height:220,
  });

  // ── Summary table ────────────────────────────────────────────
  const summary=d.summary||[];
  document.getElementById('expSummaryTable').innerHTML=makeTbl(summary,[
    {key:'period',          label:'PERIOD'},
    {key:'spot',            label:'SPOT',           fmt:v=>v!=null?fmt(v,2):'—'},
    {key:'flip_strike',     label:'γ FLIP',         fmt:v=>v!=null?fmt(v,0):'—'},
    {key:'flip_change',     label:'FLIP Δ',         fmt:v=>v!=null?sspan(v,0):'—'},
    {key:'peak_pos_strike', label:'PEAK +γ',        fmt:v=>v!=null?fmt(v,0):'—'},
    {key:'peak_neg_strike', label:'PEAK -γ',        fmt:v=>v!=null?fmt(v,0):'—'},
    {key:'total_gex_m',     label:'NET GEX ₹M',     fmt:v=>v!=null?sspan(v,4):'—'},
    {key:'gex_change',      label:'GEX Δ',          fmt:v=>v!=null?sspan(v,4):'—'},
    {key:'net_delta_flow',  label:'NET Δ FLOW',     fmt:v=>v!=null?sspan(v,0):'—'},
    {key:'ce_delta_flow',   label:'CE Δ FLOW',      fmt:v=>v!=null?sspan(v,0):'—'},
    {key:'pe_delta_flow',   label:'PE Δ FLOW',      fmt:v=>v!=null?sspan(v,0):'—'},
    {key:'rotation_peak',   label:'ROT PEAK',       fmt:v=>v!=null?fmt(v,0):'—'},
    {key:'net_vanna',       label:'NET VANNA',       fmt:v=>v!=null?sspan(v,0):'—'},
    {key:'peak_vanna_str',  label:'VANNA PEAK STR', fmt:v=>v!=null?fmt(v,0):'—'},
  ]);
}


// ══════════════════════════════════════════════════════════════════
// Exposure Screener — market-wide regime/signal scan
// ══════════════════════════════════════════════════════════════════

wireToggleGroup('esViewGroup');
document.getElementById('btnEsLoad')?.addEventListener('click', loadExposureScreener);
document.getElementById('esExpiryRank')?.addEventListener('change', loadExposureScreener);
document.getElementById('esViewGroup')?.addEventListener('click', ()=>setTimeout(loadExposureScreener,50));
document.getElementById('btnEsCsv')?.addEventListener('click', ()=>csvBtn('esTable', ()=>{
  const d=document.getElementById('esDate')?.value||new Date().toISOString().slice(0,10);
  return 'exposure_screener_'+d+'.csv';
}));
document.getElementById('btnDivCsv')?.addEventListener('click', ()=>csvBtn('divTable', 'divergence_'+new Date().toISOString().slice(0,10)+'.csv'));
document.getElementById('btnWallsCsv')?.addEventListener('click', ()=>csvBtn('oiWallsTable', 'oi_walls_'+new Date().toISOString().slice(0,10)+'.csv'));
document.getElementById('btnOiSigCsv')?.addEventListener('click', ()=>csvBtn('oiSignalTable', 'oi_signals_'+new Date().toISOString().slice(0,10)+'.csv'));
document.getElementById('btnEsGuide')?.addEventListener('click', ()=>{
  const g=document.getElementById('esGuide');
  g.style.display = g.style.display==='none' ? '' : 'none';
});

let _esInit = false;
const _metricInfo = {};  // column key → {label, meaning, interpret} for th tooltips
async function initExposureScreener(){
  if(_esInit) return;
  _esInit = true;
  try{
    // Dates
    const dd = await api('/api/exposure_screener/dates');
    const dsel = document.getElementById('esDate');
    dsel.innerHTML = (dd.dates||[]).map(d=>`<option value="${d}">${d}</option>`).join('');
    // Signal filter options + guide
    const sm = await api('/api/exposure_screener/signals_meta');
    const ssel = document.getElementById('esSignal');
    const guide = document.getElementById('esGuide');
    (sm.signals||[]).forEach(s=>{
      ssel.innerHTML += `<option value="${s.key}">${s.label}</option>`;
    });
    // Column/metric help — single source from exposure_core METRIC_INFO
    let metricsHtml = '';
    try{
      const mm = await api('/api/exposure_screener/metrics_meta');
      (mm.metrics||[]).forEach(m=>{ _metricInfo[m.key] = m; });
      metricsHtml = '<br><b style="color:var(--text)">Column Reference:</b><br>' +
        (mm.metrics||[]).map(m=>
          `<b style="color:var(--acc)">${m.label}:</b> ${m.meaning} `+
          `<span style="color:var(--muted)">→ ${m.interpret}</span>`
        ).join('<br>');
    }catch(e){ /* metrics_meta optional */ }
    guide.innerHTML = '<b style="color:var(--text)">Signal Guide:</b><br>' +
      (sm.signals||[]).map(s=>`<b style="color:var(--acc)">${s.label}:</b> ${s.description}`).join('<br>') +
      metricsHtml;
  }catch(e){ /* table may not exist yet */ }
}

async function loadExposureScreener(){
  await initExposureScreener();
  const d      = document.getElementById('esDate')?.value||'';
  const view   = document.querySelector('#esViewGroup .active')?.dataset.ev||'changed';
  const signal = document.getElementById('esSignal')?.value||'';
  const regime = document.getElementById('esRegime')?.value||'';
  const conf   = document.getElementById('esConf')?.value||'medium';
  const erank  = document.getElementById('esExpiryRank')?.value||'0';

  document.getElementById('esTable').innerHTML='<div class="loading"><div class="spinner"></div>Scanning…</div>';
  document.getElementById('esCounts').innerHTML='';

  try{
    const params=new URLSearchParams({view, min_confidence:conf, sort_by:'net_gex_norm', expiry_rank:erank});
    if(d) params.set('screen_date', d);
    if(signal) params.set('signal', signal);
    if(regime) params.set('regime', regime);
    const data = await api(`/api/exposure_screener?${params}`);
    renderExposureScreener(data);
  }catch(e){
    document.getElementById('esTable').innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;
  }
}


// #4 — filter exposure screener rows by ticker (client-side over rendered table)
function filterEsRows(query){
  const q=(query||'').trim().toUpperCase();
  const tbl=document.querySelector('#esTable table');
  if(!tbl) return;
  tbl.querySelectorAll('tbody tr').forEach(tr=>{
    const sym=(tr.querySelector('td')?.textContent||'').toUpperCase();
    tr.style.display = (!q || sym.includes(q)) ? '' : 'none';
  });
}

// ── #9: Signal icon system (compact column + severity sort + legend) ──
// Retired 2026-10-04: crash_risk, bull_trend_reinforce, bear_trend_reinforce
// (built on ce/pe vanna, which are not dealer-signed). Labels below are
// descriptive; see exposure_core.SIGNAL_INFO.
const SIGNAL_ICONS = {
  regime_flip_to_neg:    {icon:'▼', color:'var(--red)',   label:'Regime → negative (price fell below zero-gamma level)'},
  regime_flip_to_pos:    {icon:'▲', color:'var(--green)', label:'Regime → positive (price back above zero-gamma level)'},
  pin_strengthening:     {icon:'◎', color:'var(--muted)', label:'Zone narrowed (per-strike transition; mostly strike grid)'},
  instability_widening:  {icon:'◇', color:'var(--amber)', label:'Neg-γ strikes up (share of negative-gamma strikes rose)'},
  flip_drift_up:         {icon:'⤴', color:'var(--muted)', label:'Flip drift up'},
  flip_drift_down:       {icon:'⤵', color:'var(--muted)', label:'Flip drift down'},
};
// Sort order only (lower sorts first): regime changes → structural → drift.
const SIGNAL_SEVERITY = {
  regime_flip_to_neg:1, regime_flip_to_pos:1,
  pin_strengthening:3, instability_widening:3,
  flip_drift_up:4, flip_drift_down:4,
};
function signalIcons(v){
  if(!v) return '—';
  const sigs=String(v).split(',').map(s=>s.trim()).filter(Boolean);
  // sort by severity so the most urgent icon shows first
  sigs.sort((a,b)=>(SIGNAL_SEVERITY[a]??9)-(SIGNAL_SEVERITY[b]??9));
  return sigs.map(s=>{
    const m=SIGNAL_ICONS[s];
    if(!m) return `<span title="${s}">${s}</span>`;
    return `<span title="${m.label}" style="color:${m.color};font-size:13px;margin-right:3px;cursor:default">${m.icon}</span>`;
  }).join('');
}
// Min-severity of a row's signals — drives column sorting (urgent rows to top)
function signalSeverityKey(v){
  if(!v) return 99;
  return Math.min(...String(v).split(',').map(s=>SIGNAL_SEVERITY[s.trim()]??9));
}
// Header legend — terse icon→label key
function signalLegend(){
  return Object.entries(SIGNAL_ICONS).map(([k,m])=>
    `<span style="margin-right:12px;white-space:nowrap;font-size:10px;color:var(--muted)">
       <span style="color:${m.color};font-size:12px">${m.icon}</span> ${k.replace(/_/g,' ')}</span>`
  ).join('');
}
// Agg-sign colored +/- icon (consistent with regime coloring)
function aggSignIcon(v){
  if(v==null||v==='') return '—';
  const s=String(v).toLowerCase();
  const pos=s.includes('pos')||v==='+'||v===1||v>0;
  const neg=s.includes('neg')||v==='-'||v===-1||v<0;
  if(pos) return '<span style="color:var(--green);font-weight:700;font-size:13px">+</span>';
  if(neg) return '<span style="color:var(--red);font-weight:700;font-size:13px">−</span>';
  return '—';
}

function renderExposureScreener(d){
  document.getElementById('esDateBadge').textContent = d.date||'';
  // Signal count summary
  const counts = d.counts||{};
  const order = ['regime_flip_to_neg','regime_flip_to_pos',
                 'pin_strengthening','instability_widening','flip_drift_up','flip_drift_down'];
  // Indicator counts (compression/release) shown alongside signals
  const indCounts = d.indicator_counts||{};
  const colors = {regime_flip_to_neg:'var(--red)',
    regime_flip_to_pos:'var(--green)',
    instability_widening:'var(--amber)',pin_strengthening:'var(--muted)'};
  const cntEl=document.getElementById('esCounts');
  let pills = order.filter(k=>counts[k]).map(k=>{
    const col=colors[k]||'var(--muted)';
    return `<div class="kpi" style="cursor:pointer;border-color:${col}"
      onclick="document.getElementById('esSignal').value='${k}';loadExposureScreener()">
      <div class="kpi-label">${k.replace(/_/g,' ')}</div>
      <div class="kpi-val" style="color:${col}">${counts[k]}</div></div>`;
  });
  // Indicator pills (compression state + release events) — distinct styling
  if(indCounts.regime_compression){
    pills.push(`<div class="kpi" style="border-color:var(--acc);border-style:dashed">
      <div class="kpi-label">🌀 compressing</div>
      <div class="kpi-val" style="color:var(--acc)">${indCounts.regime_compression}</div></div>`);
  }
  if(indCounts.compression_release){
    pills.push(`<div class="kpi" style="border-color:var(--red);border-style:dashed">
      <div class="kpi-label">⚡ release</div>
      <div class="kpi-val" style="color:var(--red)">${indCounts.compression_release}</div></div>`);
  }
  // #4: counts are MARKET-WIDE (whole day, all symbols) — NOT the filtered table below
  const wideLbl = '<span style="font-family:var(--mono);font-size:9px;color:var(--muted);'
    + 'margin-right:8px;align-self:center">MARKET-WIDE · '+(d.date||'')+' →</span>';
  cntEl.innerHTML = pills.length
    ? (wideLbl + pills.join(''))
    : '<span style="font-family:var(--mono);font-size:10px;color:var(--muted)">No signals fired on this date</span>';

  const rows = d.rows||[];
  const isDropped = (d.view==='dropped');
  if(!rows.length){
    const msg = isDropped
      ? `No tickers dropped their signals since ${d.prev_date||'the previous day'}`
      : 'No tickers match the current filters';
    document.getElementById('esTable').innerHTML=`<div class="empty">${msg}</div>`;
    return;
  }
  // Dropped-view banner
  if(isDropped){
    const cntEl2=document.getElementById('esCounts');
    if(cntEl2) cntEl2.innerHTML=`<span style="font-family:var(--mono);font-size:10px;color:var(--muted)">
      ${rows.length} ticker(s) had a signal on ${d.prev_date||'prev day'} but none today (${d.date}) —
      their signal reset. Yesterday's signal shown below.</span>`;
  }
  // Header legend for signal icons (#9)
  const legendEl=document.getElementById('esSignalLegend');
  if(legendEl) legendEl.innerHTML='<span style="font-size:9px;color:var(--muted);margin-right:8px">SIGNALS:</span>'+signalLegend();

  const droppedCol = isDropped ? [
    {key:'prev_signals', label:'YDAY SIGNAL', fmt:v=>signalIcons(v), sortValue:v=>signalSeverityKey(v)},
  ] : [];
  const esCols = [
    {key:'symbol',      label:'SYMBOL', fmt:(v)=>`<span style="cursor:pointer;color:var(--acc);font-weight:600"
                                        onclick="jumpHistory('${v}')" title="Open trend history for ${v}">${v}</span>`},
    ...droppedCol,
    {key:'next_results_date', label:'RESULTS', fmt:(v,r)=>eventCell(r),
     sortValue:(v,r)=>r.near_earnings ? 0 : (v ? 1 : 2), exportValue:v=>v?String(v).slice(0,10):''},
    // #9c: primary price = FUT (fut_price); spot shown in tooltip
    {key:'fut_price',   label:'FUT',     fmt:(v,r)=>v!=null?`<span title="spot: ${r&&r.spot!=null?fmt(r.spot,1):'—'}">${fmt(v,1)}</span>`:'—', exportValue:v=>v==null?'':v},
    {key:'gex_regime',  label:'REGIME',  fmt:(v,r)=>regimePillWithDays(v, r)},
    {key:'days_in_regime',label:'DAYS', fmt:(v,r)=>daysInRegimeBadge(v, r&&r.gex_regime)},
    {key:'gamma_position',label:'γ POS', fmt:v=>gammaPosCell(v), exportValue:v=>v==null?'':v},
    // #9b: Agg Sign → colored +/- icon (consistent with regime)
    {key:'net_gex_sign',label:'AGG',     fmt:v=>aggSignIcon(v), exportValue:v=>v||''},
    {key:'net_gex_norm',label:'LOPSIDED', fmt:v=>v!=null?sspan(v,3):'—'},
    {key:'gamma_flip',  label:'γ FLIP',  fmt:v=>v!=null?fmt(v,0):'—'},
    {key:'gamma_shelf_center',label:'γ SHELF', fmt:(v,r)=>gammaShelfCell(v, r)},
    {key:'flip_velocity',label:'FLIP Δ/d', fmt:v=>v!=null?sspan(v,1):'—'},
    {key:'migration_effectiveness',label:'MIG EFF', fmt:v=>migEffCell(v)},
    {key:'flip_norm_distance',label:'FLIP DIST',fmt:v=>v!=null?sspan(v,2):'—'},
    {key:'transition_width_norm',label:'TRANS W', fmt:v=>v!=null?fmt(v,2):'—'},
    {key:'neg_gamma_fraction',label:'NEG γ%', fmt:v=>v!=null?fmt(v*100,0)+'%':'—'},
    {key:'pe_vanna',    label:'PE VANNA',fmt:v=>v!=null?sspan(v,0):'—'},
    {key:'iv_change',   label:'IV Δ',    fmt:v=>v!=null?sspan(v,2):'—'},
    // RR25 = IV(25d call) - IV(25d put), vol points. Negative = puts bid over
    // calls = downside skew. Colour is inverted vs the usual sspan convention on
    // purpose: negative skew is the risk-off reading, so red for negative.
    {key:'rr25',        label:'RR25',    fmt:v=>v==null?'—':
                          `<span style="color:${v<-0.1?'var(--red)':(v>0.1?'var(--green)':'var(--muted)')}">${fmt(v,2)}</span>`,
                        exportValue:v=>v==null?'':v},
    {key:'basis_annualized',label:'BASIS%',fmt:v=>v!=null?sspan(v,1):'—', exportValue:v=>v==null?'':v},
    {key:'basis_chg',   label:'BASIS Δ', fmt:v=>v!=null?sspan(v,1):'—', exportValue:v=>v==null?'':v},
    {key:'regime_compression',label:'COMPRESS',fmt:(v,r)=>compressionBadge(v, r&&r.compression_days)},
    {key:'compression_release',label:'REL',fmt:v=>v?'<span title="compression release" style="color:var(--red);font-weight:600">⚡</span>':'—'},
    {key:'oi_turnover_ratio',label:'OI TURN',fmt:v=>turnoverBadge(v)},
    // #9a: Signals → icons (severity-sorted), sortValue drives column sort by urgency
    {key:'signals',     label:'SIGNALS', fmt:v=>signalIcons(v), sortValue:v=>signalSeverityKey(v), exportValue:v=>v||''},
    {key:'confidence',  label:'CONF',    fmt:v=>confBadge(v)},
    {key:'next_day_realized_move',label:'NEXT MOVE%',fmt:v=>v!=null?sspan(v,2):'—'},
    {key:'_defend',     label:'DEFEND', fmt:(v,r)=>defendCell(r, r._idx),
                        sortValue:(v,r)=>{const h=_defenseHoldProb(r);return h?h.p:-1;},
                        exportValue:(v,r)=>{const h=_defenseHoldProb(r);return h?h.p:'';}},
  ];
  // attach column help (meaning + interpret) as th tooltip from METRIC_INFO
  esCols.forEach(c=>{ const m=_metricInfo[c.key]; if(m) c.help = m.meaning+' → '+m.interpret; });
  _esData = rows;                       // stash for the ⊕ DEFEND tooltip
  rows.forEach((r,i)=>{ r._idx=i; });   // stable index (survives DOM re-sort)
  document.getElementById('esTable').innerHTML = makeTbl(rows, esCols);
  _wireExpTips();
}

// ═════════════════════════════════════════════════════════════════
// Exposure narrative labels + defense-zone-hold probability
// Pure functions over stored columns (design: exposure_narrative_design.md).
// Thresholds locked from ~12k-row backfill distributions.
// ═════════════════════════════════════════════════════════════════
let _esData = [];   // last screener payload, for the DEFEND ⊕ tooltip

// Signed distance from gamma COM to fut, in expected-move units.
function _comDistEM(r){
  if(r==null||r.gamma_shelf_center==null||r.fut_price==null||!r.expected_move) return null;
  return (r.gamma_shelf_center - r.fut_price)/r.expected_move;
}

// 1. Where is dealer positioning? (gamma COM vs price, and gamma-COM vs OI-COM)
function _narrPositioning(r){
  const d=_comDistEM(r);
  if(d==null) return '—';
  const ad=Math.abs(d);
  let s = ad<=0.25 ? 'Defending at-the-money (COM ≈ price)'
                   : `Support stacked ${d>0?'above':'below'} (COM ${d>0?'+':'−'}${ad.toFixed(2)} EM)`;
  // gamma-COM vs OI-COM divergence leg (dealer_divergence): where hedging gamma
  // concentrates vs where open positions sit. Aligned = OI confirms the pin.
  if(r.dealer_divergence_label && r.oi_com_inrange!=null){
    const lbl = r.dealer_divergence_label;
    const tag = lbl==='aligned' ? 'OI-confirmed'
              : (lbl==='diverging' ? 'OI-diverging' : 'OI strongly diverging');
    s += ` · γ-COM vs OI-COM ${fmt(r.oi_com_inrange,0)}: ${tag}`;
  }
  return s;
}

// 2. How is inventory distributed? (concentration spread + lopsidedness tilt)
function _narrDistribution(r){
  const conc=r.concentration, lop=r.net_gex_norm;
  const spread = conc==null?'—':(conc>0.37?'Concentrated':(conc>0.27?'Moderate':'Diffuse'));
  const tilt = lop==null?'balanced':(lop>0.29?'call-heavy':(lop<-0.29?'put-heavy':'balanced'));
  let s=`${spread}, ${tilt}`;
  if(r.gamma_shelf_width===1 && conc!=null && conc>0.37 && r.gamma_shelf_peak_strike!=null)
    s += ` · single-strike pin at ${fmt(r.gamma_shelf_peak_strike,0)}`;
  return s;
}

// 3. How is it changing? (COM migration magnitude/direction + persistence + basis)
function _narrChange(r){
  const mig=r.gamma_com_migration, em=r.expected_move;
  let mag='—';
  if(mig!=null && em){
    const a=Math.abs(mig)/em;
    mag = a<0.25?'Stable':(a<=0.75?'Drifting':'Repositioning');
    if(a>=0.25) mag += mig>0?' up':' down';
  } else if(mig===0){ mag='Stable'; }
  let s=mag;
  if(r.days_in_regime!=null) s+=` (${r.days_in_regime}d in regime)`;
  if(r.basis_chg!=null && Math.abs(r.basis_chg)>=16.3)
    s += r.basis_chg>0?' · basis widening':' · basis compressing';
  return s;
}

// ── HISTORICAL (2026-07 rebuild) ────────────────────────────────────────────
// Retained for the reasoning on why transition_width_norm was dropped, which
// still stands. The IV-band and expiry-week axes described below were
// themselves retired in 2026-08 once the greeks.py IV bug was found — see the
// CURRENT MODEL block further down. Nothing here is live code any more.
//
// Next-day containment probability — empirical 3-way lookup (IV band x expiry
// week x gamma regime), validated out-of-sample by time-split.
//
// WHAT THIS ANSWERS: "will tomorrow's move stay inside one 1-day expected move?"
// Base rate across the book is ~61%.
//
// WHY IT WAS REBUILT (2026-07): the previous model keyed off transition_width_norm
// with a 27-point base spread (tight 31 / moderate 46 / wide 58). That axis was a
// measurement artifact, not market structure:
//   * transition_width IS the gap between the two strikes straddling the flip, so
//     it equals one strike interval ~90% of the time and correlates 0.83 with pure
//     grid density (strike_interval / expected_move).
//   * Its apparent effect REVERSED SIGN once grid density was held fixed (wide held
//     BETTER in the two densest quartiles, worse in the two coarsest) — the
//     signature of a confound, and the reason no re-tuning of 31/46/58 could work.
//   * The three "tightest" symbols in the book were NIFTY, BANKNIFTY and FINNIFTY:
//     the bands were sorting on liquidity tier (indices have the finest grids and
//     are the most macro-sensitive), so "tight zones are fragile" really said
//     "indices move more than mid-caps".
//   * The old hold measure also compared a ONE-DAY move against expected_move,
//     which scales with sqrt(dte). That ratio is ~1/sqrt(dte), so the old 62%->91%
//     "hold gradient" was mostly days-to-expiry. Fixed by measuring against a true
//     1-day EM (see expected_move_1d in exposure_core).
// Concentration was tested as a replacement axis and dropped: within grid strata
// its correlation with holding is ~0.01-0.04, and in the one stratum where it did
// move the needle it ran OPPOSITE to the old +4 adjustment.
//
// WHAT SURVIVED: gamma regime. Unlike width it keeps its sign and magnitude under
// stratification (positive beats negative in 7 of 8 grid/IV strata), and the sign
// matches theory — dealers long gamma dampen, short gamma amplify. The effect is
// strongest at high IV (there is more flow to hedge) and in expiry week.
//
// Cells are observed rates over ~13.8k rows; out-of-sample drift on a Jan-Apr /
// May-Jul split was 0.1-7.7pts (mean ~3.9). Cells with n < 80 are interpolated
// from their row rather than trusted directly.
// ── CURRENT MODEL (2026-08 rebuild) ─────────────────────────────────────────
// Single-axis empirical lookup on neg_gamma_fraction. Answers: "will
// tomorrow's move stay inside one 1-day expected move?" Book base rate is
// ~73% contained; ~5.4% breach 2x EM.
//
// WHY THE 3-WAY TABLE ABOVE WAS RETIRED: it was fitted on IV that was wrong.
// greeks.py passed (t, r) to py_vollib.black.implied_volatility, which takes
// (r, t) — the one function in that package with r before t. Every IV solved
// at a constant t = 0.10yr, so near-expiry IV was understated ~2.2x and
// far-dated overstated ~1.2x (live 2026-04-13 to 2026-08-18). Therefore:
//   * The IV axis never measured vol, it measured dte — IV was understated
//     most at the front. On corrected data the three bands span 1.4 points of
//     hold rate, i.e. noise. The old 18/27 cutpoints also stopped balancing:
//     they now split the book 3.6 / 32.1 / 64.3 percent.
//   * The expiry-week axis was the same artifact. lo_expiry reading 43/50/46
//     looked like expiry fragility; it was the 1-day EM computed from an IV
//     at half its true value, so almost nothing could contain. On corrected
//     IV expiry week holds 73% vs 72% for normal weeks. Dropped.
//   * Base rate moved 61.5% -> 72.6% for the same reason.
//
// WHAT SURVIVED: dealer gamma positioning, as in the 2026-07 rebuild. But
// neg_gamma_fraction beats the gex_regime label — 14.5 points of spread vs
// 9.6, monotone across five bands, every cell >= 547 rows. gex_regime was
// tested as a second axis and REJECTED: it splits hold rate consistently but
// INVERTS on 2x breach in the top two ngf bands (5.8 vs 7.2, 7.0 vs 9.7) on
// cells as small as n=13. Adding it would re-import exactly the thin-cell
// artifact this rebuild exists to remove.
//
// Corporate actions (VEDL demerger -64.8%, TRENT -32.9%, ...) are excluded
// via |next_day_move| <= 10%. They cost 37 rows and moved the band5/band1
// tail ratio 1.82 -> 1.95, so they were not driving the gradient.
//
// STABILITY — READ THE ORDERING, NOT THE LEVEL. On a Jan-May / May-Aug split
// the rank order is preserved but the magnitude is not: spread is 17.3 points
// in the volatile first half and 6.7 in the calm second, and absolute hold
// rates drift up to 16 points. The 2x-breach RELATIVE risk is what holds up
// (band5/band1 = 1.54 train, 1.79 test), which is why the popover leads with
// the multiple and the percentage carries a caveat.
//
// Fitted on 17,614 rows, expiry_rank=0, corrected IV.
//
// REBUILT 2026-10-04 on exposure_hist, 2021-01 to 2026-09, 257k rows (rank 0,
// results windows excluded), with the recalibrated em_1d and the unified
// dealer-gamma-profile definitions. Axis switched from neg_gamma_fraction to
// gamma_position (Gamma at fut / max|Gamma|): same ordering, wider spread in the
// current regime (68.9 -> 76.7 vs ngf 70.2 -> 77.4 on 2024-26), and it is built on
// the definitions both tables now share (ngf's count changed with the 2026-10-03
// strike-filter rebuild). Monotone in 2021-23 and 2024-26 separately, levels
// within 1.5 points of each other.
//
// WHAT IT IS: market fragility, not a stock-specific edge. Against OTHER STOCKS
// ON THE SAME DAY every band is within +/-1 point; the gradient exists because
// names deep in short gamma cluster on days that turn rough for everyone. For a
// held position that is still a fair ex-ante probability; it does not say this
// name is safer than that one today. (Same reason a gamma tilt added nothing to
// em_1d out of sample.)
const _DEFEND_BASE = { hold: 74, br2: 5.0 };
const _DEFEND_BANDS = [
  // gamma_position <= max : hold% (|next-day move| within 1x em_1d), br2% (beyond 2x)
  { max: -0.25,    hold: 69, br2: 6.5, n:   4504, label: 'deep short γ (< −0.25)' },
  { max: 0,        hold: 71, br2: 5.7, n:   6208, label: 'short γ (−0.25..0)'    },
  { max: 0.4,      hold: 72, br2: 5.3, n:  49415, label: 'shallow long γ (0..0.4)' },
  { max: 0.7,      hold: 74, br2: 4.9, n: 101205, label: 'long γ (0.4..0.7)'      },
  { max: Infinity, hold: 76, br2: 4.8, n:  95666, label: 'deep long γ (> 0.7)'    },
];

function _defenseHoldProb(r){
  const g = r.gamma_position;
  if(g==null || !isFinite(g)) return null;
  let i = 0;
  while(i < _DEFEND_BANDS.length-1 && g > _DEFEND_BANDS[i].max) i++;
  const b = _DEFEND_BANDS[i];
  const rr = b.br2 / _DEFEND_BASE.br2;        // 2x-em_1d tail risk vs base
  return {
    p: b.hold,
    br2: b.br2,
    rr,
    band: i,
    bucket: b.hold >= 74 ? 'High' : (b.hold >= 72 ? 'Moderate' : 'Low'),
    detail: b.label + ', n=' + b.n.toLocaleString() + ' · market fragility, not stock-specific',
    tail: (rr>=1.15 || rr<=0.85) ? (rr.toFixed(1)+'x base tail risk')
                                 : 'tail risk near base',
  };
}

// DEFEND cell — compact ⊕ trigger + hold-prob chip; full narrative in the popover.
function defendCell(r, idx){
  const h=_defenseHoldProb(r);
  const chip = h
    ? `<span style="font-family:var(--mono);font-size:10px;color:${
        h.bucket==='High'?'var(--green)':(h.bucket==='Low'?'var(--red)':'var(--amber)')}">${h.p}%</span>`
    : '<span style="color:var(--muted);font-size:10px">—</span>';
  return `${chip}<span class="wall-tip-trigger" tabindex="0" data-esidx="${idx}"
            style="margin-left:5px">⊕</span>`;
}

// Popover HTML — three narrative groups + COM distance + hold-prob breakdown.
//
// EM ratios are resolved to PRICES here. A "2× EM breach 12%" reads as an
// abstraction; "outside 22327–25161" is the thing you size against. Every
// input is already on the row (exposure_screener does SELECT e.* off
// exposure_eod), so this costs nothing.
//
// HORIZON WARNING: r.expected_move is the 1-sigma move to EXPIRY (calibrated
// /243, see exposure_core), NOT a next-day number — while the hold probability
// beside it IS next-day. Both horizons are labelled explicitly below so the
// band is never misread as one session.
function _emBand(r,mult){
  if(r==null||!r.expected_move||r.fut_price==null||!isFinite(r.fut_price)) return null;
  const w=r.expected_move*mult, c=r.fut_price;
  return {w:w, lo:c-w, hi:c+w};
}
function _emPts(r,ratio){
  if(r==null||!r.expected_move||ratio==null||!isFinite(ratio)) return null;
  return ratio*r.expected_move;
}
function _expDetailTip(r){
  const d=_comDistEM(r);
  const h=_defenseHoldProb(r);
  const row=(lbl,val)=>`<div style="display:flex;gap:10px;margin:2px 0">
    <span style="color:var(--muted);min-width:96px;font-size:10px">${lbl}</span>
    <span style="font-size:11px">${val}</span></div>`;
  const b2=_emBand(r,2);
  let hold;
  if(h){
    const col=h.bucket==='High'?'var(--green)':(h.bucket==='Low'?'var(--red)':'var(--amber)');
    hold=`<span style="color:${col};font-weight:600">${h.p}% — ${h.bucket}</span>
      <span style="color:var(--muted);font-size:9px"> (${h.detail})</span>
      <div style="margin-top:2px;font-size:10px">2× EM breach ${h.br2}% — <span style="color:${
        h.rr>=1.15?'var(--red)':(h.rr<=0.85?'var(--green)':'var(--muted)')}">${h.tail}</span></div>`
      + (b2?`<div style="margin-top:2px;font-size:10px;color:var(--muted)">2× EM → ±${fmt(b2.w,0)} · ${fmt(b2.lo,0)}–${fmt(b2.hi,0)} <span style="font-size:9px">(to expiry)</span></div>`:'');
  } else {
    hold='<span style="color:var(--muted)">no ATM IV reading</span>';
  }
  const comPts=_emPts(r,d);
  const divPts=_emPts(r,r.dealer_divergence);
  return `<div style="font-family:var(--mono);min-width:240px;max-width:300px">
    <div style="font-weight:600;margin-bottom:5px;color:var(--acc)">${r.symbol}
      <span style="color:var(--muted);font-weight:400">· defense readout</span></div>
    ${row('Positioning', _narrPositioning(r))}
    ${row('Distribution', _narrDistribution(r))}
    ${row('Changing', _narrChange(r))}
    <div style="border-top:1px solid var(--bd);margin:5px 0"></div>
    ${row('Centers', (r.gamma_shelf_center!=null?`γ-COM ${fmt(r.gamma_shelf_center,0)}`:'γ-COM —')
        + (r.oi_com_inrange!=null?` · OI-COM ${fmt(r.oi_com_inrange,0)}`:'')
        + (r.dealer_divergence!=null?` · Δ ${(r.dealer_divergence>0?'+':'')}${r.dealer_divergence.toFixed(2)} EM${divPts!=null?` (${divPts>0?'+':''}${fmt(divPts,0)} pts)`:''}`:'')
        + (r.dealer_divergence_label?` ${r.dealer_divergence_label}`:''))}
    ${row('COM dist', d==null?'—'
        :`${d>0?'+':''}${d.toFixed(2)} EM${comPts!=null?` = ${comPts>0?'+':''}${fmt(comPts,0)} pts`:''}`
         +(r.fut_price!=null&&comPts!=null?` <span style="color:var(--muted)">from ${fmt(r.fut_price,0)}</span>`:''))}
    ${row('EM (expiry)', r.expected_move!=null
        ?`±${fmt(r.expected_move,0)}${r.dte!=null?` over ${r.dte}d`:''}`
        :'—')}
    ${row('EM (1 day)', r.em_1d!=null
        ? emBandsHtml(r.em_1d, r.fut_price, 0)
         +(r.hv20!=null?` <span style="color:var(--muted)">hv20 ${fmt(r.hv20,1)}%</span>`:'')
        :'<span style="color:var(--muted)">— no hv20</span>')}
    ${row('Zone holds', hold)}
    <div style="color:var(--muted);font-size:9px;margin-top:5px;line-height:1.3">
      Empirical next-day containment, banded on dealer negative-gamma share.
      Trust the ordering, not the level: band ranks are stable out-of-sample
      but the percentages move with the vol regime (up to 16pts). The 2×-breach
      multiple is the steadier read — it is what sizes a short-premium position.
      EM (expiry) is the 1-sigma move to expiry from ATM IV; EM (1 day) is the
      next session, blended 10% IV / 90% realised. Both are 1-sigma — and both
      under-cover in the tail, so treat 2× bands as a floor on risk.</div>
  </div>`;
}

// Tooltip wiring — mirrors the OI-walls popover (shared .wall-tip-pop element).
function _showExpTip(trigger){
  const idx=parseInt(trigger.dataset.esidx,10);
  const r=_esData[idx]; if(!r) return;
  const el=_ensureWallTipEl();
  el.innerHTML=_expDetailTip(r);
  el.style.display='block';
  const tr=trigger.getBoundingClientRect();
  const pw=el.offsetWidth, ph=el.offsetHeight, vw=window.innerWidth, vh=window.innerHeight;
  let left=tr.right-pw; if(left<8) left=8; if(left+pw>vw-8) left=vw-8-pw;
  let top=tr.bottom+6; if(top+ph>vh-8) top=tr.top-ph-6; if(top<8) top=8;
  el.style.left=left+'px'; el.style.top=top+'px';
}
function _wireExpTips(){
  const tbl=document.querySelector('#esTable table');
  if(!tbl) return;
  tbl.querySelectorAll('.wall-tip-trigger[data-esidx]').forEach(t=>{
    t.addEventListener('mouseenter',()=>{ t._over=true;
      const el=_ensureWallTipEl(); el._triggerOver=true; _showExpTip(t); });
    t.addEventListener('mouseleave',()=>{ t._over=false;
      const el=document.getElementById('wallTipPop'); if(el) el._triggerOver=false;
      _hideWallTip(); });
    t.addEventListener('focus',()=>_showExpTip(t));
    t.addEventListener('blur',()=>_hideWallTip());
  });
}

// ── Regime colour ramp (mirrors exposure_core.REGIME_COLOR_RAMP) ──
// Ordered stabilising→destabilising. Word shown as tooltip on the box. Single
// JS source so the exposure screener AND history view render identically.
const REGIME_RAMP = {
  all_positive:{color:'#0c8f4d',order:0,label:'all positive'},  // deep green
  positive:    {color:'#10b981',order:1,label:'positive'},      // green
  mixed:       {color:'#9ca3af',order:2,label:'mixed'},         // grey (both signs)
  negative:    {color:'#f59e0b',order:3,label:'negative'},      // orange
  all_negative:{color:'#dc2626',order:4,label:'all negative'},  // deep red
};
function regimeColor(v){ return (REGIME_RAMP[v]||{color:'var(--muted)',label:(v||'')}); }
// Ordered colour box (the word is the tooltip, not the visible text).
function regimeBox(v){
  if(!v) return '—';
  const m=regimeColor(v);
  const txt=(v||'').replace('_',' ');
  return `<span title="${txt}" style="display:inline-block;min-width:46px;text-align:center;
    font-family:var(--mono);font-size:9px;font-weight:600;padding:2px 7px;border-radius:3px;
    background:${m.color}22;color:${m.color};border:1px solid ${m.color}">${txt}</span>`;
}
function regimePill(v){ return regimeBox(v); }
function regimePillWithDays(v){ return regimeBox(v); }

// days_in_regime badge — green if positive regime, red if negative, grey if mixed; intensity hints persistence
function daysInRegimeBadge(days, regime){
  if(days==null) return '—';
  let col = 'var(--red)';
  if(regime && regime.indexOf('positive')>=0) col = 'var(--green)';
  else if(regime === 'mixed') col = 'var(--muted)';
  return `<span style="font-family:var(--mono);font-size:10px;color:${col};font-weight:600">${days}d</span>`;
}

// Dealer gamma position = Gamma(fut)/max|Gamma| from the dealer gamma profile,
// in [-1, +1]: +1 deep in dealer long gamma (moves dampened), -1 deep in short
// gamma (moves amplified), ~0 at the zero-gamma flip. Shows HOW DEEP in the
// regime price sits, which is what predicts (next-day move t -5.4 over six
// years); a long DAYS streak at +0.65 and one at +0.20 are different states.
function gammaPosCell(v){
  if(v==null || !isFinite(v)) return '\u2014';
  const a = Math.min(1, Math.abs(v));
  const col = v>=0 ? 'var(--green)' : 'var(--red)';
  const w = Math.round(a*28);
  const bar = `<span style="display:inline-block;width:28px;height:6px;background:var(--border);border-radius:2px;vertical-align:middle;margin-left:4px;position:relative">`+
              `<span style="position:absolute;${v>=0?'left':'right'}:0;top:0;height:6px;width:${w}px;background:${col};border-radius:2px"></span></span>`;
  const tip = 'Dealer gamma position (Gamma at fut / max |Gamma|): +1 deep long gamma (dampened), -1 deep short gamma (amplified), 0 at the flip';
  return `<span title="${tip}" style="font-family:var(--mono);font-size:10px;color:${col};font-weight:600">${v>=0?'+':''}${fmt(v,2)}</span>${bar}`;
}

// γ shelf cell — center of mass of the dominant gamma shelf; flags single-strike
// (sharp, fragile pin) and shows shelf width on hover.
function gammaShelfCell(v, r){
  if(v==null) return '—';
  const w = r && r.gamma_shelf_width;
  const single = r && r.gamma_shelf_single_strike;
  const peak = r && r.gamma_shelf_peak_strike;
  const tip = `shelf center ${fmt(v,0)}` +
              (w!=null?` · width ${w} strike${w==1?'':'s'}`:'') +
              (peak!=null?` · peak ${fmt(peak,0)}`:'') +
              (single?' · SINGLE STRIKE (sharp pin)':'');
  const col = single ? 'var(--amber)' : 'var(--fg)';
  const mark = single ? ' ◆' : '';
  return `<span title="${tip}" style="font-family:var(--mono);font-size:10px;color:${col}">${fmt(v,0)}${mark}</span>`;
}

// migration effectiveness = |spot move%| / |COM move%|. ≈1 orderly (COM tracks
// spot); ≫1 price running from a static structure (caution); ≈0 anticipatory.
function migEffCell(v){
  if(v==null) return '—';
  let col = 'var(--green)';                 // ≈ 1 orderly
  if(v >= 2.5) col = 'var(--red)';          // price far outrunning structure
  else if(v >= 1.6 || v <= 0.4) col = 'var(--amber)';
  const tip = v>=1.6 ? 'price running ahead of dealer repositioning'
            : (v<=0.4 ? 'COM repositioning without spot (anticipatory)'
                      : 'orderly: COM tracking spot');
  return `<span title="${tip}" style="font-family:var(--mono);font-size:10px;color:${col}">${fmt(v,2)}</span>`;
}

// compression badge — shows coiling state + consecutive days
function compressionBadge(v, days){
  if(!v) return '—';
  return `<span style="display:inline-block;font-size:9px;padding:1px 6px;border-radius:3px;
    border:1px solid var(--acc);color:var(--acc);font-family:var(--mono)">🌀 ${days||1}d</span>`;
}

// OI turnover — two-sided caution: very low (stale) and >1 (thin OI) both flagged
function turnoverBadge(v){
  if(v==null) return '—';
  let col='var(--green)';           // healthy mid-band
  let note='';
  if(v < 0.05){ col='var(--muted)'; note=' ⚠'; }       // stale/quiet
  else if(v > 1.0){ col='var(--amber)'; note=' ⚠'; }   // thin OI, unreliable
  else if(v > 0.5){ col='var(--text)'; }               // high but ok
  return `<span style="font-family:var(--mono);font-size:10px;color:${col}">${fmt(v,2)}${note}</span>`;
}

function confBadge(v){
  const map={high:'var(--green)',medium:'var(--amber)',low:'var(--muted)'};
  const col=map[v]||'var(--muted)';
  return `<span style="font-family:var(--mono);font-size:9px;color:${col}">${(v||'').toUpperCase()}</span>`;
}
function signalChips(v){
  return String(v).split(',').map(s=>{
    const red=['regime_flip_to_neg'].includes(s);
    const grn=['regime_flip_to_pos'].includes(s);
    const col=red?'var(--red)':grn?'var(--green)':'var(--amber)';
    return `<span style="display:inline-block;font-size:8px;padding:1px 5px;margin:1px;
      border:1px solid ${col};color:${col};border-radius:3px">${s.replace(/_/g,' ')}</span>`;
  }).join('');
}



// ════════════════════════════════════════════════════════════════════
// HISTORY (Symbol Trend) view
// Time-axis companion to the all-cross-sectional dashboard. Pick a symbol +
// date range → see how its gamma structure evolved → "stabilising or noisy?".
// Multi-symbol READY (length-1 today): data is symbol-keyed, chart builders take
// arrays of series, layout is a list of symbol-blocks. v2 adds the 2nd selector.
// ════════════════════════════════════════════════════════════════════

const _hsMetricInfo = {};   // column key → {label, meaning, interpret}

// Type-aware change map for the summary box. pct = % change (prices/levels),
// delta = absolute Δ (bounded ratios — % of these is nonsense), state = first→last
// transition with no number.
const HS_CHANGE_TYPE = {
  fut_price:'pct', spot:'pct', expected_move:'pct',
  atm_iv_smoothed:'pct', atm_iv:'pct', basis_annualized:'pct',
  net_gex_norm:'delta', flip_norm_distance:'delta', neg_gamma_fraction:'delta',
  transition_width_norm:'delta', gamma_flip:'delta',
  gex_regime:'state', confidence:'state',
};

// Strength axis labels (mirror exposure_core.strength_axes keys) for the tooltip.
// trans_w removed 2026-10-04 (grid-confounded, pointed the wrong way).
const HS_AXIS_LABEL = {
  lopsided:'lopsided', neg_gamma:'neg-γ%', iv:'IV',
  flip:'flip dist', regime:'regime',
};

// One-session expected move with its CALIBRATED bands. em_1d is a variance-
// matched 1-sigma (oc/em1d.py), and returns are fat-tailed, so ±1 em_1d covers
// ~73%, not 68%. Empirical multipliers, stable 2021-26 (exposure_core
// EM1D_BAND_MULT): 68% band ±0.86 em_1d, 95% band ±2.0 em_1d.
function emBandsHtml(em, fut, d){
  if(em==null || !isFinite(em)) return '—';
  const a=0.86*em, b=2.0*em;
  const tip='68% of next-day moves fell inside ±0.86×em_1d and 95% inside ±2.0×em_1d (2021-26, every year on target). Plain ±em_1d covers ~73%.';
  let s=`<span title="${tip}">±${fmt(em,d)}</span> <span style="color:var(--muted);font-size:9px">68% ±${fmt(a,d)} · 95% ±${fmt(b,d)}</span>`;
  if(fut) s+=` <span style="color:var(--muted);font-size:9px">· 68% ${fmt(fut-a,0)}–${fmt(fut+a,0)}</span>`;
  return s;
}

let _hsInitPromise = null;
function initHistory(){
  // Return a SHARED promise so concurrent callers (nav click VIEW_INIT +
  // jumpHistory) await the SAME completion instead of one short-circuiting on a
  // half-set flag and finding the symbol dropdown still empty.
  if(_hsInitPromise) return _hsInitPromise;
  _hsInitPromise = (async ()=>{
    try{
      const ss = await api('/api/symbol_history/symbols');
      const sel = document.getElementById('hsSymbol');
      sel.innerHTML = (ss.symbols||[]).map(s=>`<option value="${s}">${s}</option>`).join('');
    }catch(e){ /* table may not exist yet */ }
    // Column reference guide (shared METRIC_INFO + strength_score)
    try{
      const mm = await api('/api/symbol_history/metrics_meta');
      (mm.metrics||[]).forEach(m=>{ _hsMetricInfo[m.key]=m; });
      const g = document.getElementById('hsGuide');
      g.innerHTML = '<b style="color:var(--text)">Column Reference:</b><br>' +
        (mm.metrics||[]).map(m=>
          `<b style="color:var(--acc)">${m.label}:</b> ${m.meaning} `+
          `<span style="color:var(--muted)">→ ${m.interpret}</span>`).join('<br>');
    }catch(e){}
    // Default TO = today, FROM = 7d back (overwritten by jumpHistory / data on load)
    const to = document.getElementById('hsTo');
    const from = document.getElementById('hsFrom');
    if(to && !to.value){ to.value = new Date().toISOString().slice(0,10); }
    if(from && !from.value){
      const d=new Date(); d.setDate(d.getDate()-7);
      from.value = d.toISOString().slice(0,10);
    }
  })();
  return _hsInitPromise;
}

// Quick-range toggle: set FROM = TO − N days (N=0 → MAX, clear FROM)
function _hsApplyQuickRange(days){
  const to = document.getElementById('hsTo').value || new Date().toISOString().slice(0,10);
  const from = document.getElementById('hsFrom');
  if(!days){ from.value=''; return; }       // MAX
  const d = new Date(to+'T00:00:00');
  d.setDate(d.getDate()-days);
  from.value = d.toISOString().slice(0,10);
}

async function loadHistory(){
  await initHistory();
  const sym  = document.getElementById('hsSymbol')?.value||'';
  const from = document.getElementById('hsFrom')?.value||'';
  const to   = document.getElementById('hsTo')?.value||'';
  if(!sym){ document.getElementById('hsBlocks').innerHTML='<div class="empty">Select a symbol</div>'; return; }
  document.getElementById('hsBlocks').innerHTML='<div class="loading"><div class="spinner"></div>Loading trend…</div>';
  try{
    const erank = document.getElementById('hsExpiryRank')?.value||'0';
    const p = new URLSearchParams({symbol:sym, expiry_rank:erank});
    if(from) p.set('date_from', from);
    if(to)   p.set('date_to', to);
    const data = await api(`/api/symbol_history?${p}`);
    renderHistory(data);
  }catch(e){
    document.getElementById('hsBlocks').innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;
  }
}

// ── Type-aware change for the summary box ──
function _hsChange(metric, first, last){
  const type = HS_CHANGE_TYPE[metric]||'delta';
  if(first==null && last==null) return {txt:'—', cls:'neu'};
  if(type==='state'){
    const a=(first==null?'—':String(first).replace('_',' '));
    const b=(last==null?'—':String(last).replace('_',' '));
    if(a===b) return {txt:b, cls:'neu'};
    return {txt:`${a} → ${b}`, cls:'neu'};
  }
  const fa=parseFloat(first), la=parseFloat(last);
  if(isNaN(fa)||isNaN(la)) return {txt:'—', cls:'neu'};
  if(type==='pct'){
    if(fa===0) return {txt:'—', cls:'neu'};
    const pc=(la-fa)/Math.abs(fa)*100;
    const c=pc>0?'up':pc<0?'down':'neu';
    return {txt:(pc>0?'+':'')+fmt(pc,1)+'%', cls:c};
  }
  // delta
  const dv=la-fa;
  const c=dv>0?'up':dv<0?'down':'neu';
  return {txt:(dv>0?'+':'')+fmt(dv,3), cls:c};
}

// Strength-score colour (diverging green↔red around 0)
function _hsScoreColor(s){
  if(s==null||s===0) return 'var(--muted)';
  if(s>0) return s>=4?'#0c8f4d':'var(--green)';
  return s<=-4?'#dc2626':'var(--red)';
}
function _hsScoreCell(s, axes){
  if(s==null) return '—';
  const col=_hsScoreColor(s);
  let tip='';
  if(axes){ tip = Object.entries(axes).filter(([k,v])=>v!==0)
      .map(([k,v])=>`${(v>0?'+':'')}${v} ${HS_AXIS_LABEL[k]||k}`).join(', ') || 'no change'; }
  return `<span title="${tip}" style="font-family:var(--mono);font-weight:700;color:${col}">${s>0?'+':''}${s}</span>`;
}

// ── Summary box (per symbol) ──
function _hsSummaryBox(sym, rows){
  if(!rows.length) return '';
  const first=rows[0], last=rows[rows.length-1];
  const cumEnd = last.strength_cumulative;
  const cumCol = _hsScoreColor(cumEnd>0?1:cumEnd<0?-1:0);
  const range = `${first.date} → ${last.date}`;
  // metrics to summarise in the box (label, key)
  const items = [
    ['FUT','fut_price'],['SPOT','spot'],['ATM IV','atm_iv_smoothed'],
    ['LOPSIDED','net_gex_norm'],['FLIP DIST','flip_norm_distance'],
    ['NEG γ%','neg_gamma_fraction'],['TRANS W','transition_width_norm'],
    ['BASIS%','basis_annualized'],['REGIME','gex_regime'],['CONF','confidence'],
  ];
  const cells = items.map(([lbl,k])=>{
    const ch=_hsChange(k, first[k], last[k]);
    let fv = first[k], lv = last[k];
    const isState = HS_CHANGE_TYPE[k]==='state';
    const fvs = isState ? '' :
      `<span style="color:var(--muted);font-size:9px">${fv==null?'—':(typeof fv==='number'?fmt(fv,2):fv)} →
       ${lv==null?'—':(typeof lv==='number'?fmt(lv,2):lv)}</span>`;
    return `<div style="min-width:120px;padding:6px 10px;border:1px solid var(--border);border-radius:4px;background:var(--surf)">
      <div class="kpi-label">${lbl}</div>
      <div class="kpi-val ${ch.cls}" style="font-size:13px">${ch.txt}</div>
      ${fvs}</div>`;
  }).join('');
  return `<div class="card" style="margin-bottom:10px">
    <div class="card-title">${sym} — STRUCTURAL TREND
      <span class="badge" style="margin-left:6px">${range}</span>
      <span class="badge" style="margin-left:4px;background:${cumCol};color:#fff"
            title="Cumulative structural strength over the window (climbing = genuine multi-day strengthening; oscillating around 0 = noisy)">
        STRENGTH Σ ${cumEnd>0?'+':''}${cumEnd}</span>
      <span style="cursor:pointer;color:var(--acc);font-size:9px;margin-left:8px;font-family:var(--mono)"
            onclick="jumpGex('${sym}')" title="Drill into the gamma snapshot at the latest date">→ GEX</span>
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:8px">${cells}</div>
    <div style="font-size:9px;color:var(--muted);margin-top:8px;font-family:var(--mono);line-height:1.5">
      Strength describes the <b>regime</b>, not exogenous shocks — a structure can stabilise right into a gap.
      Validate against NEXT MOVE% before any predictive read.</div>
  </div>`;
}

// ── Charts (N-series-ready; one trace-set per symbol, N=1 today) ──
// Each builder takes seriesList = [{sym, rows}, ...] and a target div id.
const _HS_PALETTE = [C.acc, C.acc2, C.pur, C.green];

function _hsX(rows){ return rows.map(r=>r.date); }

function hsChartPriceFlip(seriesList, id){
  const traces=[];
  seriesList.forEach((s,si)=>{
    const col=_HS_PALETTE[si%_HS_PALETTE.length];
    const tag=seriesList.length>1?` ${s.sym}`:'';
    traces.push({name:'FUT'+tag,x:_hsX(s.rows),y:s.rows.map(r=>r.fut_price),
      type:'scatter',mode:'lines+markers',line:{color:col,width:2},marker:{size:5}});
    traces.push({name:'SPOT'+tag,x:_hsX(s.rows),y:s.rows.map(r=>r.spot),
      type:'scatter',mode:'lines',line:{color:col,width:1,dash:'dot'}});
    traces.push({name:'γ FLIP'+tag,x:_hsX(s.rows),y:s.rows.map(r=>r.gamma_flip),
      type:'scatter',mode:'lines+markers',line:{color:C.amber,width:1.5,dash:'dash'},marker:{size:4}});
  });
  plot(id,traces,{...LB,xaxis:{...LB.xaxis,title:''},yaxis:{...LB.yaxis,title:'Price / Flip'},
    legend:{...LB.legend,orientation:'h',y:1.12}});
}

function hsChartIvLopsided(seriesList, id){
  const traces=[];
  seriesList.forEach((s,si)=>{
    const tag=seriesList.length>1?` ${s.sym}`:'';
    traces.push({name:'ATM IV'+tag,x:_hsX(s.rows),y:s.rows.map(r=>r.atm_iv_smoothed),
      type:'scatter',mode:'lines+markers',line:{color:C.acc,width:2},marker:{size:5},yaxis:'y'});
    traces.push({name:'LOPSIDED'+tag,x:_hsX(s.rows),y:s.rows.map(r=>r.net_gex_norm),
      type:'scatter',mode:'lines+markers',line:{color:C.green,width:2},marker:{size:5},yaxis:'y2'});
  });
  plot(id,traces,{...LB,xaxis:{...LB.xaxis,title:''},
    yaxis:{...LB.yaxis,title:'ATM IV (%)'},
    yaxis2:{overlaying:'y',side:'right',title:'Lopsided',gridcolor:'transparent',
      zerolinecolor:'#243550',tickfont:{size:9},range:[-1,1]},
    legend:{...LB.legend,orientation:'h',y:1.12}});
}

function hsChartVanna(seriesList, id){
  const traces=[];
  seriesList.forEach((s,si)=>{
    const tag=seriesList.length>1?` ${s.sym}`:'';
    traces.push({name:'CE VANNA'+tag,x:_hsX(s.rows),y:s.rows.map(r=>r.ce_vanna),
      type:'scatter',mode:'lines+markers',line:{color:C.green,width:2},marker:{size:5}});
    traces.push({name:'PE VANNA'+tag,x:_hsX(s.rows),y:s.rows.map(r=>r.pe_vanna),
      type:'scatter',mode:'lines+markers',line:{color:C.red,width:2},marker:{size:5}});
  });
  plot(id,traces,{...LB,xaxis:{...LB.xaxis,title:''},yaxis:{...LB.yaxis,title:'Vanna (never netted)'},
    legend:{...LB.legend,orientation:'h',y:1.12}});
}

function hsChartNegGamma(seriesList, id){
  const traces=[];
  seriesList.forEach((s,si)=>{
    const col=_HS_PALETTE[si%_HS_PALETTE.length];
    const tag=seriesList.length>1?` ${s.sym}`:'';
    traces.push({name:'NEG γ%'+tag,x:_hsX(s.rows),y:s.rows.map(r=>r.neg_gamma_fraction!=null?r.neg_gamma_fraction*100:null),
      type:'scatter',mode:'lines',fill:'tozeroy',line:{color:C.red,width:1.5},
      fillcolor:'rgba(244,63,94,0.15)'});
  });
  plot(id,traces,{...LB,xaxis:{...LB.xaxis,title:''},yaxis:{...LB.yaxis,title:'Neg-γ fraction (%)',rangemode:'tozero'}});
}

function hsChartBasis(seriesList, id){
  const traces=[];
  let anyData=false;
  seriesList.forEach((s,si)=>{
    const tag=seriesList.length>1?` ${s.sym}`:'';
    // dead-zoned: render in-zone points muted so noise doesn't look like a real basis
    const ba=s.rows.map(r=>r.basis_annualized);
    if(ba.some(v=>v!=null)) anyData=true;
    traces.push({name:'BASIS%'+tag,x:_hsX(s.rows),y:ba,
      type:'scatter',mode:'lines+markers',line:{color:C.acc2,width:2},
      marker:{size:s.rows.map(r=>r.basis_in_deadzone?4:7),
              color:s.rows.map(r=>r.basis_in_deadzone?'var(--muted)':C.acc2),
              symbol:s.rows.map(r=>r.basis_in_deadzone?'circle-open':'circle')}});
    traces.push({name:'BASIS Δ'+tag,x:_hsX(s.rows),y:s.rows.map(r=>r.basis_chg),
      type:'bar',marker:{color:'rgba(139,92,246,0.4)'},yaxis:'y2'});
  });
  plot(id,traces,{...LB,xaxis:{...LB.xaxis,title:''},
    yaxis:{...LB.yaxis,title:'Annualized basis (%)'},
    yaxis2:{overlaying:'y',side:'right',title:'Basis Δ',gridcolor:'transparent',
      zerolinecolor:'#243550',tickfont:{size:9}},
    legend:{...LB.legend,orientation:'h',y:1.12}});
  if(!anyData){ setEmpty(id,'No meaningful basis in window (fut==spot or dead-zoned)'); }
}

function hsChartRealized(seriesList, id){
  const traces=[];
  seriesList.forEach((s,si)=>{
    const tag=seriesList.length>1?` ${s.sym}`:'';
    traces.push({name:'NEXT MOVE%'+tag,x:_hsX(s.rows),y:s.rows.map(r=>r.next_day_realized_move),
      type:'bar',marker:{color:s.rows.map(r=>{
        const v=r.next_day_realized_move; return v==null?'var(--muted)':v>=0?C.green:C.red;})}});
  });
  plot(id,traces,{...LB,xaxis:{...LB.xaxis,title:''},yaxis:{...LB.yaxis,title:'Next-day realized move (%)'}});
}

function hsChartStrength(seriesList, id){
  const traces=[];
  seriesList.forEach((s,si)=>{
    const col=_HS_PALETTE[si%_HS_PALETTE.length];
    const tag=seriesList.length>1?` ${s.sym}`:'';
    // per-day score as bars, cumulative as a line (the headline trajectory)
    traces.push({name:'DAY SCORE'+tag,x:_hsX(s.rows),y:s.rows.map(r=>r.strength_score),
      type:'bar',marker:{color:s.rows.map(r=>_hsScoreColor(r.strength_score))},yaxis:'y'});
    traces.push({name:'CUMULATIVE'+tag,x:_hsX(s.rows),y:s.rows.map(r=>r.strength_cumulative),
      type:'scatter',mode:'lines+markers',line:{color:col,width:2.5},marker:{size:5},yaxis:'y2'});
  });
  plot(id,traces,{...LB,xaxis:{...LB.xaxis,title:''},
    yaxis:{...LB.yaxis,title:'Day score',range:[-5.5,5.5]},
    yaxis2:{overlaying:'y',side:'right',title:'Cumulative',gridcolor:'transparent',
      zerolinecolor:'#243550',tickfont:{size:9}},
    legend:{...LB.legend,orientation:'h',y:1.12}});
}

// ── Transposed table: dates = columns, metrics = rows ──
// Section structure mirrors the CSV / spec. Each metric row is a fmt function.
const _HS_TABLE_SECTIONS = [
  ['PRICE / STRUCTURE', [
    ['FUT','fut_price', (v)=>v!=null?fmt(v,1):'—'],
    ['SPOT','spot', (v)=>v!=null?fmt(v,1):'—'],
    ['γ FLIP','gamma_flip', (v)=>v!=null?fmt(v,0):'—'],
    ['FLIP DIST','flip_norm_distance', (v)=>v!=null?sspan(v,2):'—'],
    ['EXP MOVE','expected_move', (v)=>v!=null?fmt(v,1):'—'],
  ]],
  ['REGIME', [
    ['REGIME','gex_regime', (v)=>regimeBox(v)],
    ['γ POS','gamma_position', (v)=>gammaPosCell(v)],
    ['LOPSIDED','net_gex_norm', (v)=>v!=null?sspan(v,3):'—'],
    ['NEG γ%','neg_gamma_fraction', (v)=>v!=null?fmt(v*100,0)+'%':'—'],
    ['TRANS W','transition_width_norm', (v)=>v!=null?fmt(v,2):'—'],
    ['DAYS','days_in_regime', (v,r)=>daysInRegimeBadge(v, r&&r.gex_regime)],
    ['STRENGTH','strength_score', (v,r)=>_hsScoreCell(v, r&&r.strength_axes)],
  ]],
  ['VOL', [
    ['ATM IV','atm_iv_smoothed', (v)=>v!=null?fmt(v,2):'—'],
    ['IV Δ','iv_change', (v)=>v!=null?sspan(v,2):'—'],
    ['CE VANNA','ce_vanna', (v)=>v!=null?sspan(v,0):'—'],
    ['PE VANNA','pe_vanna', (v)=>v!=null?sspan(v,0):'—'],
  ]],
  ['BASIS', [
    ['BASIS%','basis_annualized', (v,r)=> v==null?'—':(r&&r.basis_in_deadzone?
      `<span title="within tick-size dead-zone — treated as flat" style="color:var(--muted)">${fmt(v,1)}</span>`
      :sspan(v,1))],
    ['BASIS Δ','basis_chg', (v,r)=> v==null?'—':(r&&!r.basis_chg_emphasis?
      `<span style="color:var(--muted)">${fmt(v,1)}</span>`:sspan(v,1))],
  ]],
  ['SIGNALS', [
    ['SIGNALS','signals', (v)=>signalIcons(v)],
    ['CONF','confidence', (v)=>confBadge(v)],
  ]],
  ['OUTCOME', [
    ['NEXT MOVE%','next_day_realized_move', (v)=>v!=null?sspan(v,2):'—'],
  ]],
];

function _hsTransposedTable(rows){
  if(!rows.length) return '<div class="empty">No data in window</div>';
  const dates=rows.map(r=>r.date);
  const nd = dates.length;
  // Date columns share the remaining width evenly; METRIC label is a fixed
  // left column. With ≤ ~18 dates this fits the full-width card without
  // horizontal scroll; beyond that the .tw wrapper scrolls gracefully.
  const datePct = nd ? (88/nd).toFixed(3) : 88;
  // header row: metric label cell + one column per date (CSV header order)
  const csvHdr = ['METRIC', ...dates].map(s=>String(s).replace(/"/g,'&quot;')).join('\u0001');
  const head = '<th onclick="sortTbl(this)" style="text-align:left;position:sticky;left:0;'
    + 'background:var(--card);width:12%;min-width:90px;z-index:2">METRIC</th>'
    + dates.map(d=>`<th style="text-align:center;width:${datePct}%;min-width:42px">${d.slice(5)}</th>`).join('');
  let body='';
  _HS_TABLE_SECTIONS.forEach(([section, metrics])=>{
    body += `<tr><td colspan="${nd+1}" style="background:var(--surf);color:var(--acc);
      font-size:9px;letter-spacing:.08em;font-weight:600;padding:4px 8px">${section}</td></tr>`;
    metrics.forEach(([lbl,key,fn])=>{
      const cells = rows.map(r=>{
        const cell = fn(r[key], r);
        let ev = r[key]; ev = (ev==null?'':String(ev)).replace(/"/g,'&quot;');
        return `<td data-export="${ev}" style="text-align:center;white-space:nowrap">${cell}</td>`;
      }).join('');
      const tip=_hsMetricInfo[key]?` title="${(_hsMetricInfo[key].meaning+' → '+_hsMetricInfo[key].interpret).replace(/"/g,'&quot;')}"`:'';
      body += `<tr><td${tip} style="text-align:left;position:sticky;left:0;background:var(--card);
        color:var(--muted);font-weight:600;z-index:1">${lbl}</td>${cells}</tr>`;
    });
  });
  return `<table style="width:100%;table-layout:fixed" data-cols="${csvHdr}">`
    + `<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

// ── Per-symbol block (summary + charts + table). List-of-blocks renderer. ──
function renderHistory(data){
  const syms = data.symbols||[];
  const series = data.series||{};
  const cont = document.getElementById('hsBlocks');
  const haveData = syms.some(s=>(series[s]||[]).length);
  if(!haveData){
    cont.innerHTML=`<div class="empty">No history for ${syms.join(', ')} in
      ${data.date_from||'?'} → ${data.date_to||'?'}</div>`;
    return;
  }
  // Build container DOM first (charts need their divs to exist before plot()).
  cont.innerHTML = syms.map(sym=>{
    const rows = series[sym]||[];
    if(!rows.length) return `<div class="card" style="margin-bottom:10px"><div class="empty">No data for ${sym}</div></div>`;
    const sx = sym.replace(/[^A-Za-z0-9]/g,'');
    return _hsSummaryBox(sym, rows) +
      `<div class="g2" style="margin-bottom:10px">
        <div class="card"><div class="card-title">PRICE + γ FLIP</div><div class="chart" id="hsc_price_${sx}"></div></div>
        <div class="card"><div class="card-title">ATM IV + LOPSIDED</div><div class="chart" id="hsc_iv_${sx}"></div></div>
       </div>
       <div class="g2" style="margin-bottom:10px">
        <div class="card"><div class="card-title">CE / PE VANNA (never netted)</div><div class="chart" id="hsc_vanna_${sx}"></div></div>
        <div class="card"><div class="card-title">NEG-γ FRACTION</div><div class="chart" id="hsc_neg_${sx}"></div></div>
       </div>
       <div class="g2" style="margin-bottom:10px">
        <div class="card"><div class="card-title">STRUCTURAL STRENGTH (day + cumulative)</div><div class="chart" id="hsc_str_${sx}"></div></div>
        <div class="card"><div class="card-title">BASIS (annualized + Δ, dead-zoned)</div><div class="chart" id="hsc_basis_${sx}"></div></div>
       </div>
       <div class="card" style="margin-bottom:10px">
         <div class="card-title">NEXT-DAY REALIZED MOVE (validation lens)</div>
         <div class="chart" id="hsc_real_${sx}"></div>
       </div>
       <div class="card" style="margin-bottom:14px">
         <div class="card-title">HISTORY TABLE (metrics × dates)
           <span class="badge" style="margin-left:6px">${rows.length} session(s)</span>
         </div>
         <div class="tw" style="max-height:none" id="hsTable_${sx}"></div>
       </div>`;
  }).join('');
  // Now render charts + table per symbol (single-series arrays; N-ready signature).
  syms.forEach(sym=>{
    const rows = series[sym]||[];
    if(!rows.length) return;
    const sx = sym.replace(/[^A-Za-z0-9]/g,'');
    const sl=[{sym, rows}];
    hsChartPriceFlip(sl, `hsc_price_${sx}`);
    hsChartIvLopsided(sl, `hsc_iv_${sx}`);
    hsChartVanna(sl, `hsc_vanna_${sx}`);
    hsChartNegGamma(sl, `hsc_neg_${sx}`);
    hsChartStrength(sl, `hsc_str_${sx}`);
    hsChartBasis(sl, `hsc_basis_${sx}`);
    hsChartRealized(sl, `hsc_real_${sx}`);
    document.getElementById(`hsTable_${sx}`).innerHTML = _hsTransposedTable(rows);
  });
}

// ── Nav: jump from any symbol click → History (replaces jumpGex as default) ──
// Auto: select symbol, TO = current exposure-screener date (or latest), FROM = TO−7d,
// switch to History, auto-load.
// Remembers where the user jumped INTO History from, so the BACK button can
// return there (views are class-toggled, not routed — browser back won't work).
let _hsBackTo = null;   // {view, label}
const _VIEW_LABELS = { expscreen:'EXPOSURE SCREENER', overview:'OVERVIEW',
  gex:'GEX', oi:'OI', iv:'IV', flow:'FLOW', dropped:'EXPOSURE SCREENER' };

function _hsShowBack(view){
  _hsBackTo = view ? { view, label:(_VIEW_LABELS[view]||view.toUpperCase()) } : null;
  const b = document.getElementById('btnHsBack');
  if(!b) return;
  if(_hsBackTo){
    b.textContent = `← ${_hsBackTo.label}`;
    b.style.display = '';
  } else {
    b.style.display = 'none';
  }
}

function goBackFromHistory(){
  const target = _hsBackTo && _hsBackTo.view;
  if(!target){ return; }
  // Switch back to the originating view. Its state (screener date/filters/table)
  // is preserved because views are toggled, never re-rendered.
  const btn = document.querySelector(`[data-view="${target}"]`);
  if(btn) btn.click();
}

function jumpHistory(sym){
  // Capture the screener's selected date BEFORE switching views (read it now so
  // there's no dependency on DOM ordering later).
  const esDate = document.getElementById('esDate')?.value || '';
  const esRank = document.getElementById('esExpiryRank')?.value || '0';
  // Remember the currently-active view so BACK can return to it.
  const origin = document.querySelector('.nav-btn.active')?.dataset.view || 'expscreen';
  document.querySelector('[data-view="history"]').click();
  _hsShowBack(origin);
  // Await the SHARED init promise (symbol dropdown + metric meta) so the symbol
  // option exists before we select it — then set symbol + dates and load.
  initHistory().then(async ()=>{
    const sel = document.getElementById('hsSymbol');
    if(sel){
      if(![...sel.options].some(o=>o.value===sym)){
        // symbol not in the list (e.g. dropped from latest scan) → add it so the
        // jump still works
        sel.insertAdjacentHTML('afterbegin', `<option value="${sym}">${sym}</option>`);
      }
      sel.value = sym;
    }
    // carry the screener's NEAR/NEXT expiry rank into the history view
    const rsel = document.getElementById('hsExpiryRank');
    if(rsel) rsel.value = esRank;
    // anchor TO on the screener date if present, else the symbol's latest data date
    let toVal = esDate;
    if(!toVal){
      try{ const dd = await api(`/api/symbol_history/dates?symbol=${encodeURIComponent(sym)}&expiry_rank=${esRank}`);
           toVal = (dd.dates||[])[0] || ''; }catch(e){}
    }
    const to = document.getElementById('hsTo'), from = document.getElementById('hsFrom');
    if(toVal){
      to.value = toVal.slice(0,10);
      // honour the active quick-range toggle (default 7D) for the FROM anchor
      const days = parseInt(document.querySelector('#hsRangeGroup .active')?.dataset.days || '7', 10);
      if(days){
        const d = new Date(to.value+'T00:00:00'); d.setDate(d.getDate()-days);
        from.value = d.toISOString().slice(0,10);
      } else {
        from.value = '';   // MAX
      }
    }
    loadHistory();
  });
}

// ── History event wiring ──
document.getElementById('btnHsBack')?.addEventListener('click', goBackFromHistory);
document.getElementById('btnHsLoad')?.addEventListener('click', loadHistory);
document.getElementById('hsSymbol')?.addEventListener('change', loadHistory);
document.getElementById('hsExpiryRank')?.addEventListener('change', loadHistory);
document.getElementById('btnHsGuide')?.addEventListener('click', ()=>{
  const g=document.getElementById('hsGuide');
  g.style.display = g.style.display==='none' ? '' : 'none';
});
document.getElementById('hsRangeGroup')?.addEventListener('click', e=>{
  const b=e.target.closest('button'); if(!b) return;
  document.querySelectorAll('#hsRangeGroup button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  _hsApplyQuickRange(parseInt(b.dataset.days,10));
});
// CSV: export the FIRST symbol's transposed table (N=1 today). Per-block button
// could be added in v2 when multiple symbols render.
document.getElementById('btnHsCsv')?.addEventListener('click', ()=>{
  const tbl=document.querySelector('#hsBlocks table');
  if(!tbl){ alert('Load a symbol first'); return; }
  const sym=document.getElementById('hsSymbol')?.value||'symbol';
  const to=document.getElementById('hsTo')?.value||new Date().toISOString().slice(0,10);
  exportTableCsv(tbl, `history_${sym}_${to}.csv`);
});


// ── MARKET STATE strip (Overview) ─────────────────────────────────
// Dealer-positioning fragility gauge. Reads /api/market_state, a query-only
// view of market_state_eod (built nightly by market_state.refresh).
// See market_state.py for derivation, validation and rejected metrics.
//
// This is a VOLATILITY gauge, not a directional one. Market breadth metrics
// (below-flip share, flip-drift net, bull-minus-bear, regime-flip net) were all
// measured against forward returns and all came back inside +/-10%, i.e. zero,
// while correlating 45-72% with the SAME DAY's move. They describe; they do not
// lead. They are deliberately absent here.
const _MS_HELP = {
  ngf: `MARKET GAMMA POSITION (fragility axis since 2026-10-04)
Across single-stock F&O names, front monthly expiry: the MEAN dealer gamma
position (dealer gamma at the futures price / largest |gamma| on the profile,
-1 to +1). Lower = more stocks sitting near or below their zero-gamma level,
where dealer hedging amplifies moves. The percentile shown is FRAGILITY: the
cycle-adjusted reading's rank, inverted, vs the trailing 60 sessions (high =
fragile).

Evidence (six years, 1,520 sessions): controlling for NIFTY's own trailing 5d and
22d realised vol and its IV, mean gamma position still predicts NIFTY's forward
5-day realised vol (t -4.0; -3.8 before Nov 2024, -2.6 after). Modest: 1 SD
lower ~ +6-9% weekly NIFTY vol. It replaced mean neg_gamma_fraction, which was
weaker (t +2.7, fading to +1.3 after the break); the earlier +51 correlation
was measured on the first 133 days and is +15 over six years.`,

  iv: `NIFTY ATM IMPLIED VOL, and its 60-session percentile.
Shown as the comparison, not the signal. A high IV percentile mostly means a
volatile week just finished (avg trailing vol 1.30 vs 0.65 on low-percentile
days), because IV reprices after the fact.`,

  pay: `VOL PAY — RETRACTED, NO LONGER SHOWN.
This cell used to display realised forward vol / implied daily vol by state
(0.76 / 0.78 / 0.92 / 1.07) as "what days like today have historically paid".
That ordering turned out to be substantially a CALENDAR effect: rank 0 rolls to
the next monthly at DTE <= 3, ngf runs ~0.045 higher on a far-dated book, and
FRAGILE states therefore land on 69% of post-roll days vs 22-24% mid-cycle.
Splitting ngf within near-DTE band, the payoff separation vanishes — in the 21+
band it is 1.060 vs 1.060, identical. The claim is not supported and the number
is no longer displayed. It is still stored for re-testing once there is more
than ~5 monthly cycles of history.`,

  state: `STATE = positioning tier _ implied vol tier, both vs their own
trailing 60 sessions. Names describe inputs only and assert nothing about
mispricing.

WHAT IT MEANS: an expectation about how much MOVEMENT is coming over the next
week. Nothing about direction, and — since the vol_pay retraction — nothing
about whether selling premium is well compensated.

Two different averages are involved — do not confuse them:
  ACROSS STOCKS, each day  ngf is the MEAN of neg_gamma_fraction. The median
                           was rejected: over 138 days it takes only 53
                           distinct values (the strike grid is coarse), so its
                           percentile moves in steps and a threshold would fire
                           on rounding. The mean takes 138 of 138.
  ACROSS TIME, 60 sessions the split below is at the 50th percentile of that
                           rolling window, i.e. the window's median.

The percentile ranks a CYCLE-ADJUSTED value (gpos_adj = mean gamma position
minus its near-DTE band's own trailing median), so a reading is compared against
days at a comparable point in the expiry cycle. Without it the state tracked the
calendar: FRAGILE on 69% of post-roll days against 22-24% mid-cycle. This means
the displayed value and the displayed percentile will not reconcile directly.

  STABLE_*  = cycle-adjusted mean gamma position above its 60-session median
  FRAGILE_* = at or below it
  *_CHEAP   = NIFTY IV below its 60-session median   *_RICH = at or above

LIMITS. 5-day windows overlap, so ~140 rows are more like ~28 independent
observations; STABLE_RICH has only 17 days. The signal COMPRESSES in calm
regimes (correlation 52.6 in the volatile half of the sample, 19.0 in the calm
half) — a quiet stretch will make this look useless exactly when it matters
least. Treat the ordering as informative and the levels as approximate.`,
};

function _msTip(key, label){
  const t = (_MS_HELP[key]||'').replace(/"/g,'&quot;');
  return `<span title="${t}" style="cursor:help;border-bottom:1px dotted var(--muted)">${label}</span>`;
}

async function loadMarketState(){
  const el = document.getElementById('ovMarketState');
  if(!el) return;
  try{
    const m = await api('/api/market_state?history=0');
    if(!m || !m.available || !m.display || !m.display.state){ el.style.display='none'; return; }
    const t = m.display, cs = m.current_stats, prev = m.prev, today = m.today;
    const frag = String(t.state).startsWith('FRAGILE');
    const stCol = frag ? 'var(--amber)' : 'var(--green)';
    const pay = cs && cs.vol_pay != null ? cs.vol_pay : null;
    const payCol = pay==null ? 'var(--muted)'
                 : pay >= 1.0 ? 'var(--red)'
                 : pay >= 0.85 ? 'var(--amber)' : 'var(--green)';
    const changed = !m.stale && prev && prev.state && prev.state !== t.state;
    const pct = v => v==null ? '—' : ('p'+Math.round(v));

    // Stale path: market_state.refresh() nulls `state` when the row is not
    // comparable to its own history — thin symbol coverage, or too little
    // history for percentiles. Show the last usable reading and flag it rather
    // than hiding the strip: positioning does not reset overnight, so a
    // day-old reading with a warning beats a blank space, and hiding it would
    // lose the warning too.
    let staleNote = '';
    if(m.stale){
      const why = m.stale_reason==='low_coverage'
        ? `Only ${today&&today.n_symbols!=null?today.n_symbols:'?'} symbols priced today — more than 10% below the trailing 20-session baseline, so ngf_avg is not comparable to its own history. Composition alone can shift the percentile: on 2026-08-21 seventeen low-ngf symbols dropped out and moved it 13 → 28 with the underlying reading flat. Showing the last usable session instead.`
        : 'Fewer than 20 sessions of history — percentiles are not yet defined.';
      staleNote = `<span style="color:var(--amber);cursor:help" title="${why.replace(/"/g,'&quot;')}">`
        + `⚠ as of ${t.date} — today not usable (${m.stale_reason==='low_coverage'?'low coverage':'no baseline'})</span>`;
    }

    el.innerHTML = `<div style="padding:6px 12px;background:var(--card);
        border:1px solid ${m.stale?'var(--amber)':'var(--border)'};border-radius:4px;display:flex;
        align-items:center;flex-wrap:wrap;gap:14px;font-family:var(--mono);font-size:10px">
      <span style="font-size:9px;color:var(--muted)">MARKET ${m.stale?'':(t.date||'')} →</span>
      <span>${_msTip('state','STATE')} <b style="color:${stCol}">${t.state}</b>${
        changed ? ` <span style="color:var(--muted)">(was ${prev.state})</span>` : ''}</span>
      <span>${_msTip('ngf','MKT γ POS')} <b>${t.gpos_avg!=null?((t.gpos_avg>=0?'+':'')+t.gpos_avg.toFixed(3)):'—'}</b>
        <span style="color:var(--muted)">fragility ${pct(t.fragility_pctile)}</span></span>
      <span>${_msTip('iv','NIFTY IV')} <b>${t.nifty_iv!=null?t.nifty_iv.toFixed(1):'—'}</b>
        <span style="color:var(--muted)">${pct(t.iv_pctile)}</span></span>
      <span>${_msTip('pay','CYCLE')} <b>${t.near_dte!=null?Math.round(t.near_dte)+'d':'—'}</b>
        <span style="color:var(--muted)">to expiry</span></span>
      ${staleNote}
      <span style="margin-left:auto;color:var(--muted);font-size:9px">
        ${t.n_symbols||'—'} stocks · vol gauge, not direction</span>
    </div>`;
    el.style.display='';
  }catch(e){ el.style.display='none'; }
}

// ── Overview meta: snapshot timestamp badge + recent-signals strip (#1,#3) ──
async function loadOverviewMeta(){
  loadMarketState();          // independent fetch; failures hide their own strip
  try{
    const m = await api('/api/overview_meta');
    // Snapshot timestamp badge
    const badge = document.getElementById('ovSnapBadge');
    if(badge) badge.textContent = m.snapshot_ts ? ('SNAP ' + m.snapshot_ts) : '';
    // Recent exposure signals strip (query-only)
    renderSignalStrip(m.signal_summary||{}, m.exposure_date);
  }catch(e){ /* exposure_eod may be absent — strip stays hidden */ }
}

function renderSignalStrip(counts, expDate){
  const el = document.getElementById('ovSignalStrip');
  if(!el) return;
  // Priority signals to surface (high-value only)
  const PRIO = [
    ['regime_flip_to_neg','▼ →neg','var(--red)'],
    ['regime_flip_to_pos','▲ →pos','var(--green)'],
    ['_releasing','⚡ release','var(--red)'],
    ['_compressing','🌀 coiling','var(--acc)'],
  ];
  const parts = PRIO.filter(([k])=>counts[k]).map(([k,label,col])=>
    `<span style="color:${col};font-family:var(--mono);font-size:10px;margin-right:14px;
       cursor:pointer" onclick="document.querySelector('[data-view=&quot;expscreen&quot;]').click()">
       ${label} <b>${counts[k]}</b></span>`);
  if(parts.length){
    el.innerHTML = `<div style="padding:6px 12px;background:var(--card);border:1px solid var(--border);
      border-radius:4px;display:flex;align-items:center;flex-wrap:wrap">
      <span style="font-family:var(--mono);font-size:9px;color:var(--muted);margin-right:12px">
        EOD SIGNALS ${expDate||''} →</span>${parts.join('')}
      <span style="font-family:var(--mono);font-size:9px;color:var(--muted);margin-left:auto;cursor:pointer"
        onclick="document.querySelector('[data-view=&quot;expscreen&quot;]').click()">full screener ›</span>
    </div>`;
    el.style.display='';
  } else {
    el.style.display='none';
  }
}

// ── Bootstrap ─────────────────────────────────────────────────────

(async function init(){
  loadSettings();
  await initSymbols();
  loadOverview().catch(e=>console.error('Overview:',e));
  updateIciciStatus().catch(()=>{});
})();


// ══════════════════════════════════════════════════════════════════
// ITEM 1: Live clock + market status (IST = UTC+5:30)
// ══════════════════════════════════════════════════════════════════
function updateClock(){
  const now = new Date(Date.now() + (5*60+30)*60000 - new Date().getTimezoneOffset()*60000);
  // For simplicity use toLocaleString with IST
  const ist = new Date(new Date().toLocaleString('en-US',{timeZone:'Asia/Kolkata'}));
  const hh=ist.getHours(), mm=ist.getMinutes(), ss=ist.getSeconds();
  const pad=n=>String(n).padStart(2,'0');
  const timeStr=`${pad(hh)}:${pad(mm)}:${pad(ss)} IST`;
  const dateStr=ist.toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'});

  let statusClass='ms-closed', statusText='CLOSED';
  const mins=hh*60+mm;
  if(mins>=9*60 && mins<9*60+15){statusClass='ms-pre';statusText='PRE-OPEN';}
  else if(mins>=9*60+15 && mins<15*60+30){statusClass='ms-open';statusText='OPEN';}
  else if(mins>=15*60+30 && mins<16*60){statusClass='ms-post';statusText='POST-CLOSE';}

  const el=document.getElementById('marketClock');
  if(el) el.innerHTML=
    `<span>${dateStr}</span><span>${timeStr}</span>`+
    `<span class="market-status ${statusClass}">${statusText}</span>`;
}
setInterval(updateClock,1000);
updateClock();

// ══════════════════════════════════════════════════════════════════
// ITEM 2: DTE badge helper
// ══════════════════════════════════════════════════════════════════
function dteBadge(expiryStr){
  if(!expiryStr)return'';
  const exp=new Date(expiryStr.substring(0,10));
  const today=new Date(); today.setHours(0,0,0,0);
  const dte=Math.ceil((exp-today)/86400000);
  if(isNaN(dte))return'';
  const cls=dte<=7?'dte-near':dte<=30?'dte-mid':'dte-far';
  return`<span class="dte-badge ${cls}">DTE: ${dte}d</span>`;
}
// Wire DTE badge to all expiry selects
function updateDteBadge(expSelId, badgeId){
  const el=document.getElementById(expSelId);
  const b=document.getElementById(badgeId);
  if(!el||!b)return;
  b.innerHTML=dteBadge(el.value);
}
// Attach to all expiry selects
['gexExpiry','ivExpiry','trendExpiry','plExpiry'].forEach(id=>{
  const el=document.getElementById(id);
  if(!el)return;
  const badgeId=id+'DteBadge';
  el.addEventListener('change',()=>updateDteBadge(id,badgeId));
});

// ══════════════════════════════════════════════════════════════════
// ITEM 4: VIX
// ══════════════════════════════════════════════════════════════════
async function loadVix(){
  const btn=document.getElementById('btnVix');
  if(btn){btn.textContent='LOADING…';btn.disabled=true;}
  const lookback=document.getElementById('vixLookback')?.value||30;
  setLoad('vixChart');
  try{
    let d;
    try{ d=await api(`/api/vix?lookback_days=${lookback}`); }
    catch(e){
      document.getElementById('vixGauge').innerHTML=`<div class="empty err" style="padding:24px">⚠ VIX fetch failed: ${e.message}<br><br><span style="font-size:9px;color:var(--muted)">Ensure NSEFetcher is installed at folder1/nse_fetcher.py and NSE cookies are valid.</span></div>`;
      setEmpty('vixChart','VIX data unavailable');
      btn.textContent='REFRESH';btn.disabled=false;
      return;
    }
    // Gauge KPIs
    const levelClass=`vix-${d.level}`;
    document.getElementById('vixGauge').innerHTML=`
      <div class="vix-gauge">
        <div class="vix-number ${(d.chg??0)>0?'down':(d.chg??0)<0?'up':''}">${fmt(d.vix,2)}</div>
        <div class="vix-level ${levelClass}">${d.level.toUpperCase()}</div>
        <div style="font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:6px">
          Prev: ${fmt(d.vix_prev,2)} &nbsp;
          Chg: <span class="${(d.chg??0)>0?'down':'up'}">${(d.chg??0)>0?'+':''}${fmt(d.chg??0,2)}</span>
          (${d.chg_pct>0?'+':''}${fmt(d.chg_pct,2)}%)
        </div>
        <div style="font-family:var(--mono);font-size:9px;color:var(--muted);margin-top:8px">
          Low &lt;12 · Normal 12–20 · Elevated 20–30 · Extreme &gt;30
        </div>
      </div>`;
    // Historical chart
    if(d.history?.length){
      const dates=d.history.map(r=>r.date);
      const closes=d.history.map(r=>r.close);
      const zones=[
        {y0:0,y1:12,color:'rgba(16,185,129,.05)'},
        {y0:12,y1:20,color:'rgba(0,200,240,.05)'},
        {y0:20,y1:30,color:'rgba(245,158,11,.07)'},
        {y0:30,y1:100,color:'rgba(244,63,94,.08)'},
      ];
      plot('vixChart',[
        {name:'India VIX',x:dates,y:closes,type:'scatter',mode:'lines',
          line:{color:C.acc2,width:2.5},fill:'tozeroy',fillcolor:'rgba(240,112,0,.06)'},
        {name:'Prev Close',x:dates,y:d.history.map(r=>r.prev),type:'scatter',mode:'lines',
          line:{color:C.muted,width:1,dash:'dot'},opacity:.5},
      ],{...LB,
        shapes:zones.map(z=>({type:'rect',x0:dates[0],x1:dates[dates.length-1],
          y0:z.y0,y1:z.y1,fillcolor:z.color,line:{width:0},layer:'below'})),
        xaxis:{...LB.xaxis,title:'Date'},
        yaxis:{...LB.yaxis,title:'VIX',rangemode:'tozero'},
      });
    }else setEmpty('vixChart','No historical data');
  }finally{
    const b=document.getElementById('btnVix');if(b){b.textContent='REFRESH';b.disabled=false;}
  }
}

// ══════════════════════════════════════════════════════════════════
// ITEM 6: OI Walls  (v3 — shelves + gamma wall, lean row + hover-tip detail)
// One <tr> per symbol so sort/filter/CSV work without special-casing.
// The CE/PE shelf + gamma-wall detail lives in a hover tooltip on a right-side
// ⊕ cell (no interleaved detail rows that broke sortTbl).
// ══════════════════════════════════════════════════════════════════
let _wallsData = [];        // last payload, for tooltip rendering
let _wallsFrac = 0.40;      // runtime-tunable shelf fraction

function _wallShelfBand(lo, hi, isShelf){
  // compact "lo–hi" band, or a lone strike when not a shelf
  if(lo==null) return '—';
  if(!isShelf || lo===hi) return `<span class="neu">${fmt(lo,0)}</span>`;
  return `${fmt(lo,0)}<span style="color:var(--muted)">–</span>${fmt(hi,0)}`;
}
function _wallMigrationArrow(mig){
  if(mig==null || Math.abs(mig)<1e-9) return '<span class="neu">→</span>';
  return mig>0 ? `<span class="up" title="shelf centre rising ${fmt(mig,1)}">↑ ${fmt(mig,1)}</span>`
              : `<span class="down" title="shelf centre falling ${fmt(mig,1)}">↓ ${fmt(Math.abs(mig),1)}</span>`;
}

// Detail tooltip body — CE/PE shelf bands + gamma wall. Returned as plain HTML
// (no <tr>/<td colspan>) so it can sit inside a hover popover div.
function _wallDetailTip(r){
  const shelfBlock = (side, lo, hi, com, oi, n, isShelf, members, oiChg, sig)=>{
    const band = (lo==null) ? '—'
      : (isShelf && lo!==hi) ? `${fmt(lo,0)} – ${fmt(hi,0)}` : `${fmt(lo,0)} (lone wall)`;
    const mem = (members&&members.length)
      ? members.map(m=>fmt(m,0)).join(' · ') : '—';
    return `<div style="flex:1;min-width:230px;padding:8px 12px;border:1px solid var(--border);
        border-radius:4px;background:var(--surf)">
      <div style="font-family:var(--mono);font-size:9px;color:${side==='CE'?'var(--red)':'var(--green)'};
        letter-spacing:.06em;margin-bottom:6px">${side} SHELF ${isShelf?'':'(lone wall)'}</div>
      <div style="display:flex;flex-wrap:wrap;gap:10px;font-family:var(--mono);font-size:10px">
        <div><span style="color:var(--muted)">BAND</span> <b>${band}</b></div>
        <div><span style="color:var(--muted)">CoM</span> ${com!=null?fmt(com,1):'—'}</div>
        <div><span style="color:var(--muted)">Σ OI</span> ${fmtL(oi)}</div>
        <div><span style="color:var(--muted)">N</span> ${n||1}</div>
        <div><span style="color:var(--muted)">ΣΔOI</span> ${oiChg!=null?sspan(oiChg,0):'—'}</div>
        <div><span style="color:var(--muted)">SIG</span> ${sig?pill(sig):'—'}</div>
      </div>
      <div style="font-family:var(--mono);font-size:9px;color:var(--muted);margin-top:6px">
        strikes: ${mem}</div>
    </div>`;
  };
  const gammaBlock = ()=>{
    const gw = r.gamma_wall_strike;
    const div = r.gamma_oi_divergence;
    return `<div style="flex:1;min-width:200px;padding:8px 12px;border:1px solid var(--border);
        border-radius:4px;background:var(--surf)">
      <div style="font-family:var(--mono);font-size:9px;color:var(--acc);letter-spacing:.06em;margin-bottom:6px">
        GAMMA WALL ${div?'<span style="color:var(--amber)" title="gamma wall ≠ OI wall — this strike does the real pinning">⚠ DIVERGENT</span>':''}</div>
      <div style="display:flex;flex-wrap:wrap;gap:10px;font-family:var(--mono);font-size:10px">
        <div><span style="color:var(--muted)">STRIKE</span> <b>${gw!=null?fmt(gw,0):'—'}</b></div>
        <div><span style="color:var(--muted)">vs CE WALL</span> ${gw!=null&&r.ce_wall_strike!=null?sspan(gw-r.ce_wall_strike,0):'—'}</div>
        <div><span style="color:var(--muted)">|γ| EXP</span> ${r.gamma_wall_net_gexv!=null?fmtL(r.gamma_wall_net_gexv):'—'}</div>
      </div>
      ${div?`<div style="font-family:var(--mono);font-size:9px;color:var(--amber);margin-top:6px">
        Price tends to pin/resist at the gamma wall (${fmt(gw,0)}), not the OI wall (${fmt(r.ce_wall_strike,0)}).</div>`:''}
    </div>`;
  };
  return `<div style="display:flex;flex-wrap:wrap;gap:10px">
      ${shelfBlock('PE', r.pe_shelf_lo, r.pe_shelf_hi, r.pe_shelf_com, r.pe_shelf_oi, r.pe_shelf_n, r.pe_is_shelf, r.pe_shelf_members, r.pe_shelf_oi_chg, r.pe_shelf_signal)}
      ${shelfBlock('CE', r.ce_shelf_lo, r.ce_shelf_hi, r.ce_shelf_com, r.ce_shelf_oi, r.ce_shelf_n, r.ce_is_shelf, r.ce_shelf_members, r.ce_shelf_oi_chg, r.ce_shelf_signal)}
      ${gammaBlock()}
    </div>`;
}

async function loadOiWalls(){
  const ft=getFilter('wallsFilterGroup');
  const fracEl=document.getElementById('wallsFracInput');
  if(fracEl && fracEl.value) _wallsFrac=parseFloat(fracEl.value)||0.40;
  // Populate expiry selector on first call (if still empty beyond the default option)
  const expSel=document.getElementById('wallsExpirySelect');
  if(expSel && expSel.options.length<=1){
    try{
      const exps=await api('/api/oi_walls_expiries');
      exps.forEach(e=>{
        const o=document.createElement('option');
        o.value=e; o.textContent=e;
        expSel.appendChild(o);
      });
    }catch(_){/* non-fatal */}
  }
  const expVal = expSel ? expSel.value : '';
  const expParam = expVal ? `&expiry=${encodeURIComponent(expVal)}` : '';
  document.getElementById('oiWallsTable').innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  document.getElementById('oiWallsKpis').innerHTML='';
  try{
    const data=await api(`/api/oi_walls?filter_type=${ft}&shelf_frac=${_wallsFrac}${expParam}`);
    if(!data||!data.length){
      document.getElementById('oiWallsTable').innerHTML='<div class="empty">No data</div>';
      return;
    }
    _wallsData=data;
    // Lean columns; SYMBOL is col 0 (sortable + filterable). Detail → hover ⊕.
    const cols=[
      {key:'symbol',label:'SYMBOL'},
      {key:'spot',label:'SPOT'},
      {key:'fut_price',label:'FUT'},
      {key:'pcr',label:'PCR'},
      {key:'pe_wall_strike',label:'PUT WALL'},
      {key:'pe_band',label:'PE SHELF'},
      {key:'pe_shelf_signal',label:'PE SIG'},
      {key:'pe_mig',label:'PE MIG'},
      {key:'ce_wall_strike',label:'CALL WALL'},
      {key:'ce_band',label:'CE SHELF'},
      {key:'ce_shelf_signal',label:'CE SIG'},
      {key:'ce_mig',label:'CE MIG'},
      {key:'gamma_wall_strike',label:'γ WALL'},
      {key:'wall_range',label:'RANGE'},
      {key:'_tip',label:'',plain:true},   // ⊕ hover-detail, last column
    ];
    const head='<tr>'+cols.map(c=>c.plain?`<th></th>`:`<th onclick="sortTbl(this)">${c.label}</th>`).join('')+'</tr>';
    const body=data.map((r,ri)=>{
      const gdiv = r.gamma_oi_divergence;
      const tds=[
        `<td data-export="${r.symbol}"><span style="cursor:pointer;color:var(--acc)"
           onclick="jumpOiSignals('${r.symbol}')" title="Jump to OI Signals for ${r.symbol}">${r.symbol} ↗</span></td>`,
        `<td data-export="${r.spot}">${fmt(r.spot,2)}</td>`,
        `<td data-export="${r.fut_price}">${r.fut_price>0?fmt(r.fut_price,2):'—'}</td>`,
        `<td data-export="${r.pcr}">${r.pcr!=null?fmt(r.pcr,3):'—'}</td>`,
        `<td data-export="${r.pe_wall_strike}"><span class="up">${fmt(r.pe_wall_strike,0)}</span></td>`,
        `<td data-export="${r.pe_shelf_lo}-${r.pe_shelf_hi}">${_wallShelfBand(r.pe_shelf_lo,r.pe_shelf_hi,r.pe_is_shelf)}</td>`,
        `<td data-export="${r.pe_shelf_signal||''}">${r.pe_shelf_signal?pill(r.pe_shelf_signal):'—'}</td>`,
        `<td data-export="${r.pe_shelf_com||''}">${_wallMigrationArrow(r.pe_shelf_members&&r.pe_shelf_members.length?(r.pe_shelf_com-r.pe_wall_strike):null)}</td>`,
        `<td data-export="${r.ce_wall_strike}"><span class="down">${fmt(r.ce_wall_strike,0)}</span></td>`,
        `<td data-export="${r.ce_shelf_lo}-${r.ce_shelf_hi}">${_wallShelfBand(r.ce_shelf_lo,r.ce_shelf_hi,r.ce_is_shelf)}</td>`,
        `<td data-export="${r.ce_shelf_signal||''}">${r.ce_shelf_signal?pill(r.ce_shelf_signal):'—'}</td>`,
        `<td data-export="${r.ce_shelf_com||''}">${_wallMigrationArrow(r.ce_shelf_members&&r.ce_shelf_members.length?(r.ce_shelf_com-r.ce_wall_strike):null)}</td>`,
        `<td data-export="${r.gamma_wall_strike||''}">${r.gamma_wall_strike!=null
           ?`<span class="${gdiv?'':'neu'}" style="${gdiv?'color:var(--amber);font-weight:600':''}"
             title="${gdiv?'gamma wall ≠ OI wall — real pin here':'gamma wall aligns with OI wall'}">${fmt(r.gamma_wall_strike,0)}${gdiv?' ⚠':''}</span>`
           :'—'}</td>`,
        `<td data-export="${r.wall_range}">${fmt(r.wall_range,0)} pts</td>`,
        // ⊕ hover-detail cell — content carried on the trigger; shown via a
        // shared fixed-position popover (escapes the .tw scroll clipping).
        `<td data-export="" style="text-align:center" class="wall-tip-cell">
           <span class="wall-tip-trigger" tabindex="0" data-wallidx="${ri}"
                 title="shelf bands + γ-wall detail">⊕</span>
         </td>`,
      ].join('');
      return `<tr>${tds}</tr>`;
    }).join('');
    const csvHdr = cols.filter(c=>!c.plain).map(c=>c.label).join('\u0001');
    document.getElementById('oiWallsTable').innerHTML =
      `<table data-cols="${csvHdr}"><thead>${head}</thead><tbody>${body}</tbody></table>`;
    _wireWallTips();
  }catch(e){
    document.getElementById('oiWallsTable').innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;
  }
}

// Shared fixed-position popover for the ⊕ detail cells. One element, reused;
// position:fixed so the .tw scroll container can't clip it.
function _ensureWallTipEl(){
  let el=document.getElementById('wallTipPop');
  if(!el){
    el=document.createElement('div');
    el.id='wallTipPop';
    el.className='wall-tip-pop';
    el.addEventListener('mouseenter',()=>{el._over=true;});
    el.addEventListener('mouseleave',()=>{el._over=false;_hideWallTip();});
    document.body.appendChild(el);
  }
  return el;
}
function _showWallTip(trigger){
  const idx=parseInt(trigger.dataset.wallidx,10);
  const r=_wallsData[idx]; if(!r) return;
  const el=_ensureWallTipEl();
  el.innerHTML=_wallDetailTip(r);
  el.style.display='block';
  // Measure then place: prefer below-left of the trigger, clamp into viewport.
  const tr=trigger.getBoundingClientRect();
  const pw=el.offsetWidth, ph=el.offsetHeight;
  const vw=window.innerWidth, vh=window.innerHeight;
  let left=tr.right-pw;                 // right-align to the ⊕
  if(left<8) left=8;
  if(left+pw>vw-8) left=vw-8-pw;
  let top=tr.bottom+6;
  if(top+ph>vh-8) top=tr.top-ph-6;     // flip above if it would overflow bottom
  if(top<8) top=8;
  el.style.left=left+'px';
  el.style.top=top+'px';
}
function _hideWallTip(){
  const el=document.getElementById('wallTipPop');
  if(!el) return;
  // small delay lets the pointer cross the gap into the popover
  setTimeout(()=>{ if(!el._over && !el._triggerOver) el.style.display='none'; },80);
}
function _wireWallTips(){
  const tbl=document.querySelector('#oiWallsTable table');
  if(!tbl) return;
  tbl.querySelectorAll('.wall-tip-trigger').forEach(t=>{
    t.addEventListener('mouseenter',()=>{ t._over=true;
      const el=_ensureWallTipEl(); el._triggerOver=true; _showWallTip(t); });
    t.addEventListener('mouseleave',()=>{ t._over=false;
      const el=document.getElementById('wallTipPop'); if(el) el._triggerOver=false;
      _hideWallTip(); });
    t.addEventListener('focus',()=>_showWallTip(t));
    t.addEventListener('blur',()=>_hideWallTip());
  });
}

// ══════════════════════════════════════════════════════════════════
// ITEM 3: Enhanced Strike Trend / Option Lens
// ══════════════════════════════════════════════════════════════════
async function loadTrend(){
  const sym   =document.getElementById('trendSymbol').value;
  const exp   =document.getElementById('trendExpiry').value;
  const strike=document.getElementById('trendStrike').value;
  if(!sym||!exp||!strike){alert('Select symbol, expiry and ATM distance');return;}

  const m1=document.getElementById('trendM1').value;
  const m2=document.getElementById('trendM2').value||'';
  const m3=document.getElementById('trendM3').value||'';
  const ts =document.getElementById('trendTimestamp')?.value||'';

  setLoad('trendChart');
  document.getElementById('trendSnapshotCard').innerHTML='';

  try{
    // Multi-metric time series
    let url=`/api/strike_trend_multi?symbol=${encodeURIComponent(sym)}&expiry=${encodeURIComponent(exp)}&strike_price=${strike}&m1=${m1}`;
    if(m2)url+=`&m2=${m2}`;
    if(m3)url+=`&m3=${m3}`;
    const[tdata, snap]=await Promise.all([
      api(url),
      api(`/api/strike_snapshot?symbol=${encodeURIComponent(sym)}&strike_price=${strike}&expiry=${encodeURIComponent(exp)}${ts?'&timestamp='+encodeURIComponent(ts):''}`),
    ]);

    document.getElementById('trendChartTitle').textContent=
      `${sym} | Strike ${strike} | ${exp}`;

    const rows=tdata.rows||[];
    const metrics=tdata.metrics||[m1];
    const colors=[C.acc,C.green,C.acc2];
    const yaxis2Metrics=['ce_oi','pe_oi','ce_volume','pe_volume','ce_tbq','pe_tbq'];
    const traces=metrics.map((m,i)=>({
      name:m,
      x:rows.map(r=>r.timestamp),
      y:rows.map(r=>r[m]),
      type:'scatter',mode:'lines+markers',
      line:{color:colors[i],width:i===0?2.5:1.8},
      marker:{size:3},
      yaxis: yaxis2Metrics.includes(m)&&i>0?'y2':'y',
    }));

    plot('trendChart',traces,{...LB,
      xaxis:{...LB.xaxis,title:'Time',tickangle:-30},
      yaxis:{...LB.yaxis,title:m1},
      yaxis2:{overlaying:'y',side:'right',title:m2||'',
        gridcolor:'transparent',tickfont:{size:9}},
    });

    // Greeks snapshot card
    if(snap){
      const ltp_s_badge=(s,type)=>{
        if(!s||s==='NA')return'';
        const isPrem=s==='premium';
        return`<span class="${isPrem?'ltp-s-prem':'ltp-s-disc'}">${isPrem?'PREM':'DISC'}</span>`;
      };
      const g=(label,val,extra='')=>`
        <div class="greek-row">
          <span class="greek-label">${label}</span>
          <span class="greek-val">${val} ${extra}</span>
        </div>`;
      document.getElementById('trendSnapshotCard').innerHTML=`
        <div class="sh">CURRENT SNAPSHOT — ${sym} ${strike} ${snap.ce_moneyness||''}</div>
        <div class="g2">
          <div>
            <div class="card-title" style="color:var(--green)">CALL (CE)</div>
            <div class="greeks-grid">
              ${g('LTP', fmt(snap.ce_ltp,2), ltp_s_badge(snap.ce_ltp_s,'ce'))}
              ${g('THEORETICAL', fmt(snap.ce_tprice,2))}
              ${g('PRICE RATIO', snap.ce_price_ratio!=null?fmt(snap.ce_price_ratio,3):'-')}
              ${g('IV', fmt(snap.ce_iv,2)+'%')}
              ${g('DELTA', fmt(snap.ce_delta,4))}
              ${g('GAMMA', fmt(snap.ce_gamma,6))}
              ${g('THETA', fmt(snap.ce_theta,4))}
              ${g('VEGA',  fmt(snap.ce_vega,4))}
              ${g('VANNA', fmt(snap.ce_vanna,6))}
              ${g('CHARM', fmt(snap.ce_charm,6))}
              ${g('INTRINSIC', fmt(snap.ce_intrinsic,2))}
              ${g('TIME VAL',  fmt(snap.ce_time_value,2))}
              ${g('BID/ASK',   fmt(snap.ce_bid,2)+'/'+fmt(snap.ce_ask,2))}
              ${g('OI', fmtL(snap.ce_oi))}
              ${g('GEXv', fmtL(snap.ce_gexv))}
            </div>
          </div>
          <div>
            <div class="card-title" style="color:var(--red)">PUT (PE)</div>
            <div class="greeks-grid">
              ${g('LTP', fmt(snap.pe_ltp,2), ltp_s_badge(snap.pe_ltp_s,'pe'))}
              ${g('THEORETICAL', fmt(snap.pe_tprice,2))}
              ${g('PRICE RATIO', snap.pe_price_ratio!=null?fmt(snap.pe_price_ratio,3):'-')}
              ${g('IV', fmt(snap.pe_iv,2)+'%')}
              ${g('DELTA', fmt(snap.pe_delta,4))}
              ${g('GAMMA', fmt(snap.pe_gamma,6))}
              ${g('THETA', fmt(snap.pe_theta,4))}
              ${g('VEGA',  fmt(snap.pe_vega,4))}
              ${g('VANNA', fmt(snap.pe_vanna,6))}
              ${g('CHARM', fmt(snap.pe_charm,6))}
              ${g('INTRINSIC', fmt(snap.pe_intrinsic,2))}
              ${g('TIME VAL',  fmt(snap.pe_time_value,2))}
              ${g('BID/ASK',   fmt(snap.pe_bid,2)+'/'+fmt(snap.pe_ask,2))}
              ${g('OI', fmtL(snap.pe_oi))}
              ${g('GEXv', fmtL(snap.pe_gexv))}
            </div>
          </div>
        </div>
        <div class="kpi-row" style="margin-top:10px">
          ${[
            {l:'SPOT',v:fmt(snap.spot,2)},
            {l:'FUT',v:snap.fut_price>0?fmt(snap.fut_price,2):'—'},
            {l:'DTE',v:fmt(snap.dte,0)+'d'},
            {l:'ATM DIST',v:fmt(snap.distance_from_atm,0)},
            {l:'RV (ANNUAL)',v:fmt(snap.rv,2)+'%'},
            {l:'RISK REV',v:fmt(snap.riskreversal,4)},
            {l:'SENTIMENT',v:sentBadge(snap.sentiment)},
            {l:'REGIME',v:regimeBadge(snap.regime)},
          ].map(k=>`<div class="kpi">
            <div class="kpi-label">${k.l}</div>
            <div class="kpi-val acc" style="font-size:14px">${k.v}</div>
          </div>`).join('')}
        </div>`;
    }
  }catch(e){setEmpty('trendChart','⚠ '+e.message);}
}

// ══════════════════════════════════════════════════════════════════
// ITEM 10: PCR per strike — added to GEX panel table
// (PCR is already computed in /api/gex response via ce_oi/pe_oi)
// We just render it in the GEX details table
// ══════════════════════════════════════════════════════════════════
// GEX details table rendered after loadGex - appended to stab-gex-main
function renderGexTable(gex){
  const el=document.getElementById('gexStrikeTable');
  if(!el||!gex?.length)return;
  el.innerHTML=makeTbl(gex,[
    {key:'strike_price',label:'STRIKE'},
    {key:'ce_oi',label:'CE OI',fmt:v=>fmtL(v)},
    {key:'pe_oi',label:'PE OI',fmt:v=>fmtL(v)},
    {key:'pcr',label:'PCR',fmt:(v,r)=>{
      if(!r.ce_oi)return'—';
      const pcr=r.pe_oi/r.ce_oi;
      const cls=pcr>1.5?'up':pcr<0.5?'down':'neu';
      return`<span class="${cls}">${fmt(pcr,2)}</span>`;
    }},
    {key:'ce_gexv',label:'CE GEX ₹M',fmt:v=>fmt(v,2)},
    {key:'pe_gexv',label:'PE GEX ₹M',fmt:v=>fmt(v,2)},
    {key:'net_gexv',label:'NET GEX ₹M',fmt:v=>sspan(v,2)},
  ]);
}

// ═══════════════════════════════════════════════════════════════════
// PLAYBOOK — gate-based screen over exposure_eod, rebuilt 2026-10-26 from a backtest.
//
// DESIGN CONTRACT (read before editing):
//   * Backtest: exposure_hist front month, 2021-01 → 2026-10, short ATM straddle
//     entered at the EOD close, exit at 50% of credit or DTE-5 (146k trades), plus
//     next-day hold (|Δfut| ≤ em_1d). Stock-level tests compare names against each
//     other ON THE SAME DAY, t-stats clustered by month.
//   * The old 0–100 weighted SELL score was dropped as a stock ranking: among
//     passing names its top third earned ~3 pts LESS of credit than its bottom
//     third (t −3.2), and it had no same-day skill on next-day hold. Lopsided γ,
//     γ POS depth, days-in-regime: negative; concentration, wings: null; "not ◆ pin"
//     was backwards (pins do better, t +2.2). What the score DOES measure is market
//     fragility, so its universe average is kept as a WEATHER input (server side).
//   * Gates that earned their place (vs same day): results before expiry −6.8 pts,
//     negative regime −3.4, regime_flip_to_neg −6.8 (2026 live).
//   * Ranking: ATM IV ÷ NSE SPAN vol (row.span_rich, joined by the endpoint).
//     Top vs bottom third +8.0 pts of credit (t 5.8), +8.4 out of sample 2024-26;
//     holds within IV-level and IV/HV20 terciles. < 0.65 THIN (−5 pts), ≥ 0.85 RICH.
//     History covers only ~70 wheel-universe names → thresholds provisional.
//   * Not used (tested, no out-of-sample value): richness_ref, NIFTY VRP z, stock
//     IV/HV20 ranking, RV next-day forecast (49 days only), instability_widening /
//     instab_score, compression thesis, deep-short-γ long straddles (3 months only).
// ═══════════════════════════════════════════════════════════════════

// ---- small helpers ----
const _pbNum = v => (typeof v === 'number' && isFinite(v)) ? v : null;
const _pbPos = r => (r.gex_regime === 'positive' || r.gex_regime === 'all_positive');
const _pbNeg = r => (r.gex_regime === 'negative' || r.gex_regime === 'all_negative');
const _pbConfRank = c => ({low:0, medium:1, high:2}[c] ?? 0);
const _pbDate = d => d ? new Date(String(d).slice(0,10)+'T00:00:00')
  .toLocaleDateString('en-GB',{day:'2-digit',month:'short'}) : '';
const PB_SPAN_THIN = 0.65, PB_SPAN_RICH = 0.85;
// Results-calendar cell (exposure_eod.next_results_date / near_earnings). Amber =
// results fall before this row's expiry; muted = next results after expiry but
// within 21 days (matters if you roll); blank otherwise / for indices.
function eventCell(r){
  const d = r && r.next_results_date;
  if(!d) return '—';
  const days = Math.round((new Date(String(d).slice(0,10)) - new Date(String(r.date).slice(0,10)))/864e5);
  const lbl = `R ${_pbDate(d)}`;
  if(r.near_earnings)
    return `<span title="Results ${String(d).slice(0,10)} (${days}d) fall BEFORE expiry ${String(r.expiry||'').slice(0,10)}: `
      + `IV includes event variance; gamma signals ignore the results gap" `
      + `style="color:var(--amber);font-weight:600;font-family:var(--mono);font-size:10px">⚠ ${lbl}</span>`;
  if(days <= 21)
    return `<span title="Next results ${String(d).slice(0,10)} (${days}d), after this expiry" `
      + `style="color:var(--muted);font-family:var(--mono);font-size:10px">${lbl}</span>`;
  return '—';
}
function _pbSingleStrike(r){
  if (typeof r.gamma_shelf_single_strike === 'boolean') return r.gamma_shelf_single_strike;
  return r.gamma_shelf_width === 1;
}
function _pbHasSignal(r, key){
  return String(r.signals || '').split(',').map(s=>s.trim()).includes(key);
}
// hold prob {p,bucket,...} — market fragility (see _defenseHoldProb), not a stock edge
function _pbHold(r){
  if (typeof _defenseHoldProb === 'function') return _defenseHoldProb(r);
  return null;
}

// ═══════════════════════════════════════════════════════════════════
// SELL — new short-premium setup. Verdict from gates; rank by ATM IV ÷ SPAN vol.
//   AVOID   negative dealer-gamma regime
//   CAUTION any caution gate fails (✕)
//   PASS    otherwise
// ═══════════════════════════════════════════════════════════════════
function pbSell(r){
  const sr = _pbNum(r.span_rich);
  const turn = _pbNum(r.oi_turnover_ratio);
  const gates = [
    {key:'regime', label:'REGIME +γ', ok:_pbPos(r), hard:true,
       why:_pbNeg(r) ? 'negative regime: −3.4 pts of credit vs same day (2021-26)'
                     : 'dealers long gamma; mixed/negative regimes earned 3% vs 9% of credit'},
    {key:'conf',   label:'CONF ≥ med', ok:_pbConfRank(r.confidence) >= 1, hard:true,
       why:'sparse chains mislead the gamma map'},
    {key:'noevent',label:'no results', ok:!r.near_earnings, hard:true,
       why: r.near_earnings ? `results ${_pbDate(r.next_results_date)} before expiry: −6.8 pts of credit vs same day`
                            : 'no results date before expiry'},
    {key:'recon',  label:'OI recon',   ok:r.oi_recon_ok !== false, hard:true,
       why:'snapshot OI disagrees with bhavcopy (−1.7 pts vs same day, 2026)'},
    {key:'span',   label:'not THIN',   ok:sr==null || sr>=PB_SPAN_THIN, hard:true,
       why: sr==null ? 'no SPAN vol for this expiry (abstains)'
                     : `ATM IV ÷ SPAN vol ${sr.toFixed(2)}; below ${PB_SPAN_THIN}: −5 pts of credit vs same day`},
    // soft / informational
    {key:'rich',   label:'RICH',       ok:sr!=null && sr>=PB_SPAN_RICH, hard:false,
       why:`ATM IV ÷ SPAN vol ≥ ${PB_SPAN_RICH}: +3.5–4 pts vs same day, in and out of sample`},
    {key:'pin',    label:'◆ pin',      ok:_pbSingleStrike(r), hard:false,
       why:'single-strike gamma shelf: pins did better for short straddles (+1.6 pts, t 2.2)'},
    {key:'turn',   label:'live book',  ok:turn!=null && turn>=0.05 && turn<=2.0, hard:false,
       why:'OI turnover outside 0.05–2 (data-quality note only; no outcome effect found)'},
  ];
  let band;
  if (_pbNeg(r)) band = 'avoid';
  else if (gates.some(g=>g.hard && !g.ok)) band = 'caution';
  else band = 'pass';
  // sort key: verdict first, then richness (null richness sorts last within band)
  const bandRank = {pass:2, caution:1, avoid:0}[band];
  return {band, gates, score: sr, sortKey: bandRank*10 + (sr==null ? 0 : Math.min(sr, 5)),
          hold:_pbHold(r)};
}

// ═══════════════════════════════════════════════════════════════════
// MANAGE — triggers for a position already held. Only triggers that showed an
// outcome effect are kept. Removed 2026-10-26: instability_widening (no effect on
// breach or P&L), γ POS < 0.25 (no effect), tight+◆ pin (no effect), "thesis
// expired" (needed prev_signals, never present in view=all).
// ═══════════════════════════════════════════════════════════════════
function pbManage(r){
  const triggers = [
    {key:'results', label:'results ahead', on: !!r.near_earnings, sev:0.90,
       act:`results ${_pbDate(r.next_results_date)} before expiry → close or size down before, or hold through on purpose (−6.8 pts vs same day)`},
    {key:'flip2neg',label:'regime→neg',    on:_pbHasSignal(r,'regime_flip_to_neg'), sev:0.85,
       act:'flipped to dealer short gamma today → reduce/roll/protect (−6.8 pts vs same day)'},
    {key:'neg',     label:'neg regime',    on:_pbNeg(r) && !_pbHasSignal(r,'regime_flip_to_neg'), sev:0.80,
       act:'dealer short gamma → hedging amplifies moves (−3.4 pts vs same day)'},
    // Boolean column, not a signals token (the old check never fired). Note the
    // compression logic itself has essentially never produced a release.
    {key:'release', label:'compression released', on: r.compression_release === true, sev:0.80,
       act:'coil released → exit/flip short premium'},
    {key:'thin',    label:'premium THIN',  on: _pbNum(r.span_rich)!=null && r.span_rich < PB_SPAN_THIN, sev:0.40,
       act:'ATM IV now well below SPAN vol → little premium left to earn; consider closing early'},
  ];
  const fired = triggers.filter(t=>t.on);
  const maxSev = fired.reduce((m,t)=>Math.max(m,t.sev), 0);
  const score100 = Math.min(100, Math.round(fired.reduce((a,t)=>a+t.sev,0)*70));
  const band = maxSev >= 0.80 ? 'act-now' : (fired.length ? 'note' : 'hold');
  return {score:score100, band, triggers, fired, maxSev, sortKey:score100};
}

// ═══════════════════════════════════════════════════════════════════
// BUY — no validated long-premium setup. Listed for information only.
//   compression thesis: long straddle −9% vs about 0 same day (n=1,295)
//   deep short γ: +8% mean but −19% median; all of it from 3 months (Jan-25,
//   Jun-25, Feb-26), month-clustered t −0.25, −7% without them.
// ═══════════════════════════════════════════════════════════════════
function pbBuy(r){
  const gpos = _pbNum(r.gamma_position);
  const amp = _pbNeg(r) && gpos!=null && gpos<0;
  let thesis = null;
  if (amp) thesis = 'dealer short γ (amplification)';
  else if (r.regime_compression) thesis = `compressing (${r.compression_days||0}d)`;
  const depth = gpos==null ? null : Math.max(0, Math.min(1, -gpos/0.5));
  return {band: thesis ? 'info' : 'n/a', thesis: thesis || 'no setup', score: depth,
          sortKey: depth ?? -1, gates: []};
}

// ═══════════════════════════════════════════════════════════════════
// RENDER
// ═══════════════════════════════════════════════════════════════════
const _PB_BANDCOL = {
  pass:'var(--green)', caution:'var(--amber)', avoid:'var(--red)',
  'act-now':'var(--red)', note:'var(--acc)', hold:'var(--muted)',
  info:'var(--muted)', 'n/a':'var(--muted)',
};

function _pbGateStrip(gates){
  return gates.map(g=>{
    if (!g.hard && !g.ok && g.key!=='turn') return '';      // soft positives: show only when present
    if (g.key==='turn' && g.ok) return '';                    // data-quality note: show only when failing
    const col = g.ok ? 'var(--green)' : (g.hard ? 'var(--red)' : 'var(--muted)');
    const mark = g.ok ? '✓' : (g.hard ? '✕' : '·');
    const ttl = `${g.label}: ${g.why}`.replace(/"/g,'&quot;');
    return `<span title="${ttl}" style="font-family:var(--mono);font-size:9px;color:${col};
      border:1px solid ${col};border-radius:3px;padding:0 3px;margin:0 2px 2px 0;
      display:inline-block;white-space:nowrap;${g.hard?'font-weight:600':''}">${mark} ${g.label}</span>`;
  }).join('');
}

function _pbBandPill(band, txt){
  const col = _PB_BANDCOL[band] || 'var(--muted)';
  const sc = txt==null ? '' : ` <span style="opacity:.7">${txt}</span>`;
  return `<span style="font-family:var(--mono);font-size:10px;font-weight:600;color:${col};
    border:1px solid ${col};border-radius:4px;padding:1px 7px;white-space:nowrap">
    ${band.toUpperCase()}${sc}</span>`;
}

function _pbSpanCell(r){
  const v = _pbNum(r.span_rich);
  if (v==null) return '<span style="color:var(--muted)" title="no SPAN vol for this expiry">—</span>';
  const col = v>=PB_SPAN_RICH ? 'var(--green)' : (v<PB_SPAN_THIN ? 'var(--red)' : 'var(--text)');
  const tag = v>=PB_SPAN_RICH ? ' RICH' : (v<PB_SPAN_THIN ? ' THIN' : '');
  return `<span style="font-family:var(--mono);color:${col}" title="ATM IV ${r.atm_iv?.toFixed?.(1)} ÷ SPAN vol ${(r.span_vol*100).toFixed(1)}">${v.toFixed(2)}${tag}</span>`;
}

let _pbData = [];
let _pbWeather = null;
let _pbTab = 'sell';   // sell | manage | buy

// Market weather: whether to sell premium at all today (market-level, from the server).
function _pbRenderWeather(){
  const el = document.getElementById('pbWeather');
  if (!el) return;
  const w = _pbWeather;
  if (!w || w.univ_score==null || w.univ_ivhv==null){ el.innerHTML=''; el.style.display='none'; return; }
  const lows = (w.univ_score_low?1:0) + (w.univ_ivhv_low?1:0);
  const st = lows===2 ? {t:'STAND ASIDE', c:'var(--red)', x:'both low: −7.1% of credit per short straddle (47 weeks, 2021-26)'}
           : lows===1 ? {t:'CAUTION',     c:'var(--amber)', x:'one low: +7–11% of credit (vs +12.6% when clear)'}
           :            {t:'CLEAR',       c:'var(--green)', x:'neither low: +12.6% of credit per short straddle'};
  const th = w.thresholds||{};
  const sf = w.span_file ? `SPAN file ${w.span_file[0]} (${w.span_file[1]}), ${w.span_coverage} rows matched` : 'no SPAN file';
  el.style.display='block';
  el.innerHTML = `<span style="font-weight:700;color:${st.c}">SHORT-PREMIUM WEATHER: ${st.t}</span>
    <span style="margin-left:10px">γ-score avg <b style="color:${w.univ_score_low?'var(--red)':'var(--text)'}">${w.univ_score.toFixed(0)}</b>
    <span style="opacity:.7">(low &lt; ${th.score})</span></span>
    <span style="margin-left:10px">median IV/HV20 <b style="color:${w.univ_ivhv_low?'var(--red)':'var(--text)'}">${w.univ_ivhv.toFixed(2)}</b>
    <span style="opacity:.7">(thin &lt; ${th.ivhv})</span></span>
    <span style="margin-left:10px;opacity:.8">→ ${st.x}</span>
    <span style="margin-left:10px;opacity:.6">· ${w.n_stocks} stocks · ${sf}</span>`;
}

function loadPlaybook(){
  const host = document.getElementById('pbTable');
  if (!host) return;
  host.innerHTML = '<div class="loading"><div class="spinner"></div>Scoring…</div>';

  const date = document.getElementById('pbDate')?.value || '';
  const rank = document.getElementById('pbExpiryRank')?.value || '0';
  // Always pull the FULL universe (view=all): score every name, not just signal-firers.
  const qs = new URLSearchParams({view:'all', expiry_rank:rank, min_confidence:'low', limit:'500'});
  if (date) qs.set('screen_date', date);

  fetch('/api/exposure_screener?'+qs.toString())
    .then(r=>r.json())
    .then(d=>{
      _pbData = d.rows || [];
      _pbWeather = d.weather || null;
      document.getElementById('pbDateBadge').textContent = d.date || '';
      _pbRenderWeather();
      renderPlaybook();
    })
    .catch(e=>{ host.innerHTML = `<div class="empty err">⚠ ${e.message}</div>`; });
}

function _pbScoreFor(r){
  if (_pbTab==='sell')   return pbSell(r);
  if (_pbTab==='manage') return pbManage(r);
  return pbBuy(r);
}

// which bands are shown by default per tab
const _PB_KEEP = {
  sell:   new Set(['pass']),
  manage: new Set(['act-now','note']),
  buy:    new Set(['info']),
};

function renderPlaybook(){
  const host = document.getElementById('pbTable');
  if (!_pbData.length){ host.innerHTML='<div class="empty">Load a date first</div>'; return; }
  const showAll = document.getElementById('pbShowAll')?.checked;

  let scored = _pbData.map((r,i)=>{ r._pbi=i; return {r, v:_pbScoreFor(r)}; });
  if (!showAll){
    const keep = _PB_KEEP[_pbTab];
    scored = scored.filter(x=> keep.has(x.v.band));
  }
  scored.sort((a,b)=> (b.v.sortKey??-1) - (a.v.sortKey??-1));

  if (!scored.length){
    host.innerHTML = `<div class="empty">No ${_pbTab.toUpperCase()} rows at this cut.
      <label style="margin-left:8px;cursor:pointer"><input type="checkbox" id="pbShowAllInline"
      onchange="document.getElementById('pbShowAll').checked=true;renderPlaybook()"> show all</label></div>`;
    return;
  }

  const holdCell = x=>{ const h=_pbHold(x.r); return h?`<span title="next-day hold for this γ position band — market fragility, not a stock edge" style="color:${
      h.bucket==='High'?'var(--green)':(h.bucket==='Low'?'var(--red)':'var(--amber)')}">${h.p}%</span>`:'—'; };
  const cols = [
    {key:'symbol', label:'SYMBOL', fmt:(v,x)=>`<span style="cursor:pointer;color:var(--acc);font-weight:600"
        onclick="jumpHistory('${x.r.symbol}')" title="open history">${x.r.symbol}</span>`},
    {key:'verdict', label:_pbTab==='manage'?'URGENCY':'VERDICT',
        sortValue:(v,x)=>x.v.sortKey,
        fmt:(v,x)=>_pbBandPill(x.v.band, _pbTab==='manage' ? x.v.score : null)},
    {key:'regime', label:'REGIME',
        fmt:(v,x)=> typeof regimePillWithDays==='function'
            ? regimePillWithDays(x.r.gex_regime, x.r) : (x.r.gex_regime||'—')},
  ];
  if (_pbTab==='sell'){
    cols.push(
      {key:'span_rich', label:'IV/SPAN', help:'ATM IV ÷ NSE SPAN vol — the ranking. ≥0.85 RICH, <0.65 THIN',
         sortValue:(v,x)=>_pbNum(x.r.span_rich)??-1, fmt:(v,x)=>_pbSpanCell(x.r),
         exportValue:(v,x)=>_pbNum(x.r.span_rich)??''},
      {key:'atm_iv', label:'ATM IV', sortValue:(v,x)=>_pbNum(x.r.atm_iv)??-1,
         fmt:(v,x)=>_pbNum(x.r.atm_iv)!=null?x.r.atm_iv.toFixed(1):'—'},
      {key:'iv_rank', label:'IV RANK', help:'information only — its edge came almost entirely from 2025-26',
         sortValue:(v,x)=>_pbNum(x.r.iv_rank)??-1, fmt:(v,x)=>_pbNum(x.r.iv_rank)!=null?x.r.iv_rank.toFixed(0):'—'},
      {key:'results', label:'RESULTS', fmt:(v,x)=>eventCell(x.r)},
      {key:'defend', label:'HOLD%', help:'market fragility (γ position band), not a stock-specific edge', fmt:(v,x)=>holdCell(x)},
      {key:'gates', label:'CHECKLIST', fmt:(v,x)=>_pbGateStrip(x.v.gates)},
    );
  } else if (_pbTab==='manage'){
    cols.push(
      {key:'defend', label:'HOLD%', fmt:(v,x)=>holdCell(x)},
      {key:'span_rich', label:'IV/SPAN', fmt:(v,x)=>_pbSpanCell(x.r), sortValue:(v,x)=>_pbNum(x.r.span_rich)??-1},
      {key:'fired', label:'TRIGGERS FIRED', fmt:(v,x)=> x.v.fired.length
          ? x.v.fired.map(t=>`<span title="${t.act.replace(/"/g,'&quot;')}"
              style="font-family:var(--mono);font-size:9px;color:var(--red);
              border:1px solid var(--red);border-radius:3px;padding:0 3px;margin:0 2px 2px 0;
              display:inline-block">${t.label}</span>`).join('')
          : '<span style="color:var(--muted);font-size:10px">— none —</span>'},
      {key:'action', label:'DO', fmt:(v,x)=>{
          if(!x.v.fired.length) return '<span style="color:var(--muted)">hold</span>';
          const top = x.v.fired.slice().sort((a,b)=>b.sev-a.sev)[0];
          return `<span style="font-size:10px">${top.act}</span>`; }},
    );
  } else {
    cols.push(
      {key:'gpos', label:'γ POS', sortValue:(v,x)=>_pbNum(x.r.gamma_position)??9,
         fmt:(v,x)=>_pbNum(x.r.gamma_position)!=null?x.r.gamma_position.toFixed(2):'—'},
      {key:'ngf', label:'NEG γ FRAC', fmt:(v,x)=>_pbNum(x.r.neg_gamma_fraction)!=null?x.r.neg_gamma_fraction.toFixed(2):'—'},
      {key:'thesis', label:'THESIS', fmt:(v,x)=>`<span style="font-size:10px;color:var(--muted)">${x.v.thesis}</span>`},
    );
  }

  const wrapped = scored.map(x=>({ ...x, symbol:x.r.symbol }));
  host.innerHTML = (typeof makeTbl==='function')
    ? makeTbl(wrapped, cols.map(c=>({...c,
        fmt:(v,row)=>c.fmt(v,row),
        sortValue: c.sortValue ? (v,row)=>c.sortValue(v,row) : undefined,
        exportValue: c.exportValue ? (v,row)=>c.exportValue(v,row) : undefined})))
    : '<div class="empty err">makeTbl missing</div>';
}

// ── init (called once by VIEW_INIT.playbook) — populate dates, then load ──
let _pbInit = false;
async function initPlaybook(){
  if (_pbInit){ if(!_pbData.length) loadPlaybook(); return; }
  _pbInit = true;
  try{
    const dd = await (typeof api==='function'
      ? api('/api/exposure_screener/dates')
      : fetch('/api/exposure_screener/dates').then(r=>r.json()));
    const dsel = document.getElementById('pbDate');
    if (dsel) dsel.innerHTML = (dd.dates||[]).map(d=>`<option value="${d}">${d}</option>`).join('');
  }catch(e){ /* dropdown stays empty; LOAD will use latest */ }
  document.getElementById('pbGuide').innerHTML = _pbGuideHtml();
  loadPlaybook();
}

function _pbGuideHtml(){
  return `<b style="color:var(--acc)">PLAYBOOK</b> — rebuilt 2026-10-26 from a backtest
  (front month 2021-01 → 2026-10; short ATM straddle at the EOD close, exit at 50% of credit
  or DTE−5, 146k trades; names compared against each other on the same day).
  <br><br>
  <b>WEATHER</b> (banner) — whether to sell premium at all today. Two market-level inputs:
  the universe-average γ-score (the old Playbook score — it measures market fragility, not which
  stock is safer) and the universe median ATM IV ÷ HV20. Both low ⇒ <span style="color:var(--red)">STAND ASIDE</span>
  (−7.1% of credit, 47 weeks); one low ⇒ CAUTION (+7–11%); neither ⇒ CLEAR (+12.6%).
  <br>
  <b>SELL</b> — verdict from gates: <span style="color:var(--red)">AVOID</span> = negative regime;
  <span style="color:var(--amber)">CAUTION</span> = any ✕ (not positive regime, low confidence, results before
  expiry, OI reconciliation failed, premium THIN); otherwise <span style="color:var(--green)">PASS</span>.
  Ranked by <b>IV/SPAN</b> = ATM IV ÷ NSE SPAN vol: top third beat bottom third by +8 pts of credit
  (t 5.8), out of sample too. ≥ 0.85 RICH, &lt; 0.65 THIN. ◆ pin is a mild plus. IV RANK is shown
  for information only. HOLD% is market fragility, not a stock edge.
  <br>
  <b>MANAGE</b> — triggers for a held position: results ahead, regime→neg, negative regime,
  compression released ⇒ ACT-NOW; premium THIN ⇒ note.
  <br>
  <b>BUY</b> — no validated long-premium setup. Deep dealer-short-γ and compressing names are
  listed for information only (deep short γ: +8% mean but −19% median, all from three months).
  <br><br>
  <span style="color:var(--amber)">CAVEATS:</span> SPAN history covers only ~70 wheel-universe names, so
  the 0.65 / 0.85 cutoffs are provisional; costs/slippage excluded (affects levels, not the comparisons);
  dropped as not validated: weighted stock score, lopsided γ, γ POS depth, concentration, wings, not-pin,
  instability, richness_ref, NIFTY VRP z, RV next-day forecast.`;
}

// tab + control wiring
function _pbWire(){
  document.querySelectorAll('#pbTabs [data-pbtab]').forEach(b=>{
    b.addEventListener('click',()=>{
      document.querySelectorAll('#pbTabs [data-pbtab]').forEach(x=>x.classList.remove('active'));
      b.classList.add('active');
      _pbTab = b.dataset.pbtab;
      renderPlaybook();
    });
  });
  document.getElementById('btnPbLoad')?.addEventListener('click', loadPlaybook);
  document.getElementById('pbShowAll')?.addEventListener('change', renderPlaybook);
  document.getElementById('pbExpiryRank')?.addEventListener('change', ()=>{ if(_pbData.length) loadPlaybook(); });
  document.getElementById('pbDate')?.addEventListener('change', ()=>{ if(_pbData.length) loadPlaybook(); });
  document.getElementById('btnPbGuide')?.addEventListener('click', ()=>{
    const g=document.getElementById('pbGuide'); if(g) g.style.display = g.style.display==='none'?'block':'none';
  });
  document.getElementById('btnPbCsv')?.addEventListener('click', ()=>{
    const t=document.querySelector('#pbTable table');
    if(t && typeof exportTableCsv==='function') exportTableCsv(t, `playbook_${_pbTab}_${document.getElementById('pbDateBadge').textContent||''}.csv`);
  });
}
_pbWire();

// ══════════════════════════════════════════════════════════════════
// RV FORECAST MONITOR — Phase 0 (next-day + rest-of-day). EXPERIMENTAL.
// Reads a rolling pseudo-OOS backtest log (rv.duckdb), not a live nightly
// forecast yet for next-day, and rest-of-day has no live path at all (same-
// day resolution, backtest only). See areas/rv-forecast.md and
// oc_dashboard/routes/rv_forecast.py.
// ══════════════════════════════════════════════════════════════════
let _rvScoreData = null;
let _rvScatterRows = [];   // last full (unfiltered) scatter payload, for filterRvBySymbol
let _rvScatterUnits = 'ann_vol_pct';

function _rvHorizon(){ return document.getElementById('rvHorizon')?.value || 'next_day'; }

async function loadRvForecast(){
  const btn=document.getElementById('btnRvFcst');
  if(btn){btn.textContent='LOADING…';btn.disabled=true;}
  document.getElementById('rvKpis').innerHTML='';
  setLoad('rvScorecard');
  setLoad('rvScatter');
  document.getElementById('rvLatestTable').innerHTML=
    '<div class="loading"><div class="spinner"></div>Loading…</div>';
  const horizon = _rvHorizon();
  document.getElementById('rvHorizonNote').textContent = horizon==='rest_of_day'
    ? 'Rest-of-day: forecasts the REMAINING variance from issue time to close, same trading day. Issued at 5 points through the session (bucket 1-5, roughly hourly from 09:15). Backtest only — resolves same-evening, no live path yet. naive here extrapolates today\'s own realised rate to the remaining buckets, not "today=tomorrow".'
    : 'Next-day: forecasts tomorrow\'s full-session RV from today\'s close. naive here is plain persistence (tomorrow = today).';
  try{
    const sc = await api(`/api/rv_forecast/scorecard?horizon=${horizon}`);
    _rvScoreData = sc;
    _renderRvBadgeAndKpis(sc, horizon);
    _renderRvScorecard(sc);
  }catch(e){
    document.getElementById('rvScorecard').innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;
    document.getElementById('rvBadge').textContent='error';
  }
  await Promise.all([_loadRvLatest(), _loadRvScatter()]);
  if(btn){btn.textContent='REFRESH';btn.disabled=false;}
}

function _renderRvBadgeAndKpis(sc, horizon){
  const badge=document.getElementById('rvBadge');
  badge.textContent = sc.ready
    ? `EXPERIMENTAL · ${sc.n_issue_days} issue days scored`
    : `COLLECTING · ${sc.n_issue_days} issue days`;
  badge.style.color = sc.ready ? '' : 'var(--amber)';

  const models = sc.models||[];
  const best = models.find(m=>m.model==='ols_aug') || models[0];
  const kpis=[
    {label:'ISSUE DAYS', value:sc.n_issue_days, sub:sc.ready?'ready':'below 20-day floor'},
    {label:'BEST QLIKE', value:best?fmt(best.qlike,4):'—',
     sub:best?best.model:''},
    {label:'BIAS RATIO', value:best?fmt(best.bias_ratio,3):'—',
     sub:'actual/fcst, 1.0 = unbiased'},
    {label:'MZ SLOPE (b)', value:best?fmt(best.mz_b,3):'—',
     sub:'1.0 = calibrated'},
    {label:'WITHIN-SYM R²', value:best&&best.mz_r2_within!=null?fmt(best.mz_r2_within,4):'—',
     sub:'day-to-day timing skill'},
  ];
  const readNote = horizon==='rest_of_day'
    ? `Within-symbol R² here is genuinely high (~0.35) — rest-of-day has real `
      +`timing skill, unlike next-day. Bucket 5 (least time left) still runs `
      +`noisier than 1-4 — fewer remaining returns to sum, a sampling-noise `
      +`floor, not a model defect.`
    : `Within-symbol R² near zero means the model mostly knows which names `
      +`run hot vs cold, not that vol is about to move for a given name `
      +`tomorrow. Fine for sizing/screening; not yet a timing signal.`;
  document.getElementById('rvKpis').innerHTML = kpis.map(k=>
    `<div class="kpi"><div class="kpi-label">${k.label}</div>`
    +`<div class="kpi-val acc">${k.value}</div>`
    +`<div class="kpi-sub">${k.sub}</div></div>`).join('')
    + `<div class="kpi" style="max-width:260px">`
    + `<div class="kpi-label">READ THIS FIRST</div>`
    + `<div style="font-size:9px;color:var(--muted);line-height:1.4;margin-top:4px">`
    + `${readNote}</div></div>`;
}

function _renderRvScorecard(sc){
  const cols=[
    {key:'model',label:'MODEL',fmt:v=>v==='naive'?v:`<b>${v}</b>`},
    {key:'n',label:'N',fmt:v=>fmt(v,0)},
    {key:'n_symbols',label:'SYMS',fmt:v=>fmt(v,0)},
    {key:'qlike',label:'QLIKE',fmt:v=>v==null?'—':fmt(v,4),
     help:'Lower is better. straddle_core\'s canonical QLIKE on variance — not MSE, which is dominated by the highest-vol names and ranks models differently (and worse).'},
    {key:'bias_ratio',label:'BIAS',fmt:v=>v==null?'—':fmt(v,3),
     help:'sum(actual)/sum(forecast). 1.0 = unbiased in aggregate. Can look fine here while individual vol deciles are still off — see straddle_core.'},
    {key:'mz_a',label:'MZ α',fmt:v=>v==null?'—':fmt(v,3),
     help:'Mincer-Zarnowitz intercept, log-variance, pooled with two-way clustered SE (straddle_core.pooled_mz_clustered). Only interpretable jointly with mz_b — see mz_b tooltip.'},
    {key:'mz_b',label:'MZ β',fmt:v=>v==null?'—':fmt(v,3),
     help:'Mincer-Zarnowitz slope. 1.0 = calibrated forecast level. This is the number that matters more than α alone.'},
    {key:'mz_r2_within',label:'R² WITHIN',fmt:v=>v==null?'—':fmt(v,4),
     help:'Within-symbol R² (pooled R² after removing each symbol\'s own mean). Measures day-to-day TIMING skill, not cross-sectional level ("high-vol stocks have high vol", which inflates plain pooled R² without being real skill).'},
  ];
  document.getElementById('rvScorecard').innerHTML = makeTbl(sc.models||[], cols)
    + `<div style="padding:6px 10px;font-family:var(--mono);font-size:9px;color:var(--muted)">${sc.note||''}</div>`;
}

async function _loadRvLatest(){
  const model = document.getElementById('rvModel')?.value || 'ols_aug';
  const horizon = _rvHorizon();
  try{
    const res = await api(`/api/rv_forecast/latest?model=${model}&horizon=${horizon}`);
    document.getElementById('rvLatestAsof').textContent = res.issue_d ? `as of ${res.issue_d}` : '—';
    if(!res.rows?.length){
      document.getElementById('rvLatestTable').innerHTML =
        '<div class="empty">No forecasts logged for this model yet.</div>';
      return;
    }
    const isRod = horizon==='rest_of_day';
    const valKey = res.units==='session_vol_pct' ? 'session_vol_pct' : 'ann_vol_pct';
    const valLabel = res.units==='session_vol_pct' ? 'FCST SESSION VOL % (not annualised)' : 'FCST ANN. VOL %';
    const cols=[
      {key:'symbol',label:'SYMBOL'},
      ...(isRod ? [{key:'issue_bucket',label:'ISSUE BKT',fmt:v=>v==null?'—':fmt(v,0)}] : []),
      // next_day forecasts issued by rv_model --live are stored with target_d NULL
      // ("pending") until the next session's data exists -- by design, so holidays
      // never mis-resolve. Show that, rather than a blank.
      {key:'target_d',label:isRod?'DATE':'TARGET DATE',
       fmt:v=>v==null ? (isRod?'—':'<span style="color:var(--muted)" title="resolves after the next session\'s EOD">pending · next session</span>') : String(v).slice(0,10)},
      {key:valKey,label:valLabel,fmt:v=>v==null?'—':fmt(v,1)},
    ];
    document.getElementById('rvLatestTable').innerHTML = makeTbl(res.rows, cols);
    const s=document.getElementById('rvSearch');
    if(s && s.value) filterTableBySymbol('rvLatestTable', s.value);
  }catch(e){
    document.getElementById('rvLatestTable').innerHTML = `<div class="empty err">⚠ ${e.message}</div>`;
  }
}

async function _loadRvScatter(){
  const model = document.getElementById('rvModel')?.value || 'ols_aug';
  const horizon = _rvHorizon();
  try{
    const res = await api(`/api/rv_forecast/scatter?model=${model}&horizon=${horizon}`);
    _rvScatterRows = res.rows||[];
    _rvScatterUnits = res.units || 'ann_vol_pct';
    const badge = document.getElementById('rvScatterBadge');
    if(badge) badge.textContent = horizon==='rest_of_day'
      ? 'rest-of-day session vol % (not annualised) · selected model'
      : 'next-day ann. vol % · selected model';
    const s=document.getElementById('rvSearch');
    _renderRvScatter(s&&s.value ? _rvScatterRows.filter(r=>r.symbol.toUpperCase().includes(s.value.toUpperCase())) : _rvScatterRows, model);
  }catch(e){
    _rvScatterRows=[];
    setEmpty('rvScatter',`⚠ ${e.message}`);
  }
}

function _renderRvScatter(rows, model){
  if(!rows.length){ setEmpty('rvScatter','No scored forecasts for this filter'); return; }
  const unitLabel = _rvScatterUnits==='session_vol_pct' ? 'session vol % (not ann.)' : 'ann. vol %';
  const xs=rows.map(r=>r.fcst_vol), ys=rows.map(r=>r.actual_vol);
  const lo=Math.min(...xs,...ys), hi=Math.max(...xs,...ys);
  plot('rvScatter',[
    {name:'ref (y=x)',x:[lo,hi],y:[lo,hi],type:'scatter',mode:'lines',
      line:{color:C.muted,width:1,dash:'dot'},hoverinfo:'skip'},
    {name:model,x:xs,y:ys,type:'scatter',mode:'markers',
      marker:{color:C.acc,size:5,opacity:.55},
      text:rows.map(r=>`${r.symbol} · ${r.d}`),hovertemplate:'%{text}<br>fcst %{x:.1f}% vs actual %{y:.1f}%<extra></extra>'},
  ],{...LB,
    xaxis:{...LB.xaxis,title:`FORECAST ${unitLabel}`},
    yaxis:{...LB.yaxis,title:`REALISED ${unitLabel}`},
    showlegend:false,
  });
}

// Symbol filter for the tab. Applies to the latest-forecast table AND the
// scatter chart (re-filtered from the cached _rvScatterRows, no refetch).
// Deliberately does NOT touch the scorecard: that table is one row per
// MODEL, aggregated across the whole universe -- filtering it by symbol
// would just blank it, not narrow it, since no row IS a symbol.
function filterRvBySymbol(query){
  filterTableBySymbol('rvLatestTable', query);
  const model = document.getElementById('rvModel')?.value || 'ols_aug';
  const rows = query ? _rvScatterRows.filter(r=>r.symbol.toUpperCase().includes(query.toUpperCase()))
                     : _rvScatterRows;
  _renderRvScatter(rows, model);
}

document.getElementById('btnRvFcst')?.addEventListener('click', loadRvForecast);
document.getElementById('rvHorizon')?.addEventListener('change', loadRvForecast);
document.getElementById('rvModel')?.addEventListener('change', ()=>{
  _loadRvLatest(); _loadRvScatter();
});

// ══════════════════════════════════════════════════════
// VRP (variance risk premium) dashboard. Phases 1-4 built and validated
// (parity, weekly forward, standardized rank, constant-maturity IV, regime
// warning + backtest, harvesting signals). EXPERIMENTAL -- reads vrp.duckdb,
// an offline EOD batch (vrp_build.py / vrp_signals.py in
// C:\Users\aniru\od\claude). See areas/vrp-dashboard.md and
// oc_dashboard/routes/vrp.py.
// ══════════════════════════════════════════════════════
const VRP_INDEXES = ['NIFTY','BANKNIFTY','FINNIFTY'];

function _vrpHorizon(){ return document.getElementById('vrpHorizon')?.value || '30d'; }
function _vrpAnchor(){ return document.getElementById('vrpAnchor')?.value || 'fwd'; }

async function loadVrp(){
  const btn=document.getElementById('btnVrpRefresh');
  if(btn){btn.textContent='LOADING…';btn.disabled=true;}
  document.getElementById('vrpKpis').innerHTML='';
  setLoad('vrpLatestTable');
  setLoad('vrpChart');
  setLoad('vrpRegimeChart');
  setLoad('vrpSignalsTable');
  await Promise.all([_loadVrpLatest(), _loadVrpChart(), _loadVrpRegime(), _loadVrpSignals()]);
  if(btn){btn.textContent='REFRESH';btn.disabled=false;}
}

// True/false/absent, rendered so all three read differently at a glance.
// True → a coloured YES badge. False → a plain, muted "no" (data IS present,
// the signal just isn't active). Absent (no row at all -- e.g. the harvesting
// signals haven't been built yet, or this date/symbol has no vrp_iv_cm
// coverage) → a dash with a tooltip, so "off" and "unknown" never look the same.
function _vrpBoolCell(v, trueClass, absentTitle){
  if(v === true) return `<span class="badge ${trueClass}">YES</span>`;
  if(v === false) return `<span style="color:var(--muted);opacity:.7">no</span>`;
  return `<span title="${absentTitle}" style="color:var(--muted);cursor:help">—</span>`;
}

async function _loadVrpLatest(){
  const horizon=_vrpHorizon(), anchor=_vrpAnchor();
  try{
    const res = await api(`/api/vrp/latest?horizon=${horizon}&anchor=${anchor}`);
    const rows = res.rows||[];
    const nWarn = rows.filter(r=>r.regime_warning).length;
    const nHarvest = rows.filter(r=>r.harvest_candidate).length;
    const avgPct = rows.filter(r=>r.pct_rank!=null);
    const meanPct = avgPct.length ? avgPct.reduce((s,r)=>s+r.pct_rank,0)/avgPct.length : null;
    document.getElementById('vrpBadge').textContent = `EXPERIMENTAL · ${horizon} · ${anchor} anchor`;

    // Freshness note: vrp_metrics/vrp_rank need options data (for IV) and can lag
    // vrp_regime, which only needs futures/index data. Surface the gap instead of
    // letting a stale-looking "AS OF" date look like a bug.
    const ivDates = rows.map(r=>r.d).filter(Boolean).sort();
    let regimeMaxDate = null;
    try{
      const rg = await api('/api/vrp/regime');
      Object.values(rg.series||{}).forEach(legs=>{
        (legs.same_index||[]).forEach(pt=>{ if(!regimeMaxDate || pt.d > regimeMaxDate) regimeMaxDate = pt.d; });
      });
    }catch(e){/* freshness note is best-effort */}
    const ivMax = ivDates.length ? ivDates[ivDates.length-1] : null;
    const note = document.getElementById('vrpFreshnessNote');
    if(note){
      if(ivMax && regimeMaxDate && ivMax !== regimeMaxDate){
        note.innerHTML = `IV/premium data through <b>${ivMax}</b>, regime data through <b>${regimeMaxDate}</b> — `
          +`the gap is upstream F&amp;O bhavcopy lag (options/futures ingestion running behind the cash index), `
          +`not a pipeline fault. Regime warnings are current; the premium table above is as fresh as the options chain.`;
      } else if(ivMax){
        note.textContent = `Data current through ${ivMax}.`;
      } else {
        note.textContent = '';
      }
    }
    const kpis=[
      {label:'REGIME WARNINGS', value:`${nWarn}/3`, sub:nWarn?'active now':'none active'},
      {label:'HARVEST CANDIDATES', value:`${nHarvest}/3`, sub:'pct≥80, z≥1, no warning, no inversion'},
      {label:'MEAN PCT RANK', value:meanPct!=null?fmt(meanPct,1):'—', sub:'across the 3 indexes'},
    ];
    document.getElementById('vrpKpis').innerHTML = kpis.map(k=>
      `<div class="kpi"><div class="kpi-label">${k.label}</div>`
      +`<div class="kpi-val acc">${k.value}</div>`
      +`<div class="kpi-sub">${k.sub}</div></div>`).join('')
      + `<div class="kpi" style="max-width:280px">`
      + `<div class="kpi-label">READ THIS FIRST</div>`
      + `<div style="font-size:9px;color:var(--muted);line-height:1.4;margin-top:4px">`
      + `Percentile/z-score are validated (calibration checked against uniform/normal `
      + `theory on the full history). The regime warning fires 2-3 days INTO a shock, `
      + `not before it -- a fast confirmation, not a prediction. Harvest/cheap/compressing `
      + `thresholds are starting points, not yet backtested for forward-premium quality.</div></div>`;

    if(!rows.length){
      document.getElementById('vrpLatestTable').innerHTML = '<div class="empty">No VRP data yet.</div>';
      return;
    }
    const cols=[
      {key:'symbol',label:'SYMBOL',fmt:v=>`<b>${v}</b>`},
      {key:'d',label:'AS OF'},
      {key:'dte_cal',label:'DTE',fmt:v=>v==null?'—':fmt(v,0)},
      {key:'pct_rank',label:'PCT RANK',fmt:v=>v==null?'—':fmt(v,1)},
      {key:'z_score',label:'Z',fmt:v=>v==null?'—':fmt(v,2)},
      {key:'log_ratio',label:'LOG RATIO',fmt:v=>v==null?'—':fmt(v,3)},
      {key:'spread_vol_pts',label:'SPREAD (VOL PTS)',fmt:v=>v==null?'—':fmt(v,2)},
      {key:'regime_z',label:'REGIME Z',fmt:(v,row)=>v==null?'—':
        `<span style="${row.regime_warning?'color:var(--red);font-weight:600':''}">${fmt(v,2)}${row.regime_warning?' ⚠':''}</span>`},
      {key:'harvest_candidate',label:'HARVEST',
        fmt:v=>_vrpBoolCell(v,'badge-g','no signal data yet for this symbol/horizon -- run vrp_signals.py --step signals')},
      {key:'premium_cheap',label:'CHEAP',
        fmt:v=>_vrpBoolCell(v,'badge-r','no signal data yet for this symbol/horizon -- run vrp_signals.py --step signals')},
      {key:'premium_compressing',label:'COMPRESSING',
        fmt:v=>_vrpBoolCell(v,'','no signal data yet for this symbol/horizon -- run vrp_signals.py --step signals')},
      {key:'term_structure_inversion',label:'INVERTED',
        fmt:v=>_vrpBoolCell(v,'','no constant-maturity IV coverage for this symbol/date (see vrp_iv_cm method=none)')},
    ];
    document.getElementById('vrpLatestTable').innerHTML = makeTbl(rows, cols);
  }catch(e){
    document.getElementById('vrpLatestTable').innerHTML = `<div class="empty err">⚠ ${e.message}</div>`;
    document.getElementById('vrpBadge').textContent='error';
  }
}

function _vrpLookback(){ return document.getElementById('vrpLookback')?.value || '180'; }

async function _loadVrpChart(){
  const symbol = document.getElementById('vrpChartSymbol')?.value || 'NIFTY';
  const horizon=_vrpHorizon(), anchor=_vrpAnchor(), limit=_vrpLookback();
  try{
    const res = await api(`/api/vrp/timeseries?symbol=${symbol}&horizon=${horizon}&anchor=${anchor}&limit=${limit}`);
    const rows = res.rows||[];
    const badge=document.getElementById('vrpChartBadge');
    if(badge) badge.textContent = `${symbol} · ${horizon} · vol-point spread`;
    if(!rows.length){ setEmpty('vrpChart', 'No history for this filter'); return; }
    plot('vrpChart',[
      {name:'spread (vol pts)', x:rows.map(r=>r.d), y:rows.map(r=>r.spread_vol_pts),
        type:'scatter', mode:'lines', line:{color:C.acc,width:1.4}},
      {name:'zero', x:[rows[0].d, rows[rows.length-1].d], y:[0,0], type:'scatter', mode:'lines',
        line:{color:C.muted,width:1,dash:'dot'}, hoverinfo:'skip'},
    ],{...LB,
      yaxis:{...LB.yaxis, title:'IV MINUS RV (VOL PTS)'},
      showlegend:false,
    });
  }catch(e){
    setEmpty('vrpChart', `⚠ ${e.message}`);
  }
}

async function _loadVrpRegime(){
  const symbol = document.getElementById('vrpChartSymbol')?.value || 'NIFTY';
  const limit=_vrpLookback();
  try{
    const res = await api(`/api/vrp/regime?limit=${limit}`);
    const series = (res.series && res.series[symbol] && res.series[symbol].same_index) || [];
    if(!series.length){ setEmpty('vrpRegimeChart', 'No regime history for this symbol'); return; }
    plot('vrpRegimeChart',[
      {name:'z_shock', x:series.map(r=>r.d), y:series.map(r=>r.z_shock),
        type:'scatter', mode:'lines', line:{color:C.acc,width:1.4}},
      {name:'warning threshold', x:[series[0].d, series[series.length-1].d], y:[2,2],
        type:'scatter', mode:'lines', line:{color:C.red,width:1,dash:'dot'}, hoverinfo:'skip'},
    ],{...LB,
      yaxis:{...LB.yaxis, title:'z_shock'},
      showlegend:false,
    });
  }catch(e){
    setEmpty('vrpRegimeChart', `⚠ ${e.message}`);
  }
}

async function _loadVrpSignals(){
  const horizon=_vrpHorizon();
  try{
    const res = await api(`/api/vrp/signals?horizon=${horizon}`);
    const rows = res.rows||[];
    document.getElementById('vrpSignalsAsof').textContent = rows.length ? `${rows[0].d} → …` : '—';
    if(!rows.length){
      document.getElementById('vrpSignalsTable').innerHTML =
        '<div class="empty">No signal history yet -- run vrp_signals.py --step signals.</div>';
      return;
    }
    const cols=[
      {key:'symbol',label:'SYMBOL'},
      {key:'d',label:'DATE'},
      {key:'signal',label:'SIGNAL'},
      {key:'state',label:'STATE'},
      {key:'value',label:'VALUE',fmt:v=>v==null?'—':fmt(v,2)},
    ];
    document.getElementById('vrpSignalsTable').innerHTML = makeTbl(rows, cols);
    _vrpFilterSignalsTable();
  }catch(e){
    document.getElementById('vrpSignalsTable').innerHTML = `<div class="empty err">⚠ ${e.message}</div>`;
  }
}

// Combines all three signal-history filters (symbol text, signal dropdown, state
// text) with AND logic, since filterTableBySymbol alone only handles one column
// and the three controls need to narrow together, not independently overwrite
// each other's row.style.display.
function _vrpFilterSignalsTable(){
  const container=document.getElementById('vrpSignalsTable');
  if(!container) return;
  const symQ=(document.getElementById('vrpSignalsSearch')?.value||'').trim().toUpperCase();
  const sigQ=document.getElementById('vrpSignalFilter')?.value||'';
  const stateMode=document.getElementById('vrpStateFilter')?.value||'';   // '', 'triggered', 'not_triggered'
  const heads=container.querySelectorAll('thead th');
  let symIdx=0, sigIdx=2, stateIdx=3;
  heads.forEach((h,i)=>{
    const t=(h.textContent||'').trim().toUpperCase();
    if(t==='SYMBOL') symIdx=i; else if(t==='SIGNAL') sigIdx=i; else if(t==='STATE') stateIdx=i;
  });
  container.querySelectorAll('tbody tr').forEach(row=>{
    const sym=row.cells[symIdx]?.textContent.trim().toUpperCase()||'';
    const sig=row.cells[sigIdx]?.textContent.trim()||'';
    const state=row.cells[stateIdx]?.textContent.trim().toUpperCase()||'';
    // relative_richness is a rank (1/2/3), never True/False -- it isn't a trigger, so the
    // triggered/not-triggered toggle never hides it, only the symbol/signal filters can.
    let stateOk=true;
    if(stateMode==='triggered') stateOk = (sig==='relative_richness') || state==='TRUE';
    else if(stateMode==='not_triggered') stateOk = (sig!=='relative_richness') && state==='FALSE';
    const ok=(!symQ||sym.includes(symQ)) && (!sigQ||sig===sigQ) && stateOk;
    row.style.display = ok ? '' : 'none';
  });
}

function _vrpGuideHtml(){
  const sec=(title,body)=>`<b style="color:var(--text)">${title}</b><br>${body}<br><br>`;
  return sec('WHAT THIS PAGE IS',
    `The variance risk premium (IV minus realised vol) for NIFTY, BANKNIFTY and FINNIFTY, `
    +`built offline nightly by <code>vrp_build.py</code> and <code>vrp_signals.py</code>. `
    +`Four phases, all validated against the full 2020-2026 history before shipping: `
    +`parity with live IV, a weekly-forward rule checked against traded futures, a `
    +`standardized percentile/z-score rank, and a same-index shock trigger backtested `
    +`against an ex-post label. The harvesting signals (bottom section) are the newest `
    +`and least proven part -- starting-point thresholds, not yet backtested for `
    +`forward-premium quality.`)

  + sec('MAIN TABLE COLUMNS',
    [['PCT RANK / Z','Where the current premium sits against its own trailing 252-day history (min 120 observations). Validated: on the full history this averages ~50 / ~0 with the expected spread, so it is trustworthy out of the box.'],
     ['LOG RATIO','ln(implied total variance / realised total variance) -- the dimensionless premium. Positive means IV is running above realised.'],
     ['SPREAD (VOL PTS)','The same premium in annualised vol points, on the trading-day (243) display clock.'],
     ['REGIME Z','The same-index shock trigger: (5-day realised variance − 20-day HV) / (std of the 20-day HV series over its own trailing year). Turns the warning on at z ≥ 2.0. Backtested at precision 0.72-0.84 / recall 0.83-0.93 (z=2.0, full history) -- but it fires 2-3 trading days INTO a shock, not before it. Read it as a fast confirmation, not a prediction.'],
     ['HARVEST / CHEAP / COMPRESSING / INVERTED','Phase 4 starting-point signals. A plain grey "no" means the signal was computed and is off; a dash with a tooltip means no data exists yet for that row (different problem, different fix -- hover it).'],
    ].map(([k,v])=>`<b style="color:var(--acc)">${k}:</b> ${v}`).join('<br>'))

  + sec('WHY TWO DATES CAN DIFFER',
    `The premium table needs the options chain (for IV); the regime chart only needs `
    +`futures/index prices. When the F&amp;O bhavcopy ingestion lags the cash index feed `
    +`by a few days, the premium table's "AS OF" date lags the regime chart's. The note `
    +`under the filter bar says so explicitly when it happens -- it is a data-freshness `
    +`gap upstream of this dashboard, not a pipeline fault.`)

  + sec('ANCHOR: FORWARD VS SPOT',
    `Two ways to read "ATM". FORWARD reads IV at the strike nearest the forward price, `
    +`stripping skew and carry out of the number -- the recommended default. SPOT reads `
    +`it at the strike nearest spot, matching the live dashboard and screeners elsewhere. `
    +`They differ by 0.1-0.4 vol point, growing with days to expiry.`)

  + sec('READING THE SIGNALS TOGETHER',
    `The four boolean signals are not independent votes -- INVERTED is itself one of the `
    +`conditions inside HARVEST: if a day shows inverted = Yes, harvest_candidate will show `
    +`False for that reason alone, whatever the percentile says. So seeing both together `
    +`is not a coincidence, it is the gate working as designed.<br><br>`
    +`INVERTED means 7-day IV is trading above 30-day IV -- usually read as the market `
    +`pricing more near-term risk than long-term. But the base rate differs sharply by `
    +`index: BANKNIFTY inverts on about 49% of days (close to a coin flip -- its term `
    +`structure genuinely runs close to flat), against about 30% for NIFTY. Treat `
    +`INVERTED = Yes on BANKNIFTY as closer to routine than alarming; the same reading `
    +`on NIFTY is a less common event and worth more attention.<br><br>`
    +`COMPRESSING = Yes means the percentile rank has dropped 20+ points in 5 trading `
    +`days -- whatever richness existed is fading fast, which argues against a fresh `
    +`harvest entry even on a day the percentile alone still looks high. None of these `
    +`thresholds have been backtested yet for actual forward-premium quality (see "What `
    +`this page is" above) -- read the direction, not the precision.`)

  + sec('SIGNAL HISTORY TABLE: STATE HAS NO MEMORY',
    `Each row is recomputed fresh from that single day's data -- state = False never `
    +`means "changed to false" or "hasn't triggered yet," only that the condition was `
    +`not met that day. HARVEST_CANDIDATE and PREMIUM_CHEAP both display that day's `
    +`percentile rank as their VALUE column, so seeing the same number on both with `
    +`both False is normal: a mid-range percentile (say 47) is neither rich enough for `
    +`harvest (needs 80+) nor low enough for cheap (needs 20 or under). `
    +`PREMIUM_COMPRESSING's value is instead the 5-day percentile CHANGE, not the level. `
    +`The TRIGGERED ONLY filter (default) hides the False rows for the four boolean `
    +`signals but always keeps RELATIVE_RICHNESS visible, since a rank of 1st/2nd/3rd `
    +`is not a trigger to filter on -- switch to ALL STATES to see every day, including `
    +`the ones nothing fired.`);
}

document.getElementById('btnVrpGuide')?.addEventListener('click',()=>{
  const g=document.getElementById('vrpGuide');
  if(!g) return;
  if(!g.dataset.built){ g.innerHTML=_vrpGuideHtml(); g.dataset.built='1'; }
  g.style.display = g.style.display==='none' ? '' : 'none';
});

document.getElementById('btnVrpRefresh')?.addEventListener('click', loadVrp);
document.getElementById('vrpHorizon')?.addEventListener('change', loadVrp);
document.getElementById('vrpAnchor')?.addEventListener('change', loadVrp);
document.getElementById('vrpChartSymbol')?.addEventListener('change', ()=>{
  _loadVrpChart(); _loadVrpRegime();
});
document.getElementById('vrpLookback')?.addEventListener('change', ()=>{
  _loadVrpChart(); _loadVrpRegime();
});

// ══ WHEEL (BACKTEST + PAPER) ═══════════════════════════════════════
// Reads oc_dashboard/routes/wheel.py -> wheel_bt.duckdb. EXPERIMENTAL:
// backtest runs are in-sample unless labelled "split ... OOS"; paper books
// are a mechanics / slippage check over weeks, not proof of edge.
let _wheelRunData = null, _wheelTradesAll = [], _wheelBooks = [];
const _WHEEL_EXIT_COL = {final_exit:C.green, time_exit:C.acc, results:C.pur, corp_action:C.muted,
  stop_mult:C.red, stop_spot:C.amber, stop_delta:C.acc2, profit:'#34d399', window_end:'#64748b', expiry:'#94a3b8', open:C.acc};
const _wPct = (v,d=1)=> v==null ? '—' : `${fmt(v*100,d)}%`;
const _wRs  = v => v==null ? '—' : `₹${fmtL(Math.round(v))}`;

function _wheelTabsInit(){
  document.querySelectorAll('#wheelTabs .pagetab').forEach(tab=>{
    if(tab.dataset.bound) return; tab.dataset.bound='1';
    tab.addEventListener('click',()=>{
      document.querySelectorAll('#wheelTabs .pagetab').forEach(t=>t.classList.remove('active'));
      document.querySelectorAll('#view-wheel > .pagetab-panel').forEach(p=>p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById(tab.dataset.wtab).classList.add('active');
      if(tab.dataset.wtab==='wt-paper' && !tab.dataset.init){ tab.dataset.init='1'; _wheelLoadBooks(); }
      window.dispatchEvent(new Event('resize'));   // Plotly sizes hidden charts at 0 width
    });
  });
  const b=document.getElementById('btnWheelRefresh'); if(b && !b.dataset.bound){ b.dataset.bound='1'; b.onclick=()=>_wheelLoadRuns(); }
  const r=document.getElementById('wheelRun'); if(r && !r.dataset.bound){ r.dataset.bound='1'; r.onchange=()=>_wheelLoadRun(); }
  const c=document.getElementById('wheelCompare'); if(c && !c.dataset.bound){ c.dataset.bound='1'; c.onchange=()=>_wheelLoadRun(); }
  const pb=document.getElementById('btnWheelPaperRefresh'); if(pb && !pb.dataset.bound){ pb.dataset.bound='1'; pb.onclick=()=>_wheelLoadBooks(); }
  const bk=document.getElementById('wheelBook'); if(bk && !bk.dataset.bound){ bk.dataset.bound='1'; bk.onchange=()=>_wheelLoadBook(); }
}

async function loadWheel(){
  _wheelTabsInit();
  await _wheelLoadRuns();
}

async function _wheelLoadRuns(){
  const sel=document.getElementById('wheelRun'), cmp=document.getElementById('wheelCompare');
  try{
    const res=await api('/api/wheel/runs');
    const rows=res.rows||[];
    if(!rows.length){
      ['wheelEqChart','wheelMonthChart','wheelYearChart','wheelExitChart'].forEach(id=>setEmpty(id,'No backtest runs yet — run oc/wheel_bt.py'));
      document.getElementById('wheelStockTable').innerHTML='<div class="empty">No runs.</div>';
      document.getElementById('wheelTradesTable').innerHTML='<div class="empty">No runs.</div>';
      return;
    }
    const prev=sel.value, prevC=cmp.value;
    const opt=r=>`<option value="${r.run_id}">${r.created} · ${r.label||'run'} · ${r.trades??'?'} tr · ${_wRs(r.pnl)}</option>`;
    sel.innerHTML=rows.map(opt).join('');
    cmp.innerHTML='<option value="">— none —</option>'+rows.map(opt).join('');
    if(rows.some(r=>r.run_id===prev)) sel.value=prev;
    if(rows.some(r=>r.run_id===prevC)) cmp.value=prevC;
    await _wheelLoadRun();
  }catch(e){
    setEmpty('wheelEqChart',`⚠ ${e.message}`);
  }
}

async function _wheelLoadRun(){
  const id=document.getElementById('wheelRun').value, cid=document.getElementById('wheelCompare').value;
  if(!id) return;
  ['wheelEqChart','wheelMonthChart','wheelYearChart','wheelExitChart','wheelStockTable','wheelTradesTable'].forEach(x=>setLoad(x));
  document.getElementById('wheelTradeCard').style.display='none';
  try{
    const [d, cmp, tr] = await Promise.all([
      api(`/api/wheel/run/${encodeURIComponent(id)}`),
      cid && cid!==id ? api(`/api/wheel/run/${encodeURIComponent(cid)}`) : Promise.resolve(null),
      api(`/api/wheel/trades?run_id=${encodeURIComponent(id)}`),
    ]);
    _wheelRunData=d; _wheelTradesAll=tr.rows||[];
    const oos = /OOS/.test(d.label||'');
    document.getElementById('wheelBadge').textContent = oos ? 'OUT-OF-SAMPLE RUN' : 'EXPERIMENTAL · IN-SAMPLE';
    const pc=d.params_changed||{};
    document.getElementById('wheelParams').innerHTML =
      `<b>${d.label||'run'}</b> · ${d.start} → ${d.end} · capital ${_wRs(d.capital)} (cash ${_wRs(d.cash)}) · `
      + (Object.keys(pc).filter(k=>!['start','end'].includes(k)).map(k=>`${k}=<b>${Array.isArray(pc[k])?pc[k].join('/'):pc[k]}</b>`).join(' · ') || 'all defaults');
    _wheelKpis(d);
    _wheelEquity(d, cmp);
    _wheelMonthly(d);
    _wheelYears(d);
    _wheelExits(d);
    _wheelStocks(d);
    const rs=document.getElementById('wheelTradeReason');
    const reasons=[...new Set(_wheelTradesAll.map(t=>t.reason))].sort();
    rs.innerHTML='<option value="">ALL EXITS</option>'+reasons.map(r=>`<option value="${r}">${r}</option>`).join('');
    _wheelRenderTrades();
  }catch(e){
    ['wheelEqChart','wheelMonthChart','wheelYearChart','wheelExitChart'].forEach(x=>setEmpty(x,`⚠ ${e.message}`));
    document.getElementById('wheelStockTable').innerHTML=`<div class="empty err">⚠ ${e.message}</div>`;
    document.getElementById('wheelTradesTable').innerHTML='';
  }
}

function _wheelKpis(d){
  const m=d.metrics||{};
  const k=(label,value,sub,cls='acc',tip='')=>`<div class="kpi"${tip?` title="${tip.replace(/"/g,'&quot;')}"`:''}><div class="kpi-label">${label}</div><div class="kpi-val ${cls}">${value}</div><div class="kpi-sub">${sub||''}</div></div>`;
  const pnlCls = (m.pnl||0)>=0?'up':'down';
  document.getElementById('wheelKpis').innerHTML = [
    k('NET P&L', _wRs(m.pnl), `${m.trades??0} trades · win ${_wPct(m.win_rate,0)}`, pnlCls),
    k('CAGR ON CASH', _wPct(m.cagr_on_cash), `on total capital ${_wPct(m.cagr_total)}`, pnlCls,
      'Annualised return on the cash leg; the pledged collateral keeps earning its own return on top.'),
    k('MAX DRAWDOWN', _wRs(m.max_dd_rs), `${_wPct(m.max_dd_pct_total)} of total · ${_wPct(m.max_dd_pct_cash)} of cash`, 'down'),
    k('WORST MONTH', _wRs(m.worst_month_rs), `${_wPct(m.pct_months_negative,0)} of months negative`, 'down'),
    k('CALMAR', m.calmar==null?'—':fmt(m.calmar,2), 'annual P&L ÷ max drawdown', 'acc',
      'Risk-adjusted return: higher is better. Compare runs on this before raw P&L.'),
    k('MARGIN USE', _wPct(m.avg_util,0), `avg of capital · peak ${_wPct(m.max_util_of_capacity,0)} of capacity`, 'acc'),
    k('BREACHES', `${m.breach_days??0} / ${m.cash_rule_breach_days??0}`, 'margin / 50:50 cash-rule days',
      (m.breach_days||m.cash_rule_breach_days)?'down':'up'),
  ].join('');
}

function _wheelEquity(d, cmp){
  const cv=d.curve||[];
  if(!cv.length){ setEmpty('wheelEqChart','No equity rows'); return; }
  const x=cv.map(r=>r.date);
  const traces=[
    {x, y:cv.map(r=>r.pnl), name:'cum P&L', type:'scatter', mode:'lines', line:{color:C.acc,width:1.6}},
    {x, y:cv.map(r=>r.dd), name:'drawdown', type:'scatter', mode:'lines', fill:'tozeroy',
     line:{color:C.red,width:1}, fillcolor:'rgba(244,63,94,.25)', yaxis:'y2'},
  ];
  if(cmp && (cmp.curve||[]).length){
    traces.push({x:cmp.curve.map(r=>r.date), y:cmp.curve.map(r=>r.pnl), name:'compare: '+(cmp.label||'run'),
                 type:'scatter', mode:'lines', line:{color:C.amber,width:1.2,dash:'dot'}});
  }
  document.getElementById('wheelEqBadge').textContent = cmp ? 'vs '+(cmp.label||'compare') : '₹';
  plot('wheelEqChart', traces, {...LB, showlegend:true, legend:{...LB.legend, orientation:'h', y:1.08},
    yaxis:{...LB.yaxis, domain:[0.34,1], title:{text:'cum P&L',font:{size:9}}},
    yaxis2:{...LB.yaxis, domain:[0,0.26], title:{text:'DD',font:{size:9}}},
    xaxis:{...LB.xaxis, anchor:'y2'}, margin:{...LB.margin, t:28}});
}

function _wheelMonthly(d){
  const ms=d.monthly||[];
  if(!ms.length){ setEmpty('wheelMonthChart','No monthly data'); return; }
  const years=[...new Set(ms.map(r=>r.year))].sort();
  const mon=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const z=years.map(y=>mon.map((_,i)=>{ const r=ms.find(o=>o.year===y&&o.month===i+1); return r?r.pnl:null; }));
  const txt=z.map(row=>row.map(v=>v==null?'':fmtL(Math.round(v))));
  const lim=Math.max(...ms.map(r=>Math.abs(r.pnl)),1);
  plot('wheelMonthChart',[{type:'heatmap', x:mon, y:years.map(String), z, text:txt, texttemplate:'%{text}',
    textfont:{size:8}, zmid:0, zmin:-lim, zmax:lim, colorscale:[[0,'#f43f5e'],[0.5,'#162236'],[1,'#10b981']],
    hovertemplate:'%{y} %{x}: ₹%{z:,.0f}<extra></extra>', showscale:false}],
    {...LB, yaxis:{...LB.yaxis, autorange:'reversed', type:'category'}, margin:{...LB.margin, l:44}});
}

function _wheelYears(d){
  const ys=d.by_year_side||[];
  if(!ys.length){ setEmpty('wheelYearChart','No trades'); return; }
  const x=ys.map(r=>String(r.year));
  plot('wheelYearChart',[
    {x, y:ys.map(r=>r.PE||0), name:'puts', type:'bar', marker:{color:C.green}},
    {x, y:ys.map(r=>r.CE||0), name:'calls', type:'bar', marker:{color:C.acc2}},
    {x, y:ys.map(r=>r.total||0), name:'total', type:'scatter', mode:'lines+markers', line:{color:C.acc,width:1.5}},
  ], {...LB, barmode:'group', showlegend:true, legend:{...LB.legend, orientation:'h', y:1.08}, margin:{...LB.margin, t:28}});
}

function _wheelExits(d){
  const mix=(d.metrics||{}).exit_mix||{};
  const ks=Object.keys(mix).sort((a,b)=>mix[b]-mix[a]);
  if(!ks.length){ setEmpty('wheelExitChart','No trades'); return; }
  const pnl=k=>_wheelTradesAll.filter(t=>t.reason===k).reduce((s,t)=>s+(t.pnl||0),0);
  plot('wheelExitChart',[{type:'bar', orientation:'h', y:ks, x:ks.map(k=>mix[k]*100),
    marker:{color:ks.map(k=>_WHEEL_EXIT_COL[k]||C.muted)},
    text:ks.map(k=>`${fmt(mix[k]*100,1)}% · ${_wRs(pnl(k))}`), textposition:'auto', textfont:{size:9},
    hovertemplate:'%{y}: %{x:.1f}% of trades<extra></extra>'}],
    {...LB, xaxis:{...LB.xaxis, title:{text:'% of trades',font:{size:9}}},
     yaxis:{...LB.yaxis, autorange:'reversed'}, margin:{...LB.margin, l:90}});
}

function _wheelStocks(d){
  const p=d.persistence||{};
  const el=document.getElementById('wheelPersist');
  if(p.pnl_corr!=null){
    const weak = Math.abs(p.rank_corr) < 0.3 && p.sign_agreement < 0.65;
    el.innerHTML = `<span style="color:var(--muted)">PERSISTENCE (before vs after ${p.split}, ${p.stocks} stocks):</span> `
      + `P&amp;L corr <b>${fmt(p.pnl_corr,2)}</b> · rank corr <b>${fmt(p.rank_corr,2)}</b> · same sign <b>${_wPct(p.sign_agreement,0)}</b> · `
      + `top-quartile-before then made <b>${_wRs(p.top_quartile_before_avg_after)}</b> vs rest <b>${_wRs(p.rest_avg_after)}</b> per stock — `
      + (weak ? `<span style="color:var(--amber)">past stock P&amp;L does <b>not</b> predict future here; don't pick stocks off this table.</span>`
              : `<span style="color:var(--green)">some persistence; still confirm out-of-sample.</span>`);
  } else el.innerHTML='';
  const rows=d.stocks||[];
  if(!rows.length){ document.getElementById('wheelStockTable').innerHTML='<div class="empty">No trades.</div>'; return; }
  const yrs=Object.keys(rows[0]).filter(k=>/^y\d{4}$/.test(k)).sort();
  const cols=[
    {key:'symbol',label:'SYMBOL',fmt:v=>`<a href="#" onclick="document.getElementById('wheelTradeSym').value='${v}';_wheelRenderTrades();document.getElementById('wheelTradesTable').scrollIntoView({behavior:'smooth'});return false">${v}</a>`},
    {key:'sector',label:'SECTOR'},
    {key:'trades',label:'TRADES',fmt:v=>fmt(v,0)},
    {key:'pnl_per_lot',label:'P&L / LOT',fmt:v=>sspan(v,0)},
    {key:'win',label:'WIN',fmt:v=>_wPct(v,0)},
    {key:'rom_pct',label:'ROM %',fmt:v=>sspan(v,2)},
    {key:'stop_rate',label:'STOPPED',fmt:v=>_wPct(v,0)},
    {key:'puts',label:'PUTS',fmt:v=>v==null?'—':sspan(v,0)},
    {key:'calls',label:'CALLS',fmt:v=>v==null?'—':sspan(v,0)},
    {key:'yrs_positive',label:'YRS +',help:'profitable years / years traded'},
    {key:'worst',label:'WORST',fmt:v=>sspan(v,0)},
    ...yrs.map(y=>({key:y,label:y.slice(1),fmt:v=>v==null?'—':sspan(v,0)})),
  ];
  document.getElementById('wheelStockTable').innerHTML=makeTbl(rows,cols);
  const s=document.getElementById('wheelStockSearch'); if(s&&s.value) filterTableBySymbol('wheelStockTable', s.value);
}

function _wheelRenderTrades(){
  const sym=(document.getElementById('wheelTradeSym').value||'').trim().toUpperCase();
  const side=document.getElementById('wheelTradeSide').value;
  const reason=document.getElementById('wheelTradeReason').value;
  const oc=document.getElementById('wheelTradeOutcome').value;
  let rows=_wheelTradesAll.filter(t=>(!sym||t.symbol.includes(sym))&&(!side||t.side===side)&&(!reason||t.reason===reason)
    &&(!oc||(oc==='win'?t.pnl>0:t.pnl<=0)));
  const tot=rows.reduce((s,t)=>s+(t.pnl||0),0);
  document.getElementById('wheelTradesBadge').textContent=`${rows.length} trades · ${_wRs(tot)}`;
  if(!rows.length){ document.getElementById('wheelTradesTable').innerHTML='<div class="empty">No trades match.</div>'; return; }
  rows=[...rows].reverse();                     // newest first
  const cols=[
    {key:'pid',label:'#',fmt:v=>`<a href="#" title="chart this trade" onclick="_wheelTrade(${v});return false">#${v}</a>`},
    {key:'symbol',label:'SYMBOL'},
    {key:'side',label:'SIDE',fmt:v=>`<span style="color:${v==='PE'?C.green:C.acc2}">${v}</span>`},
    {key:'strike',label:'STRIKE',fmt:v=>fmt(v,2)},
    {key:'expiry',label:'EXPIRY',fmt:v=>String(v).slice(0,10)},
    {key:'lots',label:'LOTS',fmt:v=>v==null?'1':fmt(v,0)},
    {key:'entry_date',label:'ENTRY',fmt:v=>String(v).slice(0,10)},
    {key:'exit_date',label:'EXIT',fmt:v=>String(v).slice(0,10)},
    {key:'days',label:'DAYS',fmt:v=>fmt(v,0)},
    {key:'entry_px',label:'ENTRY PX',fmt:v=>fmt(v,2),help:'NSE settle at entry (before slippage)'},
    {key:'exit_px',label:'EXIT PX',fmt:v=>fmt(v,2)},
    {key:'pnl',label:'P&L ₹',fmt:v=>sspan(v,0)},
    {key:'rom',label:'ROM',fmt:v=>v==null?'—':sspan(v*100,2)+'%'},
    {key:'reason',label:'EXIT',fmt:v=>`<span style="color:${_WHEEL_EXIT_COL[v]||'inherit'}">${v}</span>`},
  ];
  document.getElementById('wheelTradesTable').innerHTML=makeTbl(rows.slice(0,3000),cols)
    + (rows.length>3000?`<div class="empty">showing 3,000 of ${rows.length}; narrow the filters or use CSV</div>`:'');
}

async function _wheelTrade(pid){
  const id=document.getElementById('wheelRun').value;
  await _wheelDrill({url:`/api/wheel/trade_path?run_id=${encodeURIComponent(id)}&pid=${pid}`,
    card:'wheelTradeCard', chart:'wheelTradeChart', info:'wheelTradeInfo', badge:'wheelTradeBadge',
    stopMult:(_wheelRunData?.params||{}).stop_mult});
}

async function _wheelDrill(o){
  const card=document.getElementById(o.card); card.style.display='block';
  setLoad(o.chart); card.scrollIntoView({behavior:'smooth',block:'nearest'});
  try{
    const res=await api(o.url);
    const t=res.trade, pth=res.path||[];
    const ed=String(t.entry_date).slice(0,10), xd=t.exit_date?String(t.exit_date).slice(0,10):null;
    document.getElementById(o.badge).textContent=`${t.symbol} ${t.side} ${fmt(t.strike,2)} · exp ${String(t.expiry).slice(0,10)}`;
    document.getElementById(o.info).innerHTML=
      `entry <b>${ed}</b> @ ${fmt(t.entry_px,2)} (credit ${fmt(t.credit,2)}) → `
      + (xd ? `exit <b>${xd}</b> @ ${fmt(t.exit_px ?? t.exit_fill,2)} · <b>${t.reason}</b> · P&amp;L ${sspan(t.pnl,0)}` : '<b>still open</b>')
      + ` · lots ${t.lots??1}`;
    if(!pth.length){ setEmpty(o.chart,'No SPAN rows for this contract in bhavcopy.duckdb'); return; }
    const x=pth.map(r=>r.date);
    const traces=[
      {x, y:pth.map(r=>r.opt), name:'option (settle)', type:'scatter', mode:'lines+markers', marker:{size:4},
       line:{color:C.acc,width:1.6}},
      {x, y:pth.map(r=>r.und), name:'underlying', type:'scatter', mode:'lines', yaxis:'y2', line:{color:'#94a3b8',width:1.2}},
      {x:[x[0],x[x.length-1]], y:[t.strike,t.strike], name:'strike', type:'scatter', mode:'lines', yaxis:'y2',
       line:{color:C.amber,width:1,dash:'dash'}},
      {x:[ed], y:[t.entry_px], name:'entry', type:'scatter', mode:'markers', marker:{color:C.green,size:11,symbol:'triangle-down'}},
    ];
    if(xd) traces.push({x:[xd], y:[t.exit_px ?? t.exit_fill], name:'exit · '+t.reason, type:'scatter', mode:'markers',
                         marker:{color:_WHEEL_EXIT_COL[t.reason]||C.red,size:11,symbol:'x'}});
    if(o.stopMult) traces.push({x:[x[0],x[x.length-1]], y:[t.entry_px*o.stopMult,t.entry_px*o.stopMult],
                     name:`${o.stopMult}× stop`, type:'scatter', mode:'lines', line:{color:C.red,width:1,dash:'dot'}});
    plot(o.chart, traces, {...LB, showlegend:true, legend:{...LB.legend, orientation:'h', y:1.1},
      yaxis:{...LB.yaxis, title:{text:'option ₹',font:{size:9}}},
      yaxis2:{...LB.yaxis, overlaying:'y', side:'right', title:{text:'underlying ₹',font:{size:9}}, showgrid:false},
      margin:{...LB.margin, t:30, r:56}});
  }catch(e){ setEmpty(o.chart,`⚠ ${e.message}`); }
}

// ── PAPER tab ──────────────────────────────────────────────────────
async function _wheelLoadBooks(){
  const sel=document.getElementById('wheelBook');
  try{
    const res=await api('/api/wheel/paper/books');
    _wheelBooks=res.rows||[];
    if(!_wheelBooks.length){
      document.getElementById('wheelPaperNote').innerHTML='No paper books yet — create one with <b>python oc/wheel_paper.py init --book main …</b>';
      ['wheelPaperChart'].forEach(id=>setEmpty(id,'No paper books'));
      ['wheelPosTable','wheelPaperTrades','wheelFills'].forEach(id=>document.getElementById(id).innerHTML='<div class="empty">No paper books.</div>');
      return;
    }
    const prev=sel.value;
    sel.innerHTML=_wheelBooks.map(b=>`<option value="${b.book}">${b.book}</option>`).join('');
    if(_wheelBooks.some(b=>b.book===prev)) sel.value=prev;
    await _wheelLoadBook();
  }catch(e){
    document.getElementById('wheelPaperNote').innerHTML=`<span style="color:var(--red)">⚠ ${e.message}</span>`;
  }
}

async function _wheelLoadBook(){
  const book=document.getElementById('wheelBook').value; if(!book) return;
  const meta=_wheelBooks.find(b=>b.book===book)||{};
  ['wheelPaperChart'].forEach(x=>setLoad(x));
  document.getElementById('wheelPaperTradeCard').style.display='none';
  try{
    const d=await api(`/api/wheel/paper/book/${encodeURIComponent(book)}`);
    const pc=meta.params_changed||{};
    const today=new Date().toISOString().slice(0,10);
    const stale = meta.last_run && meta.last_run < today;
    document.getElementById('wheelPaperBadge').textContent='PAPER · NOT LIVE ORDERS';
    document.getElementById('wheelPaperNote').innerHTML =
      `book <b>${book}</b> since ${meta.created} · last run <b>${meta.last_run||'never'}</b>`
      + (stale?` <span style="color:var(--amber)">(runs after the 17:45 job on trading days)</span>`:'')
      + ` · ${Object.keys(pc).filter(k=>!['start','end'].includes(k)).map(k=>`${k}=<b>${Array.isArray(pc[k])?pc[k].join('/'):pc[k]}</b>`).join(' · ')||'all defaults'}`;
    const daily=d.daily||[], cap=meta.capital||0;
    const last=daily.length?daily[daily.length-1]:null;
    const realised=(d.trades||[]).reduce((s,t)=>s+(t.pnl||0),0);
    const unreal=(d.positions||[]).reduce((s,p)=>s+(p.unreal||0),0);
    const slip=a=>(d.slippage||[]).find(r=>r.action===a)||{};
    const sS=slip('S'), sB=slip('B');
    const slipTxt=r=>r.avg_slip==null?'—':`${fmt(r.avg_slip*100,1)}%`;
    const slipCls=r=>r.avg_slip==null?'acc':(r.avg_slip<=0.10?'up':'down');
    const k=(label,value,sub,cls='acc',tip='')=>`<div class="kpi"${tip?` title="${tip.replace(/"/g,'&quot;')}"`:''}><div class="kpi-label">${label}</div><div class="kpi-val ${cls}">${value}</div><div class="kpi-sub">${sub||''}</div></div>`;
    document.getElementById('wheelPaperKpis').innerHTML=[
      k('EQUITY P&L', last?_wRs(last.equity-cap):'—', `${daily.length} session(s)`, last&&last.equity>=cap?'up':'down'),
      k('REALISED', _wRs(realised), `${(d.trades||[]).length} closed`, realised>=0?'up':'down'),
      k('UNREALISED', _wRs(unreal), `${(d.positions||[]).length} open`, unreal>=0?'up':'down'),
      k('MARGIN USED', last?_wRs(last.used_margin):'—', last?`of ${_wRs(last.capacity)} capacity`:''),
      k('SELL SLIP', slipTxt(sS), `${sS.settled||0}/${sS.n||0} fills settled · spread ${sS.avg_spread==null?'—':fmt(sS.avg_spread*100,1)+'%'}`, slipCls(sS),
        'Average shortfall of the bid fill vs NSE settle, as % of settle. Backtest assumes 10%.'),
      k('BUY SLIP', slipTxt(sB), `${sB.settled||0}/${sB.n||0} fills settled`, slipCls(sB),
        'Average excess of the ask fill over NSE settle. Backtest assumes 10%.'),
    ].join('');
    if(daily.length){
      plot('wheelPaperChart',[{x:daily.map(r=>r.date), y:daily.map(r=>r.equity-cap), type:'scatter', mode:'lines+markers',
        marker:{size:5}, line:{color:C.acc,width:1.6}, name:'P&L'},
        {x:daily.map(r=>r.date), y:daily.map(r=>r.n_open), type:'bar', yaxis:'y2', name:'open', marker:{color:'rgba(122,156,191,.25)'}}],
        {...LB, showlegend:false, yaxis2:{...LB.yaxis, overlaying:'y', side:'right', showgrid:false, title:{text:'open',font:{size:9}}},
         margin:{...LB.margin, r:44}});
    } else setEmpty('wheelPaperChart','No sessions run yet');
    const pos=d.positions||[];
    document.getElementById('wheelPosBadge').textContent=`${pos.length} open`;
    document.getElementById('wheelPosTable').innerHTML = pos.length ? makeTbl(pos,[
      {key:'pid',label:'#',fmt:v=>`<a href="#" onclick="_wheelPaperTrade(${v});return false">#${v}</a>`},
      {key:'symbol',label:'SYMBOL'},{key:'side',label:'SIDE'},{key:'strike',label:'STRIKE',fmt:v=>fmt(v,2)},
      {key:'expiry',label:'EXPIRY'},{key:'lots',label:'LOTS',fmt:v=>fmt(v,0)},{key:'entry_date',label:'ENTRY'},
      {key:'credit',label:'CREDIT',fmt:v=>fmt(v,2)},{key:'mark',label:'MARK',fmt:v=>fmt(v,2)},
      {key:'unreal',label:'UNREAL ₹',fmt:v=>sspan(v,0)},{key:'delta',label:'Δ',fmt:v=>fmt(v,3)},
      {key:'margin',label:'MARGIN',fmt:v=>fmtL(v)}]) : '<div class="empty">No open positions.</div>';
    const trs=d.trades||[];
    document.getElementById('wheelPaperTrades').innerHTML = trs.length ? makeTbl(trs,[
      {key:'pid',label:'#',fmt:v=>`<a href="#" onclick="_wheelPaperTrade(${v});return false">#${v}</a>`},
      {key:'symbol',label:'SYMBOL'},{key:'side',label:'SIDE'},{key:'strike',label:'STRIKE',fmt:v=>fmt(v,2)},
      {key:'lots',label:'LOTS',fmt:v=>fmt(v,0)},{key:'entry_date',label:'ENTRY'},{key:'exit_date',label:'EXIT'},
      {key:'credit',label:'CREDIT',fmt:v=>fmt(v,2)},{key:'exit_fill',label:'EXIT FILL',fmt:v=>fmt(v,2)},
      {key:'pnl',label:'P&L ₹',fmt:v=>sspan(v,0)},
      {key:'reason',label:'EXIT',fmt:v=>`<span style="color:${_WHEEL_EXIT_COL[v]||'inherit'}">${v}</span>`}]) : '<div class="empty">No closed trades yet.</div>';
    const fl=d.fills||[];
    document.getElementById('wheelFills').innerHTML = fl.length ? makeTbl(fl,[
      {key:'date',label:'DATE'},{key:'action',label:'ACT',fmt:v=>v==='S'?'SELL':'BUY'},{key:'symbol',label:'SYMBOL'},
      {key:'side',label:'SIDE'},{key:'strike',label:'STRIKE',fmt:v=>fmt(v,2)},{key:'lots',label:'LOTS',fmt:v=>fmt(v,0)},
      {key:'bid',label:'BID',fmt:v=>fmt(v,2)},{key:'ask',label:'ASK',fmt:v=>fmt(v,2)},{key:'ltp',label:'LTP',fmt:v=>fmt(v,2)},
      {key:'fill',label:'FILL',fmt:v=>fmt(v,2)},
      {key:'settle',label:'NSE SETTLE',fmt:v=>v==null?'<span style="color:var(--muted)" title="filled in after that day\'s EOD bhavcopy">pending</span>':fmt(v,2)},
      {key:'slip_vs_settle',label:'SLIP',fmt:v=>v==null?'—':`<span style="color:${v<=0.10?C.green:C.red}">${fmt(v*100,1)}%</span>`},
      {key:'reason',label:'WHY'}]) : '<div class="empty">No fills yet.</div>';
  }catch(e){
    setEmpty('wheelPaperChart',`⚠ ${e.message}`);
  }
}

async function _wheelPaperTrade(pid){
  const book=document.getElementById('wheelBook').value;
  const meta=_wheelBooks.find(b=>b.book===book)||{};
  await _wheelDrill({url:`/api/wheel/trade_path?book=${encodeURIComponent(book)}&pid=${pid}`,
    card:'wheelPaperTradeCard', chart:'wheelPaperTradeChart', info:'wheelPaperTradeInfo', badge:'wheelPaperTradeBadge',
    stopMult:(meta.params||{}).stop_mult});
}

// ══════════════════════════════════════════════════════════════════
// PREMIUM: strike richness flags · richness map · participant positioning
// Backend: routes/premium.py. Flags describe the HISTORY of a moneyness x DTE
// cell (hold to expiry, futures delta-hedged, net of costs, per SPAN margin),
// not a forecast. Positioning is aggregate (all index options, all expiries).
// ══════════════════════════════════════════════════════════════════
const PM_FLAG_COL = {RICH:'var(--green)', GOOD:'#5ec8a0', FAIR:'var(--muted)',
                     WEAK:'var(--amber)', AVOID:'var(--red)', RESULTS:'var(--amber)', NONE:'var(--muted)'};
const PM_PART_COL = {Client:C.acc, FII:C.acc2, Pro:C.pur, DII:C.amber};
let _pmChain = null;

function pmFlag(f){
  const c = PM_FLAG_COL[f] || 'var(--muted)';
  return `<span style="font-family:var(--mono);font-size:9px;padding:1px 6px;border-radius:8px;border:1px solid ${c};color:${c}">${f||'—'}</span>`;
}
function pmUsual(v, pct){
  if(!v) return '—';
  const c = v==='RICHER'?'var(--green)':v==='CHEAPER'?'var(--red)':'var(--muted)';
  return `<span style="color:${c}" title="today's premium vs this cell's history (percentile)">${v}${pct!=null?` <span style="opacity:.6">${fmt(pct,0)}</span>`:''}</span>`;
}

async function initPremium(){
  try{
    document.getElementById('pmBadge').textContent = 'loading reference…';
    const m = await api('/api/premium/meta');
    const all = (m.richness||[]).map(r=>r.symbol);
    // Indices first, then stocks A-Z. '_STOCKS' is the pooled reference behind every
    // stock's flags: offered on the richness map, never as a chain to load.
    const PM_IDX = ['NIFTY','BANKNIFTY','FINNIFTY','MIDCPNIFTY','NIFTYNXT50','SENSEX','BANKEX'];
    const idxs = PM_IDX.filter(s=>all.includes(s));
    const ownStocks = all.filter(s=>!PM_IDX.includes(s) && s!=='_STOCKS');
    // Chain view: every stock with a live chain (flags come from the pooled reference,
    // so short-history names work too). Map view: only stocks with cells of their own.
    const stocks = [...new Set([...ownStocks, ...(m.stocks||[])])].sort();
    const syms = [...idxs, ...stocks];
    const heatSyms = [...idxs, ...(all.includes('_STOCKS')?['_STOCKS']:[]), ...ownStocks.sort()];
    if(!syms.length){
      // Reference DB not built yet: say so where the user is looking, instead of
      // leaving empty dropdowns and a LOAD button that silently does nothing.
      const msg = 'Richness reference not built yet. After the close run:  python oc\\build_richness_ref.py --force  '
                + '(creates richness.duckdb next to oc.duckdb; takes a few minutes), then reopen this page. '
                + 'The POSITIONING tab works without it.';
      document.getElementById('pmBadge').textContent = 'no reference';
      document.getElementById('pmBadge').style.color = 'var(--amber)';
      ['pmBest','pmTable','pmHeatChart'].forEach(id=>{
        const el=document.getElementById(id); if(el) el.innerHTML = `<div class="empty err">${msg}</div>`; });
      return;
    }
    populateSel('pmSymbol', syms);
    populateSel('pmHeatSymbol', heatSyms);
    const ref = (m.richness||[])[0];
    if(ref) document.getElementById('pmBadge').textContent =
      `reference ${String(ref.d0).slice(0,10)} → ${String(ref.d1).slice(0,10)} · built ${String(ref.built_at).slice(0,10)}`;
    if(syms.length){ await loadCascade('pmSymbol','pmExpiry','pmTimestamp'); await loadPmChain(); }
  }catch(e){
    document.getElementById('pmTable').innerHTML = `<div class="empty err">⚠ ${e.message}</div>`;
  }
}

async function loadPmChain(){
  const sym = document.getElementById('pmSymbol')?.value;
  const exp = document.getElementById('pmExpiry')?.value;
  const ts  = document.getElementById('pmTimestamp')?.value || '';
  const mk  = document.getElementById('pmMaxK')?.value || '2';
  if(!sym || !exp) return;
  document.getElementById('pmTable').innerHTML = '<div class="loading"><div class="spinner"></div>Loading…</div>';
  document.getElementById('pmBest').innerHTML  = '<div class="loading"><div class="spinner"></div>Loading…</div>';
  try{
    const d = await api(`/api/premium/chain?symbol=${encodeURIComponent(sym)}&expiry=${encodeURIComponent(exp)}`
                        + (ts?`&timestamp=${encodeURIComponent(ts)}`:'') + `&max_k=${mk}`);
    _pmChain = d;
    const nRich = d.rows.filter(r=>r.flag==='RICH').length, nGood = d.rows.filter(r=>r.flag==='GOOD').length;
    document.getElementById('pmKpis').innerHTML = [
      {label:'SPOT / FORWARD', value:fmt(d.spot,1), sub:`fwd ${fmt(d.forward,1)}`},
      {label:'ATM STRADDLE', value:fmt(d.atm_straddle,1), sub:`${fmt(d.atm_strike,0)} · σ ${fmt(d.sigma_ann_pct,1)}%`},
      {label:'DTE', value:d.dte, sub:d.bucket!=null?`ref bucket ${d.bucket}d${d.bucket_approx?' (approx)':''}`:'no reference bucket'},
      {label:'RICH / GOOD', value:`${nRich} / ${nGood}`, sub:`of ${d.rows.length} strikes shown`},
      {label:'LOT', value:d.lot??'—', sub:`as of ${d.timestamp||'—'}`},
      ...(d.is_stock ? [{label:'REFERENCE',
        value: d.results_in_window ? 'RESULTS' : 'ALL STOCKS',
        sub: d.results_in_window ? 'results before expiry — history shown, no flags'
                                 : 'pooled F&O stocks · own record in THIS STOCK'}] : []),
    ].map(k=>`<div class="kpi"><div class="kpi-label">${k.label}</div><div class="kpi-val acc">${k.value}</div><div class="kpi-sub">${k.sub}</div></div>`).join('');
    const cols = [
      {key:'flag',label:'FLAG',fmt:v=>pmFlag(v),sortValue:v=>({RICH:0,GOOD:1,FAIR:2,WEAK:3,AVOID:4,RESULTS:5,NONE:6}[v]??9)},
      {key:'type',label:'TYPE'},
      {key:'strike',label:'STRIKE',fmt:v=>fmt(v,0)},
      {key:'kz',label:'σ FROM FWD',fmt:v=>sspan(v,2),help:'ln(K/F) / (σ√T); negative = below the forward'},
      {key:'ltp',label:'LTP',fmt:v=>fmt(v,2)},
      {key:'iv',label:'IV%',fmt:v=>v!=null?fmt(v,1):'—'},
      {key:'vs_usual',label:'VS USUAL',fmt:(v,r)=>pmUsual(v,r.pn_pct),sortValue:(v,r)=>r.pn_pct??-1,
       help:"today's premium in σ units vs this cell's 2020-26 range (percentile)"},
      {key:'rom_annual_pct',label:'%/YR ON MARGIN',fmt:v=>v!=null?fmt(v,1):'—',
       help:'historical: per-trade return on SPAN+ELM margin x min(243/holding days, trades per year)'},
      {key:'hedged_bps',label:'HEDGED BPS',fmt:v=>v!=null?sspan(v,2):'—',help:'historical mean per trade, bps of spot, net of costs'},
      {key:'hedged_t',label:'T',fmt:v=>v!=null?fmt(v,1):'—'},
      {key:'hedged_h1',label:'20-23',fmt:v=>v!=null?sspan(v,1):'—',help:'hedged bps, first half of the sample'},
      {key:'hedged_h2',label:'23-26',fmt:v=>v!=null?sspan(v,1):'—',help:'hedged bps, second half of the sample'},
      {key:'payoff_ratio',label:'PAYOFF/PREM',fmt:v=>v!=null?fmt(v,2):'—',help:'share of premium historically paid back at expiry'},
      {key:'worst_rom_pct',label:'WORST %MGN',fmt:v=>v!=null?`<span class="down">${fmt(v,0)}</span>`:'—',help:'worst single trade in the cell, % of margin'},
      {key:'edge_per_lot',label:'₹/LOT (HIST)',fmt:v=>v!=null?fmt(v,0):'—',help:'historical mean hedged P&L per lot at today\'s spot'},
      {key:'oi',label:'OI',fmt:v=>fmtL(v)},
      {key:'spread_pct',label:'SPRD%',fmt:v=>v!=null?`<span class="${v>5?'down':v>2?'neu':''}">${fmt(v,1)}</span>`:'—'},
      {key:'n',label:'N',fmt:v=>v!=null?fmt(v,0):'—',help:'expiries in the reference cell'},
      {key:'own_rom',label:'THIS STOCK',sortValue:v=>v??-1e9,
       fmt:(v,r)=>r.own_n!=null?`${pmFlag(r.own_flag)} ${fmt(v,1)}%/yr · t ${fmt(r.own_t,1)} · n ${fmt(r.own_n,0)}`:'—',
       help:"this stock's own history in the same cell (stocks only; flags come from the pooled reference)"},
    ];
    document.getElementById('pmBest').innerHTML = d.best.length
      ? makeTbl(d.best, cols.filter(c=>['flag','type','strike','kz','ltp','vs_usual','rom_annual_pct','hedged_t','hedged_h1','hedged_h2','worst_rom_pct','edge_per_lot','spread_pct'].includes(c.key)))
      : '<div class="empty">No RICH or GOOD cells in this expiry'+(d.bucket==null?' — DTE has no reference bucket':'')+'</div>';
    _pmRenderTable(cols);
  }catch(e){
    document.getElementById('pmTable').innerHTML = `<div class="empty err">⚠ ${e.message}</div>`;
    document.getElementById('pmBest').innerHTML = '';
  }
}
let _pmCols = null;
function _pmRenderTable(cols){
  if(cols) _pmCols = cols;
  if(!_pmChain || !_pmCols) return;
  const show = document.getElementById('pmShow')?.value || 'all';
  let rows = _pmChain.rows.slice();
  if(show==='good') rows = rows.filter(r=>r.flag==='RICH'||r.flag==='GOOD');
  else if(show==='PE'||show==='CE') rows = rows.filter(r=>r.type===show);
  rows.sort((a,b)=>a.strike-b.strike || (a.type<b.type?-1:1));
  document.getElementById('pmTable').innerHTML = makeTbl(rows, _pmCols);
}

async function loadPmHeat(){
  const sym = document.getElementById('pmHeatSymbol')?.value;
  const metric = document.getElementById('pmHeatMetric')?.value || 'rom_annual_pct';
  if(!sym) return;
  setLoad('pmHeatChart');
  try{
    const d = await api(`/api/premium/heatmap?symbol=${encodeURIComponent(sym)}&metric=${metric}`);
    const ks = [...new Set(d.cells.map(c=>c.kz))].sort((a,b)=>a-b);
    const ds = d.dtes.filter(t=>d.cells.some(c=>c.tdte===t));
    const z = [], txt = [];
    ds.forEach(t=>{
      const zr=[], tr=[];
      ks.forEach(k=>{
        const cs = d.cells.filter(c=>c.tdte===t && c.kz===k);
        if(!cs.length){ zr.push(null); tr.push(''); return; }
        const vals = cs.map(c=>c[metric]).filter(v=>v!=null);
        zr.push(vals.length? vals.reduce((a,b)=>a+b,0)/vals.length : null);
        tr.push(cs.map(c=>`${c.t} ${k>0?'+':''}${k}σ · ${t}d · ${c.flag}<br>`+
          `%/yr ${fmt(c.rom_annual_pct,1)} · hedged ${fmt(c.hedged_bps,2)}bps · t ${fmt(c.hedged_t,1)}<br>`+
          `halves ${fmt(c.hedged_h1,1)} / ${fmt(c.hedged_h2,1)} · payoff ${fmt(c.payoff_ratio,2)} · worst ${fmt(c.worst_rom_pct,0)}% mgn · n ${c.n}`).join('<br>'));
      });
      z.push(zr); txt.push(tr);
    });
    const absMax = Math.max(...z.flat().filter(v=>v!=null).map(Math.abs), 1e-9);
    const centred = !['payoff_ratio','capture'].includes(metric);
    // outline RICH / GOOD cells
    const shapes = [];
    d.cells.forEach(c=>{
      if(c.flag!=='RICH' && c.flag!=='GOOD') return;
      const xi = ks.indexOf(c.kz), yi = ds.indexOf(c.tdte);
      shapes.push({type:'rect', xref:'x', yref:'y', x0:xi-0.48, x1:xi+0.48, y0:yi-0.48, y1:yi+0.48,
                   line:{color:c.flag==='RICH'?'#ffffff':'rgba(255,255,255,.45)', width:c.flag==='RICH'?1.6:0.8}});
    });
    plot('pmHeatChart', [{
      type:'heatmap', z, text:txt, hoverinfo:'text',
      x:ks.map(k=>(k>0?'+':'')+k), y:ds.map(t=>t+'d'),
      colorscale: centred ? [[0,'#f43f5e'],[0.5,'#0f1a2b'],[1,'#10b981']] : 'Viridis',
      zmid: centred ? 0 : undefined, zmin: centred ? -absMax : undefined, zmax: centred ? absMax : undefined,
      colorbar:{thickness:10, tickfont:{size:9}},
    }], {...LB, shapes, margin:{t:16,r:20,b:46,l:54},
         xaxis:{...LB.xaxis, title:{text:'σ from forward  (puts ← 0 → calls)', font:{size:10}}, type:'category'},
         yaxis:{...LB.yaxis, title:{text:'entry DTE', font:{size:10}}, type:'category'}});
    const m = d.meta||{};
    document.getElementById('pmHeatBadge').textContent =
      `${String(m.d0||'').slice(0,10)} → ${String(m.d1||'').slice(0,10)} · ${m.n_trades??'—'} trades · outline = RICH (bold) / GOOD`;
  }catch(e){ setEmpty('pmHeatChart', '⚠ '+e.message); }
}

async function loadPmPos(){
  const metric = document.getElementById('pmPosMetric')?.value || 'put_net_pct';
  const hist = document.getElementById('pmPosRange')?.value || '250';
  const kind = document.getElementById('pmPosKind')?.value || 'oi';
  setLoad('pmPosChart');
  try{
    const d = await api(`/api/premium/positioning?history=${hist}&kind=${kind}`);
    const by = p => d.today.find(r=>r.participant===p) || {};
    const fii = by('FII'), cli = by('Client'), pro = by('Pro');
    const z = v => v!=null ? ` · z ${v>0?'+':''}${fmt(v,1)}` : '';
    const ch = v => v!=null ? `Δ ${v>0?'+':''}${fmt(v,2)}` : '';
    const kc = v => v==null?'acc':v>0?'up':v<0?'down':'acc';
    document.getElementById('pmPosKpis').innerHTML = [
      {label:'FII INDEX FUTURES NET', v:fii.fut_net_pct, sub:ch(fii.fut_net_pct_chg)+z(fii.fut_net_pct_z)},
      {label:'FII NET PUTS', v:fii.put_net_pct, sub:ch(fii.put_net_pct_chg)+z(fii.put_net_pct_z)},
      {label:'FII NET CALLS', v:fii.call_net_pct, sub:ch(fii.call_net_pct_chg)+z(fii.call_net_pct_z)},
      {label:'CLIENT NET PUTS', v:cli.put_net_pct, sub:ch(cli.put_net_pct_chg)+z(cli.put_net_pct_z)},
      {label:'PRO NET CALLS / PUTS', raw:`${fmt(pro.call_net_pct,1)} / ${fmt(pro.put_net_pct,1)}`, sub:'prop desks (the would-be hedgers)'},
      {label:'FII PUT COVER', raw:d.fii_put_cover!=null?fmt(d.fii_put_cover,2)+'×':'—',
       sub:'net long puts ÷ net short futures (contracts)'},
    ].map(k=>`<div class="kpi"><div class="kpi-label">${k.label}</div>`+
      `<div class="kpi-val ${k.raw?'acc':kc(k.v)}">${k.raw ?? (k.v!=null?fmt(k.v,1)+'%':'—')}</div>`+
      `<div class="kpi-sub">${k.sub||''}</div></div>`).join('');
    const parts = ['Client','FII','Pro','DII'];
    const traces = parts.map(p=>{
      const s = d.series.filter(r=>r.participant===p);
      return {type:'scatter', mode:'lines', name:p, x:s.map(r=>String(r.trade_date).slice(0,10)),
              y:s.map(r=>r[metric]), line:{width:p==='DII'?1:1.6, color:PM_PART_COL[p]}};
    });
    const lab = document.getElementById('pmPosMetric').selectedOptions[0].textContent;
    plot('pmPosChart', traces, {...LB, margin:{t:16,r:14,b:40,l:54},
      yaxis:{...LB.yaxis, title:{text:lab+' (%)', font:{size:10}}, zeroline:true},
      legend:{...LB.legend, orientation:'h', y:1.08}, hovermode:'x unified'});
    document.getElementById('pmPosDate').textContent =
      `${String(d.date).slice(0,10)} · ${d.n_days} days · ${kind.toUpperCase()}`;
    document.getElementById('pmPosBadge').textContent = `${d.n_days} days loaded`;
    document.getElementById('pmPosTable').innerHTML = makeTbl(d.today, [
      {key:'participant',label:'PARTICIPANT'},
      {key:'fut_net_pct',label:'FUT NET %',fmt:v=>sspan(v,1)},
      {key:'fut_net_pct_chg',label:'Δ',fmt:v=>sspan(v,2)},
      {key:'call_net_pct',label:'CALLS NET %',fmt:v=>sspan(v,1)},
      {key:'call_net_pct_chg',label:'Δ',fmt:v=>sspan(v,2)},
      {key:'put_net_pct',label:'PUTS NET %',fmt:v=>sspan(v,1)},
      {key:'put_net_pct_chg',label:'Δ',fmt:v=>sspan(v,2)},
      {key:'call_short_share',label:'SHORT CALL SHARE',fmt:v=>fmt(v,1)+'%'},
      {key:'put_short_share',label:'SHORT PUT SHARE',fmt:v=>fmt(v,1)+'%'},
      {key:'fut_net',label:'FUT NET (CONTR.)',fmt:v=>fmtL(v)},
      {key:'put_net',label:'PUT NET (CONTR.)',fmt:v=>fmtL(v)},
    ]);
  }catch(e){ setEmpty('pmPosChart', '⚠ '+e.message); }
}

// ── PREMIUM guides (one per subtab), same style as the VRP / Vol guides ──
function _pmSec(title, body){ return `<b style="color:var(--text)">${title}</b><br>${body}<br><br>`; }
function _pmGuideRows(rows){
  return rows.map(([k,v])=>`<b style="color:var(--acc)">${k}:</b> ${v}`).join('<br>');
}
function _pmStrikesGuideHtml(){
  return _pmSec('WHAT THIS TAB IS',
    'Each out-of-the-money strike of the selected expiry is mapped to a historical reference cell '
    +'(entry DTE × distance from the forward) and shown with how that cell paid a seller: sell at entry, '
    +'hold to expiry, delta-hedge with futures at entry, net of costs, 2020-06 onward. '
    +'The flag describes the CELL\'s history, not a forecast for this trade. Hedged returns are small '
    +'(a few bps of spot); flags reward cells where that edge is statistically clean, which is why only '
    +'far-OTM strikes (|σ| ≥ 1.5, delta ≈ 0.1 and lower) tend to reach RICH on indices.')
  + _pmSec('INDICES VS STOCKS',
    'Indices use their own reference (NIFTY, BANKNIFTY), weekly DTE grid 1-28 days. '
    +'Stocks use the POOLED reference over all F&amp;O stocks (monthly DTE grid 3/7/10/14/21/28), because '
    +'about 75 monthly expiries per stock rarely reach t ≥ 3; the stock\'s own record is shown beside it '
    +'in THIS STOCK. Stock costs assume a 1.75% half-spread and 3.5% ELM. Trades spanning a split/bonus are '
    +'excluded. When results fall before the selected expiry, every strike shows RESULTS (amber) and the '
    +'results-window history instead of a flag: those windows behave differently (ATM legs lost money on '
    +'average) and are never flagged.')
  + _pmSec('FLAGS', _pmGuideRows([
      ['RICH','t ≥ 3, ≥ 20%/yr on SPAN margin, positive in both halves (2020-23 and 2023-26).'],
      ['GOOD','t ≥ 2, ≥ 10%/yr, positive in both halves.'],
      ['FAIR','positive, weaker evidence.'],
      ['WEAK','negative in one half, or t < 1.'],
      ['AVOID','the cell lost money after costs.'],
      ['RESULTS','stock with results before expiry: history shown, no flag.'],
      ['NONE','no reference cell (too few trades, or DTE outside the grid).'],
    ]))
  + _pmSec('KPI TILES', _pmGuideRows([
      ['SPOT / FORWARD','underlying and the forward from ATM put-call parity (K + C − P); all σ distances are from the forward.'],
      ['ATM STRADDLE','ATM call + put and the implied annual σ backed out of it (straddle ≈ 0.8 · F · σ√T).'],
      ['DTE','calendar days to expiry and the reference bucket used (approx = no exact bucket window, nearest used).'],
      ['RICH / GOOD','how many shown strikes carry those flags.'],
      ['REFERENCE','stocks only: ALL STOCKS (pooled) or RESULTS (results inside the window).'],
    ]))
  + _pmSec('TABLE COLUMNS (BEST ZONES and STRIKES)', _pmGuideRows([
      ['FLAG','the cell\'s flag (above).'],
      ['TYPE / STRIKE','CE or PE, and the strike. Only the OTM side is shown (both legs at ATM).'],
      ['σ FROM FWD','ln(K/F) / (σ√T): distance from the forward in ATM-implied moves; negative = below. Rounded to 0.25 to pick the cell. Rough delta guide: ±1σ ≈ 0.16, ±1.5σ ≈ 0.07, ±2σ ≈ 0.02.'],
      ['LTP / IV%','live premium and the strike\'s implied vol.'],
      ['VS USUAL','today\'s premium in σ units vs the cell\'s 2020-26 range: RICHER ≥ 75th percentile, CHEAPER ≤ 25th; the number is the percentile. This is the only column about TODAY\'s price.'],
      ['%/YR ON MARGIN','historical return on SPAN+ELM margin, annualised as per-trade ROM × min(243 / holding days, trades available per year). Assumes the capital is reused back to back.'],
      ['HEDGED BPS','historical mean P&amp;L per trade, bps of spot, after costs and the futures hedge.'],
      ['T','t-statistic of that mean. Below 2 the mean is not distinguishable from noise.'],
      ['20-23 / 23-26','hedged bps in each half of the sample (split 2023-07-01). Both positive = the edge persisted.'],
      ['PAYOFF/PREM','share of premium paid back at expiry on average. 0.3 = buyers recovered 30% of what they paid.'],
      ['WORST %MGN','worst single trade in the cell, % of margin. Naked-leg tail; history starts after the March-2020 crash.'],
      ['₹/LOT (HIST)','historical mean hedged P&amp;L per lot at today\'s spot. An expectation, not a target.'],
      ['OI / SPRD%','live open interest and bid-ask spread as % of premium (amber > 2%, red > 5%). A wide spread can eat the whole historical edge.'],
      ['N','trades (expiries) in the reference cell.'],
      ['THIS STOCK','stocks only: the stock\'s own record in the same cell — its flag, %/yr, t and n. Shown when it has ≥ 24 trades; short-history names show —.'],
    ]))
  + _pmSec('HOW TO USE IT',
    'Start from BEST ZONES (one strike per RICH/GOOD cell). Prefer RICH cells whose VS USUAL is RICHER '
    +'(an historically good cell priced above its norm today) with a tight SPRD%. CHEAPER in a RICH cell '
    +'means the edge is thinner than usual. Remember the hedge: results are for a futures-hedged leg; an '
    +'unhedged naked leg has the same mean but far wider outcomes.');
}
function _pmHeatGuideHtml(){
  return _pmSec('WHAT THE MAP SHOWS',
    'Every reference cell for the chosen symbol on one grid. Rows = entry DTE (days to expiry when sold). '
    +'Columns = distance from the forward in ATM-implied moves (σ): puts on the left (negative), calls on '
    +'the right (positive), ATM in the middle (both legs averaged). Cells outlined in green are RICH, in '
    +'teal GOOD. Hover any cell for its full statistics. Results-window cells are not drawn.')
  + _pmSec('READING THE COLOUR',
    'Diverging metrics (%/yr, hedged bps, t-stat) are centred on zero: green = sellers kept money, red = '
    +'they lost, intensity = size. PAYOFF/PREMIUM and CAPTURE use a one-sided scale. Look for a band of '
    +'colour, not a single bright cell: one isolated cell among neutral neighbours is more likely noise.')
  + _pmSec('METRICS (dropdown)', _pmGuideRows([
      ['%/YR ON MARGIN','annualised return on SPAN+ELM margin (see the Strike Flags guide).'],
      ['HEDGED BPS / TRADE','mean futures-hedged P&amp;L per trade, bps of spot, after costs.'],
      ['T-STAT','how clearly that mean differs from zero. The flag thresholds use t ≥ 2 (GOOD) and t ≥ 3 (RICH).'],
      ['CAPTURE (OF PREMIUM)','hedged P&amp;L ÷ premium received: the share of the premium a hedged seller kept.'],
      ['PAYOFF / PREMIUM','what buyers got back at expiry per rupee of premium. Below 1 = sellers ahead before costs.'],
    ]))
  + _pmSec('HOVER FIELDS', _pmGuideRows([
      ['halves','hedged bps in 2020-23 / 2023-26.'],
      ['payoff','payoff ÷ premium.'],
      ['worst','worst single trade, % of margin.'],
      ['n','trades in the cell.'],
    ]))
  + _pmSec('SYMBOLS',
    'NIFTY / BANKNIFTY use their own weekly-grid reference. _STOCKS is the pooled reference behind every '
    +'stock\'s flags. Individual stocks are listed only when they have cells of their own (≥ 24 trades); '
    +'their maps are noisier — read them as colour, not as flags.');
}
function _pmPosGuideHtml(){
  return _pmSec('WHAT THIS TAB IS',
    'Index options and index futures open interest (or volume) by participant, from NSE\'s daily '
    +'participant-wise file: Client (retail and HNI), FII, Pro (proprietary desks) and DII. It is aggregate '
    +'across NIFTY, BANKNIFTY, FINNIFTY and MIDCPNIFTY and all expiries — NSE does not split it by '
    +'underlying, expiry or strike, so it cannot re-sign per-strike GEX. Context, not a signal: it has not '
    +'been tested against next-day range yet.')
  + _pmSec('KPI TILES', _pmGuideRows([
      ['FII INDEX FUTURES NET','FII long minus short index futures, % of total long futures OI. Negative = net short. Δ = change from the previous day; z = 60-day z-score.'],
      ['FII NET PUTS / CALLS','FII long minus short index puts (calls), % of total long put (call) OI. Positive = net buyers.'],
      ['CLIENT NET PUTS','the same for Clients. Strongly negative = Clients are the main put writers (and do not delta-hedge).'],
      ['PRO NET CALLS / PUTS','prop desks — the participants most likely to hedge like textbook dealers.'],
      ['FII PUT COVER','FII net long puts ÷ FII net short futures (contracts). Near or above 1 = the short futures book is largely protected by puts.'],
    ]))
  + _pmSec('LATEST DAY TABLE', _pmGuideRows([
      ['FUT / CALLS / PUTS NET %','long minus short as % of total long OI in that instrument, per participant. The four participants\' nets sum to about zero.'],
      ['Δ','change in that net % since the previous trading day.'],
      ['SHORT CALL / PUT SHARE','that participant\'s short positions as % of all short calls (puts): who the writers are.'],
      ['FUT NET / PUT NET (CONTR.)','the same nets in contracts, for scale.'],
    ]))
  + _pmSec('CHART AND DROPDOWNS',
    'SERIES picks the measure plotted for all four participants over the RANGE. DATA = OPEN INTEREST '
    +'(positions held) or VOLUME (that day\'s trading). Read OI for who holds the risk; volume for who was active.');
}
function _pmToggleGuide(btnId, boxId, builder){
  document.getElementById(btnId)?.addEventListener('click', ()=>{
    const g = document.getElementById(boxId);
    if(!g) return;
    if(!g.dataset.built){ g.innerHTML = builder(); g.dataset.built = '1'; }
    g.style.display = g.style.display==='none' ? '' : 'none';
  });
}
_pmToggleGuide('btnPmGuide', 'pmGuide', _pmStrikesGuideHtml);
_pmToggleGuide('btnPmHeatGuide', 'pmHeatGuide', _pmHeatGuideHtml);
_pmToggleGuide('btnPmPosGuide', 'pmPosGuide', _pmPosGuideHtml);

(function wirePremium(){
  const on = (id, ev, fn) => { const el=document.getElementById(id); if(el) el.addEventListener(ev, fn); };
  on('pmSymbol','change',()=>loadCascade('pmSymbol','pmExpiry','pmTimestamp'));
  on('pmExpiry','change',()=>reloadTimestamps('pmSymbol','pmExpiry','pmTimestamp'));
  on('btnPm','click',loadPmChain);
  on('pmMaxK','change',loadPmChain);
  on('pmShow','change',()=>_pmRenderTable());
  on('btnPmHeat','click',loadPmHeat);
  on('pmHeatSymbol','change',loadPmHeat);
  on('pmHeatMetric','change',loadPmHeat);
  on('btnPmPos','click',loadPmPos);
  on('pmPosMetric','change',loadPmPos);
  on('pmPosRange','change',loadPmPos);
  on('pmPosKind','change',loadPmPos);
  // lazy-load the two secondary subtabs on first open
  document.querySelectorAll('#view-premium .subtab').forEach(t=>t.addEventListener('click',()=>{
    if(t.dataset.loaded) return; t.dataset.loaded='1';
    if(t.dataset.stab==='pm-heat') loadPmHeat();
    if(t.dataset.stab==='pm-pos') loadPmPos();
  }));
})();
