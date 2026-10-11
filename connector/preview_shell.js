(()=>{'use strict';
const $=id=>document.getElementById(id);
const stage=$('stage'),previewPane=$('previewPane'),livePane=$('livePane');
const previewFrame=$('previewFrame'),liveFrame=$('liveFrame'),snapshotFrame=$('snapshotFrame');
const desktop=$('desktop'),mobile=$('mobile'),compare=$('compare'),diff=$('diff'),state=$('diffState');
const previous=$('prevChange'),next=$('nextChange');
const liveSource=document.body.dataset.liveFrame||'',snapshotSource=document.body.dataset.snapshotFrame||'';
let marked=[],showing=false,calculating=false,current=-1,timer=null;
const setState=value=>{if(state)state.textContent=value};

function ownText(element){return [...element.childNodes].filter(node=>node.nodeType===3).map(node=>node.nodeValue||'').join(' ').replace(/\s+/g,' ').trim()}
function normalizedUrl(value,base){
 if(!value||value==='none')return value||'';
 return value.replace(/url\(["']?([^"')]+)["']?\)/g,(_,url)=>{try{const parsed=new URL(url,base);return 'url('+parsed.pathname+parsed.search+')'}catch{return 'url('+url+')'}});
}
function stableMatch(element,otherDocument){
 if(element.id&&typeof CSS!=='undefined'&&CSS.escape){
  const matches=otherDocument.querySelectorAll('#'+CSS.escape(element.id));
  if(matches.length===1&&matches[0].tagName===element.tagName)return matches[0];
 }
 const steps=[];let node=element;
 while(node&&node!==element.ownerDocument.body){
  const parent=node.parentElement;if(!parent||steps.length>=14)return null;
  const children=[...parent.children],index=children.indexOf(node);
  steps.push({index,tag:node.tagName,count:children.length});node=parent;
 }
 if(node!==element.ownerDocument.body)return null;
 let other=otherDocument.body;
 for(const step of steps.reverse()){
  const children=[...other.children];
  if(children.length!==step.count||!children[step.index]||children[step.index].tagName!==step.tag)return null;
  other=children[step.index];
 }
 return other;
}
const STYLE_PROPERTIES=['display','position','color','backgroundColor','fontSize','fontWeight','lineHeight','textAlign','borderRadius','borderWidth','borderColor','paddingTop','paddingRight','paddingBottom','paddingLeft','marginTop','marginRight','marginBottom','marginLeft','gap','gridTemplateColumns','flexDirection','justifyContent','alignItems'];
function facts(element){
 const view=element.ownerDocument.defaultView,style=view.getComputedStyle(element),rect=element.getBoundingClientRect(),parent=element.parentElement?.getBoundingClientRect();
 const styles=STYLE_PROPERTIES.map(name=>style[name]);
 const image=element.tagName==='IMG'?normalizedUrl(element.getAttribute('src')||'',element.ownerDocument.baseURI):normalizedUrl(style.backgroundImage,element.ownerDocument.baseURI);
 return {text:ownText(element),image,styles,rect,parent,visible:rect.width>0&&rect.height>0&&style.visibility!=='hidden'&&style.display!=='none'};
}
function changedReasons(left,right){
 const reasons=[];
 if(left.text!==right.text&&(left.text||right.text))reasons.push('텍스트');
 if(left.image!==right.image&&(left.image||right.image))reasons.push('이미지');
 if(left.styles.some((value,index)=>value!==right.styles[index]))reasons.push('스타일');
 if(left.visible&&right.visible&&left.parent&&right.parent){
  const delta=[left.rect.width-right.rect.width,left.rect.height-right.rect.height,(left.rect.left-left.parent.left)-(right.rect.left-right.parent.left),(left.rect.top-left.parent.top)-(right.rect.top-right.parent.top)];
  if(delta.some(value=>Math.abs(value)>3))reasons.push('배치');
 }
 return reasons;
}
function locationLabel(element){
 const section=element.closest('section,article,main,header,nav,footer');
 const heading=section?.querySelector('h1,h2,h3')?.textContent?.replace(/\s+/g,' ').trim();
 const text=(ownText(element)||element.getAttribute('alt')||element.getAttribute('aria-label')||'').slice(0,42);
 const identity=element.id?'#'+element.id:'';
 const height=Math.max(1,element.ownerDocument.documentElement.scrollHeight);
 const position=Math.max(0,Math.min(100,Math.round((element.getBoundingClientRect().top+element.ownerDocument.defaultView.scrollY)/height*100)));
 return (heading||text||identity||element.tagName.toLowerCase())+' · 위에서 '+position+'%';
}
async function settled(frame){
 const doc=frame.contentDocument;if(!doc)return;
 try{await doc.fonts?.ready}catch{}
 const images=[...doc.images].filter(image=>!image.complete).slice(0,80);
 await Promise.race([Promise.all(images.map(image=>new Promise(resolve=>{image.addEventListener('load',resolve,{once:true});image.addEventListener('error',resolve,{once:true})}))),new Promise(resolve=>setTimeout(resolve,2500))]);
}
function ensureOutlineStyle(doc){
 if(doc.getElementById('wm-preview-diff-style'))return;
 const style=doc.createElement('style');style.id='wm-preview-diff-style';style.dataset.wmIgnoreDiff='';
 style.textContent='.wm-diff-outline{outline:3px solid #27d17f!important;outline-offset:2px!important}.wm-diff-current{outline-width:5px!important;box-shadow:0 0 0 5px rgba(39,209,127,.22)!important}';
 (doc.head||doc.documentElement).append(style);
}
function updateChangeControls(){const visible=showing&&marked.length>0;previous.hidden=!visible;next.hidden=!visible}
function clearMarks(message=''){
 if(timer!==null){clearTimeout(timer);timer=null}
 for(const item of marked)item.element.classList.remove('wm-diff-outline','wm-diff-current');
 marked=[];current=-1;showing=false;diff.classList.remove('on');diff.textContent='변경 표시';updateChangeControls();
 if(message)setState(message);
}
function focusChange(index){
 if(!marked.length)return;
 current=(index+marked.length)%marked.length;
 marked.forEach((item,i)=>item.element.classList.toggle('wm-diff-current',i===current));
 const item=marked[current];item.element.scrollIntoView({block:'center',inline:'nearest',behavior:'smooth'});
 setState('변경 '+(current+1)+'/'+marked.length+' · '+item.reasons.join('·')+' · '+item.label);
}
function setVisible(value){
 showing=value;
 for(const item of marked)item.element.classList.toggle('wm-diff-outline',value);
 if(!value)for(const item of marked)item.element.classList.remove('wm-diff-current');
 diff.classList.toggle('on',value);diff.textContent=value?'변경 표시 끄기 ('+marked.length+')':'변경 표시 켜기 ('+marked.length+')';
 updateChangeControls();if(value&&marked.length)focusChange(Math.max(0,current));
}
function previewPath(){
 try{
  const path=previewFrame.contentWindow.location.pathname,marker='/preview-site/';
  if(path.startsWith(marker)){const rest=path.slice(marker.length),slash=rest.indexOf('/');return slash>=0?rest.slice(slash+1):''}
  return path.replace(/^\/+/, '');
 }catch{return ''}
}
function syncFrame(frame,base,relative){
 if(!base)return false;
 if(frame.dataset.wmPath===relative)return false;
 frame.dataset.wmPath=relative;frame.src=base+relative;return true;
}
function syncComparisonFrames(){
 const relative=previewPath();
 const liveChanged=syncFrame(liveFrame,liveSource,relative),snapshotChanged=syncFrame(snapshotFrame,snapshotSource,relative);
 if(!liveChanged&&!snapshotChanged)scheduleCalculate();
}
function scheduleCalculate(){
 if(!liveSource||!snapshotSource)return;
 if(timer!==null)clearTimeout(timer);
 timer=setTimeout(()=>{timer=null;void calculate()},350);
}
function alignComparisonViewport(){
 const width=Math.max(320,liveFrame.clientWidth||previewFrame.clientWidth||window.innerWidth);
 const height=Math.max(480,liveFrame.clientHeight||previewFrame.clientHeight||window.innerHeight);
 snapshotFrame.style.width=width+'px';snapshotFrame.style.height=height+'px';
}
async function calculate(){
 if(calculating||!liveSource||!snapshotSource)return;calculating=true;diff.disabled=true;setState('현재 운영 화면과 배포할 화면을 비교하는 중…');
 try{
  alignComparisonViewport();
  await Promise.all([settled(previewFrame),settled(snapshotFrame),settled(liveFrame)]);
  const previewDocument=previewFrame.contentDocument,snapshotDocument=snapshotFrame.contentDocument,liveDocument=liveFrame.contentDocument;
  if(!previewDocument?.body||!snapshotDocument?.body||!liveDocument?.body)throw Error('비교 화면을 준비하지 못했습니다.');
  ensureOutlineStyle(previewDocument);
  for(const item of marked)item.element.classList.remove('wm-diff-outline','wm-diff-current');
  const ignored=new Set(['HTML','BODY','HEAD','SCRIPT','STYLE','LINK','META','NOSCRIPT','TEMPLATE','PATH','G','DEFS']);
  const candidates=[];
  for(const sourceElement of snapshotDocument.body.querySelectorAll('*')){
   if(ignored.has(sourceElement.tagName)||sourceElement.closest('[data-wm-ignore-diff]'))continue;
   const liveElement=stableMatch(sourceElement,liveDocument),previewElement=stableMatch(sourceElement,previewDocument);
   if(!liveElement||!previewElement)continue;
   const left=facts(sourceElement),right=facts(liveElement);if(!left.visible&&!right.visible)continue;
   const reasons=changedReasons(left,right);if(!reasons.length)continue;
   const area=Math.max(1,left.rect.width*left.rect.height);candidates.push({element:previewElement,reasons,area,label:locationLabel(previewElement)});
  }
  candidates.sort((a,b)=>a.area-b.area);
  const selected=[];
  for(const candidate of candidates){
   if(selected.some(item=>candidate.element.contains(item.element)))continue;
   selected.push(candidate);if(selected.length>=80)break;
  }
  marked=selected;current=0;showing=true;
  for(const item of marked){item.element.classList.add('wm-diff-outline');item.element.title='변경: '+item.reasons.join(' · ')+' · '+item.label}
  diff.classList.add('on');diff.textContent='변경 표시 끄기 ('+marked.length+')';updateChangeControls();
  if(marked.length)focusChange(0);else setState('현재 운영 화면과 확실하게 대응되는 차이가 없습니다.');
 }catch(error){clearMarks();setState('변경 비교 불가 · '+error.message)}finally{calculating=false;diff.disabled=!liveSource}
}
function size(mode){
 for(const pane of [previewPane,livePane])pane?.classList.toggle('mobile',mode==='mobile');
 desktop.classList.toggle('on',mode==='desktop');mobile.classList.toggle('on',mode==='mobile');
 clearMarks('화면 크기가 바뀌어 다시 비교합니다.');scheduleCalculate();
}
desktop.onclick=()=>size('desktop');mobile.onclick=()=>size('mobile');
compare.disabled=!liveSource;compare.onclick=()=>{if(!liveSource)return;stage.classList.toggle('compare');compare.classList.toggle('on');clearMarks('화면 크기가 바뀌어 다시 비교합니다.');scheduleCalculate()};
diff.onclick=()=>{if(marked.length)setVisible(!showing);else void calculate()};
previous.onclick=()=>focusChange(current-1);next.onclick=()=>focusChange(current+1);
previewFrame.addEventListener('load',()=>{clearMarks('현재 페이지의 변경 위치를 확인하는 중…');syncComparisonFrames()});
snapshotFrame.addEventListener('load',scheduleCalculate);liveFrame.addEventListener('load',scheduleCalculate);
if(liveSource&&snapshotSource){diff.disabled=false;setState('현재 운영 화면과 배포할 화면을 자동 비교합니다.');setTimeout(syncComparisonFrames,0)}
else{diff.disabled=true;setState('운영 화면 주소가 없어 변경 비교를 사용할 수 없습니다.')}
})();
