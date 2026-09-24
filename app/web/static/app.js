/* ═══ CBH · Agentes de IA — utilidades de interfaz ═══ */
const $ = (s, el=document) => el.querySelector(s);
const $$ = (s, el=document) => [...el.querySelectorAll(s)];

function toast(msg, ms=3500){ const t=$('#toast'); if(!t) return; t.textContent=msg; t.style.display='block'; clearTimeout(t._h); t._h=setTimeout(()=>t.style.display='none', ms); }
async function api(url, method='GET', body=null){
  const r = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body: body?JSON.stringify(body):null});
  let data=null; try{ data = await r.json(); }catch(e){}
  if(!r.ok){ throw new Error((data&&data.error)||(data&&data.detail)||('HTTP '+r.status)); }
  return data;
}
/* Markdown mínimo, sin dependencias externas: títulos, listas, tablas, negritas, código, enlaces, citas */
function inline(s){
  s = esc(s);
  s = s.replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/(^|[^*])\*(?!\s)(.+?)\*(?!\*)/g,'$1<i>$2</i>');
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|\/[^\s)]*)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  return s;
}
function md(txt){
  if(!txt) return '';
  const lines = String(txt).replace(/\r/g,'').split('\n'); let out=[], i=0, list=null, para=[];
  const flush=()=>{ if(para.length){ out.push('<p>'+inline(para.join(' '))+'</p>'); para=[]; } };
  const closeList=()=>{ if(list){ out.push(list==='ul'?'</ul>':'</ol>'); list=null; } };
  while(i<lines.length){
    let l=lines[i];
    if(/^```/.test(l)){ flush(); closeList(); let code=[]; i++; while(i<lines.length && !/^```/.test(lines[i])){ code.push(lines[i]); i++; } out.push('<pre>'+esc(code.join('\n'))+'</pre>'); i++; continue; }
    if(/^\s*$/.test(l)){ flush(); closeList(); i++; continue; }
    let h=l.match(/^(#{1,4})\s+(.*)/); if(h){ flush(); closeList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i++; continue; }
    if(/^\s*\|/.test(l)){ flush(); closeList(); let rows=[]; while(i<lines.length && /^\s*\|/.test(lines[i])){ rows.push(lines[i]); i++; }
      const cells=r=>r.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());
      let body=rows.filter(r=>!/^\s*\|?\s*:?-{2,}/.test(r));
      if(body.length){ out.push('<table><thead><tr>'+cells(body[0]).map(c=>'<th>'+inline(c)+'</th>').join('')+'</tr></thead><tbody>'+body.slice(1).map(r=>'<tr>'+cells(r).map(c=>'<td>'+inline(c)+'</td>').join('')+'</tr>').join('')+'</tbody></table>'); }
      continue; }
    let ul=l.match(/^\s*[-*•]\s+(.*)/), ol=l.match(/^\s*\d+[.)]\s+(.*)/);
    if(ul||ol){ flush(); const t=ul?'ul':'ol'; if(list!==t){ closeList(); out.push(t==='ul'?'<ul>':'<ol>'); list=t; } out.push('<li>'+inline((ul||ol)[1])+'</li>'); i++; continue; }
    if(/^\s*>/.test(l)){ flush(); closeList(); out.push('<blockquote>'+inline(l.replace(/^\s*>\s?/,''))+'</blockquote>'); i++; continue; }
    if(/^\s*(-{3,}|\*{3,})\s*$/.test(l)){ flush(); closeList(); out.push('<hr>'); i++; continue; }
    closeList(); para.push(l.trim()); i++;
  }
  flush(); closeList(); return out.join('\n');
}
function esc(s){ return String(s??'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function mxn(v){ return '$'+Number(v||0).toLocaleString('es-MX',{minimumFractionDigits:2,maximumFractionDigits:2}); }
function num(v,d=0){ return Number(v||0).toLocaleString('es-MX',{minimumFractionDigits:d,maximumFractionDigits:d}); }
document.addEventListener('DOMContentLoaded', ()=>{ $$('.md[data-md]').forEach(el=>{ el.innerHTML = md(el.getAttribute('data-md')); }); });

/* ejecutar agente en segundo plano mostrando su progreso paso a paso */
const NOMBRES = {consumo:'Agente · Consumo', demanda:'Agente · Abasto', briefing:'Briefing', vigilancia:'Vigilancia'};
function pintarProgreso(t){
  const box=$('#progreso'); if(!box) return; box.style.display='block';
  $$('.pulso').forEach(p=>{ if(t.estado==='ejecutando'){ p.classList.add('run'); } else { p.classList.remove('run'); } });
  if(t.estado!=='ejecutando'){ setTimeout(()=>{ box.style.display='none'; }, 6000); }
  $('#progreso-titulo').textContent = (NOMBRES[t.agente]||t.agente)+' · '+(t.estado==='ejecutando'?'trabajando…':t.estado)+' · '+t.segundos+' s';
  const ol=$('#progreso-pasos'); ol.innerHTML='';
  (t.pasos||[]).forEach((p,i,arr)=>{ const li=document.createElement('li'); li.className=(i===arr.length-1&&t.estado==='ejecutando')?'now':''; li.innerHTML=(i<arr.length-1||t.estado!=='ejecutando'?'✓ ':'⏳ ')+esc(p.paso)+(p.detalle?' <span class="d">· '+esc(p.detalle)+'</span>':'')+' <span class="d">('+p.t+' s)</span>'; ol.appendChild(li); });
}
async function ejecutarAgente(agente, btn, opts={}){
  if(btn){ btn.classList.add('loading'); btn.disabled=true; }
  try{
    const {trabajo} = await api(`/api/agentes/${agente}/ejecutar`, 'POST', opts);
    let t;
    for(let i=0;i<900;i++){
      await new Promise(r=>setTimeout(r, 1500));
      t = await api(`/api/trabajos/${trabajo}`); pintarProgreso(t);
      if(t.estado!=='ejecutando') break;
    }
    if(t.estado==='ok'){ toast('Listo: '+((t.resultado&&t.resultado.resumen)||'terminado'), 5000); setTimeout(()=>location.reload(), 1200); }
    else { toast('Error: '+t.error, 9000); }
  }catch(e){ toast('Error: '+e.message, 8000); }
  finally{ if(btn){ btn.classList.remove('loading'); btn.disabled=false; } }
}
async function vigilarTrabajos(){ try{ const e=await api('/api/estado'); const t=(e.trabajos||[])[0]; if(t){ const d=await api('/api/trabajos/'+t.id); pintarProgreso(d); setTimeout(vigilarTrabajos, 2000);} }catch(err){} }
document.addEventListener('DOMContentLoaded', vigilarTrabajos);
async function preguntar(texto){ if(!texto.trim()) return; const r=await api('/api/conversaciones','POST'); sessionStorage.setItem('pregunta_'+r.id, texto); location.href='/copiloto?c='+r.id; }
async function resolverCaso(id, estado){ const res = prompt(estado==='resuelto'?'¿Qué se encontró y qué se hizo? (esto entrena a los agentes)':'Motivo:')??null; if(res===null) return;
  try{ await api(`/api/casos/${id}/resolver`,'POST',{estado, resolucion:res}); toast('Caso actualizado'); setTimeout(()=>location.reload(),700);}catch(e){ toast('Error: '+e.message,8000);} }
async function accion(id, op, nota){
  try{ const r = await api(`/api/acciones/${id}/${op}`, 'POST', nota!==undefined?{nota}:null);
       toast(`Acción #${id}: ${r.estado}${r.error?(' · '+r.error):''}${r.mensaje?(' · '+r.mensaje):''}`, 6000);
       setTimeout(()=>location.reload(), 900);
  }catch(e){ toast('Error: '+e.message, 8000); }
}
async function aprobarSeleccion(){
  const ids = $$('input.sel:checked').map(i=>+i.value); if(!ids.length){ toast('Selecciona al menos una acción.'); return; }
  try{ const r = await api('/api/acciones/aprobar-varias','POST',{ids}); const ok=r.filter(x=>x.estado==='ejecutada').length;
       toast(`${ok} de ${ids.length} acciones ejecutadas en Odoo.`, 6000); setTimeout(()=>location.reload(), 900);
  }catch(e){ toast('Error: '+e.message, 8000); }
}
async function clasificar(id, estado){
  const nota = prompt(`Nota para marcar el hallazgo #${id} como "${estado}" (opcional):`) ?? '';
  try{ await api(`/api/hallazgos/${id}/clasificar`,'POST',{estado, nota}); toast('Guardado. El agente aprenderá de esta clasificación.'); setTimeout(()=>location.reload(), 700); }
  catch(e){ toast('Error: '+e.message, 8000); }
}

// ── chat global: disponible en todas las pantallas, con el contexto de lo que se está viendo ──
const CG_SUG = {
  hoy: ['¿Qué es lo más urgente hoy?', '¿Qué decisiones esperan y en qué orden?', '¿Qué casos importan más?'],
  decisiones: ['¿En qué orden conviene decidir?', 'Explícame la primera propuesta con cifras', '¿Cuáles no llegan a tiempo?'],
  casos: ['¿Qué caso reviso primero?', 'Diferencia entre severidad y confianza', '¿Cuáles tienen aclaraciones pendientes?'],
  hallazgos: ['¿Qué hallazgos son patrones y cuáles aislados?', '¿Cuánto importe está sujeto a revisión?'],
  excel: ['Hazme un Excel del consumo por hospital de los últimos 30 días', 'Excel de existencias con lotes por caducar'],
  configuracion: ['¿Qué significa cada nivel de autonomía?', '¿Qué políticas limitan las propuestas?'],
  bitacora: ['¿Qué falló hoy?', '¿Cuánto llevamos de tokens este mes?'],
  consumo: ['¿Qué encontraste hoy?', '¿Cuánto importe está sujeto a revisión y en qué unidades?', '¿Qué patrón te preocupa más?'],
  demanda: ['¿Por qué propones esa transferencia?', '¿Qué pasa si el proveedor se retrasa 5 días?', '¿Qué compras no llegan antes del quiebre?'],
  caso: ['¿Qué verifico primero?', '¿Qué hecho pesa más y por qué?', 'El médico confirma que fue una cirugía de 5 horas: ¿cambia tu conclusión?'],
};
function cgCtx(){ const b=document.getElementById('chat-global'); return b?JSON.parse(b.dataset.contexto||'{}'):{}; }
function cgClave(){ const c=cgCtx(); return c.caso_id?'caso':(c.agente||c.pagina||'hoy'); }
function cgAdd(rol, html){ const d=document.createElement('div'); d.className='msg '+rol; d.innerHTML=html; const m=document.getElementById('cg-mensajes'); m.appendChild(d); m.scrollTop=1e9; }
let cgCargado=false;
async function chatGlobalToggle(){
  const box=document.getElementById('chat-global'); if(!box) return; box.hidden=!box.hidden;
  if(!box.hidden && !cgCargado){ cgCargado=true; const c=cgCtx(); const sug=document.getElementById('cg-sug'); sug.innerHTML='';
    (CG_SUG[cgClave()]||[]).forEach(t=>{ const b=document.createElement('button'); b.textContent=t; b.onclick=()=>{ document.getElementById('cg-texto').value=t; document.getElementById('cg-texto').focus(); }; sug.appendChild(b); });
    try{ const q=new URLSearchParams(c.caso_id?{caso_id:c.caso_id}:(c.agente?{agente:c.agente}:{pagina:c.pagina||'general'})); const h=await api('/api/chat/contexto?'+q);
      if(!(h.mensajes||[]).length) cgAdd('bot','<div class="md">Estoy viendo lo mismo que tú en esta pantalla. Pregúntame por cifras, propuestas o casos; si me das contexto nuevo, lo registro con tu nombre.</div>');
      (h.mensajes||[]).slice(-12).forEach(m=>cgAdd(m.rol==='user'?'user':'bot', m.rol==='user'?esc(m.texto):'<div class="md">'+md(m.texto)+'</div>')); }catch(e){}
    document.getElementById('cg-texto').addEventListener('keydown', e=>{ if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); chatGlobalEnviar(); } });
  }
  if(!box.hidden) document.getElementById('cg-texto').focus();
}
async function chatGlobalEnviar(){
  const ta=document.getElementById('cg-texto'); const t=ta.value.trim(); if(!t) return; ta.value=''; cgAdd('user', esc(t));
  const btn=document.getElementById('cg-enviar'); btn.disabled=true; btn.classList.add('loading'); document.getElementById('cg-pensando').style.display='block';
  try{ const r=await api('/api/chat','POST',{texto:t, contexto:cgCtx()}); if(r.interlocutor) document.getElementById('cg-titulo').textContent=r.interlocutor;
    let tools=''; if(r.herramientas&&r.herramientas.length){ tools='<div class="tools">🛠 '+r.herramientas.map(h=>esc(h.nombre)).join(' · ')+'</div>'; }
    cgAdd('bot','<div class="md">'+md(r.texto||'No obtuve una respuesta redactada. Vuelve a intentarlo o reformula la pregunta.')+'</div>'+tools); }
  catch(e){ cgAdd('bot','<div class="md" style="color:var(--bad)">Error: '+esc(e.message)+'</div>'); }
  finally{ btn.disabled=false; btn.classList.remove('loading'); document.getElementById('cg-pensando').style.display='none'; }
}
async function registrarAclaracion(cid){
  const texto=document.getElementById('acl-texto').value.trim(); const alcance=document.getElementById('acl-alcance').value; if(!texto){ toast('Escribe la aclaración.'); return; }
  try{ await api(`/api/casos/${cid}/aclaracion`,'POST',{texto, alcance}); toast('Aclaración registrada como declaración (no verificada).'); setTimeout(()=>location.reload(),700); }catch(e){ toast('Error: '+e.message,8000); }
}
async function verificarAclaracion(cid, aid){ try{ await api(`/api/casos/${cid}/aclaracion/${aid}/verificar`,'POST'); toast('Marcada como verificada.'); setTimeout(()=>location.reload(),600);}catch(e){ toast('Error: '+e.message,8000);} }
// v1.3.7 · tarjetas resumidas: «Ver más» despliega el detalle completo del caso
function verMas(b){ const d=b.closest('.caso').querySelector('.detalle-caso'); if(!d) return; d.hidden=!d.hidden; b.textContent = d.hidden? 'Ver más ▾' : 'Ver menos ▴'; }
