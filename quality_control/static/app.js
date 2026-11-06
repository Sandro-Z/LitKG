'use strict';
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={specialization:'领域专门化',granularity:'粒度',completeness:'完整性',correctness:'正确性',ontology:'本体合规',coverage:'覆盖代理'};
const colors=['#178581','#d49637','#6871b4','#c36d71','#409cc0','#7d9a4e'];
const verdicts={SUPPORTED:'支持',PARTIALLY_SUPPORTED:'部分支持',CONTRADICTED:'原文反证',UNSUPPORTED:'原文不支持',INSUFFICIENT_EVIDENCE:'证据不足',SOURCE_CONFLICT:'来源冲突'};
let graphs=[],selected=new Set(),config={},activeClaim=null,activeTab='compare',poll=null;
const importErrors = {
  LITKG_EXPORT_TOO_LARGE: 'LitKG 快照超过读取上限（图谱导出为 128 MiB）。请缩小知识库范围。',
  LITKG_UPSTREAM_HTTP_404: 'LitKG API 未找到该知识库或导出接口，请重新加载列表并确认 API 已更新。',
  LITKG_UPSTREAM_TIMEOUT: 'LitKG 导出等待超时，请检查主 API 的导出耗时。',
  LITKG_UPSTREAM_UNREACHABLE: '质控服务连不上 LitKG API，请检查 QC_LITKG_URL。',
  LITKG_UPSTREAM_INVALID_JSON: 'LitKG API 返回的内容无法解析为 JSON。',
  SOURCE_UPLOAD_INVALID: '原文上传数据无效。',
  SOURCE_UPLOAD_TOO_LARGE: '同次上传的原文件合计不得超过 32 MiB。',
  SOURCE_DOCUMENT_ID_UNMATCHED: '文献文件名无法与图谱的文档 ID 对应，请填写文献 ID 映射。',
  SOURCE_DOCUMENT_ID_INVALID: '文献 ID 必须是非空字符串。',
  SOURCE_DOCUMENT_ID_DUPLICATE: '同一个文档 ID 对应了多份上传文件。',
  SOURCE_INVALID_PDF: '选中的文件没有有效的 PDF 文件头。',
  SOURCE_UNREADABLE: '无法解析原文件，PDF 可能损坏或已加密。',
  SOURCE_NO_TEXT: '原文没有可提取的文本。扫描版 PDF 请先 OCR，再上传文本。',
  SOURCE_TEXT_TOO_LARGE: '单份文献的提取文本超过 200 万字符。',
  SOURCE_INVALID_UTF8: 'TXT 或 Markdown 原文必须使用 UTF-8 编码。',
  SOURCE_FILE_TYPE: '原文文件须为 PDF、TXT 或 Markdown。'
};
function importError(code) {
  if (typeof code === 'string' && /^LITKG_UPSTREAM_HTTP_[1-5]\d{2}$/.test(code)) {
    return importErrors[code] || ('LitKG 导出接口返回 HTTP ' + code.slice(-3) + '。');
  }
  return importErrors[code] || code;
}

