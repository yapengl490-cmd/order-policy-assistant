// 助手离线网页回归：真实app.js + 小DOM/可延迟fetch替身；不访问真实服务。
import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const source=readFileSync(new URL("../web/app.js",import.meta.url),"utf8");
const event={preventDefault(){}};
const reply=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body,blob:async()=>new Blob([JSON.stringify(body)],{type:"application/json"})});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const flush=async()=>{for(let i=0;i<4;i++)await new Promise(setImmediate);};
const fact={status:"ok",order_id:"PRIVATE-O001",paid_amount_fen:12500,refund_amount_fen:2500,
  net_amount_fen:10000,refund_count:1,refunds:[{refund_id:"PRIVATE-R001",refunded_at:"2026-10-20T00:00:00Z",refund_amount_fen:2500}],data_assumption:"合成数据"};
const task=(id,answer)=>({id,status:"completed",message:"已存问题",answer,facts:[{tool:"check_order",result:fact}]});

function harness(){
  const env={requests:[],downloads:[],createdUrls:[],route:()=>undefined,historyA:[],historyB:[]};
  class Node{
    constructor(tag){this.tagName=tag;this.children=[];this._text="";this.value="";this.hidden=false;this.disabled=false;}
    get textContent(){return this._text+this.children.map(x=>typeof x==="string"?x:x.textContent).join("");}
    set textContent(value){this._text=String(value);this.children=[];}
    replaceChildren(...items){this._text="";this.children=items;}
    append(...items){this.children.push(...items);}
    click(){if(this.tagName==="a")env.downloads.push(this.download);return this.onclick?.();}
  }
  const nodes=new Map();env.el=id=>{if(!nodes.has(id))nodes.set(id,new Node("div"));return nodes.get(id);};
  env.el("workspace").hidden=true;env.el("mode").value="buyer";
  let uuid=0;
  const context=vm.createContext({
    document:{getElementById:env.el,createElement:tag=>new Node(tag)},
    crypto:{randomUUID:()=>`test-uuid-${++uuid}`},console,Blob,
    URL:{createObjectURL:blob=>{env.createdUrls.push(blob);return `blob:test-${env.createdUrls.length}`;},revokeObjectURL(){}},
    setTimeout:(callback)=>setTimeout(callback,0),
    fetch:async(path,options)=>{
      const request={path,options,key:options.headers.Authorization,mode:options.headers["X-Access-Mode"]};env.requests.push(request);
      const custom=env.route(request);if(custom!==undefined)return custom;
      const a=request.key==="Bearer token-A",b=request.key==="Bearer token-B";
      if((!a&&!b)||(request.mode==="merchant"&&!a))return reply({detail:"本次凭证无此权限"},401);
      if(path==="/api/me")return reply(request.mode==="merchant"?{role:"merchant",shop_id:"S001"}:{role:"buyer",buyer_id:a?"B001":"B002"});
      if(path==="/api/orders")return reply({orders:a?[{order_id:"O001"}]:[]});
      if(path==="/api/preferences")return reply({role:request.mode,values:{},version:0,updated_at:null,source:null});
      if(path==="/api/tasks"&&!options.method)return reply(a?env.historyA:env.historyB);
      if(path.startsWith("/api/orders/"))return reply(a?fact:{status:"not_found",message:"当前范围未找到"});
      throw Error(`未配置测试请求：${options.method||"GET"} ${path}`);
    },
  });
  vm.runInContext(source,context,{filename:"web/app.js"});
  env.login=async key=>{env.el("token").value=key;await env.el("login-form").onsubmit(event);};
  env.logout=()=>env.el("logout").onclick();
  env.check=()=>{env.el("order").value="O001";return env.el("check-form").onsubmit(event);};
  env.ask=message=>{env.el("message").value=message;return env.el("ask-form").onsubmit(event);};
  env.changeMode=mode=>{env.el("mode").value=mode;return env.el("mode").onchange();};
  return env;
}

test("查订单后退出并登录另一买家，全部旧业务显示和输入被清除",async()=>{
  const h=harness();h.historyA=[task("old","PRIVATE-OLD-ANSWER")];await h.login("token-A");await h.check();
  assert.match(h.el("order-result").textContent,/125\.00/);assert.match(h.el("history").textContent,/已存问题/);
  h.el("message").value="私人草稿";h.el("status").textContent="私人状态";h.logout();
  for(const id of ["identity","orders","order-result","conversation","facts","history","status","error"])assert.equal(h.el(id).textContent,"",id);
  assert.equal(h.el("order").value,"");assert.equal(h.el("message").value,"");assert.equal(h.el("token").value,"");
  assert.equal(h.el("cancel").disabled,true);assert.equal(h.el("send").disabled,false);
  await h.login("token-B");assert.match(h.el("identity").textContent,/B002/);
  assert.equal(h.el("order-result").textContent,"");assert.equal(h.el("history").textContent,"");
});

