"""Self-contained HTML, with all trace-derived text inserted as text, not markup."""

import json


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy"
 content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'">
<title>AKS boot observatory</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--card:#152036;--line:#293a55;--text:#e7edf7;--muted:#9bb0cb;--cyan:#59d9dc;--purple:#b99cff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,sans-serif}
main{max-width:1450px;margin:auto;padding:32px}h1{font-size:30px;margin:6px 0}h2{font-size:18px}
.eyebrow{color:var(--cyan);font-size:12px;letter-spacing:2px}.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin:26px 0}
.card,section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:18px}
.card strong{display:block;font-size:28px;margin-top:8px}.toolbar{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
input,select,button{background:var(--bg);color:var(--text);border:1px solid #49617e;border-radius:6px;padding:8px}
input[type=range]{width:180px}button{cursor:pointer}canvas{width:100%;display:block;margin-top:16px;touch-action:pan-y}
.scroll{overflow:auto;max-height:520px}table{border-collapse:collapse;width:100%;white-space:nowrap}
th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line)}
th{position:sticky;top:0;background:var(--card);color:var(--muted);cursor:pointer}
tbody tr:hover{background:#20314c}li{margin:9px 0}.warning{border-left:3px solid #efb668;padding-left:16px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;color:var(--muted)}.legend{font-size:12px;margin-top:12px;color:var(--muted)}
.badge{padding:4px 8px;border-radius:5px;background:#243854;display:inline-block;margin:6px 4px 0 0}
@media(max-width:600px){main{padding:14px}.card,section{padding:12px}}
</style></head><body><main>
<header><div class="eyebrow">NODE LIFECYCLE / BOOT OBSERVATORY</div>
<h1>From Linux boot to NodeReady</h1>
<p class="muted">Measured activity, optimization candidates, and explicit coverage limits. No external assets or telemetry.</p>
<div id="identity"></div></header>
<div class="cards" id="cards"></div>
<section><h2>Boot timeline</h2><div class="toolbar">
<label>Capture <select id="capture"></select></label>
<label>Filter <input id="filter" placeholder="kubelet, containerd, thread ID..."></label>
<label>From <input id="from" type="number" min="0" step="0.1" value="0" style="width:95px"> s</label>
<label>To <input id="to" type="number" min="0" step="0.1" style="width:95px"> s</label>
<button id="reset">Reset view</button></div>
<canvas id="timeline" aria-label="Boot timeline; equivalent timings are available in the tables below"></canvas>
<div class="legend">Cyan: service activation. Purple: observed CPU occupancy per thread (100 ms bins).
Dashed lines: imported milestones and Ready. Top CPU threads only; use the filter to inspect others.
Empty space is not proof of inactivity. Service activation is not application readiness.</div>
<p id="detail" class="muted"></p></section>
<section><h2>Optimization candidates: observed thread activity</h2>
<p class="muted">Sort by CPU or runnable wait to investigate costly work and contention. These are measurements, not automatic causal conclusions.
Thread lifetimes, boot phases and multi-CPU execution must not be added together.</p>
<p class="muted">Observed span is last minus first thread observation, not its complete lifetime. Orange shows block activity, not request latency.</p>
<p class="muted">Table: up to 2,000 matching threads. Timeline: 40 highest-CPU matching threads. Full accounting is retained in report.json.</p>
<div class="scroll"><table><thead><tr id="thread-head"></tr></thead><tbody id="threads"></tbody></table></div></section>
<section><h2>Services and semantic milestones</h2><div class="scroll"><table>
<thead><tr><th>Name</th><th>Start / time (s)</th><th>Activation (s)</th><th>Evidence</th></tr></thead><tbody id="services"></tbody></table></div></section>
<section id="comparison" hidden><h2>Capture comparison</h2>
<p class="muted">Delta = candidate minus baseline. Negative means shorter, not necessarily a statistically established improvement.
Different tracing backends can have different overhead and coverage.</p>
<div id="compatibility"></div><div class="scroll"><table><thead><tr><th>Measurement</th><th>Baseline (s)</th><th>Candidate (s)</th><th>Delta (s)</th></tr></thead><tbody id="deltas"></tbody></table></div></section>
<section><h2>Coverage and interpretation</h2><ul class="warning" id="warnings"></ul>
<details><summary>Capture metadata and event counts</summary><pre id="metadata"></pre></details></section>
</main><script id="payload" type="application/json">__PAYLOAD__</script>
<script>
"use strict";
const data=JSON.parse(document.getElementById("payload").textContent), $=id=>document.getElementById(id);
let current=0, sortKey="cpu_seconds", ascending=false, hitboxes=[];
const fmt=x=>Number.isFinite(x)?x.toFixed(3):"unknown";
const cell=(row,text)=>{const td=document.createElement("td");td.textContent=text;row.append(td);};
const row=(body,values)=>{const tr=document.createElement("tr");values.forEach(x=>cell(tr,x));$(body).append(tr);return tr;};
const limit=r=>r.ready_seconds??Math.max(r.trace_last,...r.services.map(s=>s.end),1);
function visibleThreads(r){const f=$("filter").value.toLowerCase();return r.threads.filter(t=>(t.comm+" "+t.tid).toLowerCase().includes(f));}
function cards(){
 $("cards").replaceChildren();
 data.captures.forEach((r,i)=>{
  for(const [label,value] of [[`${data.labels[i]} · boot → Ready`,fmt(r.ready_seconds)+" s"],
   ["First trace event",fmt(r.trace_first)+" s"],["Observed block events",String(r.block_event_count)]]){
    const box=document.createElement("div");box.className="card";
    const labelNode=document.createElement("span");labelNode.className="muted";labelNode.textContent=label;
    const valueNode=document.createElement("strong");valueNode.textContent=value;box.append(labelNode,valueNode);$("cards").append(box);
  }
 });
}
function draw(){
 const r=data.captures[current],canvas=$("timeline"),ctx=canvas.getContext("2d");
 const from=Number($("from").value),to=Number($("to").value);
 if(!Number.isFinite(from)||!Number.isFinite(to)||to<=from){$("detail").textContent="Choose a finite interval with To > From.";return;}
 const filter=$("filter").value.toLowerCase(),services=r.services.filter(s=>s.name.toLowerCase().includes(filter));
 const threads=visibleThreads(r).slice().sort((a,b)=>b.cpu_seconds-a.cpu_seconds).slice(0,40);
 const width=Math.max(300,canvas.clientWidth),left=Math.min(240,width*0.35),height=85+(services.length+threads.length)*25;
 const ratio=window.devicePixelRatio||1;canvas.width=width*ratio;canvas.height=height*ratio;canvas.style.height=height+"px";ctx.scale(ratio,ratio);
 ctx.clearRect(0,0,width,height);ctx.font="12px system-ui";hitboxes=[];
 const x=t=>left+(t-from)/(to-from)*(width-left-20);
 ctx.strokeStyle="#30415b";ctx.fillStyle="#9bb0cb";
 for(let i=0;i<=6;i++){const t=from+(to-from)*i/6;ctx.fillText(t.toFixed(2)+"s",x(t),16);ctx.beginPath();ctx.moveTo(x(t),23);ctx.lineTo(x(t),height);ctx.stroke();}
 let y=45;
 function bar(label,start,end,color,alpha,detail){
  if(end<from||start>to)return;
  const a=x(Math.max(start,from)),b=x(Math.min(end,to));ctx.globalAlpha=alpha;ctx.fillStyle=color;
  ctx.fillRect(a,y-10,Math.max(1,b-a),14);ctx.globalAlpha=1;
  hitboxes.push({x:a,y:y-12,w:Math.max(5,b-a),h:20,detail});
 }
 for(const s of services){ctx.fillStyle="#e7edf7";ctx.fillText(s.name.slice(0,30),0,y);bar(s.name,s.start,s.end,"#59d9dc",0.85,`${s.name}: ${fmt(s.start)}–${fmt(s.end)}s; ${s.source}`);y+=25;}
 ctx.fillStyle="#e7edf7";ctx.fillText("Block I/O events (not latency)",0,y);
 for(const [start,counts] of r.io_bins??[]){bar("I/O",start,start+0.1,"#efb668",0.8,`Block events at ${fmt(start)}s: ${JSON.stringify(counts)}; no thread attribution`);}y+=25;
 for(const t of threads){ctx.fillStyle="#e7edf7";ctx.fillText(`${t.comm} [${t.tid}/${t.generation}]`.slice(0,32),0,y);
  for(const [start,cpu] of t.cpu_bins)bar(t.comm,start,start+0.1,"#b99cff",Math.max(0.25,Math.min(1,cpu/0.1)),`${t.comm} [${t.tid}]: ${fmt(cpu)} CPU seconds in bin ${fmt(start)}s`);
  y+=25;
 }
 ctx.setLineDash([4,4]);
 for(const m of r.milestones){if(m.t<from||m.t>to)continue;ctx.strokeStyle=m.name.includes("Ready")?"#efb668":"#7598bb";ctx.beginPath();ctx.moveTo(x(m.t),24);ctx.lineTo(x(m.t),height);ctx.stroke();
  hitboxes.push({x:x(m.t)-3,y:24,w:6,h:height-24,detail:`${m.name} @ ${fmt(m.t)}s; ${m.source}`});}
 ctx.setLineDash([]);
}
function refresh(){
 const r=data.captures[current];$("threads").replaceChildren();
 const threads=visibleThreads(r).slice().sort((a,b)=>{
  const av=a[sortKey],bv=b[sortKey],v=typeof av==="string"?av.localeCompare(bv):av-bv;return ascending?v:-v;
 });
 for(const t of threads.slice(0,2000))row("threads",[t.comm,`${t.tid}/${t.generation}`,fmt(t.observed_span_seconds),fmt(t.cpu_seconds),fmt(t.runnable_seconds),t.switches]);
 $("services").replaceChildren();
 r.services.forEach(s=>row("services",[s.name,fmt(s.start),fmt(s.end-s.start),s.source]));
 r.milestones.forEach(m=>row("services",[m.name,fmt(m.t),"—",m.source]));
 $("warnings").replaceChildren();
 for(const text of r.warnings){const li=document.createElement("li");li.textContent=text;$("warnings").append(li);}
 $("metadata").textContent=JSON.stringify({metadata:r.metadata,event_counts:r.event_counts,trace_first:r.trace_first,trace_last:r.trace_last,clock_sample_uncertainty_seconds:r.clock_sample_uncertainty_seconds},null,2);
 draw();
}
for(const [label,key] of [["Thread","comm"],["TID/generation","tid"],["Observed span (s)","observed_span_seconds"],["CPU (s)","cpu_seconds"],["Runnable wait (s)","runnable_seconds"],["Switch-outs","switches"]]){
 const th=document.createElement("th"),button=document.createElement("button");button.textContent=label;button.onclick=()=>{ascending=sortKey===key?!ascending:false;sortKey=key;refresh();};th.append(button);$("thread-head").append(th);
}
data.captures.forEach((r,i)=>{const option=document.createElement("option");option.value=i;option.textContent=data.labels[i]+" · "+r.metadata.backend;$("capture").append(option);
 const badge=document.createElement("span");badge.className="badge";badge.textContent=r.metadata.backend+" / "+r.metadata.kernel+" / "+r.metadata.boot_id;$("identity").append(badge);
});
function reset(){const r=data.captures[current];$("from").value=0;$("to").value=limit(r).toFixed(3);refresh();}
$("capture").onchange=()=>{current=Number($("capture").value);reset();};
["filter","from","to"].forEach(id=>$(id).addEventListener("input",refresh));$("reset").onclick=reset;
$("timeline").onclick=event=>{const rect=event.currentTarget.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;
 const hit=hitboxes.find(b=>x>=b.x&&x<=b.x+b.w&&y>=b.y&&y<=b.y+b.h);$("detail").textContent=hit?hit.detail:"Select an activity bar or milestone to inspect its evidence.";};
window.addEventListener("resize",draw);
if(data.captures.length===2){
 $("comparison").hidden=false;const [a,b]=data.captures;
 const fields=["backend","kernel","architecture","image_label"],notes=[];
 for(const field of fields)if(a.metadata[field]!==b.metadata[field])notes.push(`${field} differs: ${a.metadata[field]} / ${b.metadata[field]}`);
 for(const field of ["vm_size","node_image_version","network_plugin","network_policy","network_dataplane","os_sku","kubernetes_version","canonical_base_version","cluster_resource_id"]){
  const av=a.readiness?.configuration?.[field],bv=b.readiness?.configuration?.[field];
  if(av==null||bv==null||av===""||bv==="")notes.push(`${field}: comparability unknown`);else if(av!==bv)notes.push(`${field} differs: ${av} / ${bv}`);
 }
 $("compatibility").textContent=notes.length?notes.join("; "):"Recorded configuration fields match. External load and run-to-run variance still need review.";
 function delta(name,av,bv){row("deltas",[name,fmt(av),fmt(bv),fmt(Number.isFinite(av)&&Number.isFinite(bv)?bv-av:null)]);}
 delta("Linux boot → Ready",a.ready_seconds,b.ready_seconds);
 const am=new Map(a.services.map(s=>[s.name,s.end-s.start])),bm=new Map(b.services.map(s=>[s.name,s.end-s.start]));
 for(const name of new Set([...am.keys(),...bm.keys()]))delta(name+" activation",am.get(name),bm.get(name));
 function group(r){const result=new Map();for(const t of r.threads){const v=result.get(t.comm)??{cpu:0,wait:0};v.cpu=Number.isFinite(v.cpu)&&Number.isFinite(t.cpu_seconds)?v.cpu+t.cpu_seconds:null;v.wait=Number.isFinite(v.wait)&&Number.isFinite(t.runnable_seconds)?v.wait+t.runnable_seconds:null;result.set(t.comm,v);}return result;}
 const ag=group(a),bg=group(b),names=[...new Set([...ag.keys(),...bg.keys()])];
 names.sort((x,y)=>(ag.get(y)?.cpu??0)+(bg.get(y)?.cpu??0)-(ag.get(x)?.cpu??0)-(bg.get(x)?.cpu??0));
 for(const name of names.slice(0,50)){
  delta(`CPU sum · threads named ${name}`,ag.get(name)?.cpu,bg.get(name)?.cpu);
  delta(`Runnable sum · threads named ${name}`,ag.get(name)?.wait,bg.get(name)?.wait);
 }
}
cards();reset();
</script></body></html>
"""


def write_html(path, captures, labels):
    payload = json.dumps(
        {"captures": captures, "labels": labels}, ensure_ascii=True, allow_nan=False,
    ).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    path.write_text(PAGE.replace("__PAYLOAD__", payload), encoding="utf-8")
