'use strict';
const $=id=>document.getElementById(id), fmt=new Intl.NumberFormat('pt-BR');
const canvas=$('network'), ctx=canvas.getContext('2d',{alpha:false});
let nodes=[],edges=[],byId=new Map(),adj=new Map(),ranked=[],selected=null,hovered=null,scale=1,panX=0,panY=0,baseScale=1,cw=0,ch=0,dpr=1,drag=null,moved=false,threshold=0,minPR=0,maxPR=1,visibleCount=0,spread=1,showLabels=true,labelCount=15;
const background='#fafcf9';
function rank(n){return Number(n.attributes?.PageRank||0)}
function val(n,k){return n.attributes?.[k]??'—'}
function score(n){return Number(rank(n)).toLocaleString('pt-BR',{minimumSignificantDigits:3,maximumSignificantDigits:4})}
function coordinates(n){return {x:(n.rx??n.x)*scale*spread+panX,y:(n.ry??n.y)*scale*spread+panY}}
function resize(){const r=canvas.getBoundingClientRect();dpr=Math.min(devicePixelRatio||1,2);cw=r.width;ch=r.height;canvas.width=Math.round(cw*dpr);canvas.height=Math.round(ch*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);if(!nodes.length)return;if(!baseScale)fitGraph();requestDraw()}
function fitGraph(){if(!nodes.length||!cw||!ch)return;const xs=nodes.map(n=>n.x),ys=nodes.map(n=>n.y),xmin=Math.min(...xs),xmax=Math.max(...xs),ymin=Math.min(...ys),ymax=Math.max(...ys);baseScale=Math.min((cw-95)/(xmax-xmin)/spread,(ch-125)/(ymax-ymin)/spread);scale=baseScale;panX=cw/2-(xmin+xmax)/2*scale*spread;panY=ch/2-(ymin+ymax)/2*scale*spread;requestDraw()}
function radius(n){return Math.max(1.7,Math.min(19,1.5+Math.sqrt(Number(n.size||25))*0.6))*Math.max(.7,Math.min(1.8,Math.sqrt(scale/baseScale)))}
function isVisible(n){return rank(n)>=threshold}
let queued=false;function requestDraw(){if(queued)return;queued=true;requestAnimationFrame(()=>{queued=false;draw()})}
function draw(){if(!nodes.length)return;ctx.fillStyle=background;ctx.fillRect(0,0,cw,ch);const focus=selected&&new Set([selected.id,...(adj.get(selected.id)||[]).map(item=>item.node.id)]);ctx.lineWidth=.75;
for(const e of edges){const a=byId.get(e.source),b=byId.get(e.target);if(!a||!b||!isVisible(a)||!isVisible(b))continue;const p=coordinates(a),q=coordinates(b);if((p.x<0&&q.x<0)||(p.x>cw&&q.x>cw)||(p.y<0&&q.y<0)||(p.y>ch&&q.y>ch))continue;const highlighted=selected&&(a.id===selected.id||b.id===selected.id);ctx.beginPath();ctx.moveTo(p.x,p.y);ctx.lineTo(q.x,q.y);ctx.strokeStyle=focus?(highlighted?'rgba(22,100,70,.63)':'rgba(64,83,71,.045)'):'rgba(61,81,70,.16)';ctx.lineWidth=highlighted?1.6:.7;ctx.stroke()}
const sorted=selected?[...nodes.filter(n=>n.id!==selected.id),selected]:nodes;const topSet=showLabels?new Set(ranked.slice(0,labelCount).map(n=>n.id)):new Set();
for(const n of sorted){if(!isVisible(n))continue;const {x,y}=coordinates(n),r=radius(n);if(x<-r||x>cw+r||y<-r||y>ch+r)continue;const isFocus=n===selected||n===hovered;ctx.globalAlpha=focus&&!focus.has(n.id)?.17:1;ctx.beginPath();ctx.arc(x,y,r+(isFocus?2:0),0,Math.PI*2);ctx.fillStyle=n.color||'#22784a';ctx.fill();if(isFocus){ctx.strokeStyle='#173b2b';ctx.lineWidth=n===selected?2.4:1.4;ctx.stroke()}ctx.globalAlpha=1;
if((isFocus||topSet.has(n.id)||scale>baseScale*3&&r>3)&&r>2){const label=n.label||n.id;ctx.font='600 11px system-ui';ctx.textAlign='left';ctx.fillStyle='#233c2b';ctx.fillText(label,x+r+5,y+3)}}}
// Simulação de forças em Canvas, independente de bibliotecas externas.
// Coordenadas originais (x/y) nunca são modificadas; rx/ry são temporárias.
let elasticEnabled=true,elasticLevel=4,elasticFrame=0,elasticTicks=0,activeNode=null;
function initElastic(){for(const n of nodes){n.rx=n.x;n.ry=n.y;n.vx=0;n.vy=0}wakeElastic(170)}
function wakeElastic(ticks=120){if(!elasticEnabled||!nodes.length)return;elasticTicks=Math.max(elasticTicks,ticks);if(!elasticFrame)elasticFrame=requestAnimationFrame(elasticLoop)}
function stopElastic(){if(elasticFrame)cancelAnimationFrame(elasticFrame);elasticFrame=0;elasticTicks=0}
function restoreElastic(){stopElastic();for(const n of nodes){n.rx=n.x;n.ry=n.y;n.vx=0;n.vy=0}requestDraw()}
function elasticLoop(){elasticFrame=0;if(!elasticEnabled)return;physicsStep();requestDraw();elasticTicks=Math.max(0,elasticTicks-1);if(elasticTicks>0||activeNode)elasticFrame=requestAnimationFrame(elasticLoop)}
function physicsStep(){
  // Forças no espaço do Gephi, calibradas para produzir separação perceptível na tela.
  // A escala de referência é a escala da câmera inicial, não o zoom corrente.
  const unit=Math.max(baseScale*spread,0.0001);
  const cellSize=56/unit,grid=new Map();
  const fx=new Float64Array(nodes.length),fy=new Float64Array(nodes.length);
  for(let i=0;i<nodes.length;i++){
    const n=nodes[i],cx=Math.floor(n.rx/cellSize),cy=Math.floor(n.ry/cellSize);
    const key=cx+','+cy;
    if(!grid.has(key))grid.set(key,[]);
    grid.get(key).push(i);
  }
  for(let i=0;i<nodes.length;i++){
    const a=nodes[i],gx=Math.floor(a.rx/cellSize),gy=Math.floor(a.ry/cellSize);
    for(let xx=gx-1;xx<=gx+1;xx++)for(let yy=gy-1;yy<=gy+1;yy++){
      const peers=grid.get(xx+','+yy); if(!peers)continue;
      for(const j of peers){if(j<=i)continue;
        const b=nodes[j];let dx=b.rx-a.rx,dy=b.ry-a.ry;
        let dist=Math.hypot(dx,dy);
        if(dist<0.0001){const angle=(i*31+j*17)*2.39996323;dx=Math.cos(angle)*0.01;dy=Math.sin(angle)*0.01;dist=0.01}
        // Colisão suave com espaço mínimo entre nós e uma margem visual.
        const minPx=Math.min(19,4+Math.sqrt(a.size||25)*.7)+Math.min(19,4+Math.sqrt(b.size||25)*.7)+7;
        const minDist=minPx/unit;
        if(dist<minDist){
          const mag=(minDist-dist)*.23/dist;
          const px=dx*mag,py=dy*mag;
          fx[i]-=px;fy[i]-=py;fx[j]+=px;fy[j]+=py;
        }
      }
    }
  }
  // A repulsão ao redor do nó arrastado dá resposta visual imediata.
  if(activeNode){
    const range=120/unit;
    for(let i=0;i<nodes.length;i++){
      const n=nodes[i];if(n===activeNode)continue;
      const dx=n.rx-activeNode.rx,dy=n.ry-activeNode.ry;
      const d=Math.max(0.01,Math.hypot(dx,dy));
      if(d<range){const f=(range-d)*.14/d;fx[i]+=dx*f;fy[i]+=dy*f}
    }
  }
  for(let i=0;i<nodes.length;i++){
    const n=nodes[i];if(n===activeNode){n.vx=0;n.vy=0;continue}
    // Âncora nas coordenadas originais: equilíbrio elástico, sem alterar o arquivo.
    n.vx=(n.vx+(n.x-n.rx)*.025+fx[i])*.68;
    n.vy=(n.vy+(n.y-n.ry)*.025+fy[i])*.68;
    const limit=16/unit,speed=Math.hypot(n.vx,n.vy);
    if(speed>limit){n.vx*=limit/speed;n.vy*=limit/speed}
    n.rx+=n.vx;n.ry+=n.vy;
  }
}

