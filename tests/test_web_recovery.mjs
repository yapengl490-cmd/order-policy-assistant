// 助手离线回归：真实网页脚本 + DOM及HTTP替身，不代表真实浏览器或模型验收。
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

const saved=(id,status="running",patch={})=>({id,status,message:`问题-${id}`,answer:"",facts:[],session_id:`saved-session-${id}`,execution_mode:"multi",...patch});
const open=(h,index=0)=>h.el("history").children[index].children[1].onclick();
const posts=h=>h.requests.filter(r=>r.options.method==="POST");

test("重登恢复运行中历史：只查询原任务，可取消且等终态，不将已受理当完成",async()=>{
  const h=harness(),pending=deferred();h.historyA=[saved("active")];
  h.route=r=>r.path==="/api/tasks/active"?pending.promise:r.path==="/api/tasks/active/cancel"?reply({id:"active",status:"cancelling"}):undefined;
  await h.login("token-A");const viewing=open(h);await flush();
  assert.equal(h.el("cancel").disabled,false);assert.equal(h.el("send").disabled,true);assert.equal(h.el("execution-mode").value,"multi");
  await h.el("cancel").onclick();assert.match(h.el("status").textContent,/等待当前调用/);assert.equal(h.el("send").disabled,true);
  h.historyA=[saved("active","cancelled")];pending.resolve(reply(h.historyA[0]));await viewing;
  assert.match(h.el("status").textContent,/已取消/);assert.equal(h.el("cancel").disabled,true);assert.equal(h.el("send").disabled,false);
  assert.deepEqual(posts(h).map(r=>r.path),["/api/tasks/active/cancel"]);
});

test("连点同一历史只产生一个轮询，运行中不能切到另一任务",async()=>{
  const h=harness(),pending=deferred();h.historyA=[saved("active"),saved("other","completed",{answer:"不应打开"})];
  h.route=r=>r.path==="/api/tasks/active"?pending.promise:undefined;
  await h.login("token-A");const first=open(h),again=open(h);open(h,1);await flush();
  assert.equal(h.requests.filter(r=>r.path==="/api/tasks/active").length,1);assert.match(h.el("error").textContent,/原任务/);assert.doesNotMatch(h.el("conversation").textContent,/不应打开/);
  pending.resolve(reply(saved("active","completed",{answer:"核查完成"})));await Promise.all([first,again]);
  assert.equal(h.el("conversation").children.filter(n=>n.taskId==="active").length,1);assert.equal(posts(h).length,0);
});

test("历史查询暂断仍保留取消与原任务，只重开查询且不能新建错误会话",async()=>{
  const h=harness();h.historyA=[saved("active")];let reads=0;
  h.route=r=>r.path==="/api/tasks/active"?(++reads===1?Promise.reject(Error("connection lost")):reply(saved("active","completed",{answer:"恢复结果"}))):undefined;
  await h.login("token-A");await open(h);assert.match(h.el("error").textContent,/恢复查看/);assert.equal(h.el("cancel").disabled,false);
  await h.ask("另一个问题");h.el("new-chat").onclick();assert.equal(posts(h).length,0);assert.equal(h.el("mode").disabled,true);
  await open(h);assert.equal(reads,2);assert.match(h.el("conversation").textContent,/恢复结果/);assert.equal(h.el("send").disabled,false);
});

test("历史查询中断后取消会恢复查询，直到服务端给出终态",async()=>{
  const h=harness();h.historyA=[saved("active")];let cancelled=false;
  h.route=r=>{
    if(r.path==="/api/tasks/active/cancel"){cancelled=true;return reply({id:"active",status:"cancelling"});}
    if(r.path==="/api/tasks/active")return cancelled?reply(saved("active","cancelled")):Promise.reject(Error("connection lost"));
  };
  await h.login("token-A");await open(h);await h.el("cancel").onclick();assert.match(h.el("status").textContent,/已取消/);assert.equal(h.el("mode").disabled,false);
  assert.deepEqual(posts(h).map(r=>r.path),["/api/tasks/active/cancel"]);
});

