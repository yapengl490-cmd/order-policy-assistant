// 助手离线回归：真实网页代码，隔离 DOM/HTTP 替身；不发模型请求、不写正式数据。
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const markup=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const event={preventDefault(){}};
const reply=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const flush=async()=>{for(let i=0;i<4;i++)await new Promise(setImmediate);};
const memory=(status='proposed',patch={})=>({status,role:'buyer',values:{amount_format:'yuan',answer_style:'brief'},source_quote:'以后金额用元，简短回答',version:2,created_at:'2026-11-17T01:00:00Z',expires_at:'2099-11-17T01:30:00Z',notice:'只影响后续任务的回答偏好。',...patch});
const task=(id='t1',update=memory(),patch={})=>({id,message:'以后金额用元，简短回答',status:'completed',answer:'这是原回答。',facts:[],memory:{status:'loaded',effective_values:{answer_style:'detailed'}},memory_update:update,...patch});
function harness(){
  const h={requests:[],route:()=>undefined,records:new Map(),preferences:{role:'buyer',values:{},version:0}};
  class Node{
    constructor(tag){this.tagName=tag;this.children=[];this.value='';this._text='';this.disabled=false;this.hidden=false;}
    set textContent(v){this._text=String(v);this.children=[];}
    get textContent(){return this._text+this.children.map(x=>typeof x==='string'?x:x.textContent).join('');}
    replaceChildren(...items){this._text='';this.children=items;}
    append(...items){this.children.push(...items);}
  }
  const nodes=new Map();h.el=id=>{if(!nodes.has(id))nodes.set(id,new Node('div'));return nodes.get(id);};
  let seq=0;
  vm.runInContext(source,vm.createContext({document:{getElementById:h.el,createElement:t=>new Node(t)},crypto:{randomUUID:()=>`session-${++seq}`},setTimeout:callback=>setTimeout(callback,0),console,
    fetch:async(path,options)=>{
      const r={path,options,method:options.method||'GET',mode:options.headers['X-Access-Mode'],token:options.headers.Authorization};h.requests.push(r);
      const custom=h.route(r);if(custom!==undefined)return custom;
      if(path==='/api/me')return reply({role:r.mode,buyer_id:'B001',shop_id:'S001'});
      if(path==='/api/orders')return reply({orders:[]});
      if(path==='/api/preferences')return reply({...h.preferences,role:r.mode});
      if(path==='/api/tasks'&&r.method==='GET')return reply([...h.records.values()]);
      if(path==='/api/tasks'&&r.method==='POST')return reply({id:'t1',status:'queued'});
      const match=path.match(/^\/api\/tasks\/([^/]+)(\/memory-decision)?$/);
      if(match){
        const record=h.records.get(match[1]);if(!record)return reply({detail:'not found'},404);
        if(!match[2])return reply(record);
        const action=JSON.parse(options.body).action,next={...record,memory_update:{...record.memory_update,status:action==='accept'?'saved':'dismissed'}};
        h.records.set(next.id,next);if(action==='accept')h.preferences={role:r.mode,values:next.memory_update.values,version:3};return reply(next);
      }
      throw Error(`未配置 ${r.method} ${path}`);
    }}));
  h.login=async(token='token-A')=>{h.el('token').value=token;await h.el('login-form').onsubmit(event);};
  h.open=(index=0)=>h.el('history').children[index].children[1].onclick();
  h.change=async role=>{h.el('mode').value=role;await h.el('mode').onchange();};
  h.text=()=>h.el('task-memory-update').textContent;
  h.buttons=()=>{const result=[];const visit=n=>{if(n.tagName==='button')result.push(n);for(const child of n.children||[])visit(child);};visit(h.el('task-memory-update'));return result;};
  h.click=label=>{const b=h.buttons().find(x=>x.textContent===label);assert.ok(b,`缺少按钮：${label}`);return b.onclick();};
  h.decisions=()=>h.requests.filter(r=>r.path.endsWith('/memory-decision'));
  return h;
}

test('偏好保存与任务快照分开展示，来源和30分钟确认期清楚',async()=>{
  assert.match(markup,/id="task-memory-update"/);const h=harness();h.records.set('t1',task());await h.login();h.open();
  assert.match(h.text(),/待你确认.*适用角色：买家.*简明元.*简洁.*来源原话/);assert.match(h.text(),/有效期为30分钟/);assert.match(h.text(),/任务开始时的快照/);
  assert.match(h.el('task-preferences').textContent,/详细/);assert.equal(h.decisions().length,0);
});