test("任务条件显示用户来源且退出后不留给下一身份",async()=>{
  const h=harness();
  const row=task("condition-task","已有条件");
  row.conditions={revision:2,confirmed:{refund_before:{value:"2026-11-03T00:00:00+08:00",source:{quote:"PRIVATE-截至十一月三日"}}},defaults:{},missing:[]};
  h.historyA=[row];await h.login("token-A");h.el("history").children[0].children[1].onclick();
  assert.match(h.el("conditions").textContent,/2026-11-03/);
  assert.match(h.el("conditions").textContent,/PRIVATE-/);
  h.logout();await h.login("token-B");assert.equal(h.el("conditions").textContent,"");
});

test("旧订单在途响应不能写入新买家页面",async()=>{
  const h=harness(),pending=deferred();await h.login("token-A");
  h.route=r=>r.path.startsWith("/api/orders/")&&r.key==="Bearer token-A"?pending.promise:undefined;
  const checking=h.check();await flush();h.logout();await h.login("token-B");pending.resolve(reply(fact));await checking;
  assert.equal(h.el("order-result").textContent,"");assert.match(h.el("identity").textContent,/B002/);assert.equal(h.el("error").textContent,"");
});

test("旧订单网络异常不能污染新身份错误栏",async()=>{
  const h=harness(),pending=deferred();await h.login("token-A");
  h.route=r=>r.path.startsWith("/api/orders/")&&r.key==="Bearer token-A"?pending.promise:undefined;
  const checking=h.check();await flush();h.logout();await h.login("token-B");pending.reject(Error("PRIVATE-OLD-ERROR"));await checking;
  assert.equal(h.el("error").textContent,"");assert.equal(h.el("order-result").textContent,"");
});

test("旧任务完成及finally不能写新会话或解除新任务执行中的按钮",async()=>{
  const h=harness(),a=deferred(),b=deferred();
  h.route=r=>{
    if(r.path==="/api/tasks"&&r.options.method==="POST")return reply({id:r.key==="Bearer token-A"?"task-A":"task-B",status:"queued"});
    if(r.path==="/api/tasks/task-A")return a.promise;if(r.path==="/api/tasks/task-B")return b.promise;
  };
  await h.login("token-A");const first=h.ask("旧问题");await flush();h.logout();await h.login("token-B");const second=h.ask("新问题");await flush();
  assert.equal(h.el("send").disabled,true);assert.equal(h.el("mode").disabled,true);
  a.resolve(reply(task("task-A","PRIVATE-OLD-ANSWER")));await first;
  assert.equal(h.el("send").disabled,true);assert.equal(h.el("mode").disabled,true);
  assert.doesNotMatch(h.el("conversation").textContent,/PRIVATE-OLD|旧问题/);assert.equal(h.el("facts").textContent,"");
  b.resolve(reply({id:"task-B",status:"completed",answer:"新回答",facts:[]}));await second;
  assert.equal(h.el("send").disabled,false);assert.match(h.el("conversation").textContent,/新回答/);
});

test("旧任务受理响应晚到，不启动跨身份轮询",async()=>{
  const h=harness(),post=deferred();h.route=r=>r.path==="/api/tasks"&&r.options.method==="POST"?post.promise:undefined;
  await h.login("token-A");const asking=h.ask("旧问题");await flush();h.logout();await h.login("token-B");post.resolve(reply({id:"task-old",status:"queued"}));await asking;
  assert.equal(h.requests.filter(r=>r.path==="/api/tasks/task-old").length,0);
  assert.equal(h.el("conversation").textContent,"");assert.equal(h.el("send").disabled,false);
});

test("旧历史响应晚到不能覆盖新账号历史",async()=>{
  const h=harness(),pending=deferred();await h.login("token-A");
  h.route=r=>r.path==="/api/tasks"&&!r.options.method&&r.key==="Bearer token-A"?pending.promise:undefined;
  const refreshing=h.el("refresh").onclick();await flush();h.logout();await h.login("token-B");pending.resolve(reply([task("old","PRIVATE") ]));await refreshing;
  assert.equal(h.el("history").textContent,"");assert.equal(h.el("error").textContent,"");
});

