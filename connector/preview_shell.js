(()=>{'use strict';
const $=id=>document.getElementById(id);
const stage=$('stage'),previewPane=$('previewPane'),livePane=$('livePane');
const previewFrame=$('previewFrame'),liveFrame=$('liveFrame');
const desktop=$('desktop'),mobile=$('mobile'),compare=$('compare'),diff=$('diff'),state=$('diffState');
const previewSource=document.body.dataset.previewFrame||'',liveSource=document.body.dataset.liveFrame||'';
let marked=[],showing=false,calculating=false;
const setState=value=>{if(state)state.textContent=value};
if(previewSource)previewFrame.src=previewSource;
function size(mode){
  for(const pane of [previewPane,livePane])pane?.classList.toggle('mobile',mode==='mobile');
  desktop.classList.toggle('on',mode==='desktop');mobile.classList.toggle('on',mode==='mobile');
  clearMarks('화면 크기가 바뀌었습니다. 변경 표시를 다시 실행할 수 있습니다.');
}
desktop.onclick=()=>size('desktop');mobile.onclick=()=>size('mobile');
compare.disabled=!liveSource;
compare.onclick=()=>{if(!liveSource)return;stage.classList.toggle('compare');compare.classList.toggle('on')};
function ownText(element){
  return [...element.childNodes].filter(node=>node.nodeType===3).map(node=>node.nodeValue||'').join(' ').replace(/\s+/g,' ').trim();
}
function normalizedUrl(value,base){
  if(!value||value==='none')return value||'';
  return value.replace(/url\(["']?([^"')]+)["']?\)/g,(_,url)=>{try{const parsed=new URL(url,base);return 'url('+parsed.pathname+parsed.search+')'}catch{return 'url('+url+')'}});
}
function stableMatch(element,otherDocument){
  if(element.id&&typeof CSS!=='undefined'&&CSS.escape){
    const selector='#'+CSS.escape(element.id),matches=otherDocument.querySelectorAll(selector);
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
  const styles=STYLE_PROPERTIES.map(name=>name==='backgroundImage'?normalizedUrl(style[name],element.ownerDocument.baseURI):style[name]);
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
async function settled(frame){
  const doc=frame.contentDocument;if(!doc)return;
  try{await doc.fonts?.ready}catch{}
  const images=[...doc.images].filter(image=>!image.complete).slice(0,80);
  await Promise.race([Promise.all(images.map(image=>new Promise(resolve=>{image.addEventListener('load',resolve,{once:true});image.addEventListener('error',resolve,{once:true})}))),new Promise(resolve=>setTimeout(resolve,2500))]);
}
function clearMarks(message=''){
  for(const item of marked)item.element.classList.remove('wm-diff-outline');
  marked=[];showing=false;diff.classList.remove('on');diff.textContent='변경 표시';
  if(message)setState(message);
}
function setVisible(value){showing=value;for(const item of marked)item.element.classList.toggle('wm-diff-outline',value);diff.classList.toggle('on',value);diff.textContent=value?'변경 표시 끄기 ('+marked.length+')':'변경 표시 켜기 ('+marked.length+')'}
function ensureOutlineStyle(doc){
  if(doc.getElementById('wm-preview-diff-style'))return;
  const style=doc.createElement('style');style.id='wm-preview-diff-style';style.dataset.wmIgnoreDiff='';
  style.textContent='.wm-diff-outline{outline:3px solid #27d17f!important;outline-offset:2px!important}';
  (doc.head||doc.documentElement).append(style);
}
async function calculate(){
  if(calculating||!liveSource)return;calculating=true;diff.disabled=true;setState('운영 화면과 확실하게 대응되는 요소만 비교하는 중…');
  try{
    await Promise.all([settled(previewFrame),settled(liveFrame)]);
    const previewDocument=previewFrame.contentDocument,liveDocument=liveFrame.contentDocument;
    if(!previewDocument?.body||!liveDocument?.body)throw Error('같은 출처의 비교 화면을 준비하지 못했습니다.');
    ensureOutlineStyle(previewDocument);
    const ignored=new Set(['HTML','BODY','HEAD','SCRIPT','STYLE','LINK','META','NOSCRIPT','TEMPLATE','PATH','G','DEFS']);
    const candidates=[];
    for(const element of previewDocument.body.querySelectorAll('*')){
      if(ignored.has(element.tagName)||element.closest('[data-wm-ignore-diff]'))continue;
      const other=stableMatch(element,liveDocument);if(!other)continue;
      const left=facts(element),right=facts(other);if(!left.visible&&!right.visible)continue;
      const reasons=changedReasons(left,right);if(!reasons.length)continue;
      const area=Math.max(1,left.rect.width*left.rect.height);candidates.push({element,reasons,area});
    }
    candidates.sort((a,b)=>a.area-b.area);
    const selected=[];
    for(const candidate of candidates){
      if(selected.some(item=>candidate.element.contains(item.element)))continue;
      selected.push(candidate);if(selected.length>=80)break;
    }
    marked=selected;
    for(const item of marked){item.element.classList.add('wm-diff-outline');item.element.dataset.wmDiffReason=item.reasons.join('·')}
    showing=true;diff.classList.add('on');diff.textContent='변경 표시 끄기 ('+marked.length+')';
    setState(marked.length?marked.length+'개 변경 영역 표시 · 대응이 불확실한 요소는 제외했습니다.':'확실하게 확인된 화면 차이가 없습니다.');
  }catch(error){clearMarks();setState('변경 비교 불가 · '+error.message)}finally{calculating=false;diff.disabled=!liveSource}
}
diff.onclick=()=>{if(marked.length)setVisible(!showing);else void calculate()};
for(const frame of [previewFrame,liveFrame])frame?.addEventListener('load',()=>clearMarks('화면 이동 후 변경 표시를 다시 실행할 수 있습니다.'));
if(liveSource){liveFrame.src=liveSource;diff.disabled=false;setState('변경 표시는 확실하게 대응되는 요소만 비교합니다.')}else{diff.disabled=true;setState('운영 화면 주소가 없어 변경 비교를 사용할 수 없습니다.')}
})();