test('确认只向原任务传action，账号角色由headers绑定，保存后刷新偏好而不重跑模型',async()=>{
  const h=harness();h.records.set('t1',task());await h.login();h.open();await h.click('保存这个偏好');
  const r=h.decisions()[0];assert.equal(r.method,'POST');assert.deepEqual(JSON.parse(r.options.body),{action:'accept'});assert.equal(r.token,'Bearer token-A');assert.equal(r.mode,'buyer');
  assert.match(h.text(),/已保存聊天偏好/);assert.equal(h.el('amount-format').value,'yuan');assert.equal(h.el('answer-style').value,'brief');
  assert.equal(h.requests.filter(r=>r.path==='/api/tasks'&&r.method==='POST').length,0);assert.match(h.el('task-preferences').textContent,/详细/);
});

test('忽略后不保存偏好，重开历史保留忽略结果',async()=>{
  const h=harness();h.records.set('t1',task());await h.login();h.open();await h.click('忽略');assert.deepEqual(JSON.parse(h.decisions()[0].options.body),{action:'dismiss'});
  assert.match(h.text(),/已忽略/);h.el('new-chat').onclick();assert.equal(h.text(),'');h.open();assert.match(h.text(),/已忽略/);assert.equal(h.el('amount-format').value,'');
});

test('确认进行中禁重复，重新打开同一历史也不能发第二次操作',async()=>{
  const h=harness(),pending=deferred();h.records.set('t1',task());h.route=r=>r.path.endsWith('/memory-decision')?pending.promise:undefined;await h.login();h.open();
  const saving=h.click('保存这个偏好');await flush();assert.ok(h.buttons().every(b=>b.disabled));await h.click('忽略');h.open();assert.ok(h.buttons().every(b=>b.disabled));await h.click('保存这个偏好');assert.equal(h.decisions().length,1);
  pending.resolve(reply(task('t1',memory('saved'))));await saving;assert.match(h.text(),/已保存/);
});

test('退出和新登录清理原话，旧确认成功不能污染新身份',async()=>{
  const h=harness(),pending=deferred();h.records.set('t1',task('t1',memory('proposed',{source_quote:'PRIVATE-OLD'})));h.route=r=>r.path.endsWith('/memory-decision')?pending.promise:undefined;
  await h.login();h.open();const saving=h.click('保存这个偏好');await flush();h.el('logout').onclick();h.records.clear();await h.login('token-B');pending.resolve(reply(task('t1',memory('saved',{source_quote:'PRIVATE-OLD'}))));await saving;
  assert.equal(h.text(),'');assert.equal(h.el('error').textContent,'');assert.equal(h.el('amount-format').value,'');
});

test('切角色后旧确认网络失败不能显示旧错误或刷新新角色偏好',async()=>{
  const h=harness(),pending=deferred();h.records.set('t1',task());h.route=r=>r.path.endsWith('/memory-decision')?pending.promise:undefined;await h.login();h.open();const saving=h.click('保存这个偏好');await flush();
  await h.change('merchant');const count=h.requests.length;pending.reject(Error('PRIVATE-OLD'));await saving;assert.equal(h.text(),'');assert.equal(h.el('error').textContent,'');assert.equal(h.requests.length,count);
});

test('同角色查看另一任务，旧确认完成不覆盖当前任务卡片',async()=>{
  const h=harness(),pending=deferred();h.records.set('t1',task());h.records.set('t2',task('t2',memory('dismissed',{source_quote:'第二条原话'})));h.route=r=>r.path.endsWith('/memory-decision')?pending.promise:undefined;
  await h.login();h.open();const saving=h.click('保存这个偏好');await flush();h.open(1);pending.resolve(reply(task('t1',memory('saved'))));await saving;assert.match(h.text(),/第二条原话/);assert.doesNotMatch(h.text(),/已保存聊天偏好/);
});

test('确认响应丢失时GET恢复已保存结果，不重复写入',async()=>{
  const h=harness();h.records.set('t1',task());h.route=r=>{if(r.path.endsWith('/memory-decision')){h.records.set('t1',task('t1',memory('saved')));return Promise.reject(Error('response lost'));}};
  await h.login();h.open();await h.click('保存这个偏好');assert.match(h.text(),/已保存/);assert.equal(h.decisions().length,1);assert.equal(h.requests.filter(r=>r.path==='/api/tasks/t1').length,1);
});

