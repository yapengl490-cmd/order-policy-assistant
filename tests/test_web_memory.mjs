// 助手离线网页回归：真实 app.js + DOM/服务替身，不调用模型或真实账户。
import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const source=readFileSync(new URL("../web/app.js",import.meta.url),"utf8");
const event={preventDefault(){}};
const reply=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body,blob:async()=>new Blob([JSON.stringify(body)])});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const flush=async()=>{for(let i=0;i<4;i++)await new Promise(setImmediate);};
const snapshot=(role="buyer",values={},version=0)=>({role,values,version,updated_at:version?"2026-11-17T01:02:03Z":null,source:version?{kind:"user_settings"}:null});
const fact={status:"ok",order_id:"O001",paid_amount_fen:12500,refund_amount_fen:2505,net_amount_fen:9995,refund_count:1,refunds:[{refund_id:"R001",refunded_at:"2026-11-01T01:00:00Z",refund_amount_fen:2505}],data_assumption:"合成数据"};

function harness(){
  const env={requests:[],route:()=>undefined,store:new Map(),rows:[],downloads:[]};
  class Node{
    constructor(tag){this.tagName=tag;this.children=[];this._text="";this.value="";this.hidden=false;this.disabled=false;}
    get textContent(){return this._text+this.children.map(x=>typeof x==="string"?x:x.textContent).join("");}
    set textContent(value){this._text=String(value);this.children=[];}
    replaceChildren(...items){this._text="";this.children=items;}
    append(...items){this.children.push(...items);}
    click(){return this.onclick?.();}
  }
  const nodes=new Map();env.el=id=>{if(!nodes.has(id))nodes.set(id,new Node("div"));return nodes.get(id);};
  env.el("workspace").hidden=true;env.el("mode").value="buyer";
  let uuid=0;
  const context=vm.createContext({document:{getElementById:env.el,createElement:tag=>new Node(tag)},crypto:{randomUUID:()=>`uuid-${++uuid}`},console,Blob,
    URL:{createObjectURL:blob=>{env.downloads.push(blob);return "blob:test";},revokeObjectURL(){}},setTimeout:callback=>setTimeout(callback,0),
    fetch:async(path,options)=>{
      const request={path,options,key:options.headers.Authorization,mode:options.headers["X-Access-Mode"],method:options.method||"GET"};env.requests.push(request);
      const custom=env.route(request);if(custom!==undefined)return custom;
      const a=request.key==="Bearer token-A",b=request.key==="Bearer token-B";
      if(!a&&!b)return reply({detail:"未登录"},401);
      if(path==="/api/me")return reply(request.mode==="merchant"?{role:"merchant",shop_id:"S001"}:{role:"buyer",buyer_id:a?"B001":"B002"});
      if(path==="/api/orders")return reply({orders:[{order_id:"O001"}]});
      if(path.startsWith("/api/orders/"))return reply(fact);
      if(path==="/api/tasks"&&request.method==="GET")return reply(env.rows);
      if(path==="/api/preferences"){
        const key=`${request.key}/${request.mode}`,current=env.store.get(key)||snapshot(request.mode);
        if(request.method==="GET")return reply(current);
        const body=JSON.parse(options.body);
        if(body.expected_version!==current.version)return reply({detail:"version conflict"},409);
        const next=snapshot(request.mode,request.method==="DELETE"?{}:body.values,current.version+1);env.store.set(key,next);return reply(next);
      }
      throw Error(`未配置 ${request.method} ${path}`);
    },
  });
  vm.runInContext(source,context,{filename:"web/app.js"});
  env.login=async(key="token-A")=>{env.el("token").value=key;await env.el("login-form").onsubmit(event);};
  env.logout=()=>env.el("logout").onclick();
  env.mode=async mode=>{env.el("mode").value=mode;await env.el("mode").onchange();};
  env.set=(values)=>{env.el("amount-format").value=values.amount_format||"";env.el("answer-style").value=values.answer_style||"";};
  env.save=()=>env.el("preferences-form").onsubmit(event);
  env.remove=()=>env.el("preferences-delete").onclick();
  env.refresh=()=>env.el("preferences-refresh").onclick();
  env.check=()=>{env.el("order").value="O001";return env.el("check-form").onsubmit(event);};
  env.prefRequests=()=>env.requests.filter(r=>r.path==="/api/preferences");
  return env;
}

