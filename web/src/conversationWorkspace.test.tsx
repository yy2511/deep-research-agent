import { expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { initialView, reduce } from './reducer';
import { ConversationWorkspace, EvidenceDetail, FailedToolCalls, ProcessList, UsageOverview } from './components/ConversationWorkspace';
import { processRows } from './conversationModel';

it('证据卡显示原文校正方式、原摘录和具体差异', () => {
  const html=renderToStaticMarkup(<EvidenceDetail card={{card_id:'c1',claim:'已送达',
    quote:'紧急调拨物资。',source_ref:'s1',quotes:[{
    quote:'紧急调拨物资。',submitted_quote:'紧急调摋物资。',source_ref:'s1',
    binding:{method:'fuzzy',confirmed:false,changes:[{kind:'replace',submitted:'摋',original:'拨',
      submitted_start:3,submitted_end:4,source_start:3,source_end:4}]}}]}} sources={[]}
    runId='corrected' onSource={()=>{}} onOrigin={()=>{}} view={initialView()}/>);
  expect(html).toContain('已按原文校正');
  expect(html).toContain('近似文字定位');
  expect(html).toContain('紧急调拨物资。');
  expect(html).toContain('紧急调摋物资。');
  expect(html).toContain('模型：<code>摋</code>');
  expect(html).toContain('原文：<code>拨</code>');
  expect(html).toContain('文字定位不判断结论是否正确');
});

it('浏览器证据明确显示页面快照身份', () => {
  const html=renderToStaticMarkup(<EvidenceDetail card={{card_id:'c1',claim:'降雨量',
    quote:'降雨量 42 mm',source_ref:'s1',source_kind:'browser_snapshot'}} sources={[]}
    runId='browser' onSource={()=>{}} onOrigin={()=>{}} view={initialView()}/>);
  expect(html).toContain('浏览器页面快照</label>');
  expect(html).not.toContain('>原文</label>');
});

it('正式答案使用独立正文，不把下载报告封装标题与状态段再次显示', () => {
  const view=initialView();
  view.simple={mode:'simple',phase:'finished',draft:'正式答案 [c1]'};
  view.reportMd='# 重复标题\n正式答案 [c1]\n## 状态\n内部交付状态';
  view.endedAtMs=2000;
  const html=renderToStaticMarkup(<ConversationWorkspace view={view} runId='test-answer'/>);
  expect(html).toContain('正式答案');
  expect(html).not.toContain('重复标题');
  expect(html).not.toContain('内部交付状态');
});

it('结束过程可完整收起，用户补充和失败提示仍在外面', () => {
  let view=reduce(initialView(), {type:'subagent_tool_call',mode:'simple',phase:'done',tool:'read_source',call_id:'failed',call_no:1,ok:false,args:'大段工具参数',error:'读取失败'},1000);
  view=reduce(view,{type:'research_input',kind:'accepted',state:'open',message:{message_id:'u1',text:'改为只查官方资料',revision:1,status:'queued',at:2}},2000);
  view.endedAtMs=3000;
  const html=renderToStaticMarkup(<ConversationWorkspace view={view} runId='test-fold'/>);
  expect(html).toContain('aria-expanded="false"');
  expect(html).toContain('改为只查官方资料');
  expect(html).toContain('1 次工具调用未成功');
  expect(html).not.toContain('大段工具参数');
});

it('每次失败都有独立入口，包含子 Agent；重放不重复计数，运行中不算失败', () => {
  const first={type:'subagent_tool_call',mode:'simple',phase:'done',status:'failed',tool:'read_source',call_id:'reused',turn:6,call_no:13,ok:false,result_summary:'没有找到指定片段'};
  let view=reduce(initialView(),first,1000);
  view=reduce(view,first,1000);
  view=reduce(view,{...first,turn:10,call_no:18,tool:'bash',result_summary:JSON.stringify({ok:false,stderr:'Traceback\nIndexError: list index out of range\n'})},2000);
  view=reduce(view,{...first,call_id:'child',delegation_id:'sub1',turn:2,call_no:3,result_summary:'子任务读取超时',status:'timeout'},2500);
  view=reduce(view,{...first,call_id:'working',phase:'running',status:'running'},3000);
  const html=renderToStaticMarkup(<FailedToolCalls view={view} expanded={{}} toggle={()=>{}}/>);
  expect(html).toContain('3 次工具调用未成功');
  expect(html.match(/aria-expanded="false"/g)).toHaveLength(3);
  expect(html).toContain('主 Agent · 第 6 轮 · 工具 #13');
  expect(html).toContain('主 Agent · 第 10 轮 · 工具 #18');
  expect(html).toContain('子 Agent sub1 · 第 2 轮 · 工具 #3');
  expect(html).toContain('IndexError: list index out of range');
  expect(html).toContain('子任务读取超时');
  expect(html).not.toContain('查看最近一次');
  expect(html.indexOf('没有找到指定片段')).toBeLessThan(html.indexOf('IndexError'));
});

it('关闭运行记录视图后，较早和较晚的失败仍能各自展开输入输出', () => {
  let view=initialView();
  for(const [i,id] of ['earlier','later'].entries())view=reduce(view,{type:'subagent_tool_call',mode:'simple',phase:'done',status:'failed',tool:'bash',call_id:id,turn:i+1,call_no:i+1,ok:false,args_summary:`input-${id}`,result_summary:`error-${id}`},1000+i);
  const entries=Object.entries(view.simpleTools!);
  const expanded=Object.fromEntries(entries.map(([key])=>[`failure:${key}`,true]));
  const html=renderToStaticMarkup(<FailedToolCalls view={view} expanded={expanded} toggle={()=>{}}/>);
  for(const id of ['earlier','later']){
    expect(html).toContain(`input-${id}`);
    expect(html).toContain(`error-${id}`);
    expect(html).toContain(`<code>${id}</code>`);
  }
  expect(html.match(/aria-expanded="true"/g)).toHaveLength(2);
  expect(html).not.toContain('在运行记录中查看');
});

it('思考与公开说明分开呈现，中断的思考保留且明确标识', () => {
  let view=reduce(initialView(),{type:'agent_reasoning',msg_id:'r:reason',role:'lead',kind:'reasoning',text:'已收到的思考',call_id:'llm1',complete:false,interrupted:true},1000);
  view=reduce(view,{type:'lead_message',msg_id:'r:content',role:'lead',text:'公开说明',complete:true},2000);
  const html=renderToStaticMarkup(<ProcessList view={view} rows={processRows(view)} expanded={{'row:r:reason':true}} toggle={()=>{}} onCall={()=>{}} onAgent={()=>{}}/>);
  expect(html).toContain('思考');
  expect(html).toContain('说明');
  expect(html).toContain('已收到的思考');
  expect(html).toContain('这段记录不完整');
  expect(html).toContain('查看模型调用');
});

it('历史缺失缓存与费用记录显示未知，快放不提示可发送追问', () => {
  const view=initialView();view.endedAtMs=2000;view.replayOnly=true;
  expect(renderToStaticMarkup(<UsageOverview view={view}/>)).toContain('未知');
  const html=renderToStaticMarkup(<ConversationWorkspace view={view} runId='test-replay'/>);
  expect(html).toContain('只读快放');
  expect(html).toMatch(/textarea[^>]*disabled/);
});

it('预算停止、受限交付和覆盖缺口不藏入过程折叠区', () => {
  const view=initialView();view.endedAtMs=2000;
  view.simple={mode:'simple',phase:'finished',stopReason:'call_budget+wrapup',delivery:'limited',
    draft:'已有发现',coverage:{price:'unavailable',missing:'unanswered',done:'answered',some:'partial',open:'unanswered'},
    requirements:[{id:'price',text:'比较成交价'},{id:'missing',text:'未报覆盖的要求'},
      {id:'done',text:'已答要求'},{id:'some',text:'部分回答要求'},{id:'open',text:'明确未回答要求'}],
    coverageReasons:{price:['未找到成交公告'],missing:['Lead 未报告该要求的覆盖情况'],
      open:['Lead 自报：来源未报告该要求的覆盖情况']},warnings:['资料读取不完整']};
  const html=renderToStaticMarkup(<ConversationWorkspace view={view} runId='test-limited'/>);
  expect(html).toContain('模型调用次数用完，提前收尾');
  expect(html).toContain('部分完成');
  expect(html).toContain('资料不可得');
  expect(html).toContain('未找到成交公告');
  expect(html).toContain('资料读取不完整');
  expect(html).toContain('<strong>未报覆盖的要求</strong> · 未报告');
  expect(html).toContain('<strong>已答要求</strong> · 已回答');
  expect(html).toContain('<strong>部分回答要求</strong> · 部分回答');
  expect(html).toContain('<strong>明确未回答要求</strong> · 未回答');
});

it('停止的研究不把后端状态报告当回答显示，改为说明并保留证据入口', () => {
  const view=initialView();view.startedAtMs=1000;view.endedAtMs=21000;
  view.simple={mode:'simple',phase:'finished',stopReason:'cancelled',delivery:'failed',draft:'',
    warnings:['运行已取消：不再发起收尾调用'],coverage:{goal:'unanswered'}};
  view.reportMd='# 问题\n\n（没有可交付正文）\n\n## 状态\n- 停止原因：cancelled';
  const html=renderToStaticMarkup(<ConversationWorkspace view={view} runId='test-stopped'/>);
  expect(html).toContain('已停止');
  expect(html).toContain('这次没有写出回答');
  expect(html).toContain('用时 20 秒');
  expect(html).not.toContain('停止原因：cancelled');
  expect(html).not.toContain('运行已取消');
  expect(html).not.toContain('回答范围与覆盖说明');
});
