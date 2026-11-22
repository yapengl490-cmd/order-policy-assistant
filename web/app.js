"use strict";
// 模型文本只用textContent展示；每次登录/换角色使旧请求失效。
const el=id=>document.getElementById(id);
let token="",session=crypto.randomUUID(),currentTask=null,polling=false,mode="buyer",scopeEpoch=0,pendingSubmission=null;
let executionMode="single";
let preferences=null,preferenceBusy=false,preferenceRevision=0,lastOrder=null;
let displayedTaskId=null;
let policyReviewRevision=0;
let activePoll=null,comparisonSnapshot=null,comparisonRevision=0,comparisonBusy=false;
const comparisonFields={first_from:"first-from",first_before:"first-before",second_from:"second-from",second_before:"second-before",refund_before:"compare-cutoff"};
const memoryActions=new Map(),memoryUncertain=new Map();
const preferenceLabels={amount_format:{yuan:"简明元",yuan_fixed2:"元 · 固定两位小数",fen:"整数分"},answer_style:{brief:"简洁",standard:"标准",detailed:"详细"}};
const statuses={queued:"已受理",running:"正在查证",cancelling:"已请求取消，等待当前调用返回",cancelled:"已取消",completed:"已完成",partial:"部分完成",clarification:"需要补充条件",failed:"处理失败",limited:"达到执行上限",interrupted:"服务重启后中断"};
const finalStates=new Set(["completed","partial","clarification","failed","limited","cancelled","interrupted"]);
const executionLabels={single:"单智能体",multi:"双分支对照",dependent:"售后顺序协作",batch_sequential:"批量普通工具调用",programmatic:"批量隔离短程序",snapshot_sql:"授权快照查询对照",period_analysis:"两期变化解释",refund_investigation:"退款原因证据核对"};
const chatModes=new Set(["single","multi","batch_sequential","programmatic","snapshot_sql"]);
class StaleScope extends Error {}
function captureScope(){return {epoch:scopeEpoch,token,mode};}
function isCurrent(s){return s.epoch===scopeEpoch && s.token===token && s.mode===mode;}
function error(e,s=captureScope()){if(isCurrent(s) && !(e instanceof StaleScope))el("error").textContent=e.message||String(e);}
function clearError(s=captureScope()){if(isCurrent(s))el("error").textContent="";}
function node(tag,text,className){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(className)n.className=className;return n;}
function setPolling(value,s){
  if(!isCurrent(s))return;
  polling=value;el("send").disabled=value||!!(pendingSubmission&&pendingSubmission.kind!=="chat");
  el("cancel").disabled=!currentTask||!(value||pendingSubmission?.taskId===currentTask);
  el("new-chat").disabled=value||!!pendingSubmission;el("mode").disabled=value||!!pendingSubmission;
  el("execution-mode").disabled=value||!!pendingSubmission;
  setAnalysisControls();
}
function comparisonBody(){return Object.fromEntries(Object.entries(comparisonFields).map(([key,id])=>[key,el(id).value+":00+08:00"]));}
function comparisonMatches(){return comparisonSnapshot&&JSON.stringify(comparisonSnapshot)===JSON.stringify(comparisonBody());}
function setAnalysisControls(){
  const retry=pendingSubmission?.kind==="analysis";
  const investigationPending=retry&&pendingSubmission.analysisMode==="refund_investigation";
  el("compare-explain").disabled=!token||polling||!!(pendingSubmission&&!retry)||(!retry&&!comparisonMatches());
  el("compare-explain").disabled=el("compare-explain").disabled||investigationPending;
  el("compare-investigate").disabled=!token||polling||!!(pendingSubmission&&!investigationPending)||(!investigationPending&&!comparisonMatches());
  el("compare-investigate").textContent=investigationPending?"恢复上次原因核对":"核对退款原因记录（调用模型）";
  el("compare-explain").textContent=retry&&!investigationPending?"恢复上次解释":"解释变化（调用模型）";
  for(const id of Object.values(comparisonFields))el(id).disabled=retry;
  el("compare-submit").disabled=comparisonBusy||retry;
}
function clearPrivateView(){
  // 退出或换角色时清掉所有业务显示与输入，不只清聊天框。
  for(const id of ["identity","orders","order-result","conversation","conditions","facts","history","status","error","preferences-status","task-preferences","task-execution","task-memory-update","task-policy-review","comparison"])el(id).replaceChildren();
  policyReviewRevision+=1;
  el("message").value="";el("order").value="";el("token").value="";el("cutoff").value="2026-11-03T00:00";
  comparisonSnapshot=null;comparisonRevision+=1;comparisonBusy=false;el("compare-submit").disabled=false;
  preferences=null;preferenceBusy=false;preferenceRevision+=1;lastOrder=null;displayedTaskId=null;memoryActions.clear();memoryUncertain.clear();
  el("amount-format").value="";el("answer-style").value="";setPreferenceControls();
}
function beginScope(nextToken,nextMode){
  scopeEpoch+=1;activePoll=null;token=nextToken;mode=nextMode;executionMode="single";el("execution-mode").value=executionMode;session=crypto.randomUUID();currentTask=null;pendingSubmission=null;
  clearPrivateView();el("mode").value=mode;const s=captureScope();setPolling(false,s);return s;
}
async function api(path,options={},s=captureScope()){
  if(!isCurrent(s))throw new StaleScope();
  const headers={Authorization:`Bearer ${s.token}`,"X-Access-Mode":s.mode,...(options.body?{"Content-Type":"application/json"}:{}),...options.headers};
  const response=await fetch(path,{...options,headers});const body=await response.json();
  // 旧响应不能更新新身份页面、触发下载或显示过时错误。
  if(!isCurrent(s))throw new StaleScope();
  if(!response.ok){const e=Error(typeof body.detail==="string"?body.detail:"输入格式不正确，请核对字段。");e.httpStatus=response.status;throw e;}
  return body;
}
function money(fen){
  if(!Number.isSafeInteger(fen))return "金额超出浏览器可精确显示范围，请核对原始证据";
  const format=preferences?.values.amount_format;
  if(format==="fen")return `${fen} 分`;
  // 即使整数仍安全，直接除100再toFixed也可能损失最后一分；分开格式化元和分。
  const absolute=Math.abs(fen),yuan=Math.floor(absolute/100),cents=absolute%100;
  const fraction=format==="yuan"?(cents?`.${String(cents).padStart(2,"0").replace(/0$/,"")}`:""):`.${String(cents).padStart(2,"0")}`;
  return `${fen<0?"-":""}${yuan}${fraction} 元`;
}
function setPreferenceControls(){
  const disabled=!token||preferenceBusy||!preferences;
  for(const id of ["amount-format","answer-style","preferences-save"])el(id).disabled=disabled;
  el("preferences-delete").disabled=disabled||!Object.keys(preferences?.values||{}).length;
  el("preferences-refresh").disabled=!token||preferenceBusy;
}
function preferenceText(values){
  return Object.entries(preferenceLabels).filter(([field,labels])=>labels[values?.[field]]).map(([field,labels])=>`${field==="amount_format"?"金额":"回答"}：${labels[values[field]]}`).join("；");
}
function applyPreferences(snapshot,s){
  if(!isCurrent(s))return;
  if(snapshot?.role!==s.mode||!Number.isSafeInteger(snapshot.version)||snapshot.version<0||!snapshot.values||typeof snapshot.values!=="object"||Array.isArray(snapshot.values))throw Error("偏好响应无法核对，请刷新偏好。");
  if(Object.entries(snapshot.values).some(([field,value])=>!Object.hasOwn(preferenceLabels,field)||!Object.hasOwn(preferenceLabels[field],value)))throw Error("偏好响应无法核对，请刷新偏好。");
  preferences=snapshot;el("amount-format").value=snapshot.values.amount_format||"";el("answer-style").value=snapshot.values.answer_style||"";
  const savedAt=snapshot.updated_at?new Date(snapshot.updated_at).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai"}):"";
  el("preferences-status").textContent=`${preferenceText(snapshot.values)||"当前账号与角色尚未设置回答偏好"} · 版本 ${snapshot.version}${savedAt?` · 保存时间 ${savedAt}（北京时间）`:""}`;
  if(lastOrder)showOrder(lastOrder,s);
}
function preferenceDraftChanged(){
  return !!preferences&&(el("amount-format").value!==(preferences.values.amount_format||"")||el("answer-style").value!==(preferences.values.answer_style||""));
}
async function preferenceRequest(method,s=captureScope(),preserveDraft=false){
  if(!token||!isCurrent(s)||preferenceBusy||(method!=="GET"&&!preferences))return;
  if(preserveDraft&&preferenceDraftChanged())return;
  const requestRevision=++preferenceRevision,options={};
  if(method!=="GET"){
    const body={expected_version:preferences.version};
    if(method==="PUT"){
      body.values={};
      if(el("amount-format").value)body.values.amount_format=el("amount-format").value;
      if(el("answer-style").value)body.values.answer_style=el("answer-style").value;
    }
    options.method=method;options.body=JSON.stringify(body);
  }
  preferenceBusy=true;setPreferenceControls();el("preferences-status").textContent=method==="GET"?"正在读取当前账号与角色的偏好…":"正在更新偏好…";
  try{
    const snapshot=await api("/api/preferences",options,s);
    if(!isCurrent(s)||requestRevision!==preferenceRevision)return;
    if(preserveDraft&&preferenceDraftChanged()){el("preferences-status").textContent="聊天偏好已更新；保留你尚未保存的选项，请刷新偏好核对最新设置后再修改。";return;}
    applyPreferences(snapshot,s);
    if(method!=="GET")el("preferences-status").textContent=`${method==="DELETE"?"已删除偏好":"已保存偏好"}，后续任务生效。${el("preferences-status").textContent}`;
  }catch(e){
    if(!isCurrent(s)||requestRevision!==preferenceRevision)return;
    preferences=null;
    el("preferences-status").textContent=e.httpStatus===409?"偏好已在别处更新，请先刷新偏好，再核对并保存。":`偏好暂不可用，${method==="GET"?"尚无法确认已保存内容":"本次更新结果尚未确认"}。请刷新偏好后核对；订单核查仍可使用，暂按默认格式显示。`;
    if(lastOrder)showOrder(lastOrder,s);
  }finally{if(isCurrent(s)&&requestRevision===preferenceRevision){preferenceBusy=false;setPreferenceControls();}}
}
function showTaskPreferences(memory,s,update,presentation){
  if(!isCurrent(s))return;const box=el("task-preferences");box.replaceChildren();if(!memory)return;
  if(memory.status!=="loaded")box.append(node("p",memory.status==="not_provided"?"本次未读取已保存偏好。":"本次未取得可用的已保存偏好。","muted"));
  const applied=preferenceText(memory.effective_values);
  const overrides=Object.keys(preferenceLabels).filter(field=>memory.overrides?.[field]==="current_turn_explicit").map(field=>field==="amount_format"?"金额显示":"回答详略");
  if(!applied&&memory.status!=="loaded")return;
  box.append(node("p",`${applied?`本次回答偏好：${applied}`:"本次未应用已保存的回答偏好"}${overrides.length?`；${overrides.join("、")}按本次明确要求${update?"调整；聊天偏好是否长期保存见下方独立结果":"临时调整，未改写已保存偏好"}`:""}。`,"muted"));
  if(presentation?.currency_policy==="preserve_source_currency_no_conversion")box.append(node("p","元/分格式仅适用于人民币；本次事实与商品保留来源币种，不按语言或偏好换汇。","muted"));
}
// 任务开始时的偏好快照与聊天后保存的偏好分开显示；确认只操作原任务，不重发模型请求。
const memoryLabels={saved:"已保存聊天偏好",unchanged:"已保存偏好无需变化",proposed:"待你确认的偏好建议",rejected:"未保存偏好",conflict:"偏好已变化，未覆盖新设置",unknown:"保存结果待核对",preparing:"正在准备偏好更新",not_saved:"偏好未保存",dismissed:"已忽略偏好建议",expired:"偏好建议已过期",discarded:"本次偏好建议已作废"};
// 后端时间戳与任务记录一致，单位是秒；旧导出中的带时区ISO时间也可读取。
function memoryTime(value){
  const ms=typeof value==="number"&&Number.isFinite(value)?value*1000:typeof value==="string"&&/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value)?Date.parse(value):NaN;
  return Number.isFinite(ms)&&Number.isFinite(new Date(ms).getTime())?ms:NaN;
}
function showMemoryUpdate(record,s){
  if(!isCurrent(s)||displayedTaskId!==record.id)return;
  const box=el("task-memory-update"),update=record.memory_update;box.replaceChildren();if(!update)return;
  if(update.role!==s.mode||!Object.hasOwn(memoryLabels,update.status)){
    box.append(node("p","偏好记录的范围或状态无法核对，请刷新历史后查看。","memory-warning"));return;
  }
  const card=node("div",undefined,"memory-update"),busy=memoryActions.has(record.id),uncertain=memoryUncertain.has(record.id);
  card.append(node("h3",memoryLabels[update.status]),node("p",`适用角色：${s.mode==="merchant"?"商家":"买家"} · 仅当前登录账号；不会改变订单权限。`,"muted"));
  const values=preferenceText(update.values);if(values)card.append(node("p",values,"memory-values"));
  if(typeof update.source_quote==="string"&&update.source_quote)card.append(node("p",`来源原话：“${update.source_quote}”`,"memory-source"));
  if(typeof update.notice==="string"&&update.notice)card.append(node("p",update.notice));
  if(Number.isSafeInteger(update.version)&&update.version>=0)card.append(node("p",`记录版本：${update.version}`,"muted"));
  card.append(node("p","本区是聊天偏好的保存结果；上方“本次回答偏好”是任务开始时的快照。保存后由后续任务读取，历史回答不会因此重新生成。","muted"));
  const actions=node("div",undefined,"row memory-actions");
  const button=(label,action)=>{const b=node("button",label,action==="accept"?"":"secondary");b.type="button";b.disabled=busy;b.onclick=()=>decideMemory(record,action,s);actions.append(b);};
  if(uncertain){
    card.append(node("p","上次操作结果尚未确认。先核对原任务；不会重新调用模型或新建任务。","memory-warning"));button("核对保存结果","check");
  }else if(update.status==="proposed"){
    const expiry=memoryTime(update.expires_at),validExpiry=Number.isFinite(expiry);
    card.append(node("p",validExpiry?`确认有效期为30分钟，截止 ${new Date(expiry).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai"})}（北京时间）。过期后不会自动保存。`:"建议有效期无法核对，暂不能保存，请核对最新状态。","muted"));
    if(validExpiry&&expiry>Date.now()&&values){button("保存这个偏好","accept");button("忽略","dismiss");}
    else{card.append(node("p",validExpiry&&expiry<=Date.now()?"此建议的确认时间已过，请核对最新状态。":"偏好建议不完整，暂不能保存。","memory-warning"));button("核对保存结果","check");}
  }else if(update.status==="unknown"||update.status==="preparing")button("核对保存结果","check");
  if(busy)card.append(node("p","正在核对处理结果，请勿重复操作…","muted"));
  if(actions.children.length)card.append(actions);box.append(card);
}
function showTaskDetails(record,s){
  if(!isCurrent(s))return;displayedTaskId=record.id;
  showConditions(record.conditions,s);showFacts(record.facts,s);showTaskPreferences(record.memory,s,record.memory_update,record.presentation);showExecution(record,s);showMemoryUpdate(record,s);
  showPolicyReview(record,s);
}
async function showPolicyReview(record,s){
  const revision=++policyReviewRevision,box=el("task-policy-review");box.replaceChildren();
  if(!record.policy_status_check_available||!finalStates.has(record.status))return;
  box.append(node("p","正在核对原报告所用政策的当前状态…","muted"));
  const current=()=>isCurrent(s)&&displayedTaskId===record.id&&revision===policyReviewRevision;
  try{
    const review=await api(`/api/tasks/${record.id}/policy-status`,{},s);if(!current())return;
    box.replaceChildren();
    box.append(node("h3","原报告的依据状态"),node("p",review.message,review.status==="warning"||review.status==="unavailable"?"memory-warning":"muted"));
    const labels={active:"仍有效",withdrawn:"已撤回",corrected:"已被更正稿替代",missing:"未找到",invalid_policy:"无法核实"};
    for(const source of review.sources||[]){
      box.append(node("p",`${source.document_id}：${labels[source.status]||"无法核实"}${source.reason?`；原因：${source.reason}`:""}${source.replacement_document_id?`；更正版：${source.replacement_document_id}`:""}`));
    }
    box.append(node("p","这项提示是在查看时重新核对的。原报告、原引用和原导出保持不变；需要当前结论时，请发起新查询。","muted"));
    const refresh=node("button","重新核对依据","secondary");refresh.onclick=()=>showPolicyReview(record,s);
    const download=node("button","下载原报告及当前状态","secondary");
    download.onclick=async()=>{try{
      const response=await fetch(`/api/tasks/${record.id}/policy-status/export`,{headers:{Authorization:`Bearer ${s.token}`,"X-Access-Mode":s.mode}});
      if(!current())return;if(!response.ok)throw Error("依据核对附件未能下载，请重新核对。");
      const blob=await response.blob();if(!current())return;
      const url=URL.createObjectURL(blob),a=node("a");a.href=url;a.download=`policy-review-${record.id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(e){error(e,s);}};
    box.append(refresh,download);
  }catch(e){if(current()){box.replaceChildren();box.append(node("p","暂时无法核对政策当前状态，不能认定旧依据仍有效。原报告未改动。","memory-warning"));}}
}
async function refreshSavedPreference(record,s){
  if(!isCurrent(s)||!["saved","unchanged"].includes(record.memory_update?.status))return;
  if(preferenceBusy||preferenceDraftChanged()){
    el("preferences-status").textContent="聊天偏好已更新；保留你正在修改的选项。请刷新偏好核对最新设置后再保存。";return;
  }
  await preferenceRequest("GET",s,true);
}
async function decideMemory(record,action,s){
  if(!isCurrent(s)||displayedTaskId!==record.id||memoryActions.has(record.id))return;
  const id=record.id;memoryActions.set(id,action);showMemoryUpdate(record,s);let latest=record;
  try{
    if(action==="check"){
      latest=await api(`/api/tasks/${id}`,{},s);
      // 服务端对这两种不确定状态只核对原保存，不重新写入；其他状态只读展示。
      if(["unknown","preparing"].includes(latest.memory_update?.status))latest=await api(`/api/tasks/${id}/memory-decision`,{method:"POST",body:JSON.stringify({action:"accept"})},s);
    }else latest=await api(`/api/tasks/${id}/memory-decision`,{method:"POST",body:JSON.stringify({action})},s);
    memoryUncertain.delete(id);
  }catch(e){
    if(!isCurrent(s))return;
    // 响应丢失时只读恢复；是否已保存由服务端记录决定，不自动再次写入。
    try{latest=await api(`/api/tasks/${id}`,{},s);memoryUncertain.delete(id);}
    catch{if(!isCurrent(s))return;memoryUncertain.set(id,action);}
    if(isCurrent(s)&&displayedTaskId===id&&e.httpStatus===403)error(e,s);
  }finally{
    if(isCurrent(s)){
      memoryActions.delete(id);
      if(displayedTaskId===id){showMemoryUpdate(latest,s);refreshTaskAnswer(latest,s);}
    }
  }
  if(!isCurrent(s))return;
  await refreshSavedPreference(latest,s);
  // 重新打开历史时也应显示最新保存状态，刷新失败不影响已显示的确认结果。
  try{await history(s);}catch(e){error(e,s);}
}
// 展示任务自身保存的执行摘要；不能从当前选择器推断旧任务使用的方案。
function showExecution(record,s){
  if(!isCurrent(s))return;const box=el("task-execution");box.replaceChildren();
  const plan=record.orchestration,budget=record.budget;
  if(!plan&&!budget&&!["period_analysis","refund_investigation"].includes(record.execution_mode))return; // 普通旧记录不虚构执行摘要。
  const detail=node("div",undefined,"execution-summary");
  const requested=executionLabels[plan?.requested||record.execution_mode||"single"]||"未知方案";
  const effective=executionLabels[plan?.effective||record.execution_mode||"single"]||"未知方案";
  detail.append(node("p",`执行方案：${requested}${requested!==effective?`；实际使用${effective}`:""}`));
  if(plan?.requested==="multi"&&plan?.effective==="single")detail.append(node("p",plan.reason==="conditions_not_ready"?"本次查询条件尚未齐全，先按单路澄清，没有拆分调用。":"本次条件没有形成金额与政策两个独立分工，按单路处理。","muted"));
  if(record.status==="partial")detail.append(node("p","本任务只完成了部分。可用事实仍保留，未完成分支不代表无订单或零退款。","partial-notice"));
  if(plan?.effective==="dependent"){
    detail.append(node("p","先读取授权订单事实，冻结后交给政策角色；后一步使用本次交接内容。"));
    if(plan.handoff?.sha256)detail.append(node("p",`事实交接指纹：${plan.handoff.sha256}`,"muted"));
    if(plan.followup)detail.append(node("p",`证据补查：${plan.followup.attempts??0}/${plan.followup.limit??1} 次；${plan.followup.status||"未执行"}。补查不改变身份范围，也不保证取得新依据。`,"muted"));
  }
  const validNumber=n=>typeof n==="number"&&Number.isFinite(n)&&n>=0;
  if(budget?.used&&budget?.limits){
    const counts=[["model_requests","模型请求"],["tool_calls","工具尝试"]].filter(([key])=>validNumber(budget.used[key])&&validNumber(budget.limits[key])).map(([key,label])=>`${label} ${budget.used[key]}/${budget.limits[key]}`);
    if(counts.length)detail.append(node("p",`整条任务共享额度：${counts.join("；")}。`));
    if(validNumber(budget.used.reserved_output_tokens)&&validNumber(budget.limits.reserved_output_tokens))detail.append(node("p",`模型输出额度预留 ${budget.used.reserved_output_tokens}/${budget.limits.reserved_output_tokens}，预留量不等于实际用量或费用。`,"muted"));
    if(budget.stop_reason==="deadline")detail.append(node("p","达到任务等待上限，未继续启动外部调用。","partial-notice"));
    if(budget.last_denial)detail.append(node("p","共享调用额度不足，部分后续调用未执行。","partial-notice"));
  }
  if(validNumber(plan?.wall_seconds))detail.append(node("p",`本次执行耗时：${plan.wall_seconds.toFixed(2)} 秒。`,"muted"));
  if(Array.isArray(plan?.branches)&&plan.branches.length){
    const list=node("ul");
    for(const branch of plan.branches){
      const label={amount:"金额核查",order_facts:"订单事实",policy:"政策解释"}[branch.name]||branch.name||"分支";
      const usage=branch.usage||{},counts=[];
      if(validNumber(usage.model_requests))counts.push(`模型请求 ${usage.model_requests} 次`);
      if(validNumber(usage.tool_calls))counts.push(`工具尝试 ${usage.tool_calls} 次`);
      if(validNumber(branch.wall_seconds))counts.push(`${branch.wall_seconds.toFixed(2)} 秒`);
      const insuff=branch.evidence_status==="insufficient_policy_evidence"?"；政策依据不足":"";
      list.append(node("li",`${label}：${statuses[branch.status]||branch.status||"状态未记录"}${counts.length?` · ${counts.join("，")}`:""}${insuff}`));
    }
    detail.append(list);
  }
  if(Array.isArray(record.program_executions))for(const run of record.program_executions){
    const execution=run.execution||{};
    detail.append(node("p",`隔离程序：${run.status||"状态未记录"}；工具尝试 ${execution.tool_calls??"未记录"}；容器回收 ${execution.container_cleanup||"未记录"}。`));
    if(execution.in_flight_tool_may_continue)detail.append(node("p","停止时已有只读请求在途，不能把取消视为远端请求已撤回。","muted"));
  }
  if(record.sql_validation){const pre=node("pre");pre.textContent=JSON.stringify(record.sql_validation,null,2);detail.append(node("p","快照查询检查（执行成功和金额口径一致分别核对）"),pre);}
  if(["period_analysis","refund_investigation"].includes(record.execution_mode)){
    const panel=node("details"),validation=record.analysis_validation||{};
    panel.append(node("summary","解释引用与固定摘要"));
    const label={accepted:"通过有限校验",rejected:"模型解释未被采纳",limited:"达到执行限制",cancelled:"已取消",query_failed:"比较事实不可用",facts_ready:"比较事实已准备",not_run:"尚未执行"}[validation.status]||"状态未记录";
    panel.append(node("p",`解释校验：${label}${validation.status?`（${validation.status}）`:""}。`));
    if(Array.isArray(validation.reasons)&&validation.reasons.length)panel.append(node("p",`原因记录：${validation.reasons.join("、")}`));
    if(validation.fallback==="fixed_facts")panel.append(node("p","本次回退到程序固定事实，不能作为模型解释已完成的证据。","partial-notice"));
    panel.append(node("p","有限校验不保证自由文本语义全对；渠道差额贡献不等于已经查明变化原因。","muted"));
    const evidence=record.analysis_evidence;
    if(evidence&&typeof evidence==="object"&&!Array.isArray(evidence)&&Object.keys(evidence).length){
      const table=node("table"),head=node("tr");
      for(const title of ["引用编号","说明","数值","事实位置"])head.append(node("th",title));table.append(head);
      for(const [id,item] of Object.entries(evidence)){
        const row=node("tr");
        for(const value of [id,item?.label,item?.display,item?.path])row.append(node("td",typeof value==="string"?value:"未提供"));
        table.append(row);
      }
      panel.append(table);
    }else panel.append(node("p","本记录没有可展示的解释引用映射，请核对原始事实或导出。","muted"));
    panel.append(node("p","程序固定摘要（原文）"));
    panel.append(node("pre",typeof record.fixed_answer==="string"&&record.fixed_answer?record.fixed_answer:"尚无可用固定摘要。"));
    detail.append(panel);
  }
  box.append(detail);
}
function hasUnsafeInteger(value){
  if(typeof value==="number")return !Number.isFinite(value)||(Number.isInteger(value)&&!Number.isSafeInteger(value));
  if(Array.isArray(value))return value.some(hasUnsafeInteger);
  return value!==null&&typeof value==="object"?Object.values(value).some(hasUnsafeInteger):false;
}
function showOrder(r,s){
  if(!isCurrent(s))return;lastOrder=r;const box=el("order-result");box.replaceChildren();
  if(r.status!=="ok"){box.append(node("p",r.message||"未得到可用结果"));return;}
  const cards=node("div",undefined,"metrics");
  for(const [label,key] of [["支付","paid_amount_fen"],["已退","refund_amount_fen"],["净额","net_amount_fen"]]){const item=node("div");item.append(node("span",label),node("strong",money(r[key])));cards.append(item);}
  box.append(cards,node("p",`订单 ${r.order_id} · ${r.refund_count} 笔有效退款`));
  const table=node("table"),head=node("tr");["退款编号","退款时间","金额"].forEach(x=>head.append(node("th",x)));table.append(head);
  (r.refunds||[]).forEach(refund=>{const row=node("tr");[refund.refund_id,new Date(refund.refunded_at).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai"}),money(refund.refund_amount_fen)].forEach(x=>row.append(node("td",x)));table.append(row);});
  box.append(table,node("p",r.data_assumption,"muted"));
}
function showConditions(conditions,s){
  if(!isCurrent(s))return;const box=el("conditions");box.replaceChildren();if(!conditions)return;
  const names={order_id:"订单编号",paid_from:"支付开始",paid_before:"支付结束",refund_before:"退款截止",policy_as_of:"政策日期",assessment_at:"售后判断时刻",is_unworn:"是否未穿过",tags_intact:"吊牌是否完好",reason:"退货原因",category:"商品品类",currency:"币种",budget_max_minor:"单件标价上限（最小货币单位）",material:"材质",cotton_percentage:"棉含量（%）",size:"尺码",require_returnable:"要求有可退依据",policy_at:"政策查询时刻"};
  const detail=node("details");detail.append(node("summary",`查看本次条件与来源 · 版本 ${conditions.revision}`));
  for(const [field,item] of Object.entries(conditions.confirmed||{}))detail.append(node("p",`${names[field]||field}：${item.value}；来自用户原话“${item.source?.quote||""}”`));
  for(const [field,item] of Object.entries(conditions.defaults||{}))detail.append(node("p",`${names[field]||field}：${item.value}；程序默认，非用户指定`));
  for(const [field,item] of Object.entries(conditions.claims||{}))detail.append(node("p",`${names[field]||field}：${item.value}；本轮用户自述，尚未核实；原话“${item.source?.quote||""}”`));
  if(conditions.kind==="support"){
    detail.append(node("p","政策版本由订单支付时刻、店铺和商品类别确定；用户自述不能替换后台收货记录。"));
    if(conditions.support_missing?.length)detail.append(node("p",`查询后仍缺：${conditions.support_missing.map(x=>names[x]||({delivered_at:"后台收货时间",product_category:"商品类别",is_custom:"定制状态"}[x])||x).join("、")}`));
  }
  if(conditions.missing?.length)detail.append(node("p",`尚缺：${conditions.missing.map(x=>names[x]||x).join("、")}`));
  if(conditions.clarification)detail.append(node("p",conditions.clarification));box.append(detail);
}
function showFacts(facts,s){
  if(!isCurrent(s))return;const box=el("facts");box.replaceChildren();
  if(!facts||!facts.length){box.append(node("p","本次暂无已执行工具依据。","muted"));return;}
  if(hasUnsafeInteger(facts)){box.append(node("p","本次依据含超出浏览器安全整数范围的数值，页面无法精确显示。请在历史记录中下载原始证据核对；未将舍入后的内容当作原始事实。","muted"));return;}
  facts.forEach(f=>{
    const detail=node("details");detail.append(node("summary",f.tool==="review_after_sales"?"展开完整政策原文与核查明细":`查看查询依据：${({lookup_general_policy:"店铺一般规则",recommend_products:"商品推荐",shopify_check_order:"平台测试订单",shopify_current_policy:"平台当前政策"}[f.tool]||f.tool)}`));
    if(["review_after_sales","lookup_general_policy","recommend_products","shopify_check_order","shopify_current_policy"].includes(f.tool)){
      const index=f.result?.citation_index;
      if(index&&typeof index==="object"&&!Array.isArray(index))for(const [id,item] of Object.entries(index)){
        if(!item||typeof item!=="object")continue;
        detail.append(node("strong",`[${id}]`),node("p",`来源：${item.source_path||item.document_id||"未记录"} · ${item.pointer?`目录位置 ${item.pointer}`:`第${item.line_start??"?"}–${item.line_end??"?"}行`}`),node("pre",typeof item.text==="string"?item.text:"原文未提供"));
      }
      detail.append(node("p","原文、条件来源及工具返回随任务保存；下载证据包含完整内容，不因页面折叠删减。","muted"));
    }
    if(["review_after_sales","lookup_general_policy","recommend_products","shopify_check_order","shopify_current_policy"].includes(f.tool)){const raw=node("details");raw.append(node("summary","完整工具数据"),node("pre",JSON.stringify(f,null,2)));detail.append(raw);}
    else detail.append(node("pre",JSON.stringify(f,null,2)));
    box.append(detail);
  });
}
function chat(role,text,s,taskId=null){if(!isCurrent(s))return;const b=node("div",undefined,"bubble "+role);if(role==="assistant")b.taskId=taskId;b.append(node("strong",role==="user"?"你":"助手"),node("p",text));el("conversation").append(b);}
function refreshTaskAnswer(record,s){
  if(!isCurrent(s)||displayedTaskId!==record.id||typeof record.answer!=="string")return;
  // 仅替换已展示的同一任务正文，使用服务端保存后的真实通知，不重新生成模型回答。
  for(const bubble of el("conversation").children)if(bubble.taskId===record.id)bubble.replaceChildren(node("strong","助手"),node("p",record.answer));
}
function openHistory(record,s){
  if(!isCurrent(s))return;
  if(currentTask===record.id&&(activePoll?.id===record.id||pendingSubmission?.taskId===record.id))return poll(record.id,s);
  if(polling||pendingSubmission){error(Error("当前任务或上次提交尚未确认，请先恢复或取消原任务，再查看另一项。"),s);return;}
  clearError(s);currentTask=record.id;
  // 普通任务续问使用它原来的会话与方案；专用解释和缺少会话信息的旧记录不混入聊天上下文。
  const canContinue=chatModes.has(record.execution_mode||"single")&&typeof record.session_id==="string"&&/^[a-zA-Z0-9_-]{1,64}$/.test(record.session_id);
  session=canContinue?record.session_id:crypto.randomUUID();
  if(canContinue){executionMode=record.execution_mode||"single";el("execution-mode").value=executionMode;}
  el("conversation").replaceChildren();el("message").value="";
  if(record.message)chat("user",record.message,s);
  showTaskDetails(record,s);el("status").textContent=`任务 ${record.id} · ${statuses[record.status]||record.status}`;
  if(finalStates.has(record.status)){
    chat("assistant",record.answer||statuses[record.status],s,record.id);setPolling(false,s);return;
  }
  // 已知任务只GET恢复，不重新POST；网络中断也保留它的控制权及原身份范围。
  pendingSubmission={kind:"history",taskId:record.id,body:{message:record.message},displayed:true};
  return poll(record.id,s);
}
function renderHistory(rows,s){
  if(!isCurrent(s))return;const box=el("history");box.replaceChildren();
  rows.forEach(r=>{
    const row=node("div",undefined,"history-row");row.append(node("span",`${statuses[r.status]||r.status} · ${executionLabels[r.execution_mode||"single"]||"未知方案"} · ${r.message}`));
    const open=node("button",finalStates.has(r.status)?"查看":"恢复查看","secondary");open.onclick=()=>openHistory(r,s);row.append(open);
    const download=node("button","导出依据","secondary");download.onclick=async()=>{try{
      if(!isCurrent(s))return;
      const response=await fetch(`/api/tasks/${r.id}/export`,{headers:{Authorization:`Bearer ${s.token}`,"X-Access-Mode":s.mode}});
      // 直接保存服务端字节，不先JSON解析/再编码，避免BIGINT在浏览器中被舍入。
      const blob=await response.blob();if(!isCurrent(s))return;
      if(!response.ok)throw Error("证据导出失败，请核对登录及任务状态。");
      const url=URL.createObjectURL(blob),a=node("a");a.href=url;a.download=`task-${r.id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(e){error(e,s);}};row.append(download);box.append(row);
  });
}
async function history(s){const rows=await api("/api/tasks",{},s);renderHistory(rows,s);}
async function loadScope(s){
  const [me,r,rows]=await Promise.all([api("/api/me",{},s),api("/api/orders",{},s),api("/api/tasks",{},s),preferenceRequest("GET",s)]);
  if(!isCurrent(s))return;
  el("identity").textContent=me.role==="merchant"?`当前授权本地演示店铺：${me.shop_id}${me.platform_context?.shop_domain?`；另已授权Shopify开发店：${me.platform_context.shop_domain}（只读，示例：核查 Shopify #1001）`:""}`:`当前测试买家：${me.buyer_id}`;
  el("orders").textContent=r.orders.length?`当前范围订单：${r.orders.map(x=>x.order_id).join("、")}`:"当前范围没有订单";renderHistory(rows,s);
}
function poll(id,s){
  if(!isCurrent(s))return;
  if(activePoll?.id===id&&isCurrent(activePoll.scope))return activePoll.promise;
  const owner={id,scope:s,promise:null};activePoll=owner;setPolling(true,s);
  owner.promise=(async()=>{
    try{while(isCurrent(s)&&token&&currentTask===id&&activePoll===owner){
      const r=await api(`/api/tasks/${id}`,{},s);if(!isCurrent(s)||currentTask!==id||activePoll!==owner)return;
      if(r.id!==id)throw Error("任务响应编号不一致，请重新核对原任务。");
      el("status").textContent=`${statuses[r.status]||r.status} · 任务 ${id}`;
      if(finalStates.has(r.status)){
        if(pendingSubmission?.taskId===id)pendingSubmission=null;
        chat("assistant",r.answer||statuses[r.status],s,r.id);showTaskDetails(r,s);
        await refreshSavedPreference(r,s);await history(s);break;
      }
      await new Promise(resolve=>setTimeout(resolve,1000));
    }}catch(e){
      const action=pendingSubmission?.kind==="chat"?"再次发送原问题":pendingSubmission?.kind==="analysis"?"点击恢复上次解释":"重新点击原任务的恢复查看";
      error(pendingSubmission?.taskId===id?Error(`查询暂未完成，原任务已保留；${action}即可继续查询，不会重新创建任务。`):e,s);
    }finally{if(isCurrent(s)&&activePoll===owner){activePoll=null;setPolling(false,s);}}
  })();
  return owner.promise;
}
el("login-form").onsubmit=async e=>{
  // 登录前可选角色，仅传递查询范围；是否获准仍由后端根据凭证核验。
  e.preventDefault();const nextToken=el("token").value,nextMode=el("login-mode").value||"buyer",s=beginScope(nextToken,nextMode);el("login").hidden=false;el("workspace").hidden=true;
  try{await loadScope(s);if(!isCurrent(s))return;el("login").hidden=true;el("workspace").hidden=false;}
  catch(e){if(!isCurrent(s))return;const empty=beginScope("","buyer");error(e,empty);}
};
el("mode").onchange=async()=>{
  const old=mode,s=beginScope(token,el("mode").value);el("mode").disabled=true;el("send").disabled=true;
  try{await loadScope(s);if(!isCurrent(s))return;setPolling(false,s);el("status").textContent="已切换授权范围并新建对话；旧结果保留在原模式的历史中。";}
  catch(e){
    if(!isCurrent(s))return;
    // 仅当前切换失败才恢复原角色；晚到的错误不得回滚新登录。
    const restored=beginScope(token,old);
    try{await loadScope(restored);error(e,restored);}
    catch(restoreError){if(!isCurrent(restored))return;const empty=beginScope("","buyer");el("login").hidden=false;el("workspace").hidden=true;error(restoreError,empty);}
  }
};
el("execution-mode").onchange=()=>{
  if(polling||pendingSubmission){el("execution-mode").value=executionMode;error(Error("当前任务或上次提交尚未确认，请先完成、取消或恢复查询，再切换方案。"));return;}
  const selected=el("execution-mode").value;
  if(!chatModes.has(selected)){el("execution-mode").value=executionMode;return;}
  if(selected===executionMode)return;
  executionMode=selected;session=crypto.randomUUID();currentTask=null;displayedTaskId=null;clearError();
  for(const id of ["conversation","facts","conditions","task-preferences","task-execution","task-memory-update","task-policy-review"])el(id).replaceChildren();
  el("status").textContent=`已切换为${executionLabels[executionMode]}并新建对话。旧任务保留在历史中；发送后才调用模型。`;
};
el("logout").onclick=()=>{beginScope("","buyer");el("login").hidden=false;el("workspace").hidden=true;};
el("check-form").onsubmit=async e=>{
  e.preventDefault();const s=captureScope();clearError(s);
  try{const r=await api(`/api/orders/${encodeURIComponent(el("order").value)}?refund_before=${encodeURIComponent(el("cutoff").value+":00+08:00")}`,{},s);showOrder(r,s);}catch(e){error(e,s);}
};
el("ask-form").onsubmit=async e=>{
  e.preventDefault();if(polling)return;const s=captureScope();clearError(s);
  if(pendingSubmission&&pendingSubmission.kind!=="chat"){error(Error("原任务尚未确认，请先恢复查看或取消；不能用新问题重新提交。"),s);return;}
  const draft=el("message").value.trim(),message=draft||pendingSubmission?.body.message;
  if(!message)return;
  if(pendingSubmission&&message!==pendingSubmission.body.message){error(Error("上次提交结果尚未确认，请先重试原问题，再发送不同内容。"),s);return;}
  if(!pendingSubmission)currentTask=null;
  // 编号属于这次提交及原会话。响应丢失时重发同一个请求；已知任务只恢复查询。
  const pending=pendingSubmission||(pendingSubmission={kind:"chat",body:{message,session_id:session,request_id:crypto.randomUUID(),execution_mode:executionMode},taskId:null,displayed:false,uncertain:false});
  let launched=false;setPolling(true,s);
  try{
    if(!pending.taskId){
      const r=await api("/api/tasks",{method:"POST",body:JSON.stringify(pending.body)},s);
      if(!isCurrent(s))return;pending.taskId=r.id;
    }
    if(!isCurrent(s))return;
    if(!pending.displayed){chat("user",pending.body.message,s);pending.displayed=true;el("message").value="";}
    currentTask=pending.taskId;launched=true;await poll(pending.taskId,s);
  }catch(e){
    if(!isCurrent(s))return;
    // 首次明确4xx拒绝可重新编辑；已有未知提交时，后来的拒绝不能证明原任务不存在。
    if(!pending.taskId&&e.httpStatus>=400&&e.httpStatus<500&&!pending.uncertain)pendingSubmission=null;
    else pending.uncertain=true;
    error(pendingSubmission?Error("上次提交结果尚未确认；请重试原问题，将复用原请求或查询已受理任务。" ):e,s);
  }finally{if(isCurrent(s)&&!launched)setPolling(false,s);}
};
el("cancel").onclick=async()=>{
  const s=captureScope(),id=currentTask;if(!id)return;
  try{const r=await api(`/api/tasks/${id}/cancel`,{method:"POST"},s);if(!isCurrent(s)||currentTask!==id)return;el("status").textContent=statuses[r.status]||r.status;if(!polling)await poll(id,s);}catch(e){error(e,s);}
};
el("new-chat").onclick=()=>{if(polling)return;if(pendingSubmission){error(Error("上次提交结果尚未确认，请先重试原问题，再新建对话。"));return;}session=crypto.randomUUID();currentTask=null;displayedTaskId=null;for(const id of ["conversation","facts","conditions","task-preferences","task-execution","task-memory-update","task-policy-review"])el(id).replaceChildren();el("status").textContent="已新建对话；旧任务仍保存在历史记录中。";};
el("refresh").onclick=()=>{const s=captureScope();return history(s).catch(e=>error(e,s));};
el("preferences-form").onsubmit=e=>{e.preventDefault();return preferenceRequest("PUT");};
el("preferences-delete").onclick=()=>preferenceRequest("DELETE");
el("preferences-refresh").onclick=()=>preferenceRequest("GET");

// 固定计算入口使用同一身份范围；晚到的响应不能写进新角色页面。
for(const id of Object.values(comparisonFields))el(id).oninput=()=>{
  comparisonSnapshot=null;comparisonRevision+=1;comparisonBusy=false;setAnalysisControls();
};
el("compare-form").onsubmit=async event=>{
  event.preventDefault();if(comparisonBusy||pendingSubmission?.kind==="analysis")return;
  const s=captureScope(),revision=++comparisonRevision,body=comparisonBody();
  clearError(s);comparisonSnapshot=null;comparisonBusy=true;setAnalysisControls();
  try{
    const result=await api("/api/analysis/compare",{method:"POST",body:JSON.stringify(body)},s);
    if(revision!==comparisonRevision||JSON.stringify(body)!==JSON.stringify(comparisonBody()))return;
    const box=el("comparison");box.replaceChildren();
    if(result.status!=="ok"){box.append(node("p",result.message||"比较未完成，未显示部分总额。"));return;}
    const table=node("table"),head=node("tr");for(const text of ["口径","第一期","第二期","差额"])head.append(node("th",text));table.append(head);
    for(const [key,label] of [["paid_amount_fen","支付金额"],["refund_amount_fen","退款金额"],["net_amount_fen","净额"]]){const row=node("tr");row.append(node("th",label));for(const part of ["first","second","delta"])row.append(node("td",money(result[part][key])));table.append(row);}box.append(table,node("p",result.reason_notice));
    for(const channel of result.channels)box.append(node("p",`${channel.label}：净额差 ${money(channel.contribution_net_fen)}；${{new:"本期新增",disappeared:"本期消失",continued:"两期都有"}[channel.presence]||channel.presence}`));
    const details=node("details"),summary=node("summary","查看订单、条件与导入来源"),pre=node("pre");pre.textContent=JSON.stringify(result,null,2);details.append(summary,pre);box.append(details);
    comparisonSnapshot=body;
  }catch(e){if(revision===comparisonRevision)error(e,s);}finally{if(isCurrent(s)&&revision===comparisonRevision){comparisonBusy=false;setAnalysisControls();}}
};
// 比较只算金额；解释是单独、明确的付费操作，冻结本次条件并使用全新会话。
async function startAnalysis(analysisMode){
  if(polling)return;const s=captureScope();clearError(s);
  if(pendingSubmission&&(pendingSubmission.kind!=="analysis"||(pendingSubmission.analysisMode||"period_analysis")!==analysisMode)){error(Error("请先恢复或取消当前任务，再解释两期变化。"),s);return;}
  if(!pendingSubmission&&!comparisonMatches()){error(Error("条件已变化或尚未成功比较，请先重新比较两期。"),s);setAnalysisControls();return;}
  if(!pendingSubmission){
    pendingSubmission={kind:"analysis",analysisMode,body:{...comparisonSnapshot,session_id:crypto.randomUUID(),request_id:crypto.randomUUID()},taskId:null,displayed:false,uncertain:false};
    session=crypto.randomUUID();currentTask=null;el("conversation").replaceChildren();el("message").value="";
  }
  const pending=pendingSubmission;let launched=false;setPolling(true,s);
  try{
    if(!pending.taskId){
      const r=await api(analysisMode==="refund_investigation"?"/api/analysis/investigate":"/api/analysis/tasks",{method:"POST",body:JSON.stringify(pending.body)},s);
      if(!isCurrent(s))return;
      if(typeof r.id!=="string"||!r.id||r.session_id!==pending.body.session_id||r.execution_mode!==analysisMode)throw Error("解释任务的会话或方案无法核对，请恢复原提交。");
      pending.taskId=r.id;pending.message=r.message;
    }
    if(!isCurrent(s))return;
    if(!pending.displayed){chat("user",pending.message||"解释已核对的两期金额变化",s);pending.displayed=true;}
    currentTask=pending.taskId;launched=true;await poll(pending.taskId,s);
  }catch(e){
    if(!isCurrent(s))return;
    if(!pending.taskId&&e.httpStatus>=400&&e.httpStatus<500&&!pending.uncertain)pendingSubmission=null;
    else pending.uncertain=true;
    error(pendingSubmission?Error("上次解释结果尚未确认；点击恢复上次解释，复用原请求或查询原任务，不会改变条件。"):e,s);
  }finally{if(isCurrent(s)&&!launched)setPolling(false,s);}
};

el("compare-explain").onclick=()=>startAnalysis("period_analysis");
el("compare-investigate").onclick=()=>startAnalysis("refund_investigation");