function hitNode(x,y){let best=null,dist=Infinity;for(const n of nodes){if(!isVisible(n))continue;const p=coordinates(n),r=radius(n)+7,dx=p.x-x,dy=p.y-y,d=dx*dx+dy*dy;if(d<r*r&&d<dist){best=n;dist=d}}return best}
function zoomAt(factor,x=cw/2,y=ch/2){const newScale=Math.max(baseScale*.25,Math.min(baseScale*55,scale*factor));const f=newScale/scale;panX=x-(x-panX)*f;panY=y-(y-panY)*f;scale=newScale;requestDraw()}
function centerNode(n){if(!n)return;scale=Math.max(baseScale*2.7,Math.min(scale,baseScale*5));panX=cw/2-(n.rx??n.x)*scale*spread;panY=ch/2-(n.ry??n.y)*scale*spread;requestDraw()}
function updateStats(){const counts=fmt.format(nodes.length),ec=fmt.format(edges.length);$('stat-nodes').textContent=$('metric-nodes').textContent=counts;$('stat-edges').textContent=$('metric-edges').textContent=ec;$('metric-mod').textContent=fmt.format(new Set(nodes.map(n=>val(n,'Modularity Class')).filter(x=>x!=='—')).size)}
function makeRanking(){const root=$('ranking');root.replaceChildren();ranked.slice(0,10).forEach((n,i)=>{const li=document.createElement('li'),button=document.createElement('button'),num=document.createElement('span'),name=document.createElement('span'),pr=document.createElement('span');num.className='rank-number';num.textContent=i+1;name.className='rank-name';name.textContent=n.label;pr.className='rank-score';pr.textContent=score(n);button.append(num,name,pr);button.onclick=()=>choose(n,true);li.append(button);root.append(li)})}
function choose(n,center=false){if(n && $('app').classList.contains('details-collapsed'))toggleDetails(false);selected=n;hovered=null;$('details-empty').hidden=!!n;$('details-selected').hidden=!n;$('details').classList.toggle('details-open',!!n);if(!n){$('graph-state').textContent='Rede completa';requestDraw();return}if(!isVisible(n)){threshold=0;$('min-rank').value=0;applyThreshold()}
$('node-label').textContent=n.label;$('node-fullname').textContent=val(n,'fullname')==='—'?'Usuário da rede':val(n,'fullname');$('node-pr').textContent=score(n);$('node-indeg').textContent=val(n,'Grau de entrada');$('node-outdeg').textContent=val(n,'Grau de saída');$('node-degree').textContent=val(n,'Grau');const attrs=$('node-attributes');attrs.replaceChildren();for(const [field,title] of [['followers','Seguidores'],['following','Seguindo'],['verified','Verificado'],['Modularity Class','Classe de modularidade']]){const v=val(n,field);if(v==='—')continue;const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=title;dd.textContent=(field==='followers'||field==='following')?fmt.format(Number(v)):field==='verified'?(v==='true'?'Sim':'Não'):v;attrs.append(dt,dd)}
const related=adj.get(n.id)||[];$('connections-number').textContent='('+related.length+')';const connections=$('connections-list');connections.replaceChildren();related.slice().sort((a,b)=>rank(b.node)-rank(a.node)).slice(0,150).forEach(item=>{const btn=document.createElement('button'),a=document.createElement('span'),label=document.createElement('span');btn.className='connection';a.className='arr';a.textContent=item.direction==='out'?'→':'←';label.textContent=item.node.label;btn.title=item.direction==='out'?'Este perfil mencionou o usuário':'Este perfil foi mencionado pelo usuário';btn.append(a,label);btn.onclick=()=>choose(item.node,true);connections.append(btn)});if(related.length>150){const p=document.createElement('p');p.className='hint';p.textContent='Mostrando as primeiras 150 conexões, ordenadas por PageRank.';connections.append(p)}$('graph-state').textContent='Selecionado: '+n.label;if(center)centerNode(n);requestDraw()}
function applyThreshold(){const fraction=Number($('min-rank').value)/100;threshold=fraction===0?0:minPR*Math.pow(maxPR/minPR,fraction);$('rank-value').textContent=fraction===0?'Todos':threshold.toExponential(2).replace('.',',');visibleCount=nodes.filter(isVisible).length;$('visible-nodes').textContent=fmt.format(visibleCount)+' nós';requestDraw()}
function updateSearch(){const text=$('search').value.toLocaleLowerCase('pt-BR').trim(),out=$('search-results');out.replaceChildren();if(text.length<2)return;const results=nodes.filter(n=>String(n.label+' '+(n.attributes?.fullname||'')).toLocaleLowerCase('pt-BR').includes(text)).sort((a,b)=>rank(b)-rank(a)).slice(0,12);if(!results.length){const p=document.createElement('p');p.className='hint';p.textContent='Nenhum usuário encontrado.';out.append(p);return}results.forEach(n=>{const b=document.createElement('button'),label=document.createElement('span'),pr=document.createElement('small');b.className='search-result';label.textContent=n.label;pr.textContent='PR '+score(n);b.append(label,pr);b.onclick=()=>{choose(n,true);out.replaceChildren();$('sidebar').classList.remove('open')};out.append(b)})}
// Títulos com inicial Libre Baskerville e continuidade em SF Pro.
function setModalTitle(title){
 const root=$('modal-title');root.replaceChildren();
 const letters=Array.from(title),initial=document.createElement('span'),rest=document.createElement('span');
 initial.className='modal-initial';initial.textContent=letters.shift()||'';
 rest.className='modal-title-rest';rest.textContent=letters.join('');root.append(initial,rest);
}