test("身份切换后旧导出响应不能创建下载",async()=>{
  const h=harness(),pending=deferred();h.historyA=[task("task-old","PRIVATE")];await h.login("token-A");
  const row=h.el("history").children[0],open=row.children[1],download=row.children[2];
  h.route=r=>r.path==="/api/tasks/task-old/export"?pending.promise:undefined;
  const exporting=download.onclick();await flush();h.logout();await h.login("token-B");pending.resolve(reply({private:"old-export"}));await exporting;
  open.onclick();assert.equal(h.downloads.length,0);assert.equal(h.createdUrls.length,0);assert.equal(h.el("conversation").textContent,"");
});

test("换模式失败的旧回调不能回滚后来登录的买家",async()=>{
  const h=harness(),pending=deferred();await h.login("token-A");
  h.route=r=>r.path==="/api/me"&&r.mode==="merchant"?pending.promise:undefined;
  const switching=h.changeMode("merchant");await flush();h.logout();await h.login("token-B");pending.resolve(reply({detail:"PRIVATE-OLD-MODE-ERROR"},401));await switching;
  assert.equal(h.el("mode").value,"buyer");assert.match(h.el("identity").textContent,/B002/);
  assert.equal(h.el("error").textContent,"");assert.equal(h.el("workspace").hidden,false);
});

test("当前模式切换失败可以恢复原角色并显示当前错误",async()=>{
  const h=harness();await h.login("token-B");await h.changeMode("merchant");
  assert.equal(h.el("mode").value,"buyer");assert.match(h.el("identity").textContent,/B002/);
  assert.match(h.el("error").textContent,/无此权限/);assert.equal(h.el("send").disabled,false);
});

test("成功模式切换清空买家结果并展示授权店铺",async()=>{
  const h=harness();await h.login("token-A");await h.check();await h.changeMode("merchant");
  assert.equal(h.el("order-result").textContent,"");assert.equal(h.el("mode").value,"merchant");assert.match(h.el("identity").textContent,/S001/);
});

test("晚到的取消回复不改变新登录状态",async()=>{
  const h=harness(),polling=deferred(),cancel=deferred();
  h.route=r=>{
    if(r.path==="/api/tasks"&&r.options.method==="POST")return reply({id:"task-A",status:"queued"});
    if(r.path==="/api/tasks/task-A")return polling.promise;if(r.path==="/api/tasks/task-A/cancel")return cancel.promise;
  };
  await h.login("token-A");const asking=h.ask("旧问题");await flush();const cancelling=h.el("cancel").onclick();await flush();h.logout();await h.login("token-B");
  cancel.resolve(reply({id:"task-A",status:"cancelling"}));await cancelling;polling.resolve(reply(task("task-A","PRIVATE")));await asking;
  assert.equal(h.el("status").textContent,"");assert.equal(h.el("error").textContent,"");assert.equal(h.el("conversation").textContent,"");
});

test("旧登录失败不能清掉较新的成功登录",async()=>{
  const h=harness(),pending=deferred();h.route=r=>r.path==="/api/me"&&r.key==="Bearer token-A"?pending.promise:undefined;
  const first=h.login("token-A");await flush();await h.login("token-B");pending.resolve(reply({detail:"旧登录失败"},401));await first;
  assert.match(h.el("identity").textContent,/B002/);assert.equal(h.el("workspace").hidden,false);assert.equal(h.el("error").textContent,"");
});

test("大整数金额拒绝舍入显示，安全整数上界仍精确到分",async()=>{
  const h=harness();await h.login("token-A");
  h.route=r=>r.path.startsWith("/api/orders/")?reply({...fact,paid_amount_fen:9223372036854775807,refund_amount_fen:Number.MAX_SAFE_INTEGER,net_amount_fen:0}):undefined;
  await h.check();const displayed=h.el("order-result").textContent;
  assert.match(displayed,/金额超出浏览器可精确显示范围/);
  assert.match(displayed,/90071992547409\.91 元/);
  assert.doesNotMatch(displayed,/92233720368547760|92233720368547758/);
});

test("嵌套依据含不安全整数时不展示舍入后的JSON",async()=>{
  const h=harness();h.historyA=[{...task("huge","模型说明"),facts:[{tool:"check_order",result:{refunds:[{amount_fen:9007199254740993}]}}]}];
  await h.login("token-A");h.el("history").children[0].children[1].onclick();
  const text=h.el("facts").textContent;assert.match(text,/下载原始证据/);assert.match(text,/未将舍入后的内容当作原始事实/);
  assert.doesNotMatch(text,/9007199254740992/);
});

