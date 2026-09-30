import type {LeadMessage,ResearchUserMessage,RunView,ToolCall} from './types';

export type ProcessRow = {key:string;at:number;kind:'compaction';data:Record<string,unknown>} | {key:string;at:number;kind:'tool';call:ToolCall} | {key:string;at:number;kind:'message';message:LeadMessage};
export function owner(message:LeadMessage, view:RunView):string|undefined {
 if(message.role==='lead') return undefined;
 return view.simple?.delegations?.find(d=>message.msgId.includes(`:${d.id}:`) || message.msgId.includes(`:${d.parentCallId}:${d.id}:`))?.id ?? 'unassigned';
}
export function processRows(view:RunView,agent?:string):ProcessRow[] {
 const messages=new Map((view.leadMessages??[]).map(m=>[m.msgId,m]));
 return view.timeline.flatMap((item):ProcessRow[]=>{
  if(item.t==='note' && item.card.kind==='context_compaction') {
   const data=item.card as unknown as Record<string,unknown>;
   if ((data.agent_id==='lead'?undefined:data.agent_id)!==agent) return [];
   return [{kind:'compaction',key:`context:${data.checkpoint_id}:${data.status}`,at:item.atMs,data}];
  }
  if(item.t==='tool') { const call=view.simpleTools?.[item.key];
   if(!call || call.delegationId!==agent) return [];
   return [{kind:'tool',key:item.key,at:item.atMs,call}];
  }
  if(item.t==='msg') {const message=messages.get(item.msgId);
   if(!message || owner(message,view)!==agent) return [];
   return [{kind:'message',key:item.msgId,at:item.atMs,message}];
  }
  return [];
 });
}
export type ProcessSegment={kind:'process';key:string;rows:ProcessRow[]}|{kind:'user';key:string;message:ResearchUserMessage};
export function processSegments(view:RunView):ProcessSegment[] {
 const rows: Array<ProcessRow|{kind:'user';key:string;at:number;message:ResearchUserMessage}>=[...processRows(view),...(view.userMessages??[]).map(message=>({kind:'user' as const,key:message.message_id,at:message.at*1000,message}))];
 // A streaming completion never changes the timestamp of its first appearance.
 rows.sort((a,b)=>a.at-b.at);
 const out:ProcessSegment[]=[];
 for(const row of rows) {
  if(row.kind==='user') {out.push({kind:'user',key:row.key,message:row.message});continue;}
  const last=out.at(-1);
  if(last?.kind==='process') last.rows.push(row); else out.push({kind:'process',key:row.key,rows:[row]});
 }
 return out;
}
export function usageSummary(view:RunView) {
 const rows=view.usageAttempts??[];
 const known=rows.filter(r=>['reported','derived_miss'].includes(r.cache_status??''));
 const sum=(field:string,list=rows):number|null=>{const values=list.map(r=>r[field]).filter((v):v is number=>typeof v==='number');return values.length?values.reduce((a,b)=>a+b,0):null};
 const denominator=sum('input_tokens',known),hit=sum('cache_read_tokens',known);
 return {requests:rows.length,input:sum('input_tokens'),output:sum('output_tokens'),hit,miss:sum('cache_miss_tokens',known),
  cacheKnown:known.length,unknown:rows.length-known.length,rate:denominator&&hit!==null?hit/denominator:null,
  inputKnown:rows.filter(r=>typeof r.input_tokens==='number').length,cost:sum('balance_charge_usd'),costKnown:rows.filter(r=>typeof r.balance_charge_usd==='number').length};
}
export function cardCall(view:RunView,id:string):string|undefined {
 return Object.entries(view.simpleTools??{}).find(([,c])=>Array.isArray(c.display?.card_ids)&&c.display!.card_ids.includes(id))?.[0];
}
export const statusName=(s?:string|null)=>({complete:'已完成',completed:'已完成',done:'已完成',partial:'部分完成',failed:'失败',cancelled:'已取消',running:'进行中',queued:'等待中',cancel_requested:'取消中',empty:'无产出',todo:'待做',doing:'进行中',dropped:'已放弃',ok:'成功',rejected:'被拒收',timeout:'超时',preparing:'准备中'}[s??'']??s??'未记录');
export const numberText=(n:unknown)=>typeof n==='number'?n.toLocaleString('zh-CN',{maximumFractionDigits:2}):'未知';

export function modelRecordKey(view:RunView,callId:string):string|undefined {
 if(view.timeline.some(item=>item.t==='llm' && item.call.callId===callId))return `model:${callId}`;
 const request=view.usageAttempts?.find(item=>item.call_id===callId);
 return request?`request:${request.request_id}`:undefined;
}