test("登录只读取偏好，未设置不自动替买家或商家保存",async()=>{
  const h=harness();await h.login();assert.equal(h.el("workspace").hidden,false);
  assert.match(h.el("preferences-status").textContent,/尚未设置/);assert.equal(h.el("amount-format").value,"");assert.equal(h.el("answer-style").value,"");
  assert.equal(h.el("preferences-delete").disabled,true);assert.equal(h.el("preferences-save").disabled,false);
  await h.check();assert.match(h.el("order-result").textContent,/125\.00 元/);
  await h.mode("merchant");assert.equal(h.el("amount-format").value,"");
  assert.deepEqual(h.prefRequests().map(r=>r.method),["GET","GET"]);
});

test("显式保存只发送选择项与版本，认证范围来自 headers",async()=>{
  const h=harness();await h.login();h.set({amount_format:"yuan",answer_style:"brief"});assert.equal(h.prefRequests().length,1);
  await h.save();const sent=h.prefRequests().at(-1);
  assert.equal(sent.method,"PUT");assert.equal(sent.key,"Bearer token-A");assert.equal(sent.mode,"buyer");
  assert.deepEqual(JSON.parse(sent.options.body),{expected_version:0,values:{amount_format:"yuan",answer_style:"brief"}});
  assert.match(h.el("preferences-status").textContent,/已保存偏好.*版本 1.*保存时间/);assert.equal(h.el("preferences-delete").disabled,false);
  h.set({answer_style:"detailed"});await h.save();assert.deepEqual(JSON.parse(h.prefRequests().at(-1).options.body),{expected_version:1,values:{answer_style:"detailed"}});
  h.set({});await h.save();assert.deepEqual(JSON.parse(h.prefRequests().at(-1).options.body),{expected_version:2,values:{}});
  assert.match(h.el("preferences-status").textContent,/尚未设置/);
});

test("同账号角色偏好分别读取，新对话保留长期选择",async()=>{
  const h=harness();h.store.set("Bearer token-A/buyer",snapshot("buyer",{amount_format:"yuan",answer_style:"brief"},2));
  h.store.set("Bearer token-A/merchant",snapshot("merchant",{amount_format:"yuan_fixed2",answer_style:"detailed"},5));
  await h.login();assert.equal(h.el("amount-format").value,"yuan");
  const reads=h.prefRequests().length;h.el("new-chat").onclick();assert.equal(h.prefRequests().length,reads);assert.equal(h.el("answer-style").value,"brief");
  await h.mode("merchant");assert.equal(h.el("amount-format").value,"yuan_fixed2");assert.match(h.el("preferences-status").textContent,/版本 5/);
  await h.mode("buyer");assert.equal(h.el("answer-style").value,"brief");assert.match(h.el("preferences-status").textContent,/版本 2/);
});

test("删除使用当前版本，清掉选择并保留删除后的新版本",async()=>{
  const h=harness();h.store.set("Bearer token-A/buyer",snapshot("buyer",{answer_style:"brief"},3));await h.login();await h.remove();
  const sent=h.prefRequests().at(-1);assert.equal(sent.method,"DELETE");assert.deepEqual(JSON.parse(sent.options.body),{expected_version:3});
  assert.equal(h.el("answer-style").value,"");assert.match(h.el("preferences-status").textContent,/已删除偏好.*版本 4/);assert.equal(h.el("preferences-delete").disabled,true);
});

test("并发版本冲突必须刷新核对，不能带旧版本继续覆盖",async()=>{
  const h=harness();await h.login();h.store.set("Bearer token-A/buyer",snapshot("buyer",{answer_style:"detailed"},1));h.set({answer_style:"brief"});await h.save();
  assert.match(h.el("preferences-status").textContent,/别处更新.*刷新偏好/);assert.equal(h.el("preferences-save").disabled,true);assert.equal(h.el("preferences-refresh").disabled,false);
  const requests=h.prefRequests().length;await h.save();assert.equal(h.prefRequests().length,requests);
  await h.refresh();assert.equal(h.el("answer-style").value,"detailed");h.set({answer_style:"brief"});await h.save();
  assert.equal(JSON.parse(h.prefRequests().at(-1).options.body).expected_version,1);
});

test("503读取失败不阻止登录/订单，也不能冒称无偏好",async()=>{
  const h=harness();h.route=r=>r.path==="/api/preferences"?reply({detail:"unavailable"},503):undefined;
  await h.login();assert.equal(h.el("workspace").hidden,false);assert.match(h.el("identity").textContent,/B001/);
  assert.match(h.el("preferences-status").textContent,/偏好暂不可用.*尚无法确认/);assert.doesNotMatch(h.el("preferences-status").textContent,/尚未设置|已删除偏好|已保存偏好/);
  assert.equal(h.el("preferences-save").disabled,true);assert.equal(h.el("error").textContent,"");await h.check();assert.match(h.el("order-result").textContent,/125\.00 元/);
  h.route=()=>undefined;await h.refresh();assert.match(h.el("preferences-status").textContent,/尚未设置/);
});