test('确认与GET均失败显示未知，手动核对只GET，未自动重发任务或确认',async()=>{
  const h=harness();h.records.set('t1',task());h.route=r=>r.path==='/api/tasks/t1'||r.path.endsWith('/memory-decision')?Promise.reject(Error('offline')):undefined;await h.login();h.open();await h.click('保存这个偏好');
  assert.match(h.text(),/结果尚未确认/);assert.deepEqual(h.buttons().map(b=>b.textContent),['核对保存结果']);h.route=()=>undefined;await h.click('核对保存结果');
  assert.match(h.text(),/待你确认/);assert.equal(h.decisions().length,1);assert.equal(h.requests.filter(r=>r.path==='/api/tasks'&&r.method==='POST').length,0);
});

for(const status of ['unknown','preparing'])test(`${status}核对服务端原保存，不新增模型任务`,async()=>{
  const h=harness();h.records.set('t1',task('t1',memory(status)));await h.login();h.open();await h.click('核对保存结果');assert.equal(h.decisions().length,1);assert.deepEqual(JSON.parse(h.decisions()[0].options.body),{action:'accept'});assert.match(h.text(),/已保存/);assert.equal(h.requests.filter(r=>r.path==='/api/tasks'&&r.method==='POST').length,0);
});

test('过期、冲突、拒绝等状态不得出现保存按钮，未知角色不显示原话',async()=>{
  const h=harness();const states=['expired','conflict','rejected','not_saved','discarded','dismissed','saved','unchanged'];for(const status of states)h.records.set(status,task(status,memory(status)));h.records.set('foreign',task('foreign',memory('proposed',{role:'merchant',source_quote:'PRIVATE-FOREIGN'})));
  await h.login();for(let i=0;i<states.length;i++){h.open(i);assert.equal(h.buttons().length,0,states[i]);}h.open(states.length);assert.match(h.text(),/范围或状态无法核对/);assert.doesNotMatch(h.text(),/PRIVATE-FOREIGN/);
});

test('客户端发现候选期限已过或缺少期限，不提供确认保存',async()=>{
  const h=harness();h.records.set('old',task('old',memory('proposed',{expires_at:'2000-01-01T00:00:00Z'})));h.records.set('broken',task('broken',memory('proposed',{expires_at:null})));await h.login();h.open();assert.match(h.text(),/确认时间已过/);assert.deepEqual(h.buttons().map(b=>b.textContent),['核对保存结果']);h.open(1);assert.match(h.text(),/有效期无法核对/);assert.equal(h.decisions().length,0);
});

test('保存聊天偏好不覆盖设置表单未保存草稿',async()=>{
  const h=harness();h.records.set('t1',task());await h.login();h.open();h.el('answer-style').value='detailed';const reads=h.requests.filter(r=>r.path==='/api/preferences').length;await h.click('保存这个偏好');
  assert.equal(h.el('answer-style').value,'detailed');assert.match(h.el('preferences-status').textContent,/保留你正在修改/);assert.equal(h.requests.filter(r=>r.path==='/api/preferences').length,reads);assert.match(h.text(),/已保存/);
});

test('保存后刷新期间新草稿也不被晚到响应覆盖',async()=>{
  const h=harness(),pending=deferred();h.records.set('t1',task());await h.login();h.open();h.route=r=>r.path==='/api/preferences'?pending.promise:undefined;
  const saving=h.click('保存这个偏好');await flush();h.el('answer-style').value='detailed';pending.resolve(reply({role:'buyer',values:{amount_format:'yuan',answer_style:'brief'},version:3}));await saving;
  assert.equal(h.el('answer-style').value,'detailed');assert.match(h.el('preferences-status').textContent,/保留你尚未保存/);
});

test('完成任务自动保存提示显示并刷新设置，旧无memory_update任务仍可打开',async()=>{
  const h=harness();h.records.set('t1',task('t1',memory('saved')));h.records.set('old',task('old',undefined));delete h.records.get('old').memory_update;await h.login();h.el('message').value='以后金额用元';await h.el('ask-form').onsubmit(event);
  assert.match(h.text(),/已保存/);assert.equal(h.requests.filter(r=>r.path==='/api/tasks'&&r.method==='POST').length,1);h.open(1);assert.equal(h.text(),'');
});

