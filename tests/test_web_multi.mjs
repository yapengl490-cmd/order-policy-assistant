// 助手离线回归：运行真实 app.js，DOM/HTTP 使用隔离替身，不发模型请求。
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
const budget={limits:{model_requests:4,tool_calls:6,reserved_output_tokens:4800},used:{model_requests:4,tool_calls:2,reserved_output_tokens:4800},timeout_seconds:90,elapsed_seconds:3,remaining_seconds:87,stop_reason:null,last_denial:null,monetary_hard_limit:false};
const row=(id='done',patch={})=>({id,message:'核查退款并说明政策',execution_mode:'multi',status:'partial',answer:'金额已查证；政策读取失败。',facts:[{tool:'check_order',result:{net_amount_fen:8500}}],budget,orchestration:{requested:'multi',effective:'multi',wall_seconds:3,branches:[{name:'amount',status:'completed',wall_seconds:2.5,usage:{model_requests:2,tool_calls:1},evidence_status:'tool_results_available'},{name:'policy',status:'failed',wall_seconds:2,usage:{model_requests:2,tool_calls:1},evidence_status:'no_tool_evidence'}]},...patch});
function harness(){
  const h={requests:[],route:()=>undefined,rows:[]};
  class Node{
    constructor(tag){this.tagName=tag;this.children=[];this.value='';this._text='';this.disabled=false;this.hidden=false;}
    set textContent(v){this._text=String(v);this.children=[];}
    get textContent(){return this._text+this.children.map(x=>typeof x==='string'?x:x.textContent).join('');}
    replaceChildren(...items){this._text='';this.children=items;}
    append(...items){this.children.push(...items);}
    click(){return this.onclick?.();}
  }
  const nodes=new Map();h.el=id=>{if(!nodes.has(id))nodes.set(id,new Node('div'));return nodes.get(id);};
  let seq=0;
  const context=vm.createContext({document:{getElementById:h.el,createElement:t=>new Node(t)},crypto:{randomUUID:()=>`session-${++seq}`},console,setTimeout:callback=>setTimeout(callback,0),fetch:async(path,options)=>{
    const r={path,options,method:options.method||'GET',mode:options.headers['X-Access-Mode']};h.requests.push(r);
    const custom=h.route(r);if(custom!==undefined)return custom;
    if(path==='/api/me')return reply({role:r.mode,buyer_id:'B001',shop_id:'S001'});
    if(path==='/api/orders')return reply({orders:[]});
    if(path==='/api/preferences')return reply({role:r.mode,values:{},version:0});
    if(path==='/api/tasks'&&r.method==='GET')return reply(h.rows);
    if(path==='/api/tasks'&&r.method==='POST')return reply({id:'done',status:'queued'});
    if(path==='/api/tasks/done')return reply(row('done',{execution_mode:JSON.parse(h.requests.findLast(x=>x.method==='POST').options.body).execution_mode}));
    throw Error(`未配置测试响应 ${path}`);
  }});
  vm.runInContext(source,context);
  h.login=async(token='private-token')=>{h.el('token').value=token;await h.el('login-form').onsubmit(event);};
  h.ask=async(message='核查退款并说明政策')=>{h.el('message').value=message;await h.el('ask-form').onsubmit(event);};
  h.change=mode=>{h.el('execution-mode').value=mode;return h.el('execution-mode').onchange();};
  h.posts=()=>h.requests.filter(r=>r.method==='POST').map(r=>JSON.parse(r.options.body));
  return h;
}

test('页面提供两方案与整条共享额度说明，默认单路，选择不会自动调用模型',async()=>{
  assert.match(markup,/id="execution-mode"/);assert.match(markup,/单智能体（默认）/);assert.match(markup,/多角色对照（售后顺序／金额政策并行）/);assert.match(markup,/整条任务共享/);
  const h=harness();await h.login();assert.equal(h.el('execution-mode').value,'single');h.change('multi');assert.equal(h.posts().length,0);
  assert.match(h.el('status').textContent,/新建对话.*发送后才调用模型/);
});