test("保存响应丢失后不假报成功，刷新后按服务端实际版本恢复",async()=>{
  const h=harness();await h.login();h.set({amount_format:"fen"});
  h.route=r=>{if(r.path==="/api/preferences"&&r.method==="PUT"){h.store.set("Bearer token-A/buyer",snapshot("buyer",{amount_format:"fen"},1));return Promise.reject(Error("response lost"));}};
  await h.save();assert.match(h.el("preferences-status").textContent,/更新结果尚未确认/);assert.doesNotMatch(h.el("preferences-status").textContent,/已保存偏好/);
  assert.equal(h.el("preferences-save").disabled,true);h.route=()=>undefined;await h.refresh();assert.equal(h.el("amount-format").value,"fen");assert.match(h.el("preferences-status").textContent,/版本 1/);
});

test("偏好写入在途期间重复保存、删除、刷新均不再发请求",async()=>{
  const h=harness(),pending=deferred();await h.login();h.set({answer_style:"brief"});h.route=r=>r.path==="/api/preferences"&&r.method==="PUT"?pending.promise:undefined;
  const saving=h.save();await flush();assert.equal(h.el("preferences-save").disabled,true);await h.save();await h.remove();await h.refresh();assert.equal(h.prefRequests().length,2);
  pending.resolve(reply(snapshot("buyer",{answer_style:"brief"},1)));await saving;assert.equal(h.el("preferences-save").disabled,false);
});

test("退出清空偏好与任务说明，旧保存响应不能带入新账号",async()=>{
  const h=harness(),pending=deferred();await h.login();h.set({answer_style:"brief"});h.el("task-preferences").textContent="旧任务偏好";
  h.route=r=>r.path==="/api/preferences"&&r.method==="PUT"?pending.promise:undefined;
  const saving=h.save();await flush();h.logout();assert.equal(h.el("amount-format").value,"");assert.equal(h.el("answer-style").value,"");assert.equal(h.el("preferences-status").textContent,"");assert.equal(h.el("task-preferences").textContent,"");
  await h.login("token-B");pending.resolve(reply(snapshot("buyer",{answer_style:"brief"},10)));await saving;
  assert.equal(h.el("answer-style").value,"");assert.match(h.el("preferences-status").textContent,/版本 0/);assert.doesNotMatch(h.el("preferences-status").textContent,/版本 10|已保存偏好/);
});

test("旧角色读取或失败晚到，不能覆盖新角色选择与错误",async()=>{
  for(const fail of [false,true]){
    const h=harness(),pending=deferred();await h.login();
    h.route=r=>r.path==="/api/preferences"&&r.mode==="buyer"?pending.promise:undefined;
    const refreshing=h.refresh();await flush();await h.mode("merchant");
    if(fail)pending.reject(Error("old error"));else pending.resolve(reply(snapshot("buyer",{answer_style:"brief"},9)));
    await refreshing;assert.equal(h.el("mode").value,"merchant");assert.equal(h.el("answer-style").value,"");assert.match(h.el("preferences-status").textContent,/版本 0/);assert.equal(h.el("error").textContent,"");
  }
});

test("偏好响应范围不匹配视为不可用，不应用到当前角色",async()=>{
  const h=harness();h.route=r=>r.path==="/api/preferences"?reply(snapshot("merchant",{amount_format:"fen"},8)):undefined;
  await h.login();assert.equal(h.el("workspace").hidden,false);assert.equal(h.el("amount-format").value,"");assert.match(h.el("preferences-status").textContent,/暂不可用/);assert.equal(h.el("preferences-save").disabled,true);
});

test("简明元保留分额，固定两位与整数分按偏好显示且不改原始依据",async()=>{
  const h=harness();h.rows=[{id:"task-a",status:"completed",message:"核查",answer:"已核查",facts:[{tool:"check_order",result:fact}]}];await h.login();await h.check();
  h.set({amount_format:"yuan"});await h.save();assert.match(h.el("order-result").textContent,/125 元/);assert.match(h.el("order-result").textContent,/25\.05 元/);assert.match(h.el("order-result").textContent,/99\.95 元/);
  h.set({amount_format:"yuan_fixed2"});await h.save();assert.match(h.el("order-result").textContent,/125\.00 元/);
  h.set({amount_format:"fen"});await h.save();assert.match(h.el("order-result").textContent,/12500 分/);assert.match(h.el("order-result").textContent,/2505 分/);
  h.el("history").children[0].children[1].onclick();const evidence=h.el("facts").children[0].children[1].textContent;assert.deepEqual(JSON.parse(evidence),{tool:"check_order",result:fact});
  const raw='{"paid_amount_fen":9223372036854775807,"unit":"分"}\n';let parsed=0;
  h.route=r=>r.path==="/api/tasks/task-a/export"?{ok:true,blob:async()=>new Blob([raw]),json:async()=>{parsed++;return JSON.parse(raw);}}:undefined;
  await h.el("history").children[0].children[2].onclick();assert.equal(await h.downloads[0].text(),raw);assert.equal(parsed,0);
});

