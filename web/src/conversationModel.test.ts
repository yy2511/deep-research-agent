import {describe,it,expect} from 'vitest';
import {initialView,reduce} from './reducer';
import {processRows,processSegments,usageSummary,cardCall,modelRecordKey} from './conversationModel';

describe('production conversation projection',()=>{
 it('keeps one call in its original position, with user messages outside folded groups',()=>{
  let v=initialView();
  const rows=[
   {type:'research_status',phase:'starting',mode:'simple'},
   {type:'subagent_tool_call',mode:'simple',turn:1,call_id:'t1',tool:'read_source',phase:'running'},
   {type:'research_input',state:'open',message:{message_id:'u1',text:'change',at:2,revision:1,status:'queued'}},
   {type:'subagent_tool_call',mode:'simple',turn:1,call_id:'t1',tool:'read_source',phase:'done',ok:true},
   {type:'lead_message',msg_id:'m1',part_seq:0,text:'updated',mode:'replace',complete:true},
  ];
  rows.forEach((e,i)=>{v=reduce(v,{...e,seq:i+1},i*1000)});
  expect(processRows(v).filter(r=>r.kind==='tool')).toHaveLength(1);
  expect(processSegments(v).map(s=>s.kind)).toEqual(['process','user','process']);
 });
 it('keeps cache attempt identity and reasoning separate, never fabricates zero',()=>{
  let v=initialView();
  v=reduce(v,{type:'agent_reasoning',seq:1,msg_id:'r1',call_id:'budget-1',kind:'reasoning',part_seq:0,mode:'append',text:'thought'},10);
  v=reduce(v,{type:'agent_reasoning',seq:2,msg_id:'r1',call_id:'budget-1',part_seq:1,mode:'complete',complete:false,interrupted:true},11);
  v=reduce(v,{type:'llm_usage',seq:3,request_id:'req1',call_id:'budget-1',input_tokens:100,output_tokens:5,cache_read_tokens:80,cache_miss_tokens:20,cache_status:'reported'},12);
  v=reduce(v,{type:'llm_usage',seq:4,request_id:'req2',call_id:'budget-2',input_tokens:null,output_tokens:null,cache_status:'unknown'},13);
  expect(v.leadMessages?.[0]).toMatchObject({kind:'reasoning',interrupted:true,complete:false,callId:'budget-1'});
  expect(usageSummary(v)).toMatchObject({requests:2,input:100,hit:80,miss:20,cacheKnown:1,unknown:1});
  expect(usageSummary(initialView()).input).toBeNull();
 });
 it('links a card only to an explicit saved or imported card id',()=>{
  const v=initialView();
  v.simpleTools={t:{callNo:1,tool:'save_evidence',ok:true,args:'c9',result:'',atMs:1,display:{card_ids:['c1']}}};
  expect(cardCall(v,'c1')).toBe('t');
  expect(cardCall(v,'c9')).toBeUndefined();
 });
});
it('精简事件保留延迟读取身份，开始/结束仍合并为一次工具调用', () => {
 let view=reduce(initialView(),{type:'subagent_tool_call',mode:'simple',phase:'running',tool:'read_source',call_id:'t1',call_no:1,detail_seq:4,args_summary:'s1'},1000);
 view=reduce(view,{type:'subagent_tool_call',mode:'simple',phase:'done',tool:'read_source',call_id:'t1',call_no:1,detail_seq:5,result_summary:'已读取',ok:true},2000);
 view=reduce(view,{type:'llm_call',mode:'simple',call_id:'m1',detail_seq:6,step:'research',ms:12},2001);
 expect(Object.values(view.simpleTools??{})).toHaveLength(1);
 expect(Object.values(view.simpleTools??{})[0].detailSeq).toBe(5);
 expect(view.timeline.find(item=>item.t==='llm')?.call.detailSeq).toBe(6);
});

it('整理开始和完成归并，子 Agent 的整理只在自己的过程展示',()=>{
 let v=reduce(initialView(),{type:'context_compaction',checkpoint_id:'ctx-a',agent_id:'lead',status:'started'},10);
 v=reduce(v,{type:'context_compaction',checkpoint_id:'ctx-a',agent_id:'lead',status:'completed',after_tokens_estimate:100},20);
 v=reduce(v,{type:'context_compaction',checkpoint_id:'ctx-b',agent_id:'sub1',status:'completed'},30);
 expect(processRows(v)).toHaveLength(1);
 expect(processRows(v)[0]).toMatchObject({kind:'compaction',at:10,data:{status:'completed',after_tokens_estimate:100}});
 expect(processRows(v,'sub1')).toHaveLength(1);
});

it('摘要只有 usage 记录时仍可定位到真实请求详情',()=>{
 const v=reduce(initialView(),{type:'llm_usage',request_id:'compact-request',call_id:'lead-8',usage_role:'compact',input_tokens:100},10);
 expect(modelRecordKey(v,'lead-8')).toBe('request:compact-request');
 expect(modelRecordKey(v,'missing')).toBeUndefined();
});