test("导出直接保存服务端原始字节，保留BIGINT和中文，不JSON解析",async()=>{
  const h=harness();h.historyA=[task("huge","模型说明")];await h.login("token-A");
  const raw='{"金额":9223372036854775807,"嵌套":{"退款":9007199254740993},"文本":"原样保留"}\n';
  let jsonCalls=0;
  h.route=r=>r.path==="/api/tasks/huge/export"?{ok:true,status:200,blob:async()=>new Blob([raw],{type:"application/json"}),json:async()=>{jsonCalls++;return JSON.parse(raw);}}:undefined;
  await h.el("history").children[0].children[2].onclick();
  assert.equal(jsonCalls,0);assert.equal(h.downloads.length,1);assert.equal(h.createdUrls.length,1);
  assert.equal(await h.createdUrls[0].text(),raw);
});

test("导出失败不把错误响应保存成成功证据",async()=>{
  const h=harness();h.historyA=[task("denied","模型说明")];await h.login("token-A");
  h.route=r=>r.path==="/api/tasks/denied/export"?reply({detail:"任务不可读"},404):undefined;
  await h.el("history").children[0].children[2].onclick();
  assert.equal(h.downloads.length,0);assert.match(h.el("error").textContent,/证据导出失败/);
});

test("原始导出响应体晚到也不能跨身份下载",async()=>{
  const h=harness(),body=deferred();h.historyA=[task("late","模型说明")];await h.login("token-A");
  h.route=r=>r.path==="/api/tasks/late/export"?{ok:true,status:200,blob:()=>body.promise}:undefined;
  const exporting=h.el("history").children[0].children[2].onclick();await flush();h.logout();await h.login("token-B");
  body.resolve(new Blob(['{"private":9223372036854775807}']));await exporting;
  assert.equal(h.downloads.length,0);assert.equal(h.createdUrls.length,0);assert.equal(h.el("error").textContent,"");
});


test("提交已受理但响应丢失，重试原问题复用原编号与会话",async()=>{
  const h=harness(),accepted=new Map();let first=true;
  h.route=r=>{
    if(r.path==="/api/tasks"&&r.options.method==="POST"){
      const body=JSON.parse(r.options.body);accepted.set(body.request_id,body);
      if(first){first=false;return Promise.reject(Error("response lost"));}
      return reply({id:"accepted-task",status:"completed"});
    }
    if(r.path==="/api/tasks/accepted-task")return reply(task("accepted-task","仅执行一次"));
  };
  await h.login("token-A");await h.ask("同一个问题");await h.ask("同一个问题");
  const posts=h.requests.filter(r=>r.options.method==="POST").map(r=>JSON.parse(r.options.body));
  assert.equal(posts.length,2);assert.equal(accepted.size,1);assert.deepEqual(posts[0],posts[1]);
  assert.match(h.el("conversation").textContent,/仅执行一次/);
});

test("提交结果不明时阻止把不同内容或新对话混到原请求",async()=>{
  const h=harness();h.route=r=>r.options.method==="POST"?Promise.reject(Error("response lost")):undefined;
  await h.login("token-A");await h.ask("原问题");await h.ask("不同问题");
  assert.equal(h.requests.filter(r=>r.options.method==="POST").length,1);
  assert.match(h.el("error").textContent,/原问题|上次提交/);
  h.el("new-chat").onclick();assert.match(h.el("error").textContent,/原问题|上次提交/);
});

test("明确拒绝后可改问题，轮询失败后则恢复已受理任务",async()=>{
  const h=harness();let rejected=true,polls=0;
  h.route=r=>{
    if(r.options.method==="POST"){
      if(rejected){rejected=false;return reply({detail:"参数错误"},422);}
      return reply({id:"known-task",status:"running"});
    }
    if(r.path==="/api/tasks/known-task"){
      polls++;return polls===1?Promise.reject(Error("connection lost")):reply(task("known-task","已恢复"));
    }
  };
  await h.login("token-A");await h.ask("错误问题");await h.ask("修正问题");await h.ask("修正问题");
  assert.equal(h.requests.filter(r=>r.options.method==="POST").length,2);
  assert.equal(polls,2);assert.match(h.el("conversation").textContent,/已恢复/);
});


test("未知提交之后出现明确拒绝，也不能抛弃最初请求编号",async()=>{
  const h=harness();let posts=0;
  h.route=r=>{
    if(r.options.method==="POST"){
      posts++;
      if(posts===1)return Promise.reject(Error("response lost"));
      if(posts===2)return reply({detail:"暂时无法核验权限"},401);
      return reply({id:"original-task",status:"completed"});
    }
    if(r.path==="/api/tasks/original-task")return reply(task("original-task","原结果"));
  };
  await h.login("token-A");await h.ask("原问题");await h.ask("原问题");await h.ask("原问题");
  const bodies=h.requests.filter(r=>r.options.method==="POST").map(r=>JSON.parse(r.options.body));
  assert.equal(new Set(bodies.map(body=>body.request_id)).size,1);
});