test("简明元和整数分均保留安全上界、负分并拒绝不安全整数",async()=>{
  const h=harness();await h.login();h.route=r=>r.path.startsWith("/api/orders/")?reply({...fact,paid_amount_fen:Number.MAX_SAFE_INTEGER,refund_amount_fen:-5,net_amount_fen:Number.MAX_SAFE_INTEGER+1}):undefined;
  h.set({amount_format:"yuan"});await h.save();await h.check();assert.match(h.el("order-result").textContent,/90071992547409\.91 元/);assert.match(h.el("order-result").textContent,/-0\.05 元/);assert.match(h.el("order-result").textContent,/超出浏览器/);
  h.set({amount_format:"fen"});await h.save();assert.match(h.el("order-result").textContent,/9007199254740991 分/);assert.match(h.el("order-result").textContent,/-5 分/);assert.doesNotMatch(h.el("order-result").textContent,/9007199254740992 分/);
});

test("历史任务展示当次偏好和临时覆盖，原始事实不受当前选择重写",async()=>{
  const h=harness();h.rows=[{id:"task-a",status:"completed",message:"核查",answer:"原回答",facts:[{tool:"check_order",result:fact}],memory:{status:"loaded",stored_values:{amount_format:"yuan",answer_style:"brief"},effective_values:{amount_format:"fen",answer_style:"brief"},overrides:{amount_format:"current_turn_explicit"},version:3,updated_at:"2026-11-17T01:02:03Z"}}];
  await h.login();h.el("history").children[0].children[1].onclick();assert.match(h.el("task-preferences").textContent,/整数分.*简洁.*临时调整.*未改写/);
  assert.equal(h.el("amount-format").value,"");assert.equal(h.prefRequests().filter(r=>r.method!=="GET").length,0);
  h.el("new-chat").onclick();assert.equal(h.el("task-preferences").textContent,"");
});

test("任务轮询完成展示偏好，新旧无memory任务可以兼容",async()=>{
  const h=harness();h.route=r=>{
    if(r.path==="/api/tasks"&&r.method==="POST")return reply({id:"done"});
    if(r.path==="/api/tasks/done")return reply({id:"done",status:"completed",answer:"答复",facts:[],memory:{status:"loaded",stored_values:{answer_style:"brief"},effective_values:{answer_style:"brief"},overrides:{},version:1}});
  };
  await h.login();h.el("message").value="问题";await h.el("ask-form").onsubmit(event);assert.match(h.el("task-preferences").textContent,/回答：简洁/);
  h.rows=[{id:"legacy",status:"completed",message:"旧记录",answer:"旧答复",facts:[]}];await h.el("refresh").onclick();h.el("history").children[0].children[1].onclick();assert.equal(h.el("task-preferences").textContent,"");
});

test("未读取偏好的任务如有本次要求，单独显示临时选择且不声称已保存",async()=>{
  const h=harness();h.rows=[{id:"no-read",status:"completed",message:"问题",answer:"答复",facts:[],memory:{status:"not_provided",stored_values:{},effective_values:{answer_style:"detailed"},overrides:{answer_style:"current_turn_explicit"},version:null}}];
  await h.login();h.el("history").children[0].children[1].onclick();assert.match(h.el("task-preferences").textContent,/未读取.*详细.*本次明确要求临时调整/);assert.doesNotMatch(h.el("task-preferences").textContent,/已保存偏好，|保存成功/);
});

test("购物任务偏好注明原币保留，元分偏好不能暗示美元已换汇",async()=>{
  const h=harness();h.rows=[{id:"native-usd",status:"completed",message:"核查Shopify #1002",answer:"净额70.00 USD",facts:[],memory:{status:"loaded",stored_values:{amount_format:"yuan",answer_style:"brief"},effective_values:{amount_format:"yuan",answer_style:"detailed"},overrides:{answer_style:"current_turn_explicit"},version:3},presentation:{currency_policy:"preserve_source_currency_no_conversion"}}];
  await h.login();h.el("history").children[0].children[1].onclick();
  assert.match(h.el("task-preferences").textContent,/详细.*按本次明确要求/);
  assert.match(h.el("task-preferences").textContent,/元\/分格式仅适用于人民币.*保留来源币种.*不按语言或偏好换汇/);
  assert.equal(h.prefRequests().filter(r=>r.method!=="GET").length,0);
});