// Ranking somente de leitura: dados fornecidos pelo pesquisador via hashtags.json.
let hashtagRows=[];
function normalizedTags(rows){
 if(!Array.isArray(rows))return [];
 const used=new Set(),result=[];
 for(const item of rows){
   const tag=String(item?.hashtag??'').trim().replace(/^#+/,'');
   const raw=item?.quantidade;
   const n=Number(raw);
   if(!tag||!Number.isSafeInteger(n)||n<0||raw===''||raw==null)continue;
   const key=tag.toLocaleLowerCase('pt-BR');if(used.has(key))continue;
   used.add(key);result.push({hashtag:'#'+tag,quantidade:n});
 }
 return result.sort((a,b)=>b.quantidade-a.quantidade||a.hashtag.localeCompare(b.hashtag,'pt-BR')).slice(0,10);
}
async function loadHashtags(){
 try{
   const res=await fetch('./hashtags.json',{cache:'no-store'});
   if(!res.ok)throw new Error('HTTP '+res.status);
   hashtagRows=normalizedTags(await res.json());
 }catch(error){hashtagRows=[];console.warn('Não foi possível carregar hashtags.json',error)}
}
function renderTagsModal(){
 const root=$('modal-body');
 const current=normalizedTags(hashtagRows);
 if(!current.length){
   const empty=document.createElement('p');empty.className='hashtag-empty';
   empty.textContent='Ranking indisponível. Adicione suas hashtags e frequências ao arquivo hashtags.json na pasta do site.';
   root.appendChild(empty);
   return;
 }
 const ranking=document.createElement('ol');ranking.className='ranking modal-ranking';
 current.forEach((item,i)=>{
   const li=document.createElement('li'),line=document.createElement('div');line.className='hashtag-rank-row';
   const position=document.createElement('span');position.className='rank-number';position.textContent=i+1;
   const name=document.createElement('span');name.className='rank-name';name.textContent=item.hashtag;
   const count=document.createElement('strong');count.className='rank-score';count.textContent=fmt.format(item.quantidade)+' ocorrências';
   line.append(position,name,count);li.append(line);ranking.appendChild(li);
 });
 root.appendChild(ranking);
}

function openModal(type){const titles={pagerank:'O que é PageRank?',users:'Top 10 usuários por PageRank',tags:'Top 10 hashtags',home:'Rede de atores',about:'Sobre o projeto',method:'Metodologia',data:'Sobre os dados',read:'Como ler esta rede?'};const bodies={pagerank:'PageRank é uma métrica de centralidade que estima a importância estrutural de cada nó com base nas conexões recebidas e na importância dos nós de origem dessas conexões. Nesta rede dirigida de menções, um PageRank mais alto indica que o perfil recebe conexões de outros perfis com relevância na estrutura da rede. Não equivale a quantidade de seguidores nem a uma medida direta de influência política. Nesta exportação, a escala de cores e tamanhos foi definida a partir dos valores calculados no Gephi.',tags:'Hashtags mais frequentes na coleta, conforme os valores fornecidos no arquivo hashtags.json. O ranking não é inferido das arestas da rede.',home:'Esta página permite explorar uma rede de perfis e relações extraída de dados de uma plataforma social. Utilize a busca, o ranking ou selecione diretamente um nó no grafo.',about:'Interface experimental de visualização de redes. Os dados foram exportados do Gephi e a identidade visual foi adaptada de um protótipo criado no Figma. Detalhes institucionais e autoria podem ser acrescentados nesta seção.',method:'As posições dos nós correspondem ao layout exportado do Gephi (Yifan Hu, conforme informado pelo projeto). A cor e o tamanho de cada nó vêm dos atributos visuais presentes no arquivo original, configurados para PageRank. As linhas representam relações dirigidas entre usuários; as métricas de grau e PageRank já estavam calculadas na base.',data:`A base contém ${fmt.format(nodes.length)} usuários (nós) e ${fmt.format(edges.length)} relações (arestas). Os relacionamentos carregam atributos de menções, respostas, republicações e citações. O conjunto enviado não possui registros independentes de postagens, hashtags nem datas por aresta, portanto esses indicadores não podem ser calculados com segurança nesta versão. O período mostrado na interface foi informado no protótipo, não validado no arquivo da rede.`,read:'Cada círculo é um perfil. Uma linha conecta dois perfis quando existe uma relação registrada no arquivo; a direção original da aresta é preservada nos dados e indicada no painel de conexões. Círculos maiores e mais verdes correspondem a maior PageRank, segundo o mapeamento visual exportado. Arraste para navegar, use zoom, clique em um círculo para ver suas conexões e procure usuários pela barra lateral. Filtrar pelo PageRank reduz os nós exibidos sem alterar a estrutura armazenada.'};setModalTitle(titles[type]||'Informações');$('modal-body').replaceChildren();const p=document.createElement('p');p.textContent=bodies[type]||'';$('modal-body').append(p);if(type==='users'){const ol=document.createElement('ol');ol.className='ranking modal-ranking';ranked.slice(0,10).forEach((n,i)=>{const li=document.createElement('li'),button=document.createElement('button'),num=document.createElement('span'),name=document.createElement('span'),pr=document.createElement('span');num.className='rank-number';num.textContent=i+1;name.className='rank-name';name.textContent=n.label;pr.className='rank-score';pr.textContent=score(n);button.append(num,name,pr);button.onclick=()=>{$('modal').hidden=true;choose(n,true)};li.append(button);ol.append(li)});p.textContent='Classificação calculada a partir dos valores de PageRank da exportação do Gephi.';$('modal-body').append(ol)}if(type==='tags')renderTagsModal();$('modal').hidden=false}
function toggleSidebar(hidden){$('app').classList.toggle('sidebar-collapsed',hidden);$('sidebar').classList.toggle('open',!hidden&&window.innerWidth<=650);$('reopen-sidebar').hidden=!hidden;setTimeout(()=>{resize();},270)}
function toggleDetails(hidden){$('app').classList.toggle('details-collapsed',hidden);$('details').classList.toggle('details-force-hidden',hidden);$('reopen-details').hidden=!hidden;setTimeout(()=>resize(),270)}
function listeners(){canvas.addEventListener('pointerdown',e=>{const r=canvas.getBoundingClientRect(),n=elasticEnabled?hitNode(e.clientX-r.left,e.clientY-r.top):null;activeNode=n;drag={x:e.clientX,y:e.clientY,px:panX,py:panY,node:n,startX:n?.rx,startY:n?.ry};moved=false;canvas.setPointerCapture(e.pointerId);canvas.classList.add('dragging');if(n){$('tooltip').hidden=true;wakeElastic(130)}});canvas.addEventListener('pointermove',e=>{const r=canvas.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top;if(drag){const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.abs(dx)+Math.abs(dy)>4)moved=true;if(drag.node){drag.node.rx=drag.startX+dx/(scale*spread);drag.node.ry=drag.startY+dy/(scale*spread);wakeElastic(90)}else{panX=drag.px+dx;panY=drag.py+dy}requestDraw();return}const n=hitNode(x,y);canvas.style.cursor=n?'grab':'grab';if(n!==hovered){hovered=n;requestDraw()}const t=$('tooltip');if(n){t.hidden=false;t.textContent=n.label+' · PageRank '+score(n);t.style.left=Math.min(cw-180,Math.max(8,x+12))+'px';t.style.top=Math.max(8,y-32)+'px'}else t.hidden=true});canvas.addEventListener('pointerup',e=>{const r=canvas.getBoundingClientRect();if(drag&&!moved)choose(hitNode(e.clientX-r.left,e.clientY-r.top));drag=null;activeNode=null;wakeElastic(170);canvas.classList.remove('dragging')});canvas.addEventListener('pointercancel',()=>{drag=null;activeNode=null;wakeElastic(120);canvas.classList.remove('dragging')});canvas.addEventListener('pointerleave',()=>{$('tooltip').hidden=true;hovered=null;requestDraw()});canvas.addEventListener('wheel',e=>{e.preventDefault();const r=canvas.getBoundingClientRect();zoomAt(e.deltaY<0?1.17:1/1.17,e.clientX-r.left,e.clientY-r.top)},{passive:false});$('zoom-in').onclick=()=>zoomAt(1.3);$('zoom-out').onclick=()=>zoomAt(1/1.3);$('zoom-home').onclick=()=>{fitGraph();choose(null)};$('close-details').onclick=()=>toggleDetails(true);$('clear-selection').onclick=()=>choose(null);$('return-network').onclick=()=>{choose(null);fitGraph()};$('search').addEventListener('input',updateSearch);$('clear-search').onclick=()=>{$('search').value='';$('search-results').replaceChildren();$('search').focus()};$('min-rank').addEventListener('input',applyThreshold);$('reset-filter').onclick=()=>{$('min-rank').value=0;applyThreshold()};$('about-data').onclick=()=>openModal('data');$('how-read').onclick=()=>openModal('read');$('pagerank-help').onclick=()=>openModal('pagerank');$('open-users-ranking').onclick=()=>openModal('users');$('open-tags-ranking').onclick=()=>openModal('tags');$('show-labels').onchange=e=>{showLabels=e.target.checked;requestDraw()};$('label-count').oninput=e=>{labelCount=Number(e.target.value);$('label-count-value').textContent=labelCount;requestDraw()};$('spread').oninput=e=>{const old=spread;spread=Number(e.target.value)/100;$('spread-value').textContent=spread.toFixed(1).replace('.',',')+'×';const f=spread/old;panX=cw/2-(cw/2-panX)*f;panY=ch/2-(ch/2-panY)*f;requestDraw()};$('reset-display').onclick=()=>{spread=1;showLabels=true;labelCount=15;$('show-labels').checked=true;$('label-count').value=15;$('label-count-value').textContent='15';$('spread').value=100;$('spread-value').textContent='1,0×';fitGraph()};$('toggle-sidebar').onclick=()=>toggleSidebar(true);$('reopen-sidebar').onclick=()=>toggleSidebar(false);$('reopen-details').onclick=()=>toggleDetails(false);document.querySelectorAll('[data-page]').forEach(b=>b.onclick=e=>{e.preventDefault();document.querySelectorAll('.main-nav .nav-link').forEach(a=>a.classList.toggle('active',a.dataset.page===b.dataset.page));if(b.dataset.page==='graph'){choose(null);fitGraph()}else openModal(b.dataset.page)});$('modal-close').onclick=()=>$('modal').hidden=true;$('modal').addEventListener('click',e=>{if(e.target===$('modal'))$('modal').hidden=true});document.addEventListener('keydown',e=>{if(e.key==='Escape'){$('modal').hidden=true;choose(null)}});$('open-menu').onclick=()=>{$('sidebar').classList.add('open');document.querySelector('#app').classList.remove('sidebar-collapsed')};window.addEventListener('resize',resize)}
async function initialize(){listeners();try{const res=await fetch('./data.json');if(!res.ok)throw Error('HTTP '+res.status);const data=await res.json();nodes=data.nodes;edges=data.edges;byId=new Map(nodes.map(n=>[n.id,n]));adj=new Map(nodes.map(n=>[n.id,[]]));for(const e of edges){const a=byId.get(e.source),b=byId.get(e.target);if(!a||!b)continue;adj.get(a.id).push({node:b,direction:'out'});adj.get(b.id).push({node:a,direction:'in'})}ranked=[...nodes].sort((a,b)=>rank(b)-rank(a));const positive=ranked.map(rank).filter(v=>v>0);minPR=Math.min(...positive);maxPR=Math.max(...positive);updateStats();resize();fitGraph();initElastic();applyThreshold();$('loading').textContent='';}catch(e){$('loading').textContent='Erro ao carregar data.json';$('graph-state').textContent='Abra por um servidor local (veja README)';console.error(e)}}
$('reopen-details').hidden=false;
loadHashtags();
initialize();