test("历史任务续问恢复其原会话和方案，普通新问题不误用登录时的会话",async()=>{
  const h=harness();h.historyA=[saved("finished","completed",{answer:"原答案"})];
  h.route=r=>r.path==="/api/tasks"&&r.options.method==="POST"?reply({id:"followup"}):r.path==="/api/tasks/followup"?reply(saved("followup","completed")):undefined;
  await h.login("token-A");open(h);await h.ask("接着核对上一笔");
  const body=JSON.parse(posts(h)[0].options.body);assert.equal(body.session_id,"saved-session-finished");assert.equal(body.execution_mode,"multi");
  assert.equal(h.requests.filter(r=>r.path==="/api/tasks/finished").length,0);
});

test("未知提交不能被历史查看覆盖，重试仍用原请求号和会话",async()=>{
  const h=harness();h.historyA=[saved("old","completed")];let submits=0;
  h.route=r=>r.path==="/api/tasks"&&r.options.method==="POST"?(++submits===1?Promise.reject(Error("response lost")):reply({id:"accepted"})):r.path==="/api/tasks/accepted"?reply(saved("accepted","completed")):undefined;
  await h.login("token-A");open(h);await h.ask("原问题");open(h);assert.match(h.el("error").textContent,/尚未确认/);assert.equal(h.el("cancel").disabled,true);
  await h.ask("原问题");assert.deepEqual(JSON.parse(posts(h)[0].options.body),JSON.parse(posts(h)[1].options.body));
});

test("旧角色的历史轮询晚到，不覆盖新身份任务或解锁其按钮",async()=>{
  const h=harness(),old=deferred(),fresh=deferred();h.historyA=[saved("old")];h.historyB=[saved("fresh")];
  h.route=r=>r.path==="/api/tasks/old"?old.promise:r.path==="/api/tasks/fresh"?fresh.promise:undefined;
  await h.login("token-A");const viewing=open(h);h.logout();await h.login("token-B");const newViewing=open(h);
  old.resolve(reply(saved("old","completed",{answer:"旧身份的私有信息"})));await viewing;
  assert.doesNotMatch(h.el("conversation").textContent,/旧身份/);assert.equal(h.el("send").disabled,true);assert.equal(h.el("cancel").disabled,false);
  fresh.resolve(reply(saved("fresh","completed",{answer:"新身份结果"})));await newViewing;
  assert.match(h.el("conversation").textContent,/新身份结果/);assert.equal(posts(h).length,0);
});

test("已完成旧记录无会话时只查看，随后提问创建新会话；原字节导出保持可用",async()=>{
  const h=harness(),row=saved("legacy","completed",{session_id:undefined,answer:"旧结果"});h.historyA=[row];
  h.route=r=>r.path==="/api/tasks/legacy/export"?reply({id:"legacy",money:"9007199254740993"}):r.path==="/api/tasks"&&r.options.method==="POST"?reply({id:"new"}):r.path==="/api/tasks/new"?reply(saved("new","completed")):undefined;
  await h.login("token-A");open(h);await h.el("history").children[0].children[2].onclick();assert.deepEqual(h.downloads,["task-legacy.json"]);
  await h.ask("一个新问题");const body=JSON.parse(posts(h)[0].options.body);assert.notEqual(body.session_id,row.session_id);assert.equal(body.execution_mode,"single");
});

test("错编号响应不能完成所选任务或开放新的提交",async()=>{
  const h=harness();h.historyA=[saved("expected")];h.route=r=>r.path==="/api/tasks/expected"?reply(saved("other","completed",{answer:"串任务"})):undefined;
  await h.login("token-A");await open(h);assert.doesNotMatch(h.el("conversation").textContent,/串任务/);assert.equal(h.el("send").disabled,true);assert.equal(h.el("cancel").disabled,false);
});

const compareResult={status:"ok",first:{paid_amount_fen:12500,refund_amount_fen:4000,net_amount_fen:8500},second:{paid_amount_fen:10000,refund_amount_fen:0,net_amount_fen:10000},delta:{paid_amount_fen:-2500,refund_amount_fen:-4000,net_amount_fen:1500},channels:[],reason_notice:"金额差不等于已证明原因"};
const dates={"first-from":"2026-10-01T00:00","first-before":"2026-10-17T00:00","second-from":"2026-10-17T00:00","second-before":"2026-11-01T00:00","compare-cutoff":"2026-11-03T00:00"};
function fillDates(h){for(const [id,value]of Object.entries(dates))h.el(id).value=value;}
function analysisAccepted(request){const body=JSON.parse(request.options.body);return reply({id:"analysis",status:"queued",message:"解释两期变化",session_id:body.session_id,execution_mode:"period_analysis"});}