test('两种方案提交使用不同会话，当前选择写入任务且不改旧记录',async()=>{
  const h=harness();await h.login();await h.ask();assert.equal(h.posts()[0].execution_mode,'single');
  assert.match(h.el('conversation').textContent,/金额已查证/);h.change('multi');assert.equal(h.el('conversation').textContent,'');assert.equal(h.el('task-execution').textContent,'');
  await h.ask();const [a,b]=h.posts();assert.equal(b.execution_mode,'multi');assert.notEqual(a.session_id,b.session_id);assert.notEqual(a.request_id,b.request_id);
});

test('部分完成为终态，展示成功与失败分支、全局计数和真实事实，不继续轮询',async()=>{
  const h=harness();await h.login();h.change('multi');await h.ask();
  assert.match(h.el('status').textContent,/部分完成/);assert.equal(h.el('send').disabled,false);assert.equal(h.el('execution-mode').disabled,false);
  const text=h.el('task-execution').textContent;
  assert.match(text,/本任务只完成了部分/);assert.match(text,/金额核查：已完成.*2\.50 秒/);assert.match(text,/政策解释：处理失败/);
  assert.match(text,/模型请求 4\/4；工具尝试 2\/6/);assert.match(text,/预留量不等于实际用量或费用/);
  assert.match(h.el('facts').textContent,/8500/);assert.equal(h.requests.filter(r=>r.path==='/api/tasks/done').length,1);
});

test('提交结果未知时不能切换方案，重试保持原方案/请求号/会话',async()=>{
  const h=harness();let n=0;h.route=r=>r.path==='/api/tasks'&&r.method==='POST'?(++n===1?Promise.reject(Error('lost')):reply({id:'done',status:'queued'})):undefined;
  await h.login();h.change('multi');await h.ask();assert.equal(h.el('execution-mode').disabled,true);h.change('single');assert.equal(h.el('execution-mode').value,'multi');
  assert.match(h.el('error').textContent,/上次提交尚未确认/);await h.ask();assert.deepEqual(h.posts()[0],h.posts()[1]);assert.equal(h.posts()[1].execution_mode,'multi');
});

test('在途任务不能改方案；旧身份完成不能污染新身份的执行摘要',async()=>{
  const h=harness(),pending=deferred();h.route=r=>r.path==='/api/tasks/done'?pending.promise:undefined;
  await h.login();h.change('multi');const asking=h.ask();await flush();assert.equal(h.el('execution-mode').disabled,true);
  h.change('single');assert.equal(h.el('execution-mode').value,'multi');h.el('logout').onclick();await h.login('other-token');assert.equal(h.el('execution-mode').value,'single');
  pending.resolve(reply(row()));await asking;assert.equal(h.el('task-execution').textContent,'');assert.equal(h.el('status').textContent,'');
});

test('历史重开展示已保存执行方案，政策证据不足单列，不由当前选择覆盖',async()=>{
  const h=harness();const r=row('history',{status:'completed'});r.orchestration.branches[1]={name:'policy',status:'completed',evidence_status:'insufficient_policy_evidence',usage:{model_requests:2,tool_calls:1}};h.rows=[r];
  await h.login();assert.match(h.el('history').textContent,/双分支对照/);assert.equal(h.el('execution-mode').value,'single');h.el('history').children[0].children[1].onclick();
  assert.match(h.el('task-execution').textContent,/执行方案：双分支对照/);assert.match(h.el('task-execution').textContent,/政策依据不足/);
});

