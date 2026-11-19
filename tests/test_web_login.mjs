// 助手离线回归：实际 app.js；DOM 和鉴权响应为隔离替身，无真实凭据和模型调用。
import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const source=readFileSync(new URL("../web/app.js",import.meta.url),"utf8");
const markup=readFileSync(new URL("../web/index.html",import.meta.url),"utf8");
const event={preventDefault(){}};
const reply=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body});
function harness(){
  const h={requests:[]};
  class Node{
    constructor(tag){this.tagName=tag;this.children=[];this.value="";this._text="";this.hidden=false;this.disabled=false;}
    set textContent(v){this._text=String(v);this.children=[];}
    get textContent(){return this._text+this.children.map(x=>typeof x==="string"?x:x.textContent).join("");}
    replaceChildren(...items){this._text="";this.children=items;}
    append(...items){this.children.push(...items);}
  }
  const nodes=new Map();h.el=id=>{if(!nodes.has(id))nodes.set(id,new Node("div"));return nodes.get(id);};
  h.el("workspace").hidden=true;h.el("login-mode").value="buyer";
  let sequence=0;
  vm.runInContext(source,vm.createContext({
    document:{getElementById:h.el,createElement:tag=>new Node(tag)},crypto:{randomUUID:()=>`session-${++sequence}`},console,setTimeout,
    fetch:async(path,options)=>{
      const r={path,method:options.method||"GET",mode:options.headers["X-Access-Mode"],token:options.headers.Authorization};h.requests.push(r);
      const allowed=r.token==="Bearer demo-merchant"?["merchant"]:r.token==="Bearer old-dual"?["buyer","merchant"]:[];
      if(!allowed.includes(r.mode))return reply({detail:"本次凭证无此权限"},401);
      const publicDemo=r.token==="Bearer demo-merchant",shop=publicDemo?"S_UCI_DEMO":"S001";
      if(path==="/api/me")return reply(r.mode==="merchant"?{role:r.mode,shop_id:shop}:{role:r.mode,buyer_id:"B001"});
      if(path==="/api/orders")return reply({orders:[{order_id:r.mode==="merchant"?`${shop}-ORDER`:"B001-ORDER"}]});
      if(path==="/api/preferences")return reply({role:r.mode,values:{},version:0});
      if(path==="/api/tasks")return reply([{id:`${shop}-${r.mode}-history`,status:"completed",execution_mode:"single",session_id:`saved-${r.mode}`,message:`${shop}-${r.mode}-问题`,answer:`${shop}-${r.mode}-已有答案`,facts:[]}]);
      throw Error(`未配置替身：${path}`);
    },
  }),{filename:"web/app.js"});
  h.login=async(token,role)=>{h.el("token").value=token;if(role!==undefined)h.el("login-mode").value=role;await h.el("login-form").onsubmit(event);};
  h.change=async role=>{h.el("mode").value=role;await h.el("mode").onchange();};
  h.open=()=>h.el("history").children[0].children[1].onclick();
  return h;
}

test("登录前可选择已授权角色，默认买家；选择器不藏在工作区",()=>{
  const login=markup.slice(markup.indexOf('<form id="login-form">'),markup.indexOf('<div id="workspace" hidden>'));
  assert.match(login,/<label for="login-mode">登录角色/);
  assert.match(login,/<select id="login-mode"[^>]*>\s*<option value="buyer"(?: selected)?/);
  assert.match(login,/<option value="merchant">商家/);
  assert.match(login,/选择角色不会增加账号权限/);
});

test("旧双角色账号保持默认买家登录",async()=>{
  const h=harness();await h.login("old-dual");assert.equal(h.el("workspace").hidden,false);
  assert.match(h.el("identity").textContent,/B001/);assert.equal(h.el("mode").value,"buyer");
  assert.ok(h.requests.every(r=>r.mode==="buyer"&&r.method==="GET"));
});

test("仅商家账号通过正常登录表单进入自己的店铺并重开已有历史",async()=>{
  const h=harness();await h.login("demo-merchant","merchant");
  assert.equal(h.el("login").hidden,true);assert.equal(h.el("workspace").hidden,false);assert.equal(h.el("mode").value,"merchant");
  assert.match(h.el("identity").textContent,/S_UCI_DEMO/);h.open();
  assert.match(h.el("conversation").textContent,/S_UCI_DEMO-merchant-已有答案/);
  assert.ok(h.requests.every(r=>r.mode==="merchant"&&r.method==="GET"));
});

test("仅商家账号误选买家仍被后端拒绝；显式改选并重填凭证后可登录",async()=>{
  const h=harness();await h.login("demo-merchant","buyer");
  assert.equal(h.el("workspace").hidden,true);assert.equal(h.el("login").hidden,false);assert.match(h.el("error").textContent,/无此权限/);
  assert.equal(h.el("history").textContent,"");assert.equal(h.el("token").value,"");
  assert.ok(h.requests.every(r=>r.mode==="buyer"));
  await h.login("demo-merchant","merchant");assert.equal(h.el("workspace").hidden,false);assert.equal(h.el("error").textContent,"");
});

test("仅商家登录后越权切买家被拒，恢复原商家及历史，不扩大授权",async()=>{
  const h=harness();await h.login("demo-merchant","merchant");await h.change("buyer");
  assert.equal(h.el("workspace").hidden,false);assert.equal(h.el("mode").value,"merchant");assert.match(h.el("identity").textContent,/S_UCI_DEMO/);
  assert.match(h.el("error").textContent,/无此权限/);h.open();assert.match(h.el("conversation").textContent,/S_UCI_DEMO-merchant-已有答案/);
  assert.equal(h.requests.filter(r=>r.method!=="GET").length,0);
});

test("旧账号双角色切换和退出重登仍隔离并恢复对应历史",async()=>{
  const h=harness();await h.login("old-dual");h.open();assert.match(h.el("conversation").textContent,/S001-buyer-已有答案/);
  await h.change("merchant");assert.equal(h.el("conversation").textContent,"");h.open();assert.match(h.el("conversation").textContent,/S001-merchant-已有答案/);
  h.el("logout").onclick();assert.equal(h.el("history").textContent,"");assert.equal(h.el("conversation").textContent,"");
  await h.login("old-dual","buyer");h.open();assert.match(h.el("conversation").textContent,/S001-buyer-已有答案/);
  assert.doesNotMatch(h.el("conversation").textContent,/merchant-已有答案/);
});
