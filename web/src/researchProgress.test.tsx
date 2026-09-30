import {expect, it} from 'vitest';
import { activityState } from './components/conversationHelpers';
import {initialView, reduce, mergeSimpleResult} from './reducer';
import type {RunView} from './types';

function tool(view:RunView, id:string, args:Record<string, unknown>, extra:Record<string,unknown>={}) {
  return reduce(view,{type:'subagent_tool_call',mode:'simple',role:'lead',tool:'inspect_research',
    call_id:id,turn:1,phase:'done',ok:true,args_summary:JSON.stringify(args),...extra},1000);
}
it.each([
  {role:'subagent',delegation_id:'sub1',parent_call_id:'d'},
  {role:'subagent'}, // 身份缺失的旧事件也不能冒充主线。
])('子任务交稿不使主线进入保存状态：%j',scope=>{
  let v=tool(initialView(),'child',{}, {...scope,tool:'finish_research'});
  expect(activityState(v)?.text).not.toContain('已提交答案');
  v=tool(v,'rejected',{}, {tool:'finish_research',ok:false,status:'rejected'});
  expect(activityState(v)?.text).not.toContain('已提交答案');
  v=tool(v,'accepted',{}, {tool:'finish_research'});
  expect(activityState(v)?.text).toContain('已提交答案');
});

it('子任务工具运行不会遮住主线的真实活动',()=>{
  let v=tool(initialView(),'child',{}, {tool:'read_source',role:'subagent',delegation_id:'sub1',phase:'running',ok:false});
  v=tool(v,'parent',{view:'cards',card_id:'c36'},{phase:'running',ok:false});
  expect(activityState(v)?.text).toContain('回读证据 c36');
});

it('最终路线缺失标记在实时与刷新一致，旧记录不补造缺失判断',()=>{
  const base=reduce(initialView(),{type:'research_plan_updated',items:[{id:'a',text:'核对材料',status:'doing'}]},1000);
  const live=reduce(base,{type:'research_status',phase:'finished',plan_sync_status:'missing'},2000);
  const history=mergeSimpleResult(base,{plan_sync_status:'missing'} as any,null);
  for(const v of [live,history]) {
    expect(v.simple?.planSyncStatus).toBe('missing');
    expect(v.simple?.researchPlan?.items[0].status).toBe('doing');
  }
  expect(mergeSimpleResult(base,{},null).simple?.planSyncStatus).toBeUndefined();
  expect(mergeSimpleResult(base,{plan_sync_status:'reported'},null).simple?.planSyncStatus).toBe('reported');
});

it('KOL 原始事件摘录：1:55 未交稿，2:07 保留 8 完成与 2 待做',()=>{
  const first=Array.from({length:8},(_,i)=>({id:`r${i+1}`,text:`路线${i+1}`,status:i?'todo':'doing'}));
  let v=reduce(initialView(),{type:'research_plan_updated',items:first,seq:51},7880);
  v=tool(v,'child2',{}, {role:'subagent',delegation_id:'sub2',tool:'finish_research',seq:796});
  v=tool(v,'child1',{}, {role:'subagent',delegation_id:'sub1',tool:'finish_research',seq:934});
  expect(activityState(v)?.text).not.toContain('已提交答案');
  expect(v.simple?.researchPlan?.items.filter(i=>i.status==='todo')).toHaveLength(7);
  const end=[...first.map(i=>({...i,status:'done'})),...['合同报价','其他地区合规'].map((text,i)=>({id:`r${9+i}`,text,status:'todo'}))];
  const update={type:'research_plan_updated',items:end,seq:970};
  v=reduce(v,update,127210);
  v=reduce(v,update,127210);
  v=tool(v,'finish',{}, {tool:'finish_research',seq:972});
  expect(activityState(v)?.text).toContain('已提交答案');
  expect(v.simple?.researchPlan?.items.filter(i=>i.status==='done')).toHaveLength(8);
  expect(v.simple?.researchPlan?.items.filter(i=>i.status==='todo')).toHaveLength(2);
  expect(v.simple?.planHistory).toHaveLength(2);
});