test('条件未齐或无双职责时展示实际单路回退，旧任务没有摘要也不伪造',async()=>{
  const h=harness();h.rows=[row('clarify',{status:'clarification',orchestration:{requested:'multi',effective:'single',reason:'conditions_not_ready',branches:[],wall_seconds:0}}),{id:'old',status:'completed',message:'旧任务',answer:'旧答案',facts:[]}];await h.login();
  h.el('history').children[0].children[1].onclick();assert.match(h.el('task-execution').textContent,/实际使用单智能体/);assert.match(h.el('task-execution').textContent,/条件尚未齐全.*没有拆分调用/);
  h.el('history').children[1].children[1].onclick();assert.equal(h.el('task-execution').textContent,'');
});

test('执行摘要仅作文本展示，超时与额度不足不显示为成功',async()=>{
  const h=harness();h.rows=[row('hostile',{status:'limited',budget:{...budget,stop_reason:'deadline',last_denial:'model_requests_limit'},orchestration:{requested:'multi',effective:'multi',branches:[{name:'<img src=x onerror=alert(1)>',status:'failed'}]}})];await h.login();h.el('history').children[0].children[1].onclick();
  const text=h.el('task-execution').textContent;assert.match(text,/<img src=x onerror=alert\(1\)>/);assert.match(text,/达到任务等待上限/);assert.match(text,/共享调用额度不足/);assert.equal(h.el('task-execution').children[0].children.at(-1).children[0].children.length,0);
});

test('三个新增方案分别提交和历史重开，展示自身执行证据，不自动调用模型',async()=>{
  const labels={batch_sequential:'批量普通工具调用',programmatic:'批量隔离短程序',snapshot_sql:'授权快照查询对照'};
  for(const [mode,label] of Object.entries(labels)){
    assert.match(markup,new RegExp(`value="${mode}"`));
    const h=harness();await h.login();h.change(mode);assert.equal(h.posts().length,0);
    h.route=r=>r.path==='/api/tasks/done'?reply(row('done',{execution_mode:mode,status:'completed',
      orchestration:{requested:mode,effective:mode,wall_seconds:1,branches:[]},
      program_executions:mode==='programmatic'?[{status:'ok',execution:{tool_calls:3,container_cleanup:'removed',in_flight_tool_may_continue:true}}]:undefined,
      sql_validation:mode==='snapshot_sql'?{syntax:'passed',execution:'passed',metric:'passed'}:undefined})):undefined;
    await h.ask('使用当前所选方案核对已授权订单');assert.equal(h.posts()[0].execution_mode,mode);
    assert.match(h.el('task-execution').textContent,new RegExp(`执行方案：${label}`));
    if(mode==='programmatic'){
      assert.match(h.el('task-execution').textContent,/工具尝试 3；容器回收 removed/);
      assert.match(h.el('task-execution').textContent,/不能把取消视为远端请求已撤回/);
    }
    if(mode==='snapshot_sql')assert.match(h.el('task-execution').textContent,/金额口径一致.*"metric": "passed"/s);
    h.rows=[row('old-mode',{execution_mode:mode,orchestration:{requested:mode,effective:mode,branches:[]}})];
    await h.el('refresh').onclick();h.change('single');h.el('history').children[0].children[1].onclick();
    assert.match(h.el('task-execution').textContent,new RegExp(`执行方案：${label}`));
  }
});

const comparison=(marker='buyer-result')=>({status:'ok',
  first:{paid_amount_fen:12500,refund_amount_fen:4000,net_amount_fen:8500},
  second:{paid_amount_fen:10000,refund_amount_fen:0,net_amount_fen:10000},
  delta:{paid_amount_fen:-2500,refund_amount_fen:-4000,net_amount_fen:1500},
  channels:[{label:marker,presence:'continued',contribution_net_fen:1500}],
  reason_notice:'金额变化不代表已知原因。'});
function comparisonInputs(h){
  for(const [id,value] of Object.entries({'first-from':'2026-10-01T00:00','first-before':'2026-10-17T00:00',
    'second-from':'2026-10-17T00:00','second-before':'2026-11-01T00:00','compare-cutoff':'2026-11-03T00:00'}))h.el(id).value=value;
}