async function api(path,data){const r=await fetch(path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const v=await r.json();if(!r.ok)throw Error(importError(v.error)||'请求失败');return v;}
function notice(text,error=false){$('#notice').textContent=text;$('#notice').classList.toggle('error',error);}
async function guarded(fn){try{await fn();}catch(e){notice(e.message,true);}}
function on(id,fn){$(id).addEventListener('click',()=>guarded(fn));}
function score(v){return v===null||v===undefined?'—':Number(v).toFixed(1);}
function badge(text,kind=''){return `<span class="badge ${kind}">${esc(text)}</span>`;}
function switchTab(tab){activeTab=tab;document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('hidden',x.id!==tab));document.querySelectorAll('[data-tab]').forEach(x=>x.classList.toggle('active',x.dataset.tab===tab));if(tab==='audit')guarded(renderAudit);if(tab==='human')guarded(renderSample);}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));
function currentGraph(selector){return graphs.find(g=>g.id===$(selector).value);}
function displayClaim(c){const value=x=>x?.label??x?.value??x?.text??x?.id??'?';return `${value(c.subject)} — ${c.predicate} → ${value(c.object)}`;}
async function refresh(){const oldAudit=$('#audit-graph').value,oldHuman=$('#human-graph').value;graphs=(await api('/api/graphs')).graphs;selected=new Set([...selected].filter(id=>graphs.some(g=>g.id===id)));if(!selected.size)graphs.slice(0,3).forEach(g=>selected.add(g.id));const options=graphs.map(g=>`<option value="${esc(g.id)}">${esc(g.name)} · ${g.claim_count} claims</option>`).join('');document.querySelectorAll('.graph-select').forEach(s=>s.innerHTML=options);if(graphs.some(g=>g.id===oldAudit))$('#audit-graph').value=oldAudit;if(graphs.some(g=>g.id===oldHuman))$('#human-graph').value=oldHuman;renderCompare();if(activeTab==='audit')await renderAudit();if(activeTab==='human')await renderSample();if(poll)clearTimeout(poll);if(graphs.some(g=>['queued','running'].includes(g.latest_run?.status)))poll=setTimeout(()=>guarded(refresh),3500);}
function renderCompare(){const scenario=$('#scenario').value;$('#graphs').innerHTML=graphs.length?graphs.map(g=>`<div class="graph-row"><input type="checkbox" data-graph="${esc(g.id)}" ${selected.has(g.id)?'checked':''}><div><strong>${esc(g.name)}</strong><small>${g.claim_count} claims · ${g.document_count} 文献 · ${esc(g.snapshot_hash.slice(0,10))}</small></div>${badge(g.latest_run?.status||'未评审',g.latest_run?.report?.complete?'':'warning')}</div>`).join(''):'<div class="empty">还没有图谱。先导入一个快照。</div>';$('#graphs').querySelectorAll('[data-graph]').forEach(c=>c.onchange=()=>{c.checked?selected.add(c.dataset.graph):selected.delete(c.dataset.graph);renderCompare();});const chosen=graphs.filter(g=>selected.has(g.id));renderRadar(chosen);$('#scoretable').innerHTML=`<table><thead><tr><th>图谱</th><th>综合分</th><th>暂定分</th><th>已测权重</th>${Object.values(labels).map(l=>`<th>${l}</th>`).join('')}<th>适用性</th></tr></thead><tbody>${chosen.map(g=>{const r=g.latest_run?.report,s=r?.scenarios?.[scenario];return `<tr><td>${esc(g.name)}</td><td>${score(s?.overall)}</td><td>${score(s?.provisional)}</td><td>${s?(100*s.measured_weight).toFixed(0)+'%':'—'}</td>${Object.keys(labels).map(k=>`<td>${score(r?.dimensions?.[k])}</td>`).join('')}<td>${badge(s?.suitability||'待验证',s?.suitability==='适用'?'':s?.suitability==='不适用'?'danger':'warning')}</td></tr>`;}).join('')}</tbody></table>`;const runs=chosen.map(g=>g.latest_run?.report).filter(Boolean);const same=field=>new Set(runs.map(r=>r[field])).size<=1;$('#comparability').textContent=chosen.length<2?'选择多个图谱，可比较相同语料上的不同抽取系统。':`语料一致：${same('corpus_hash')?'是':'否，结果不宜直接排名'}；范围/本体一致：${same('policy_hash')?'是':'否'}；评审模型一致：${same('model')?'是':'否'}；提示词版本一致：${same('prompt_version')?'是':'否'}。相同条件仅表示可比基础，不证明评分已校准。`;}
function renderRadar(chosen){const dims=Object.keys(labels),cx=230,cy=200,r=135;const point=(i,v)=>[cx+Math.sin(i*Math.PI/3)*r*v/100,cy-Math.cos(i*Math.PI/3)*r*v/100];const pts=v=>dims.map((_,i)=>point(i,v).join(',')).join(' ');let svg=`<svg viewBox="0 0 460 400" role="img" aria-label="多图谱六维质量雷达图">${[20,40,60,80,100].map(v=>`<polygon points="${pts(v)}" fill="none" stroke="#dfe9e6"/>`).join('')}${dims.map((k,i)=>{const [x,y]=point(i,124),[ex,ey]=point(i,100);return `<line x1="${cx}" y1="${cy}" x2="${ex}" y2="${ey}" stroke="#e5eeeb"/><text x="${x}" y="${y+4}" text-anchor="middle" font-size="12" fill="#647c80">${labels[k]}</text>`;}).join('')}`;chosen.forEach((g,j)=>{const d=g.latest_run?.report?.dimensions;if(!d)return;const color=colors[j%colors.length];if(dims.every(k=>d[k]!==null&&d[k]!==undefined))svg+=`<polygon points="${dims.map((k,i)=>point(i,d[k]).join(',')).join(' ')}" fill="${color}" fill-opacity=".09" stroke="${color}" stroke-width="2"/>`;else dims.forEach((k,i)=>{const next=(i+1)%6;if(d[k]!=null&&d[dims[next]]!=null){const a=point(i,d[k]),b=point(next,d[dims[next]]);svg+=`<line x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}" stroke="${color}" stroke-width="2" stroke-dasharray="5 4"/>`;}});dims.forEach((k,i)=>{if(d[k]!=null){const [x,y]=point(i,d[k]);svg+=`<circle cx="${x}" cy="${y}" r="4" fill="${color}"><title>${esc(g.name)} · ${labels[k]} ${score(d[k])}</title></circle>`;}});});svg+='</svg>';$('#radar').innerHTML=svg;$('#legend').innerHTML=chosen.map((g,i)=>`<span><span class="dot color-${i%colors.length}"></span>${esc(g.name)}</span>`).join('');}
async function readDocuments() {
  const documents = [], source_files = [];
  const input = $('#source-map').value.trim();
  const mapping = input ? JSON.parse(input) : {};
  if (!mapping || typeof mapping !== 'object' || Array.isArray(mapping)) {
    throw Error('文献 ID 映射须为 JSON 对象。');
  }
  let sourceBytes = 0;
  for (const file of $('#sources').files) {
    if (/\.json$/i.test(file.name)) {
      const value = JSON.parse(await file.text());
      documents.push(...(Array.isArray(value) ? value : value.documents || [value]));
      continue;
    }
    sourceBytes += file.size;
    if (sourceBytes > 32 * 1024 * 1024) throw Error(importErrors.SOURCE_UPLOAD_TOO_LARGE);
    const content = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',', 2)[1]);
      reader.onerror = () => reject(Error('无法读取原文文件。'));
      reader.readAsDataURL(file);
    });
    const item = {filename: file.name, content};
    if (Object.hasOwn(mapping, file.name)) item.document_id = mapping[file.name];
    source_files.push(item);
  }
  return {documents, source_files, source_complete: $('#source-complete').checked};
}
on('#import-json',async()=>{const files=[...$('#files').files],raw=$('#json').value.trim();if(!files.length&&!raw)throw Error('请选择JSON文件或粘贴内容');const {documents,source_files,source_complete}=await readDocuments();const extras={source_files,source_complete};if(documents.length)extras.documents=documents;if($('#scope').value.trim())extras.scope=$('#scope').value.trim();if($('#mapping').value.trim())extras.mapping=JSON.parse($('#mapping').value);if($('#ontology').files[0])extras.ontology=JSON.parse(await $('#ontology').files[0].text());const inputs=files.length?await Promise.all(files.map(async f=>({name:f.name,payload:JSON.parse(await f.text())}))):[{name:'',payload:JSON.parse(raw)}];let count=0;try{for(const input of inputs){await api('/api/graphs',{...extras,payload:input.payload,name:[$('#graph-name').value.trim(),input.name].filter(Boolean).join(' · ')});count++;}}finally{await refresh();}notice(`已创建 ${count} 个图谱快照。`);switchTab('compare');});
on('#load-kbs',async()=>{const v=await api('/api/litkg/knowledge-bases');$('#kb').innerHTML=(v.knowledge_bases||[]).map(k=>`<option value="${Number(k.id)}">${esc(k.name)} · ${Number(k.claim_count||0)} claims</option>`).join('');if(!$('#kb').value)notice('本机 LitKG 暂无知识库。');});
on('#import-local', async () => {
  if(!$('#kb').value)throw Error('请先加载并选择知识库');
  const button = $('#import-local');
  button.disabled = true;
  notice('正在导出快照并关联原文，请等待…');
  try {
    const result=await api('/api/import/litkg',{knowledge_base_id:Number($('#kb').value),name:$('#local-name').value.trim(),scope:$('#local-scope').value.trim()});await refresh();const sources=result.original_sources||{};const attached=sources.attached||0;const missing=Object.values(sources).reduce((total,n)=>total+n,0)-attached;notice("导入成功。已关联原始 PDF："+attached+"；未关联："+missing);switchTab('compare');
  } finally {
    button.disabled = false;
  }
});
async function renderAudit(){const g=currentGraph('#audit-graph');if(!g){$('#audit-result').innerHTML='<p class="muted">请先导入图谱。</p>';return;}const run=g.latest_run,r=run?.report;$('#run-llm').disabled=!config.llm_configured||['queued','running'].includes(run?.status);$('#run-auto').disabled=['queued','running'].includes(run?.status);$('#llm-note').textContent=config.llm_configured?`模型：${config.model}；所有调用只在点击评审后开始。`:'尚未配置 LLM：可进行本地本体检测与人工评审。';$('#audit-result').innerHTML=run?`${badge(run.status)} ${r?.gate?badge(r.gate,r.gate==='PASS'?'':'warning'):''}<div class="stats"><div><span class="score">${r?.metrics?.reviewed??0}</span><small>已审 / ${g.claim_count} 条</small></div><div><span class="score">${score(r?.dimensions?.correctness)}</span><small>正确性：严格支持率</small></div><div><span class="score">${r?.ontology?.violations??0}</span><small>形式约束问题</small></div></div>${run.error?`<p>${esc(run.error)}</p>`:''}${r?.errors?.length?`<details><summary>${r.errors.length} 个未完成项 / 错误</summary><pre>${esc(JSON.stringify(r.errors,null,2))}</pre></details>`:''}`:'<p class="muted">尚未运行评审。</p>';$('#findings').innerHTML=r?`<div class="scroll"><table><thead><tr><th>Claim / 对象</th><th>判定</th><th>理由</th></tr></thead><tbody>${(r.assessments||[]).map(a=>`<tr><td>${esc(a.claim_id)}</td><td>${badge(verdicts[a.verdict]||a.verdict,a.verdict==='SUPPORTED'?'':'warning')}</td><td class="wrap">${esc(a.rationale)}<details><summary>证据 ${a.evidence?.length||0} 段</summary><pre>${esc(JSON.stringify(a.evidence,null,2))}</pre></details></td></tr>`).join('')}${(r.ontology?.findings||[]).map(f=>`<tr><td>${esc(f.target)}</td><td>${badge(f.rule,'danger')}</td><td class="wrap">${esc(f.message)}</td></tr>`).join('')}</tbody></table></div>`:'';$('#omissions').innerHTML=r?`<pre>${esc(JSON.stringify(r.proxies||{},null,2))}</pre><div class="scroll"><table><thead><tr><th>候选</th><th>匹配</th><th>理由</th></tr></thead><tbody>${(r.matches||[]).map(m=>{const c=r.candidates.find(c=>c.id===m.candidate_id);return `<tr><td class="wrap">${esc(c?`${c.subject} — ${c.predicate} → ${c.object}`:m.candidate_id)}<details><summary>来源证据</summary><pre>${esc(JSON.stringify(c?.evidence||[],null,2))}</pre></details></td><td>${esc(m.status)}</td><td class="wrap">${esc(m.rationale)}</td></tr>`;}).join('')}</tbody></table></div><details><summary>实体合并 / 拆分疑点与文献覆盖</summary><pre>${esc(JSON.stringify({identity_issues:r.identity_issues,coverage:r.document_coverage},null,2))}</pre></details>`:'';}
for(const [id,mode] of [['#run-auto','automatic'],['#run-llm','llm']])on(id,async()=>{const g=currentGraph('#audit-graph');if(!g)throw Error('请先导入图谱');await api(`/api/graphs/${g.id}/runs`,{mode});notice(mode==='llm'?'原文评审已排队，可切换页面；进度与结果会保存。':'本地约束检测已开始。');await refresh();});
on('#fork',async()=>{const g=currentGraph('#audit-graph');if(!g)throw Error('请先选择图谱');const old=(await api(`/api/graphs/${g.id}`)).graph;const scope=$('#fork-scope').value.trim()||old.policy.scope;if(!scope)throw Error('请填写抽取范围');const payload={graph:{nodes:old.nodes,claims:old.claims},documents:old.documents,policy:{...old.policy,scope},metadata:{...old.metadata,parent_snapshot:old.snapshot_hash}};if($('#fork-complete').checked)payload.documents.forEach(d=>d.parsing_status='complete');await api('/api/graphs',{name:g.name+' · 补充核验',payload});await refresh();notice('已创建补充核验快照；请对新快照重新评审。');});
on('#export',async()=>{const g=currentGraph('#audit-graph');if(!g)throw Error('请先选择图谱');const v=await api(`/api/graphs/${g.id}/export`);const blob=new Blob([JSON.stringify(v,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`quality-${g.id}.json`;a.click();URL.revokeObjectURL(url);});
async function renderSample(){const g=currentGraph('#human-graph');if(!g)return;const state=await api(`/api/graphs/${g.id}/sample`);if(!state.sample){$('#sample-info').textContent='尚未创建样本。';$('#sample-list').innerHTML='';$('#claim-detail').innerHTML='';activeClaim=null;return;}const m=state.metrics;$('#sample-info').innerHTML=`<p class="muted">固定样本 ${state.sample.claim_ids.length} 条 · 已评 ${m.reviewed} · 未定 ${m.uncertain} · 严格支持率 ${m.strict_support_rate==null?'—':(m.strict_support_rate*100).toFixed(1)+'%'} · Wilson 95% ${m.wilson_95?m.wilson_95.map(v=>(v*100).toFixed(1)+'%').join(' – '):'—'}</p><details><summary>抽样与评审历史</summary><pre>${esc(JSON.stringify({sample:state.sample,history:state.history},null,2))}</pre></details>`;$('#sample-list').innerHTML=state.sample.claim_ids.map((id,i)=>{const review=state.reviews.find(r=>r.claim_id===id);return `<button data-claim="${esc(id)}" class="${activeClaim===id?'selected':''}">${i+1}. ${esc(id)}${review?' ✓':''}</button>`;}).join('');$('#sample-list').querySelectorAll('[data-claim]').forEach(b=>b.onclick=()=>guarded(()=>showClaim(g.id,b.dataset.claim)));}
async function showClaim(gid,cid){activeClaim=cid;const v=await api(`/api/graphs/${gid}/claims/${encodeURIComponent(cid)}`);$('#claim-detail').innerHTML=`<div class="claim-label">${esc(displayClaim(v.claim))}</div><details><summary>完整声明与限定条件</summary><pre>${esc(JSON.stringify(v.claim,null,2))}</pre></details>${v.evidence.length?v.evidence.map(e=>{const text=e.block?.text,ref=e.reference,a=e.anchor;const highlighted=text===undefined?esc(ref.quote||ref.sentence||'未导入原文'):a?esc(text.slice(0,a.start))+'<mark>'+esc(text.slice(a.start,a.end))+'</mark>'+esc(text.slice(a.end)):esc(text);return `<div class="evidence-block">${badge(e.verified?'锚点已校验':'未定位','warning')} <span class="muted">文献 ${esc(a?.doc_id||ref.doc_id||ref.document_id||e.block?.doc_id)} · 页 ${esc(ref.page||e.block?.page||'未知')} · 块 ${esc(a?.block_id||ref.block_id||ref.chunk_id||e.block?.id)} · ${esc(ref.section||e.block?.section||'')}${e.block?.source_origin?' · 原文 '+esc(e.block.source_filename||''):''}</span><div class="source">${highlighted}</div>${e.warning?`<p class="muted">${esc(e.warning)}</p>`:''}</div>`;}).join(''):'<p class="muted">缺少原文。可标为“证据不足”，或补齐来源后创建新快照。</p>'}<div class="review-form"><label>人工判定<select id="human-verdict"><option value="supported">正确：原文完整支持</option><option value="partial">部分正确：存在限定条件或语义偏移</option><option value="incorrect">错误：原文不支持或明确反证</option><option value="uncertain">证据不足 / 无法判定</option></select></label><label>评审依据<textarea id="human-note" maxlength="6000" placeholder="记录证据与判定理由"></textarea></label><button id="save-review" class="primary">保存人工判定</button></div>`;on('#save-review',async()=>{await api(`/api/graphs/${gid}/reviews`,{claim_id:cid,reviewer:$('#reviewer').value,verdict:$('#human-verdict').value,note:$('#human-note').value});notice('人工判定已保存，历史记录保留。');await renderSample();});await renderSample();}
on('#sample',async()=>{const g=currentGraph('#human-graph');if(!g)throw Error('请先选择图谱');await api(`/api/graphs/${g.id}/sample`,{adaptive:true});await renderSample();notice('随机样本已固定；再次点击会读取同一样本。');});
$('#audit-graph').onchange=()=>guarded(renderAudit);$('#human-graph').onchange=()=>{activeClaim=null;$('#claim-detail').innerHTML='';guarded(renderSample);};$('#scenario').onchange=renderCompare;on('#refresh',refresh);
guarded(async()=>{config=await api('/api/config');$('#connection').textContent=config.llm_configured?'模型已配置':'本地检测可用';$('#scenario').innerHTML=Object.entries(config.scenarios).map(([k,v])=>`<option value="${k}">${esc(v.name)}</option>`).join('');$('#scenario').value='field_review';await refresh();});
