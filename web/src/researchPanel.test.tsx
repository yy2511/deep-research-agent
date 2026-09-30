import { expect, it } from 'vitest';
import { initialView, reduce } from './reducer';
import type { RunView } from './types';

function fixture() {
  let v = initialView();
  for (const [i, parent] of ['a','b'].entries()) {
    const id = `sub${i+1}`;
    v = reduce(v, {type:'delegation_updated', kind:'state',
      task:{id, parent_call_id:parent, goal:`目标${parent}`, status:'running', started_at:i+1}}, (i+1)*1000);
    for (const phase of ['running','done']) v = reduce(v, {type:'subagent_tool_call', mode:'simple', role:'subagent',
      delegation_id:id, parent_call_id:parent, call_id:'same', turn:1, tool:'search_sources', phase,
      ok:phase==='done', args_summary:JSON.stringify({query:`检索${parent}`})}, (i+1)*1000+100);
  }
  return v;
}

it('并行子任务的相同工具 id 不互相覆盖', () => {
  let v=fixture();
  v=reduce(v,{type:'subagent_tool_call',role:'lead',call_id:'main',turn:1,
    tool:'search_sources',phase:'done',ok:true},3000);
  expect(v.simple?.delegations?.map(d=>[d.id,d.goal,d.startedAtMs,d.steps.length]))
    .toEqual([['sub1','目标a',1000,1],['sub2','目标b',2000,1]]);
  expect(Object.values(v.simpleTools ?? {}).filter(c=>c.delegationId)).toHaveLength(2);
  expect(v.simple?.delegations?.every(d=>d.endedAtMs===undefined)).toBe(true);
});

it('重复工具与结果终态不重复统计，失败结果仍保留已有材料',()=>{
  let v=fixture();
  const event={type:'subagent_tool_call',mode:'simple',role:'subagent',delegation_id:'sub1',parent_call_id:'a',call_id:'save',turn:2,tool:'save_evidence',phase:'done',ok:true,display:{card_ids:['c1']}};
  v=reduce(v,event,3000);v=reduce(v,event,3000);
  expect(v.simple?.delegations?.[0].cardCount).toBe(1);
  const result={type:'delegation_updated',kind:'result',task:{id:'sub1',parent_call_id:'a',
    status:'failed',started_at:1,ended_at:4,result:{card_ids:['c1'],gaps:['还有未回答的问题'],
      summary_available:false,error:{stage:'execution',type:'TimeoutError',message:'超时'}}}};
  v=reduce(v,result,4000);v=reduce(v,result,4000);
  expect(v.simple?.delegations?.[0]).toMatchObject({status:'failed',endedAtMs:4000,cardCount:1,gaps:1,
    result:{cardIds:['c1'],summaryAvailable:false,error:{type:'TimeoutError'}}});
  expect(v.simple?.delegations?.[0].steps).toHaveLength(2);
  expect(v.simple?.delegations?.[1].endedAtMs).toBeUndefined();
});

it('子任务状态与结束时间只由生命周期事件决定',()=>{
  let v=fixture();
  v=reduce(v,{type:'subagent_tool_call',mode:'simple',role:'subagent',delegation_id:'sub1',parent_call_id:'a',call_id:'finish',turn:3,tool:'finish_research',phase:'done',ok:true},3000);
  expect(v.simple?.delegations?.[0]).toMatchObject({status:'running'});
  expect(v.simple?.delegations?.[0].endedAtMs).toBeUndefined();
  v=reduce(v,{type:'delegation_updated',kind:'result',task:{id:'sub1',parent_call_id:'a',status:'completed',started_at:1,ended_at:3}},3100);
  // 旧同步回执不能覆盖现役事件确定的状态，也不能把等待其他任务的时间加进来。
  v=reduce(v,{type:'subagent_tool_call',mode:'simple',role:'lead',tool:'delegate_research',call_id:'a',turn:1,phase:'done',ok:true,display:{tasks:[{delegation_id:'sub1',goal:'旧目标',status:'failed'}]}},8000);
  expect(v.simple?.delegations?.[0]).toMatchObject({goal:'目标a',status:'completed',endedAtMs:3000});
  expect(v.simple?.delegations?.[1].endedAtMs).toBeUndefined();
});

it('只有工具回执时保留工具记录，不推测子任务生命周期',()=>{
  let v: RunView={...initialView(),simple:{mode:'simple',phase:'research'}};
  v=reduce(v,{type:'subagent_tool_call',role:'subagent',delegation_id:'sub1',parent_call_id:'a',
    call_id:'finish',turn:1,tool:'finish_research',phase:'done',ok:true},3000);
  expect(Object.values(v.simpleTools ?? {})).toHaveLength(1);
  expect(v.simple?.delegations ?? []).toHaveLength(0);
});