test("成功比较不调用模型，只有明确点解释才提交专用任务与冻结条件、新会话",async()=>{
  const h=harness();h.route=r=>r.path==="/api/analysis/compare"?reply(compareResult):r.path==="/api/analysis/tasks"?analysisAccepted(r):r.path==="/api/tasks/analysis"?reply(saved("analysis","completed",{execution_mode:"period_analysis",answer:"净额增加15元，原因待核对"})):r.path==="/api/tasks"&&r.options.method==="POST"?reply({id:"ordinary"}):r.path==="/api/tasks/ordinary"?reply(saved("ordinary","completed")):undefined;
  await h.login("token-A");fillDates(h);assert.equal(h.el("compare-explain").disabled,true);await h.el("compare-form").onsubmit(event);
  assert.equal(h.el("compare-explain").disabled,false);assert.equal(posts(h).filter(r=>r.path==="/api/analysis/tasks").length,0);
  await h.el("compare-explain").onclick();const body=JSON.parse(posts(h).find(r=>r.path==="/api/analysis/tasks").options.body);
  assert.deepEqual(Object.keys(body).sort(),["first_before","first_from","refund_before","request_id","second_before","second_from","session_id"].sort());
  assert.equal(body.first_from,"2026-10-01T00:00:00+08:00");assert.equal(body.refund_before,"2026-11-03T00:00:00+08:00");assert.match(h.el("conversation").textContent,/净额增加15元/);
  await h.ask("普通订单新问题");const chatBody=JSON.parse(posts(h).find(r=>r.path==="/api/tasks").options.body);assert.notEqual(chatBody.session_id,body.session_id);assert.equal(chatBody.execution_mode,"single");
});

test("任一比较时间改变后必须重算，过时比较响应不重新启用解释",async()=>{
  for(const id of Object.keys(dates)){
    const h=harness();h.route=r=>r.path==="/api/analysis/compare"?reply(compareResult):undefined;await h.login("token-A");fillDates(h);await h.el("compare-form").onsubmit(event);
    h.el(id).value="2026-11-04T00:00";h.el(id).oninput();assert.equal(h.el("compare-explain").disabled,true);await h.el("compare-explain").onclick();assert.equal(posts(h).filter(r=>r.path==="/api/analysis/tasks").length,0);
  }
  const h=harness(),pending=deferred();h.route=r=>r.path==="/api/analysis/compare"?pending.promise:undefined;await h.login("token-A");fillDates(h);const comparing=h.el("compare-form").onsubmit(event);
  h.el("compare-cutoff").value="2026-11-04T00:00";h.el("compare-cutoff").oninput();pending.resolve(reply(compareResult));await comparing;assert.equal(h.el("compare-explain").disabled,true);assert.equal(h.el("comparison").textContent,"");
});

test("解释受理响应丢失：原请求重试、条件锁定，不允许普通聊天绕过未知提交",async()=>{
  const h=harness();let attempts=0;h.route=r=>r.path==="/api/analysis/compare"?reply(compareResult):r.path==="/api/analysis/tasks"?(++attempts===1?Promise.reject(Error("response lost")):analysisAccepted(r)):r.path==="/api/tasks/analysis"?reply(saved("analysis","completed",{execution_mode:"period_analysis"})):undefined;
  await h.login("token-A");fillDates(h);await h.el("compare-form").onsubmit(event);await h.el("compare-explain").onclick();
  assert.match(h.el("compare-explain").textContent,/恢复/);assert.equal(h.el("first-from").disabled,true);assert.equal(h.el("compare-submit").disabled,true);await h.ask("另一问题");assert.equal(posts(h).filter(r=>r.path==="/api/tasks").length,0);
  await h.el("compare-explain").onclick();const requests=posts(h).filter(r=>r.path==="/api/analysis/tasks");assert.equal(requests.length,2);assert.deepEqual(JSON.parse(requests[0].options.body),JSON.parse(requests[1].options.body));assert.equal(h.el("first-from").disabled,false);
});

