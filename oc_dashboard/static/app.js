
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
  rows.sort((a,b)=>{
    const ac=a.children[idx], bc=b.children[idx];
    // prefer explicit data-sort (e.g. signal severity) over visible text
    const ads=ac.getAttribute('data-sort'), bds=bc.getAttribute('data-sort');
    if(ads!=null && bds!=null){
      const an2=parseFloat(ads),bn2=parseFloat(bds);
      if(!isNaN(an2)&&!isNaN(bn2))return asc?an2-bn2:bn2-an2;
    }
    const av=ac.textContent.trim().replace(/[,₹%+CrL]/g,'');
    const bv=bc.textContent.trim().replace(/[,₹%+CrL]/g,'');
    const an=parseFloat(av),bn=parseFloat(bv);
    if(!isNaN(an)&&!isNaN(bn))return asc?an-bn:bn-an;
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
 'esViewGroup']
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
    plot('netGexChart',[{name:'Net GEX',x:strikes,y:netG,type:'bar',marker:{color:netG.map(v=>v>=0?C.green:C.red),opacity:.8}}],
      {...LB,xaxis:{...LB.xaxis,title:'Strike',tickangle:-45},yaxis:{...LB.yaxis,title:'Net GEX (₹M)'}});
    if(gp?.profile?.length){
      const prof=gp.profile,lvl=prof.map(r=>r.level);
      const cg=prof.map(r=>r.call_gamma_billions),pg=prof.map(r=>r.put_gamma_billions),tg=prof.map(r=>r.total_gamma_billions);
      const pshapes=[];
      if(gp.gamma_flip!=null)pshapes.push({type:'line',x0:gp.gamma_flip,x1:gp.gamma_flip,y0:0,y1:1,yref:'paper',line:{color:C.red,width:2,dash:'dot'}});
      if(gp.fut!=null)pshapes.push({type:'line',x0:gp.fut,x1:gp.fut,y0:0,y1:1,yref:'paper',line:{color:C.acc2,width:1.5,dash:'dash'}});
      if(gp.spot!=null)pshapes.push({type:'line',x0:gp.spot,x1:gp.spot,y0:0,y1:1,yref:'paper',line:{color:C.acc,width:1,dash:'dot'}});
      if(gp.magnet)pshapes.push({type:'rect',x0:gp.magnet.lower,x1:gp.magnet.upper,y0:0,y1:1,yref:'paper',fillcolor:'rgba(0,200,240,.05)',line:{width:0}});
      plot('gammaProfileChart',[
        {name:'Call GEX',x:lvl,y:cg,type:'scatter',mode:'lines',fill:'tozeroy',line:{color:C.green,width:1.5},fillcolor:'rgba(16,185,129,.1)'},
        {name:'Put GEX', x:lvl,y:pg,type:'scatter',mode:'lines',fill:'tozeroy',line:{color:C.red,  width:1.5},fillcolor:'rgba(244,63,94,.1)'},
        {name:'Total',   x:lvl,y:tg,type:'scatter',mode:'lines',line:{color:C.acc,width:2.5}},
      ],{...LB,shapes:pshapes,xaxis:{...LB.xaxis,title:'Price Level'},yaxis:{...LB.yaxis,title:'GEX (₹B)'}});
      const _ref = gp.fut!=null?gp.fut:gp.spot;
      const regime=_ref>=(gp.gamma_flip||0)?'Positive Gamma':'Negative Gamma';
      document.getElementById('gexKpis').innerHTML=[
        {label:'SPOT',value:fmt(gp.spot),sub:''},
        {label:'GAMMA FLIP',value:gp.gamma_flip!=null?fmt(gp.gamma_flip,0):'—',sub:regime,subcls:regime==='Positive Gamma'?'up':'down'},
        {label:'MAGNET CTR',value:gp.magnet?fmt(gp.magnet.center,0):'—',sub:gp.magnet?fmt(gp.magnet.lower,0)+'–'+fmt(gp.magnet.upper,0):''},
        {label:'STRENGTH',value:gp.magnet?fmt(gp.magnet.strength,4):'—',sub:'₹B'},
      ].map(k=>`<div class="kpi"><div class="kpi-label">${k.label}</div><div class="kpi-val acc">${k.value}</div><div class="kpi-sub ${k.subcls||''}">${k.sub}</div></div>`).join('');
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
      document.getElementById('ivRankKpis').innerHTML=[
        {label:'ATM IV',value:fmt(ivr.atm_iv,2)+'%',sub:''},
        {label:'IV RANK',value:ivRankBar(ivr.iv_rank),sub:`${ivr.hist_count} samples / ${lookback}d`},
        {label:'IV PCTILE',value:ivr.iv_pctile!=null?fmt(ivr.iv_pctile,1)+'%':'—',sub:''},
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
      {label:'25Δ RISK REVERSAL',value:d.rr_25d!=null?sspan(d.rr_25d):'-',sub:'PE IV – CE IV at 25Δ'},
      {label:'25Δ PUT IV',value:d.pe_25d_iv!=null?fmt(d.pe_25d_iv,2)+'%':'—',sub:d.pe_25d_strike?'@ '+d.pe_25d_strike:''},
      {label:'25Δ CALL IV',value:d.ce_25d_iv!=null?fmt(d.ce_25d_iv,2)+'%':'—',sub:d.ce_25d_strike?'@ '+d.ce_25d_strike:''},
      {label:'LEFT WING SLOPE',value:d.left_wing_slope!=null?fmt(d.left_wing_slope,4):'—',sub:'put wing slope'},
      {label:'RIGHT WING SLOPE',value:d.right_wing_slope!=null?fmt(d.right_wing_slope,4):'—',sub:'call wing slope'},
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
  updateDsRangeLabel();
  document.getElementById('dsTable').innerHTML='<div class="loading"><div class="spinner"></div>Loading…</div>';
  document.getElementById('dsKpis').innerHTML='';
  try{
    const tsParam=ts?`&timestamp=${encodeURIComponent(ts)}`:'';
    const res=await api(`/api/delta_screener?target_delta=${tgtDlt}&min_delta=${minDlt}&filter_type=${ft}${tsParam}`);
    _dsCurrentRows=res.rows||[];
    _dsSessionLoaded=true;
    const minOi=getSetting('stgDsMinOi'),minVol=getSetting('stgDsMinVol');
    const visible=_dsCurrentRows.filter(r=>r.oi>=minOi&&r.volume>=minVol);
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
};

// ── #10: Screener mid-level page-tabs (Delta/Premium | Divergence | Shockers | Movers) ──
// Each panel inits lazily the first time its tab is shown.
const PTAB_INIT = {
  'sp-delta':      ()=>loadDsTimestamps(),
  'sp-divergence': ()=>{ if(typeof loadDivergence==='function') loadDivergence(); },
  'sp-shockers':   ()=>{ loadVolShock(); loadIvShock(); },
  'sp-movers':     ()=>loadMovers(),
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
VIEW_INIT['delta'] = ()=>{
  loadDsTimestamps();  // populates dsTimestamp using best available symbol
  // For premium lens: if plSymbol already has a selection, cascade it;
  // otherwise it will populate once user picks a symbol.
  const plSym=document.getElementById('plSymbol')?.value;
  if(plSym) loadCascade('plSymbol','plExpiry','plTimestamp');
};

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
  const rows=container.querySelectorAll('tbody tr');
  rows.forEach(row=>{
    // First cell is always the symbol
    const sym=row.cells[0]?.textContent.trim().toUpperCase()||'';
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
const SIGNAL_ICONS = {
  regime_flip_to_neg:    {icon:'▼', color:'var(--red)',   label:'Regime flip → negative'},
  regime_flip_to_pos:    {icon:'▲', color:'var(--green)', label:'Regime flip → positive'},
  crash_risk:            {icon:'⚡', color:'var(--red)',   label:'Crash risk (disorderly, rising IV + PE vanna)'},
  bull_trend_reinforce:  {icon:'↑', color:'var(--green)', label:'Bull trend reinforce (orderly upside)'},
  bear_trend_reinforce:  {icon:'↓', color:'var(--amber)', label:'Bear trend reinforce (orderly pullback, falling IV)'},
  pin_strengthening:     {icon:'◎', color:'var(--acc)',   label:'Pin strengthening (transition narrowing)'},
  instability_widening:  {icon:'◇', color:'var(--amber)', label:'Instability widening (neg-gamma fraction rising)'},
  flip_drift_up:         {icon:'⤴', color:'var(--muted)', label:'Flip drift up'},
  flip_drift_down:       {icon:'⤵', color:'var(--muted)', label:'Flip drift down'},
};
// Severity rank — lower = more urgent (sorts first). crash/flips → reinforce/structural → drift.
const SIGNAL_SEVERITY = {
  crash_risk:0, regime_flip_to_neg:1, regime_flip_to_pos:1,
  bull_trend_reinforce:2, bear_trend_reinforce:2,
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
  const order = ['regime_flip_to_neg','regime_flip_to_pos','crash_risk',
                 'bull_trend_reinforce','bear_trend_reinforce',
                 'pin_strengthening','instability_widening','flip_drift_up','flip_drift_down'];
  // Indicator counts (compression/release) shown alongside signals
  const indCounts = d.indicator_counts||{};
  const colors = {regime_flip_to_neg:'var(--red)',crash_risk:'var(--red)',
    regime_flip_to_pos:'var(--green)',bull_trend_reinforce:'var(--green)',
    bear_trend_reinforce:'var(--amber)',
    instability_widening:'var(--amber)',pin_strengthening:'var(--acc)'};
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
    {key:'_defend',     label:'DEFEND', fmt:(v,r)=>defendCell(r, r._idx),
                        sortValue:(v,r)=>{const h=_defenseHoldProb(r);return h?h.p:-1;},
                        exportValue:(v,r)=>{const h=_defenseHoldProb(r);return h?h.p:'';}},
    // #9c: primary price = FUT (fut_price); spot shown in tooltip
    {key:'fut_price',   label:'FUT',     fmt:(v,r)=>v!=null?`<span title="spot: ${r&&r.spot!=null?fmt(r.spot,1):'—'}">${fmt(v,1)}</span>`:'—', exportValue:v=>v==null?'':v},
    {key:'gex_regime',  label:'REGIME',  fmt:(v,r)=>regimePillWithDays(v, r)},
    {key:'days_in_regime',label:'DAYS', fmt:(v,r)=>daysInRegimeBadge(v, r&&r.gex_regime)},
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
    {key:'basis_annualized',label:'BASIS%',fmt:v=>v!=null?sspan(v,1):'—', exportValue:v=>v==null?'':v},
    {key:'basis_chg',   label:'BASIS Δ', fmt:v=>v!=null?sspan(v,1):'—', exportValue:v=>v==null?'':v},
    {key:'regime_compression',label:'COMPRESS',fmt:(v,r)=>compressionBadge(v, r&&r.compression_days)},
    {key:'compression_release',label:'REL',fmt:v=>v?'<span title="compression release" style="color:var(--red);font-weight:600">⚡</span>':'—'},
    {key:'oi_turnover_ratio',label:'OI TURN',fmt:v=>turnoverBadge(v)},
    // #9a: Signals → icons (severity-sorted), sortValue drives column sort by urgency
    {key:'signals',     label:'SIGNALS', fmt:v=>signalIcons(v), sortValue:v=>signalSeverityKey(v), exportValue:v=>v||''},
    {key:'confidence',  label:'CONF',    fmt:v=>confBadge(v)},
    {key:'next_day_realized_move',label:'NEXT MOVE%',fmt:v=>v!=null?sspan(v,2):'—'},
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

// 1. Where is dealer positioning? (gamma COM vs price)
function _narrPositioning(r){
  const d=_comDistEM(r);
  if(d==null) return '—';
  const ad=Math.abs(d);
  if(ad<=0.25) return 'Defending at-the-money (COM ≈ price)';
  return `Support stacked ${d>0?'above':'below'} (COM ${d>0?'+':'−'}${ad.toFixed(2)} EM)`;
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

// Defense-zone-hold probability — empirical additive model (validated ±3.5pts).
// base by width band + concentration adj + regime adj, clamped [5,95].
// null when no flip zone exists (monotone curve → no transition_width).
function _defenseHoldProb(r){
  const twn=r.transition_width_norm, conc=r.concentration, reg=r.gex_regime;
  if(twn==null) return null;
  const wb = twn<=0.15?'tight':(twn<=0.24?'moderate':'wide');
  let p = wb==='tight'?31:(wb==='moderate'?46:58);
  if(conc!=null){ if(conc>0.37) p+=4; else if(conc<=0.27) p-=4; }
  if(reg==='positive'||reg==='all_positive') p+=3;
  else if(reg==='negative'||reg==='all_negative') p-=4;
  p=Math.max(5,Math.min(95,p));
  return {p:Math.round(p), bucket:(p<40?'Low':(p<=55?'Moderate':'High')), wb};
}

// DEFEND cell — compact ⊕ trigger + hold-prob chip; full narrative in the popover.
function defendCell(r, idx){
  const h=_defenseHoldProb(r);
  const chip = h
    ? `<span style="font-family:var(--mono);font-size:10px;color:${
        h.bucket==='High'?'var(--green)':(h.bucket==='Low'?'var(--red)':'var(--amber)')}">${h.p}%</span>`
    : '<span style="color:var(--muted);font-size:10px">—</span>';
  return `<span class="wall-tip-trigger" tabindex="0" data-esidx="${idx}"
            style="margin-right:5px">⊕</span>${chip}`;
}

// Popover HTML — three narrative groups + COM distance + hold-prob breakdown.
function _expDetailTip(r){
  const d=_comDistEM(r);
  const h=_defenseHoldProb(r);
  const row=(lbl,val)=>`<div style="display:flex;gap:10px;margin:2px 0">
    <span style="color:var(--muted);min-width:96px;font-size:10px">${lbl}</span>
    <span style="font-size:11px">${val}</span></div>`;
  let hold;
  if(h){
    const col=h.bucket==='High'?'var(--green)':(h.bucket==='Low'?'var(--red)':'var(--amber)');
    hold=`<span style="color:${col};font-weight:600">${h.p}% — ${h.bucket}</span>
      <span style="color:var(--muted);font-size:9px"> (${h.wb} zone${
        (r.gex_regime||'').indexOf('negative')>=0&&h.wb==='wide'?', width-dominated':''})</span>`;
  } else {
    hold='<span style="color:var(--muted)">no flip zone (one-sided γ)</span>';
  }
  return `<div style="font-family:var(--mono);min-width:240px;max-width:300px">
    <div style="font-weight:600;margin-bottom:5px;color:var(--acc)">${r.symbol}
      <span style="color:var(--muted);font-weight:400">· defense readout</span></div>
    ${row('Positioning', _narrPositioning(r))}
    ${row('Distribution', _narrDistribution(r))}
    ${row('Changing', _narrChange(r))}
    <div style="border-top:1px solid var(--bd);margin:5px 0"></div>
    ${row('COM dist', d==null?'—':`${d>0?'+':''}${d.toFixed(2)} EM from price`)}
    ${row('Zone holds', hold)}
    <div style="color:var(--muted);font-size:9px;margin-top:5px;line-height:1.3">
      Empirical next-day hold rate. Wider zones hold more (partly mechanical);
      signal is concentration + regime tilt within a width band.</div>
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
    const red=['regime_flip_to_neg','crash_risk'].includes(s);
    const grn=['regime_flip_to_pos','bull_trend_reinforce'].includes(s);
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
const HS_AXIS_LABEL = {
  lopsided:'lopsided', neg_gamma:'neg-γ%', iv:'IV',
  flip:'flip dist', regime:'regime', trans_w:'trans W',
};

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
    yaxis:{...LB.yaxis,title:'Day score',range:[-6.5,6.5]},
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


// ── Overview meta: snapshot timestamp badge + recent-signals strip (#1,#3) ──
async function loadOverviewMeta(){
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
    ['crash_risk','⚡ crash','var(--red)'],
    ['regime_flip_to_neg','▼ →neg','var(--red)'],
    ['regime_flip_to_pos','▲ →pos','var(--green)'],
    ['_releasing','⚡ release','var(--red)'],
    ['_compressing','🌀 coiling','var(--acc)'],
    ['bull_trend_reinforce','↑ bull','var(--green)'],
    ['bear_trend_reinforce','↓ bear','var(--amber)'],
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