test('两期比较发送明确北京时间边界及可信角色，金额与渠道显示且不创建模型任务',async()=>{
  const h=harness();await h.login();comparisonInputs(h);
  h.route=r=>r.path==='/api/analysis/compare'?reply(comparison()):undefined;
  await h.el('compare-form').onsubmit(event);
  const request=h.requests.find(r=>r.path==='/api/analysis/compare');assert.equal(request.mode,'buyer');
  assert.deepEqual(JSON.parse(request.options.body),{first_from:'2026-10-01T00:00:00+08:00',first_before:'2026-10-17T00:00:00+08:00',second_from:'2026-10-17T00:00:00+08:00',second_before:'2026-11-01T00:00:00+08:00',refund_before:'2026-11-03T00:00:00+08:00'});
  assert.match(h.el('comparison').textContent,/支付金额125\.00 元100\.00 元-25\.00 元/);
  assert.match(h.el('comparison').textContent,/buyer-result：净额差 15\.00 元/);
  assert.match(h.el('comparison').textContent,/金额变化不代表已知原因/);
  assert.equal(h.requests.filter(r=>r.path==='/api/tasks'&&r.method==='POST').length,0);
  assert.equal(h.el('compare-submit').disabled,false);
});

test('比较请求跨角色晚到不污染新角色，并恢复新角色的比较按钮',async()=>{
  const h=harness(),pending=deferred();await h.login();comparisonInputs(h);
  h.route=r=>r.path==='/api/analysis/compare'?pending.promise:undefined;
  const old=h.el('compare-form').onsubmit(event);await flush();assert.equal(h.el('compare-submit').disabled,true);
  h.el('mode').value='merchant';await h.el('mode').onchange();
  assert.equal(h.el('compare-submit').disabled,false,'新授权范围应可提交自己的比较');
  pending.resolve(reply(comparison('OLD-BUYER-PRIVATE')));await old;
  assert.equal(h.el('comparison').textContent,'');assert.equal(h.el('error').textContent,'');
  assert.equal(h.el('compare-submit').disabled,false);
});

test('比较失败只显示错误，不保留部分总额；退出后旧失败也不污染新登录',async()=>{
  const h=harness();await h.login();comparisonInputs(h);
  h.route=r=>r.path==='/api/analysis/compare'?reply({status:'too_many_rows',message:'范围过大，未显示部分总额。'}):undefined;
  await h.el('compare-form').onsubmit(event);assert.equal(h.el('comparison').textContent,'范围过大，未显示部分总额。');
  const pending=deferred();h.route=r=>r.path==='/api/analysis/compare'?pending.promise:undefined;
  const old=h.el('compare-form').onsubmit(event);await flush();h.el('logout').onclick();await h.login('other-token');
  pending.resolve(reply({detail:'旧买家请求错误'},422));await old;
  assert.equal(h.el('comparison').textContent,'');assert.equal(h.el('error').textContent,'');assert.equal(h.el('compare-submit').disabled,false);
});

// 新顺序模式必须展示已保存交接，不由页面当前选择伪造。
test('售后顺序协作展示事实交接与有界补查，保留原并行历史',async()=>{
  const h=harness();h.rows=[row('dependent',{status:'completed',orchestration:{requested:'multi',effective:'dependent',handoff:{sha256:'abc-frozen-evidence'},followup:{attempts:1,limit:1,status:'completed'},branches:[{name:'order_facts',status:'completed',usage:{model_requests:1,tool_calls:1}},{name:'policy',status:'completed',usage:{model_requests:3,tool_calls:2}}]}})];
  await h.login();h.el('history').children[0].children[1].onclick();
  const text=h.el('task-execution').textContent;assert.match(text,/售后顺序协作/);assert.match(text,/abc-frozen-evidence/);assert.match(text,/证据补查：1\/1 次/);assert.match(text,/订单事实：已完成/);assert.equal(h.posts().length,0);
});