test("已知解释任务查询失败只恢复GET，不重复创建解释；连点不会并发提交",async()=>{
  const h=harness(),pending=deferred();let reads=0;
  h.route=r=>r.path==="/api/analysis/compare"?reply(compareResult):r.path==="/api/analysis/tasks"?pending.promise:r.path==="/api/tasks/analysis"?(++reads===1?Promise.reject(Error("lost")):reply(saved("analysis","completed",{execution_mode:"period_analysis"}))):undefined;
  await h.login("token-A");fillDates(h);await h.el("compare-form").onsubmit(event);const first=h.el("compare-explain").onclick();await h.el("compare-explain").onclick();
  const submitted=posts(h).find(r=>r.path==="/api/analysis/tasks");pending.resolve(analysisAccepted(submitted));await first;assert.equal(h.el("cancel").disabled,false);
  await h.el("compare-explain").onclick();assert.equal(posts(h).filter(r=>r.path==="/api/analysis/tasks").length,1);assert.equal(reads,2);
});

test("专用解释历史只恢复原任务，不重生成也不把专用会话送入普通聊天",async()=>{
  const h=harness();h.historyA=[saved("analysis","running",{execution_mode:"period_analysis"})];h.route=r=>r.path==="/api/tasks/analysis"?reply(saved("analysis","completed",{execution_mode:"period_analysis"})):r.path==="/api/tasks"&&r.options.method==="POST"?reply({id:"ordinary"}):r.path==="/api/tasks/ordinary"?reply(saved("ordinary","completed")):undefined;
  await h.login("token-A");await open(h);assert.equal(posts(h).length,0);assert.match(h.el("history").textContent,/两期变化解释/);await h.ask("新的普通问题");
  const body=JSON.parse(posts(h)[0].options.body);assert.equal(body.execution_mode,"single");assert.notEqual(body.session_id,"saved-session-analysis");
});

test("角色切换后旧解释受理响应不发起查询，不污染新范围或解锁其在途任务",async()=>{
  const h=harness(),pending=deferred();h.route=r=>r.path==="/api/analysis/compare"?reply(compareResult):r.path==="/api/analysis/tasks"?pending.promise:undefined;
  await h.login("token-A");fillDates(h);await h.el("compare-form").onsubmit(event);const asking=h.el("compare-explain").onclick();const request=posts(h).find(r=>r.path==="/api/analysis/tasks");h.logout();await h.login("token-B");pending.resolve(analysisAccepted(request));await asking;
  assert.equal(h.requests.filter(r=>r.path==="/api/tasks/analysis").length,0);assert.equal(h.el("conversation").textContent,"");assert.equal(h.el("compare-explain").disabled,true);
});

test("两期解释历史显示可读引用映射与固定摘要，引用文本不执行HTML",async()=>{
  const h=harness(),fixed="前期净额85元，后期100元。原因仍待查证。";
  h.historyA=[saved("analysis","completed",{execution_mode:"period_analysis",answer:"净额增加15元[E4]。",
    fixed_answer:fixed,analysis_validation:{status:"accepted",reasons:[],fallback:null},
    analysis_evidence:{E4:{label:"净额差额<img src=x onerror=alert(1)>",display:"15元",path:"delta.net_amount_fen"}}})];
  await h.login("token-A");open(h);
  const box=h.el("task-execution"),text=box.textContent;assert.match(text,/解释引用与固定摘要/);assert.match(text,/E4.*净额差额.*15元.*delta.net_amount_fen/s);assert.match(text,/通过有限校验/);assert.ok(text.includes(fixed));
  const tags=[];const visit=n=>{tags.push(n.tagName);for(const child of n.children)if(typeof child!=="string")visit(child);};visit(box);
  assert.equal(tags.includes("img"),false);assert.equal(tags.includes("script"),false);assert.equal(posts(h).length,0);
});

test("模型解释降级显示校验状态与原因及固定原文，不伪装解释完成",async()=>{
  const h=harness(),fixed="固定摘要：原净额85元，现净额100元。";
  h.historyA=[saved("analysis","partial",{execution_mode:"period_analysis",answer:fixed,fixed_answer:fixed,
    analysis_validation:{status:"rejected",reasons:["numeric_value_or_direction_mismatch","answer_too_long"],fallback:"fixed_facts"},analysis_evidence:{}})];
  await h.login("token-A");open(h);const text=h.el("task-execution").textContent;
  assert.match(text,/模型解释未被采纳（rejected）/);assert.match(text,/numeric_value_or_direction_mismatch、answer_too_long/);assert.match(text,/回退到程序固定事实/);assert.ok(text.includes(fixed));assert.doesNotMatch(text,/通过有限校验/);
});

