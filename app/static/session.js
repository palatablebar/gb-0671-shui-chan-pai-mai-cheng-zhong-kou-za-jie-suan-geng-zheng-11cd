const feedback=document.querySelector('#feedback');
const sessionId=document.body.dataset.sessionId;
async function post(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await r.json();if(!r.ok)throw Error(data.detail||'操作失败，请重试');return data;}
function reload(){const url=new URL(location.href);if(sessionId)url.searchParams.set('session_id',sessionId);location.href=url;}
function fail(error){feedback.textContent=error.message;}
const picker=document.querySelector('#session-picker');
if(picker)picker.addEventListener('change',()=>{const url=new URL(location.href);url.searchParams.set('session_id',picker.value);location.href=url;});
const publish=document.querySelector('#publish');
if(publish)publish.addEventListener('click',async()=>{publish.disabled=true;try{const version=parseInt(document.body.dataset.currentVersion||'0',10);const result=await post('/api/session/publish',{session_id:sessionId,expect_version:version});feedback.textContent=result.unchanged?`内容未变，仍为 v${result.version}`:`已发布 v${result.version} · 新增 ${result.added} · 撤回 ${result.withdrawn}`;setTimeout(reload,600);}catch(error){fail(error);publish.disabled=false;}});
async function loadDemo(button){button.disabled=true;try{await post('/api/session/demo',{});const url=new URL(location.href);url.searchParams.set('session_id','evening-demo');location.href=url;}catch(error){fail(error);button.disabled=false;}}
const demo=document.querySelector('#demo');if(demo)demo.addEventListener('click',()=>loadDemo(demo));
const demoEmpty=document.querySelector('#demo-empty');if(demoEmpty)demoEmpty.addEventListener('click',()=>loadDemo(demoEmpty));
const upload=document.querySelector('#upload');
if(upload)upload.addEventListener('click',async()=>{upload.disabled=true;try{let transactions;try{transactions=JSON.parse(document.querySelector('#up-transactions').value);}catch(e){throw Error('成交JSON无法解析');}const result=await post('/api/session/uploads',{session_id:document.querySelector('#up-session').value.trim(),terminal_id:document.querySelector('#up-terminal').value.trim(),seq:parseInt(document.querySelector('#up-seq').value,10),transactions});feedback.textContent=result.status==='duplicate'?'该批次已补传过，未新增成交':`已并入 ${result.received} 笔成交`;setTimeout(reload,600);}catch(error){fail(error);upload.disabled=false;}});