test('原话与notice只按文本显示，不生成HTML节点',async()=>{
  const h=harness();h.records.set('t1',task('t1',memory('proposed',{source_quote:'<img src=x onerror=alert(1)>',notice:'<script>alert(1)</script>'})));await h.login();h.open();assert.match(h.text(),/<img/);assert.match(h.text(),/<script>/);
  const tags=[];const visit=n=>{tags.push(n.tagName);for(const child of n.children)visit(child);};visit(h.el('task-memory-update'));assert.equal(tags.includes('script'),false);assert.equal(tags.includes('img'),false);
});

test('聊天已保存时，任务临时覆盖区不错误声称长期偏好未写入',async()=>{
  const h=harness(),r=task('t1',memory('saved'));r.memory.overrides={answer_style:'current_turn_explicit'};h.records.set('t1',r);await h.login();h.open();
  assert.match(h.el('task-preferences').textContent,/聊天偏好是否长期保存见下方/);assert.doesNotMatch(h.el('task-preferences').textContent,/未改写已保存偏好/);
});


test('真实后端Unix秒时间戳显示截止时间并提供确认按钮',async()=>{
  const h=harness(),now=Math.floor(Date.now()/1000);h.records.set('t1',task('t1',memory('proposed',{created_at:now,expires_at:now+1800})));await h.login();h.open();
  assert.match(h.text(),/确认有效期为30分钟/);assert.doesNotMatch(h.text(),/有效期无法核对/);assert.deepEqual(h.buttons().map(b=>b.textContent),['保存这个偏好','忽略']);await h.click('保存这个偏好');assert.match(h.text(),/已保存聊天偏好/);
});

test('真实后端过期秒时间戳不提供保存按钮，非法时间不能误判为有效',async()=>{
  const h=harness(),now=Math.floor(Date.now()/1000);h.records.set('expired-seconds',task('expired-seconds',memory('proposed',{expires_at:now-1})));h.records.set('bad-seconds',task('bad-seconds',memory('proposed',{expires_at:Infinity})));await h.login();h.open();assert.match(h.text(),/确认时间已过/);assert.deepEqual(h.buttons().map(b=>b.textContent),['核对保存结果']);h.open(1);assert.match(h.text(),/有效期无法核对/);assert.equal(h.decisions().length,0);
});

test('确认成功仅刷新当前所选历史任务，其他回答仍在历史记录，不新增模型请求',async()=>{
  const h=harness();h.records.set('t1',task('t1',memory('proposed'),{answer:'原业务回答。偏好建议尚未保存。'}));h.records.set('t2',task('t2',undefined,{answer:'另一任务的业务回答。'}));
  h.route=r=>{if(r.path.endsWith('/memory-decision')){const updated=task('t1',memory('saved'),{answer:'原业务回答。偏好已保存，后续任务生效。'});h.records.set('t1',updated);return reply(updated);}};
  await h.login();h.open(1);h.open(0);h.open(0);assert.match(h.el('conversation').textContent,/尚未保存/);await h.click('保存这个偏好');
  assert.doesNotMatch(h.el('conversation').textContent,/尚未保存/);assert.match(h.el('conversation').textContent,/偏好已保存/);assert.doesNotMatch(h.el('conversation').textContent,/另一任务的业务回答/);assert.equal(h.records.get('t2').answer,'另一任务的业务回答。');assert.equal(h.requests.filter(r=>r.path==='/api/tasks'&&r.method==='POST').length,0);
});

test('查看其他任务后晚到的确认正文不替换原任务或当前任务内容',async()=>{
  const h=harness(),pending=deferred();h.records.set('t1',task('t1',memory('proposed'),{answer:'第一条旧正文。'}));h.records.set('t2',task('t2',undefined,{answer:'第二条正文。'}));h.route=r=>r.path.endsWith('/memory-decision')?pending.promise:undefined;
  await h.login();h.open();const saving=h.click('保存这个偏好');await flush();h.open(1);pending.resolve(reply(task('t1',memory('saved'),{answer:'第一条新通知。'})));await saving;
  assert.doesNotMatch(h.el('conversation').textContent,/第一条旧正文/);assert.match(h.el('conversation').textContent,/第二条正文/);assert.doesNotMatch(h.el('conversation').textContent,/第一条新通知/);
});