test("普通任务不添加空解释面板；专用失败无固定摘要时明确缺失",async()=>{
  const h=harness();h.historyA=[saved("normal","completed",{orchestration:{requested:"multi",effective:"multi"}}),
    saved("analysis","failed",{execution_mode:"period_analysis",analysis_validation:{status:"query_failed",reasons:["comparison_failed_or_inconsistent"]},fixed_answer:null})];
  await h.login("token-A");open(h);assert.doesNotMatch(h.el("task-execution").textContent,/解释引用与固定摘要/);
  open(h,1);assert.match(h.el("task-execution").textContent,/比较事实不可用/);assert.match(h.el("task-execution").textContent,/尚无可用固定摘要/);
});

const investigationAccepted=request=>{
  const body=JSON.parse(request.options.body);
  return reply({id:"investigation",status:"queued",message:"核对退款原因记录",session_id:body.session_id,execution_mode:"refund_investigation"});
};

test("原因核对必须先成功比较，独立按钮仅提交investigate且沿用冻结比较条件",async()=>{
  const h=harness();h.route=r=>r.path==="/api/analysis/compare"?reply(compareResult):r.path==="/api/analysis/investigate"?investigationAccepted(r):r.path==="/api/tasks/investigation"?reply(saved("investigation","completed",{execution_mode:"refund_investigation",answer:"退款原因记录不足，不能推断实际责任"})):undefined;
  await h.login("token-A");fillDates(h);assert.equal(h.el("compare-investigate").disabled,true);
  await h.el("compare-investigate").onclick();assert.equal(posts(h).length,0);
  await h.el("compare-form").onsubmit(event);assert.equal(h.el("compare-investigate").disabled,false);
  assert.equal(posts(h).filter(r=>r.path==="/api/analysis/investigate").length,0);
  await h.el("compare-investigate").onclick();const body=JSON.parse(posts(h).find(r=>r.path==="/api/analysis/investigate").options.body);
  assert.deepEqual(Object.keys(body).sort(),[...Object.keys(dates).map(id=>({"first-from":"first_from","first-before":"first_before","second-from":"second_from","second-before":"second_before","compare-cutoff":"refund_before"}[id])),"request_id","session_id"].sort());
  assert.equal(body.first_from,"2026-10-01T00:00:00+08:00");assert.equal(body.refund_before,"2026-11-03T00:00:00+08:00");
  assert.equal(posts(h).filter(r=>r.path==="/api/analysis/tasks"||r.path==="/api/tasks").length,0);
  assert.match(h.el("conversation").textContent,/退款原因记录不足/);
});

test("原因核对的任一比较条件变化后必须重新比较，失败比较不能启用按钮",async()=>{
  for(const id of Object.keys(dates)){
    const h=harness();h.route=r=>r.path==="/api/analysis/compare"?reply(compareResult):undefined;
    await h.login("token-A");fillDates(h);await h.el("compare-form").onsubmit(event);
    h.el(id).value="2026-11-04T00:00";h.el(id).oninput();assert.equal(h.el("compare-investigate").disabled,true);
    await h.el("compare-investigate").onclick();assert.equal(posts(h).filter(r=>r.path==="/api/analysis/investigate").length,0);
  }
  const h=harness();h.route=r=>r.path==="/api/analysis/compare"?reply({status:"failed",message:"事实不完整"}):undefined;
  await h.login("token-A");fillDates(h);await h.el("compare-form").onsubmit(event);assert.equal(h.el("compare-investigate").disabled,true);
});

test("原因核对受理未知时重用同请求号和条件，禁止切到两期解释或聊天",async()=>{
  const h=harness();let attempts=0;h.route=r=>r.path==="/api/analysis/compare"?reply(compareResult):r.path==="/api/analysis/investigate"?(++attempts===1?Promise.reject(Error("response lost")):investigationAccepted(r)):r.path==="/api/tasks/investigation"?reply(saved("investigation","completed",{execution_mode:"refund_investigation"})):undefined;
  await h.login("token-A");fillDates(h);await h.el("compare-form").onsubmit(event);await h.el("compare-investigate").onclick();
  assert.match(h.el("compare-investigate").textContent,/恢复上次原因核对/);assert.equal(h.el("compare-explain").disabled,true);assert.equal(h.el("compare-submit").disabled,true);
  for(const id of Object.keys(dates))assert.equal(h.el(id).disabled,true);
  await h.el("compare-explain").onclick();await h.ask("普通新问题");
  assert.equal(posts(h).filter(r=>r.path==="/api/analysis/tasks"||r.path==="/api/tasks").length,0);
  await h.el("compare-investigate").onclick();const requests=posts(h).filter(r=>r.path==="/api/analysis/investigate");
  assert.equal(requests.length,2);assert.deepEqual(JSON.parse(requests[0].options.body),JSON.parse(requests[1].options.body));
  for(const id of Object.keys(dates))assert.equal(h.el(id).disabled,false);
});

test("原因核对已知任务读取失败仅恢复原GET，连点不重复POST",async()=>{
  const h=harness(),pending=deferred();let reads=0;
  h.route=r=>r.path==="/api/analysis/compare"?reply(compareResult):r.path==="/api/analysis/investigate"?pending.promise:r.path==="/api/tasks/investigation"?(++reads===1?Promise.reject(Error("lost")):reply(saved("investigation","completed",{execution_mode:"refund_investigation"}))):undefined;
  await h.login("token-A");fillDates(h);await h.el("compare-form").onsubmit(event);
  const first=h.el("compare-investigate").onclick();await h.el("compare-investigate").onclick();await h.el("compare-explain").onclick();
  const submitted=posts(h).find(r=>r.path==="/api/analysis/investigate");pending.resolve(investigationAccepted(submitted));await first;
  assert.equal(h.el("cancel").disabled,false);await h.el("compare-investigate").onclick();
  assert.equal(reads,2);assert.equal(posts(h).filter(r=>r.path==="/api/analysis/investigate").length,1);
  assert.equal(posts(h).filter(r=>r.path==="/api/analysis/tasks").length,0);
});

test("原因核对收到错误period_analysis受理不能串成解释任务，重试仍保留原请求",async()=>{
  const h=harness();let attempts=0;
  h.route=r=>r.path==="/api/analysis/compare"?reply(compareResult):r.path==="/api/analysis/investigate"?(++attempts===1?analysisAccepted(r):investigationAccepted(r)):r.path==="/api/tasks/investigation"?reply(saved("investigation","completed",{execution_mode:"refund_investigation"})):undefined;
  await h.login("token-A");fillDates(h);await h.el("compare-form").onsubmit(event);await h.el("compare-investigate").onclick();
  assert.equal(h.requests.filter(r=>r.path==="/api/tasks/analysis").length,0);assert.match(h.el("compare-investigate").textContent,/恢复/);
  await h.el("compare-investigate").onclick();const requests=posts(h).filter(r=>r.path==="/api/analysis/investigate");
  assert.deepEqual(JSON.parse(requests[0].options.body),JSON.parse(requests[1].options.body));assert.equal(posts(h).filter(r=>r.path==="/api/analysis/tasks").length,0);
});

test("售后短答与完整原文分开展示，旧历史和下载仍保存完整结果",async()=>{
  const h=harness();const sourceText="必要条件：商品未穿着且吊牌完好。\n例外：定制商品不适用。";
  const row=saved("support-brief","completed",{execution_mode:"single",answer:"依自述暂满足条件。依据：[DOC:return]",facts:[{tool:"review_after_sales",result:{citation_index:{"DOC:return":{source_path:"knowledge/policy.md",line_start:22,line_end:26,text:sourceText}},unknown:"责任未核实"}}]});
  h.historyA=[row];h.route=r=>r.path==="/api/tasks/support-brief/export"?reply({result:row}):undefined;
  await h.login("token-A");await open(h);
  assert.doesNotMatch(h.el("conversation").textContent,/必要条件：/);
  const detail=h.el("facts").children[0];assert.equal(detail.tagName,"details");assert.notEqual(detail.open,true);
  assert.match(detail.textContent,/定制商品不适用/);assert.match(detail.textContent,/第22–26行/);
  await h.el("history").children[0].children[2].onclick();
  const exported=JSON.parse(await h.createdUrls[0].text());
  assert.equal(exported.result.facts[0].result.citation_index["DOC:return"].text,sourceText);
  assert.equal(exported.result.answer,row.answer);
  h.logout();await h.login("token-A");await open(h);assert.match(h.el("facts").textContent,/责任未核实/);
});
