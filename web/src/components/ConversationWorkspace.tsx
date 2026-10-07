import {createContext,useContext,useEffect,useLayoutEffect,useRef,useState,type ReactNode} from 'react';
import ReactMarkdown from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';
import {RunSkills} from './SkillsPanel';
import {getJSON,postJSON} from '../api';
import type {DelegationView,RunView,SimpleCard,SimpleSource,EvidenceQuote,ToolCall,UsageAttempt,PlanItemView} from '../types';
import {cardCall,modelRecordKey,numberText,processRows,processSegments,statusName,usageSummary,type ProcessRow} from '../conversationModel';
import {answerWithCitations,buildRunIndex,toolFacts,toolStatus,activityState} from './conversationHelpers';
import {toolCallLabel,STOP_LABEL,COVERAGE_LABEL} from '../displayLabels';
import {ResearchComposer} from './ResearchComposer';
import {DocumentLink} from './DocumentLink';
import {McpSourceDetails} from './McpSourceDetails';
import {ComputationSourceDetails} from './ComputationSourceDetails';
import {EvidenceJudgment} from './EvidenceJudgment';
import {cardQuotes} from '../evidenceQuotes';
import {ArtifactFiles} from './ArtifactFiles';
import {ExecutionDetails} from './ExecutionDetails';
import {runOutcome} from './runStatusView';
import {durationText} from '../format';
import {ChevronDown,ChevronRight,ArrowLeft,ArrowUpRight,BookOpen,Brain,Check,GitBranch,Search,Terminal,X,Info,Square,TriangleAlert,Circle,Minus} from 'lucide-react';

type Tab='conversation'|'evidence'|'records';
interface Turn {run_id:string;turn_index?:number;query:string;status:string;conversation_id?:string;finished_at?:number;config_summary?:{source_mode?:string};}
interface Navigation {tab:Tab;card:string|null;agent:string|null;source:boolean;quoteIndex?:number;call:string|null;recordTab:string;scope:string;back?:Omit<Navigation,'back'>;}
interface SavedUI {nav:Navigation;expanded:Record<string,boolean>;scroll:Record<string,number>;}
const saved=new Map<string,SavedUI>();
const RunIdentity=createContext<string|null>(null);
interface EventDetail {input?:string;output?:string;tools?:unknown[];arguments_raw?:string;result_full?:string;io_complete?:boolean;request_messages?:Array<{role:string;[key:string]:unknown}>;request_tools?:unknown[];}
function useEventDetail(seq?:number) {
 const runId=useContext(RunIdentity),key=runId&&seq!==undefined?`${runId}/${seq}`:null;
 const [state,setState]=useState<{key:string;data?:EventDetail;error?:string}|null>(null);
 useEffect(()=>{if(!key)return;let alive=true;
  getJSON<EventDetail>(`/api/runs/${runId}/events/${seq}`).then(data=>{if(alive)setState({key,data})}).catch(e=>{if(alive)setState({key,error:e instanceof Error?e.message:'调用详情读取失败'})});
  return()=>{alive=false};
 },[key,runId,seq]);
 return {data:state?.key===key?state?.data:undefined,error:state?.key===key?state?.error:undefined,loading:!!key&&(state?.key!==key)};
}
function ToolIO({call}:{call:ToolCall}) {
 const detail=useEventDetail(call.detailSeq);
 if(detail.loading)return <p className='cw-muted'>正在读取调用详情…</p>;
 if(detail.error)return <p className='cw-warning'>{detail.error}</p>;
 const complete=detail.data?.io_complete??call.ioComplete;
 return <><strong className='cw-io-label'>输入{complete?'':'（摘要）'}</strong><Payload value={detail.data?.arguments_raw??call.argsFull??call.args}/><strong className='cw-io-label'>输出{complete?'':'（摘要）'}</strong><Payload value={detail.data?.result_full??call.resultFull??call.result}/></>;
}
function failureSummary(call:ToolCall):string {
 const raw=call.resultFull??call.result;
 try {
  const result=JSON.parse(raw);
  const detail=result.stderr||result.reason||result.error?.message||result.error||result.error_code;
  if(typeof detail==='string')return detail.trim().split('\n').filter(Boolean).at(-1)??toolStatus(call).label;
 } catch { /* 文本回执直接展示；完整回执在展开后查看。 */ }
 return raw||toolStatus(call).label;
}

export function FailedToolCalls({view,expanded,toggle,onCall}:{view:RunView;expanded:Record<string,boolean>;toggle:(id:string,open:boolean)=>void;onCall?:(key:string)=>void}) {
 const failed=Object.entries(view.simpleTools??{}).filter(([,call])=>call.phase==='done'&&!call.ok)
  .sort(([,a],[,b])=>a.atMs-b.atMs);
 if(!failed.length)return null;
 return <section className='cw-tool-failures' aria-label='未成功的工具调用'>
  <div className='cw-failure-heading'><TriangleAlert size={18}/><strong>{failed.length} 次工具调用未成功</strong></div>
  <p className='cw-muted'>每次调用都保留在下面，点击可查看输入和错误详情。</p>
  <div className='cw-failure-list'>{failed.map(([key,call])=>{
   const status=toolStatus(call),reason=failureSummary(call).replace(/\s+/g,' ').slice(0,240);
   const identity=`${call.delegationId?`子 Agent ${call.delegationId}`:'主 Agent'}${call.turn!==undefined?` · 第 ${call.turn} 轮`:''}${call.callNo?` · 工具 #${call.callNo}`:''}`;
   return <Fold key={key} id={`failure:${key}`} expanded={expanded} toggle={toggle} className='cw-failure-entry'
    label={<><span className='cw-failure-label'><span><strong>{toolCallLabel(call)}</strong><small>{identity}</small></span><span className='cw-failure-reason'>{reason}</span></span><span className={`cw-status ${status.tone}`}>{status.label}</span></>}>
     <p className='cw-muted'>{identity} · <code>{call.tool}</code></p>
     {call.callId&&<p className='cw-muted'>调用 ID：<code>{call.callId}</code></p>}
     <ToolIO call={call}/>
     {call.tool==='bash'&&call.display&&<ExecutionDetails display={call.display}/>}
     {onCall&&<div className='cw-detail-actions'><button className='cw-link' onClick={()=>onCall(`tool:${key}`)}>在运行记录中查看 <ArrowUpRight size={13}/></button></div>}
    </Fold>;
  })}</div>
 </section>;
}
const blank=():SavedUI=>({nav:{tab:'conversation',card:null,agent:null,source:false,call:null,recordTab:'概览',scope:'all'},expanded:{},scroll:{}});
const fmt=(v:unknown)=>typeof v==='string'?v:JSON.stringify(v,null,2);
function Payload({value,missing='没有保存这项记录'}:{value:unknown;missing?:string}) {return value===undefined||value===null?<p className='cw-empty'>{missing}</p>:<pre className='cw-payload'>{fmt(value)}</pre>}
/** c12 → 12：界面上只显示编号数字。 */
const cardNo=(id:string)=>id.replace(/^c/,'');
function hostOf(url?:string|null) {try{return url?new URL(url).hostname.replace(/^www\./,''):''}catch{return ''}}

export function UsageOverview({view}:{view:RunView}) {
 const s=usageSummary(view);
 const models=view.timeline.filter(i=>i.t==='llm').length||s.requests;
 const tools=Object.keys(view.simpleTools??{}).length;
 return <div className='cw-usage'><div className='cw-metrics'>
  <div><small>模型调用</small><strong>{models}<em>次</em></strong></div>
  <div><small>工具调用</small><strong>{tools}<em>次</em></strong></div>
  <div><small>输入 token</small><strong>{numberText(s.input)}{s.rate!==null&&<em className='is-ok'>缓存命中 {(s.rate*100).toFixed(0)}%</em>}</strong></div>
  <div><small>输出 token</small><strong>{numberText(s.output)}</strong></div>
 </div><p>费用 {s.cost===null?'未知':`$${s.cost.toFixed(4)}`}（{s.costKnown}/{s.requests} 次请求有记录）· 缓存数据 {s.cacheKnown}/{s.requests} 次有记录 · 累计用量不等于当前上下文大小</p></div>;
}

function Fold({id,label,children,expanded,toggle,defaultOpen=false,className=''}:{id:string;label:ReactNode;children:ReactNode;expanded:Record<string,boolean>;toggle:(id:string,open:boolean)=>void;defaultOpen?:boolean;className?:string}) {
 const open=expanded[id]??defaultOpen;
 return <section className={`cw-fold ${open?'is-open':''} ${className}`}><button className='cw-summary' aria-expanded={open} onClick={()=>toggle(id,!open)}>{open?<ChevronDown size={14}/>:<ChevronRight size={14}/>} {label}</button>{open&&<div className='cw-fold-body'>{children}</div>}</section>;
}

function ContextHistory({checkpoint}:{checkpoint:string}) {
 const runId=useContext(RunIdentity);
 const [page,setPage]=useState<{text:string;next_cursor:number|null}|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 async function load(cursor=0) {
  if(!runId)return;setBusy(true);setError('');
  try{setPage(await getJSON(`/api/runs/${runId}/context/${encodeURIComponent(checkpoint)}?cursor=${cursor}`));}
  catch(e){setError(e instanceof Error?e.message:'读取失败');}finally{setBusy(false);}
 }
 return <div><button className='cw-link' disabled={busy} onClick={()=>load()}>查看整理前的原始过程</button>
  {error&&<p className='cw-warning'>{error}</p>}{page&&<><p className='cw-muted'>原始记录分页显示，每页最多 8,000 字。</p><pre className='cw-prose'>{page.text}</pre>
  {page.next_cursor!==null&&<button className='cw-link' disabled={busy} onClick={()=>load(page.next_cursor!)}>下一段</button>}</>}
 </div>;
}

export function ProcessList({rows,view,expanded,toggle,onCall,onAgent}:{rows:ProcessRow[];view:RunView;expanded:Record<string,boolean>;toggle:(id:string,open:boolean)=>void;onCall?:(id:string)=>void;onAgent:(id:string)=>void}) {
 const index=buildRunIndex(view);
 return <div className='cw-process-rows'>{rows.map(row=>{
  if(row.kind==='compaction') {const d=row.data; const recordKey=modelRecordKey(view,String(d.call_id));
   return <Fold key={row.key} id={`row:${row.key}`} expanded={expanded} toggle={toggle}
    label={<><span className='cw-row-icon'><Info size={15}/></span><span className='cw-row-type'>整理研究上下文</span><span className='cw-row-excerpt'>{d.status==='started'?'正在整理':d.status==='retrying'?'整理请求暂时失败，正在重试':d.status==='completed'?'整理完成':'没有整理成功，保留原来的上下文'}</span></>}>
    <p>估算输入 {numberText(d.before_tokens_estimate)} → {numberText(d.after_tokens_estimate)} token · 保留最近 {numberText(d.retained_rounds)} 轮</p>
    <p>整理本身用了输入 {numberText(d.input_tokens)} / 输出 {numberText(d.output_tokens)} token · {numberText(d.elapsed_ms)} 毫秒</p>
    <p className='cw-muted'>回读编号：<code>{String(d.checkpoint_id)}</code></p>
    {recordKey&&onCall&&<button className='cw-link' onClick={()=>onCall(recordKey)}>查看整理时的模型调用</button>}
    {d.status==='completed'&&<ContextHistory checkpoint={String(d.checkpoint_id)}/>}
    {d.error!=null&&<p className='cw-warning'>{String(d.error)}</p>}
    {d.summary!=null&&<pre className='cw-prose'>{JSON.stringify(d.summary,null,2)}</pre>}
   </Fold>;
  }
  if(row.kind==='message') {const m=row.message;
   if(m.kind!=='reasoning')return <article key={row.key} className='cw-markdown cw-intermediate'>
    <ReactMarkdown remarkPlugins={[remarkGfm,remarkBreaks]}>{m.text}</ReactMarkdown>
    {(m.gap||m.interrupted)&&<p className='cw-warning'>这段记录不完整，保留已收到内容。</p>}
   </article>;
   return <Fold key={row.key} id={`row:${row.key}`} expanded={expanded} toggle={toggle}
   label={<><span className='cw-row-icon'><Brain size={15}/></span><span className='cw-row-type'>思考</span><span className='cw-row-excerpt'>{m.text}</span>{(m.interrupted||m.gap)&&<small className='cw-status warn'>不完整</small>}</>}>
    <div className='cw-prose'>{m.text}</div>{(m.gap||m.interrupted)&&<p className='cw-warning'>这段记录不完整，保留已收到内容。</p>}
    {m.callId&&onCall&&<button className='cw-link' onClick={()=>onCall(`model:${m.callId}`)}>查看模型调用 <ArrowUpRight size={13}/></button>}
   </Fold>}
  const call=row.call, facts=toolFacts(call,index),status=toolStatus(call);
  const tasks=view.simple?.delegations?.filter(d=>d.parentCallId===(call.display?.dispatch_id??call.callId))??[];
  return <Fold key={row.key} id={`row:${row.key}`} expanded={expanded} toggle={toggle}
   label={<><span className={`cw-row-icon tone-${status.tone}`}>{status.tone==='run'?<span className='lamp lamp-current'/>:call.tool.includes('search')?<Search size={15}/>:call.tool==='delegate_research'?<GitBranch size={15}/>:call.tool==='read_source'?<BookOpen size={15}/>:<Terminal size={15}/>}</span><span className='cw-row-type'>{toolCallLabel(call)}</span><span className='cw-row-excerpt'>{facts.main==='（参数已截断）'?'':facts.main===toolCallLabel(call)?facts.counters.join(' · '):facts.main}</span>{status.tone!=='ok'&&status.tone!=='run'&&<small className={`cw-status ${status.tone}`}>{status.label}</small>}</>}>
   {facts.counters.length>0&&<p className='cw-muted'>{facts.counters.join(' · ')}</p>}
   {facts.lines.length>0&&<ul className='cw-fact-lines'>{facts.lines.map((line,i)=><li key={i}>{line.href?<a href={line.href} target='_blank' rel='noreferrer'>{line.text}</a>:line.text}{line.note&&<small>{line.note}</small>}</li>)}</ul>}
   {facts.warn&&<p className='cw-warning'>{facts.warn}</p>}
   {tasks.map(d=><button className='cw-agent-link' key={d.id} onClick={()=>onAgent(d.id)}><GitBranch size={14}/><span>{d.goal}</span><small>{statusName(d.status)}</small></button>)}
   {call.tool==='bash'&&call.display&&<ExecutionDetails display={call.display}/>}
   <details className='disclosure'><summary>原始输入输出</summary><ToolIO call={call}/></details>
   {onCall&&<div className='cw-detail-actions'><code>{call.tool}</code><button className='cw-link' onClick={()=>onCall(`tool:${row.key}`)}>在运行记录中查看 <ArrowUpRight size={13}/></button></div>}
  </Fold>;
 })}</div>;
}

function ImageDetails({runId,quote}:{runId:string;quote:EvidenceQuote}) {
 const image=quote.observation;
 if(!image)return null;
 return <div><p className='cw-muted'>视觉观察，未经机器核验{image.page?` · PDF第${image.page}页`:''}{image.region?` · 裁剪 ${image.region.join(', ')}`:''}{image.model?` · ${image.model}`:''}</p>
  <img src={`/api/runs/${runId}/images/${image.image_sha256}`} alt='证据所依据的页图或裁剪' style={{maxWidth:'100%'}}/>
  <a className='cw-link' href={`/api/runs/${runId}/sources/${image.original_source_ref}/original`} target='_blank' rel='noreferrer'>查看原件</a>
 </div>;
}

function SourceText({runId,card,wide=false}:{runId:string;card:EvidenceQuote;wide?:boolean}) {
 const contentName=card.source_kind==='computation'?'计算输出':card.source_kind==='observation'?'视觉模型输出':card.source_kind==='browser_snapshot'?'浏览器页面快照':'原文';
 const [text,setText]=useState<string|null>(null),[error,setError]=useState('');
 const mark=useRef<HTMLElement>(null);
 useEffect(()=>{if(card.location?.startsWith('image='))return;let alive=true;setText(null);setError('');const abort=new AbortController();
  fetch(`/api/runs/${runId}/sources/${card.source_ref}`,{signal:abort.signal}).then(async res=>{if(!res.ok)throw new Error(`保存的${contentName}不可用，或校验没有通过`);return res.text()}).then(t=>{if(alive)setText(t)}).catch(e=>{if(alive)setError(e.message)});
  return ()=>{alive=false;abort.abort()};
 },[runId,card.source_ref,card.location,contentName]);
 useEffect(()=>{if(wide&&text!==null)mark.current?.scrollIntoView({block:'center'})},[text,wide]);
 if(card.location?.startsWith('image=')&&card.observation)return <ImageDetails runId={runId} quote={card}/>;
 if(error)return <p role='alert' className='cw-warning'>{error}</p>;
 if(text===null)return <p className='cw-empty'>正在读取保存的{contentName}…</p>;
 const chars=Array.from(text),match=/^chars=(\d+)-(\d+)$/.exec(card.location??'');
 const a=Number(match?.[1]),b=Number(match?.[2]);
 const valid=!!match&&a>=0&&b>a&&b<=chars.length&&chars.slice(a,b).join('')===card.quote;
 if(!valid)return <p className='cw-warning'>引文和保存的{contentName}对不上，没法标出位置。<a href={`/api/runs/${runId}/sources/${card.source_ref}`} target='_blank' rel='noreferrer'>查看保存的{contentName}</a></p>;
 const start=wide?0:Math.max(0,a-400),end=wide?chars.length:Math.min(chars.length,b+400);
 return <><p className='cw-found'><Check size={14}/>在保存的{contentName}中找到了这段话（找到原话不等于事实已核实）</p><div className='cw-source-text'>{start>0&&'…'}{chars.slice(start,a).join('')}<mark ref={mark}>{chars.slice(a,b).join('')}</mark>{chars.slice(b,end).join('')}{end<chars.length&&'…'}</div></>;
}

function QuoteDetail({quote,source,runId,index,total,onSource}:{quote:EvidenceQuote;source?:SimpleSource;runId:string;index:number;total:number;onSource:()=>void}) {
 const [expanded,setExpanded]=useState(false);
 const url=quote.deep_link||quote.source_url||source?.url;
 const contentName=source?.computation||quote.source_kind==='computation'?'计算输出':quote.source_kind==='observation'?'视觉观察':quote.source_kind==='browser_snapshot'?'浏览器页面快照':'原文';
 return <section className='cw-quote-section' aria-label={`依据 ${index+1}`}>
  <div className='cw-source-row'>
   <span className='cw-source-badge'>{(hostOf(source?.url??quote.source_url)||source?.document?.file_name||'资料').slice(0,2).toUpperCase()}</span>
   <div><p className='cw-source-title'>{source?.title??quote.source_title??'没有记录来源标题'}</p>
    <small className='cw-muted'>{source?.document?`我的资料 · ${source.document.file_name}`:hostOf(source?.url??quote.source_url)||'来源'}{quote.published_at??source?.published_at?` · 发布于 ${quote.published_at??source?.published_at}`:''}</small></div>
  </div>
  <label className='cw-label'>{total>1?`依据 ${index+1} · `:''}{contentName}</label><blockquote>{quote.quote}</blockquote>
  {quote.submitted_quote&&quote.submitted_quote!==quote.quote&&<details className='cw-judge-context'><summary>查看模型提交的摘句</summary><p className='cw-muted'>匹配时允许空白差异，上面保留的是原文里的原话。</p><pre>{quote.submitted_quote}</pre></details>}
  {!!quote.context?.length&&<details className='cw-judge-context'><summary>判断时参考的上下文 · {quote.context.length} 处</summary><p className='cw-muted'>从同一份{contentName}里提取，按位置排列；各段之间可能有省略。</p>{quote.context.map((c,i)=><div key={i}><small className='cw-muted'>{c.kind==='structure'?'章节 / 表头':`附近${contentName}`} · {c.location}</small><pre>{c.text}</pre></div>)}</details>}
  {source?.document&&<p className='cw-muted'>使用的是固定版本 <code title={source.document.version_id}>{source.document.version_id.slice(0,12)}</code>，之后更新资料库不会改动它。</p>}
  {source?.mcp&&<McpSourceDetails source={source} runId={runId}/>}
  {source?.computation&&<ComputationSourceDetails source={source}/>}
  {quote.observation&&<ImageDetails runId={runId} quote={quote}/>}
  {source?.truncated&&<p className='cw-warning'>来源太长，只保存了前面一部分。</p>}
  <div className='cw-detail-actions'>
   <button onClick={()=>setExpanded(!expanded)}>{expanded?'收起上下文':'看前后文'}</button>
   <button onClick={onSource}><BookOpen size={14}/>{quote.observation?quote.location?.startsWith('image=')?'回看图片':'读视觉观察记录':source?.computation?'读保存的计算输出':'读保存的全文'}</button>
   {source?.document?<DocumentLink runId={runId} source={source} location={quote.location}/>:!source?.mcp&&!quote.observation&&url&&<a className='cw-link' href={url} target='_blank' rel='noreferrer'>打开原网页 <ArrowUpRight size={13}/></a>}
   {source?.download&&<DocumentLink runId={runId} source={source}/>}
  </div>
  {expanded&&<SourceText runId={runId} card={quote}/>}
  <p className='cw-muted cw-fetched'>获取于 {source?.fetched_at?new Date(source.fetched_at*1000).toLocaleString():'未记录'}{source?.revalidated_at?` · 本轮重新获取 ${new Date(source.revalidated_at*1000).toLocaleString()}`:''}</p>
 </section>;
}

export function EvidenceDetail({card,sources,runId,onSource,onCall,onOrigin,view}:{card:SimpleCard;sources:SimpleSource[];runId:string;onSource:(index:number)=>void;onCall?:(key:string)=>void;onOrigin:(runId:string,cardId:string)=>void;view:RunView}) {
 const call=cardCall(view,card.card_id),quotes=cardQuotes(card);
 return <div className='cw-evidence-detail'><small className='cw-eyebrow'>证据 {cardNo(card.card_id)}{quotes.length>1?` · ${quotes.length} 段依据`:''}</small><h2>{card.claim}</h2>
  {card.origin&&<p className='cw-origin'>沿用第 {card.origin.turn_index} 轮的证据 {cardNo(card.origin.card_id)} <button className='cw-link' onClick={()=>onOrigin(card.origin!.run_id,card.origin!.card_id)}>查看原卡</button></p>}
  {card.judgment&&<EvidenceJudgment judgment={card.judgment} detail/>}
  {quotes.map((quote,i)=><QuoteDetail key={i} quote={quote} index={i} total={quotes.length} source={sources.find(s=>s.ref===quote.source_ref)} runId={runId} onSource={()=>onSource(i)}/>)}
  {onCall&&call&&<div className='cw-evidence-links'><button className='cw-link' onClick={()=>onCall(`tool:${call}`)}>保存这张证据的调用 <ArrowUpRight size={13}/></button></div>}
 </div>;
}

function AgentDetail({agent,view,expanded,toggle,onCall,onAgent}:{agent:DelegationView;view:RunView;expanded:Record<string,boolean>;toggle:(id:string,open:boolean)=>void;onCall?:(key:string)=>void;onAgent:(id:string)=>void}) {
 const rows=processRows(view,agent.id);
 return <><small className='cw-eyebrow'>{agent.id} · {statusName(agent.status)}</small><h2>子任务研究过程</h2>
 <details className='cw-agent-goal'><summary>{agent.goal.length>100?agent.goal.slice(0,100)+'…':agent.goal}</summary><p className='cw-prose'>{agent.goal}</p></details>
 <p className='cw-muted'>{rows.filter(r=>r.kind==='tool').length} 次工具调用 · 保存 {agent.cardCount} 条证据 · {agent.gaps} 处没查清</p>
 {agent.result?.error&&<p className='cw-warning'>{agent.result.error.message}。已保存的材料都保留了。</p>}
 <ProcessList rows={rows} view={view} expanded={expanded} toggle={toggle} onCall={onCall} onAgent={onAgent}/>
 {!rows.length&&<p className='cw-empty'>还没有过程记录。</p>}
 {(agent.messages??[]).length>0&&<details className='disclosure'><summary>协作消息</summary>{agent.messages!.map(m=><p key={m.id}><small>{m.sender} → {m.target}</small><br/>{m.text}</p>)}</details>}
 {agent.result&&<div className='cw-agent-result'><h3>子任务交回的结果</h3><div className='cw-prose'>{agent.result.findings||'没有提交总结'}</div>{agent.result.gaps.map((g,i)=><p className='cw-warning' key={i}>{g}</p>)}</div>}</>;
}

function RunRecords({view,nav,navigate}:{view:RunView;nav:Navigation;navigate:(patch:Partial<Navigation>)=>void}) {
 const tools=Object.entries(view.simpleTools??{});
 const models=view.timeline.filter(i=>i.t==='llm').map(i=>i.call);
 const attempts=view.usageAttempts??[];
 const calls=[...models.map((m,i)=>({key:`model:${m.callId??'legacy-'+i}`,name:m.model??m.step,scope:m.delegationId||'lead',type:'model' as const,model:m})),
  ...attempts.filter(a=>!models.some(m=>m.callId===a.call_id)).map(a=>({key:`request:${a.request_id}`,name:a.model??'模型请求',scope:a.delegation_id||'lead',type:'request' as const,attempt:a})),
  ...tools.map(([key,tool])=>({key:`tool:${key}`,name:toolCallLabel(tool),scope:tool.delegationId||'lead',type:'tool' as const,tool}))];
 const selected=calls.find(c=>c.key===nav.call);
 const scope=nav.scope;
 const requests:UsageAttempt[]=selected?.type==='request'?[selected.attempt]:selected?.type==='model'?attempts.filter(a=>a.call_id===selected.model.callId):[];
 const [attemptId,setAttemptId]=useState('');
 const attempt=requests.find(r=>r.request_id===attemptId)??requests.at(-1);
 const needsIO=nav.recordTab!=='概览';
 const callDetail=useEventDetail(needsIO?(selected?.type==='model'?selected.model.detailSeq:selected?.type==='tool'?selected.tool.detailSeq:undefined):undefined);
 const requestDetail=useEventDetail(needsIO?attempt?.detail_seq as number|undefined:undefined);
 const messages=requestDetail.data?.request_messages??attempt?.request_messages;
 const tabs=['概览','提示词','输入','输出','参数'];
 let body:ReactNode=<p className='cw-empty'>从左边选一条模型或工具调用查看详情。</p>;
 if(selected){
  if(nav.recordTab==='概览')body=<><dl className='cw-kv'><div><dt>归属</dt><dd>{selected.scope==='lead'?'主 Agent':selected.scope}</dd></div><div><dt>类型</dt><dd>{selected.type==='tool'?'工具':'模型'}</dd></div><div><dt>调用编号</dt><dd><code>{selected.key}</code></dd></div></dl>
   {selected.type==='tool'?<p className='cw-muted'>结果：{toolStatus(selected.tool).label}</p>:<><dl className='cw-kv'>{[
    ['HTTP 请求',attempt?.request_id??'未记录'],['第几次尝试',attempt?.attempt??'未知'],['结果',attempt?.outcome??'未知'],['输入 token（含缓存）',numberText(attempt?.input_tokens)],['输出 token',numberText(attempt?.output_tokens)],['缓存命中 / 未命中',`${numberText(attempt?.cache_read_tokens)} / ${numberText(attempt?.cache_miss_tokens)}`],['首个输出耗时（毫秒）',numberText(attempt?.first_token_ms)],['首段正文耗时（毫秒）',numberText(attempt?.first_content_ms)],['总耗时（毫秒）',numberText(attempt?.duration_ms)],['缓存数据来源',attempt?.cache_status??'未知'],['错误',attempt?.error_type??'无']
   ].map(([k,v])=><div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl><details className='disclosure'><summary>原始 usage</summary><Payload value={attempt?.raw_usage}/></details></>}
  </>;
  if(nav.recordTab==='提示词')body=selected.type==='tool'?<p className='cw-muted'>这是工具调用，没有提示词。</p>:<Payload value={messages?.filter(m=>m.role==='system'||m.role==='developer')} missing='没有保存结构化提示词；可以在「输入」里看已保存的摘要。'/>;
  if(nav.recordTab==='输入')body=selected.type==='tool'?<Payload value={callDetail.data?.arguments_raw??selected.tool.argsFull??selected.tool.args}/>:<><Payload value={messages??callDetail.data?.input??(selected.type==='model'?selected.model.input:undefined)}/><details className='disclosure'><summary>工具定义</summary><Payload value={requestDetail.data?.request_tools??attempt?.request_tools??callDetail.data?.tools??(selected.type==='model'?selected.model.tools:undefined)}/></details></>;
  if(nav.recordTab==='输出')body=selected.type==='model'&&requests.length>1&&attempt!==requests.at(-1)
   ?<p className='cw-empty'>这次 HTTP 尝试没有单独保存响应；最终输出属于整次模型调用，切到最后一次尝试可以查看。</p>
   :<>{selected.type==='model'&&<p className='cw-muted'>这是整次模型调用的最终输出，不代表每次 HTTP 尝试都产生了它。</p>}<Payload value={selected.type==='model'?callDetail.data?.output??selected.model.output:selected.type==='tool'?callDetail.data?.result_full??selected.tool.resultFull??selected.tool.result:undefined} missing='这次请求没有保存输出，可能失败或中断了。'/></>;
  if(nav.recordTab==='参数')body=selected.type==='tool'?<Payload value={callDetail.data?.arguments_raw??selected.tool.argsFull??selected.tool.args}/>:<><p className='cw-muted'>只列出实际发送了的参数；没列出的参数没有发送，服务商的默认值未知。</p><Payload value={attempt?.request_parameters}/></>;
  if(needsIO&&(callDetail.loading||requestDetail.loading))body=<p className='cw-muted'>正在读取调用详情…</p>;
  if(needsIO&&(callDetail.error||requestDetail.error))body=<p className='cw-warning'>{callDetail.error||requestDetail.error}</p>;
 }
 return <section className='cw-records'>
  <p className='cw-muted cw-records-hint'>这一页用于排查问题，日常使用不需要看。</p>
  <UsageOverview view={view}/>
  <div className='cw-record-layout'><div className='cw-call-list'>
   <div className='cw-call-filter'><label>显示 <select aria-label='调用归属' value={scope} onChange={e=>navigate({scope:e.target.value})}><option value='all'>全部</option><option value='lead'>主 Agent</option>{(view.simple?.delegations??[]).map(d=><option key={d.id} value={d.id}>{d.id}</option>)}</select></label>
    <small className='cw-muted'>{models.length} 次模型 · {tools.length} 次工具{attempts.length>models.length?` · ${attempts.length} 次 HTTP 请求`:''}</small></div>
   <div className='cw-call-items'>
   {calls.filter(c=>scope==='all'||c.scope===scope).map(c=><button key={c.key} aria-pressed={nav.call===c.key} onClick={()=>{setAttemptId('');navigate({call:c.key})}}>
    <span className={`badge ${c.type==='tool'?'':'badge-model'}`}>{c.type==='tool'?'工具':'模型'}</span>
    <span className='cw-call-name'>{c.name}</span>
    <small>{c.scope==='lead'?'主 Agent':c.scope}</small></button>)}
   {!calls.length&&<p className='cw-empty'>这一轮没有调用记录。</p>}</div></div>
  <div className='cw-call-detail'>
   {selected&&<><h2>{selected.name}</h2><div className='cw-subtabs'>{tabs.map(tab=><button key={tab} aria-pressed={nav.recordTab===tab} onClick={()=>navigate({recordTab:tab})}>{tab}</button>)}</div>
   {requests.length>1&&<label className='cw-call-filter'>HTTP 尝试 <select value={attempt?.request_id} onChange={e=>setAttemptId(e.target.value)}>{requests.map(r=><option key={r.request_id} value={r.request_id}>第 {r.attempt} 次 · {r.outcome}</option>)}</select></label>}
   {(selected.type==='tool'?needsIO&&!callDetail.loading&&!(callDetail.data?.io_complete??selected.tool.ioComplete):!attempt?.io_complete)&&<p className='cw-muted'>没有保存完整输入输出，下面可能是截断后的摘要。</p>}</>}{body}</div></div></section>;
}

const PLAN_ICON:Record<PlanItemView['status'],ReactNode>={done:<Check size={15}/>,doing:<span className='plan-dot'/>,todo:<Circle size={11}/>,dropped:<Minus size={14}/>};

function PlanList({view}:{view:RunView}) {
 const plan=view.simple?.researchPlan;
 return <>
  <ul className='plan-items'>{plan?.items.map(i=><li className={`plan-item is-${i.status}`} key={i.id}><span className='plan-icon'>{PLAN_ICON[i.status]}</span><span>{i.text}</span>{i.status==='dropped'&&<small>已放弃</small>}</li>)}</ul>
  {view.simple?.planSyncStatus==='missing'&&<p className='cw-warning'>交稿时没有同步最终计划，这里显示的是最后一次更新。</p>}
  {(view.simple?.planHistory?.length??0)>1&&<details className='disclosure'><summary>计划改动记录</summary>{[...(view.simple?.planHistory??[])].reverse().map((p,i)=><div className='cw-plan-change' key={i}><strong>{p.note||'更新计划'}</strong><ul>{p.items.filter(item=>!p.previous.some(old=>old.id===item.id&&old.text===item.text&&old.status===item.status)).map(item=><li key={item.id}>{item.text} · {statusName(item.status)}</li>)}{p.previous.filter(old=>!p.items.some(item=>item.id===old.id)).map(old=><li key={old.id}>已移除：{old.text}</li>)}</ul></div>)}</details>}
  <small className='cw-muted'>进度由 Agent 自己汇报。</small>
 </>;
}

export function ConversationWorkspace({view:original,runId,canSend=false,onSend,onOpenRun,onFollowupStarted,status,showRecords=true}:{view:RunView;runId:string|null;mode?:'clean'|'debug';canSend?:boolean;onSend?:(id:string,text:string)=>Promise<void>;onOpenRun?:(id:string,status?:string)=>void;onFollowupStarted?:(id:string)=>void;
 /** 顶栏右侧：研究状态与停止按钮。 */
 status?:ReactNode;
 /** 开发者选项“显示运行记录”。 */
 showRecords?:boolean}) {
 const key=runId??'demo';
 const [ui,setUI]=useState<SavedUI>(()=>saved.get(key)??blank());
 const [turns,setTurns]=useState<Turn[]>([]),[latest,setLatest]=useState(runId),[meta,setMeta]=useState<Turn|null>(null);
 const [materials,setMaterials]=useState<{cards:SimpleCard[];sources:SimpleSource[]}|null>(null),[loadError,setLoadError]=useState(''),[materialsError,setMaterialsError]=useState('');
 const [continueError,setContinueError]=useState(''),[continuing,setContinuing]=useState(false);
 const nav=ui.nav;
 const scroll=useRef<HTMLDivElement>(null),current=useRef(ui);current.current=ui;
 const [search,setSearch]=useState('');
 const restoreFocus=useRef<HTMLElement|null>(null);
 const restoreCitation=useRef<{id:string;index:number}|null>(null);
 const mounted=useRef(true);
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[]);
 const view:RunView=materials?{...original,simple:{...(original.simple??{mode:'simple',phase:'research'}),cards:materials.cards,sources:materials.sources}}:original;
 const cards=view.simple?.cards??[],sources=view.simple?.sources??[],tasks=view.simple?.delegations??[],plan=view.simple?.researchPlan;
 const card=cards.find(c=>c.card_id===nav.card),agent=tasks.find(d=>d.id===nav.agent);
 const ended=!!view.endedAtMs,live=!ended;
 useEffect(()=>{saved.set(key,ui)},[ui,key]);
 useEffect(()=>{if(!runId)return;let alive=true;
  async function load(){try{const detail=await getJSON<{meta:Turn}>(`/api/runs/${runId}`);if(!alive)return;setMeta(detail.meta);
   let cursor:number|undefined=0,all:Turn[]=[];
   while(cursor!==undefined&&cursor!==null){const page:{turns:Turn[];latest_run_id:string;next_cursor?:number}=await getJSON<{turns:Turn[];latest_run_id:string;next_cursor?:number}>(`/api/conversations/${detail.meta.conversation_id??runId}?cursor=${cursor}`);all.push(...page.turns);cursor=page.next_cursor;if(alive)setLatest(page.latest_run_id)}
   if(alive){setTurns(all);setLoadError('')}
  }catch(e){if(alive)setLoadError(e instanceof Error?e.message:'会话信息暂时读不到')}}void load();return()=>{alive=false};
 },[runId,ended]);
 useEffect(()=>{if(!runId)return;let alive=true;getJSON<{cards:SimpleCard[];sources:SimpleSource[]}>(`/api/runs/${runId}/materials`).then(m=>{if(alive){setMaterials(m);setMaterialsError('')}}).catch(()=>{if(alive)setMaterialsError('证据材料暂时读不到，请刷新后重试。')});return()=>{alive=false};},[runId,view.simple?.cardCount,original.simple?.evidenceRevision,ended]);
 const selectedQuote=card?cardQuotes(card)[nav.quoteIndex??0]??cardQuotes(card)[0]:undefined;
 const scrollKey=`${nav.tab}:${nav.source?'source':'main'}`;
 const ready=!!original.startedAtMs||!!original.endedAtMs||!!original.processMissing;
 // 在换内容之前记位置；layout effect 的 cleanup 已在 DOM 更新后，短原文会把旧位置夹到 0。
 const rememberScroll=()=>{if(scroll.current&&ready){const state=current.current;state.scroll[scrollKey]=scroll.current.scrollTop;saved.set(key,state)}};
 useLayoutEffect(()=>{if(scroll.current&&ready)scroll.current.scrollTop=current.current.scroll[scrollKey]??0},[key,scrollKey,ready]);
 const navigate=(patch:Partial<Navigation>)=>{rememberScroll();setUI(s=>({...s,nav:{...s.nav,...patch}}))};
 const toggle=(id:string,open:boolean)=>setUI(s=>({...s,expanded:{...s.expanded,[id]:open}}));
 const openCall=(call:string)=>{rememberScroll();restoreFocus.current=document.activeElement as HTMLElement;setUI(s=>({...s,nav:{...s.nav,back:{...s.nav,back:undefined},tab:'records',source:false,call,recordTab:'概览',scope:'all'}}))};
 const onCall=showRecords?openCall:undefined;
 const openCard=(id:string|null)=>{restoreFocus.current=document.activeElement as HTMLElement;
  if(id){const matches=Array.from(document.querySelectorAll<HTMLElement>('.cw-answer [data-cite]')).filter(el=>el.dataset.cite===id);restoreCitation.current={id,index:Math.max(0,matches.indexOf(restoreFocus.current))}}
  navigate({card:id,agent:null,source:false,quoteIndex:0})};
 const closeDetail=()=>{const citation=card?restoreCitation.current:null;navigate({card:null,agent:null});requestAnimationFrame(()=>{
  const target=citation?Array.from(document.querySelectorAll<HTMLElement>('.cw-answer [data-cite]')).filter(el=>el.dataset.cite===citation.id)[citation.index]:restoreFocus.current;
  if(target?.isConnected)target.focus({preventScroll:true});
 })};
 const openAgent=(id:string)=>setUI(s=>({...s,nav:{...s.nav,agent:id,card:null,source:false}}));
 const openOrigin=(id:string,cardId:string)=>{rememberScroll();const old=saved.get(id)??blank();saved.set(id,{...old,nav:{...old.nav,tab:'evidence',card:cardId,source:false,agent:null,back:undefined}});onOpenRun?.(id)};
 const back=()=>{if(nav.back){const previous=nav.back;navigate({...previous,back:undefined})}else navigate({source:false,agent:null,card:null,tab:'conversation'})};
 const turnNo=meta?.turn_index??1,isLatest=latest===runId;
 const canFollowup=ended&&isLatest&&!!meta&&!view.replayOnly&&!['running','preparing'].includes(meta.status);
 async function send(id:string,text:string){if(canFollowup&&runId){const res=await postJSON<{run_id:string}>(`/api/research/${runId}/followup`,{request_id:id,text},AbortSignal.timeout(20000));if(mounted.current)onFollowupStarted?.(res.run_id)}else if(canSend&&onSend)await onSend(id,text);else throw new Error('这一轮暂时不接收消息')}
 async function continueFromClues(){setContinuing(true);setContinueError('');try{await send(crypto.randomUUID(),'请基于已经找到的线索继续研究，完成原来的问题。')}catch(e){setContinueError(e instanceof Error?e.message:'没能继续，请稍后重试')}finally{if(mounted.current)setContinuing(false)}}
 // 停止或未交付时，后端的 report.md 只是一份状态说明，不当作回答显示。
 const cancelled=view.simple?.stopReason==='cancelled';
 const noDeliverable=cancelled||view.simple?.delivery==='failed';
 const answer=!view.simple?.draftRejected?(view.simple?.draft||(noDeliverable?'':view.reportMd)):'';
 const pane=nav.tab==='conversation'&&(card||agent)&&!nav.source;
 const activity=activityState(view);
 const outcome=runOutcome(view);
 const title=meta?.query??view.simple?.query??view.simple?.effectiveQuery??(runId?'研究':'演示研究');
 const stopNote=view.simple?.stopReason&&!['finished','cancelled'].includes(view.simple.stopReason)?STOP_LABEL[view.simple.stopReason]?.text??view.simple.stopReason:'';
 const metaParts=[cards.length?`保存了 ${cards.length} 条证据`:'',view.startedAtMs!==undefined&&view.endedAtMs!==undefined?`用时 ${durationText(view.endedAtMs-view.startedAtMs)}`:'',stopNote].filter(Boolean);
 const planItems=plan?.items??[];
 const tabLabels:Record<Tab,string>={conversation:'对话',evidence:'证据',records:'运行记录'};
 const visibleTabs:Tab[]=showRecords?['conversation','evidence','records']:['conversation','evidence'];
 return <RunIdentity.Provider value={runId}><div className='cw-workspace'>
 <header className='cw-toolbar'>
  <div className='cw-title' title={title}>{title}</div>
  <nav aria-label='研究视图' className='tabs'>{visibleTabs.map(tab=><button key={tab} className='tab' aria-current={nav.tab===tab?'page':undefined} onClick={()=>navigate({tab,source:false,back:undefined})}>{tabLabels[tab]}{tab==='evidence'&&cards.length>0&&<span className='tab-count'>{cards.length}</span>}</button>)}</nav>
  {turns.length>1&&<label className='cw-round'><span className='sr-only'>当前查看轮次</span><select aria-label='当前查看轮次' value={runId??''} onChange={e=>{const t=turns.find(t=>t.run_id===e.target.value);if(t)onOpenRun?.(t.run_id,t.status)}}>{turns.map(t=><option key={t.run_id} value={t.run_id}>第 {t.turn_index??1} 轮 · {t.query.slice(0,25)}</option>)}</select></label>}
  <span className='spacer'/>
  {status}
 </header>
 {loadError&&<div className='cw-notice cw-notice-bar'>{loadError}</div>}{materialsError&&<div className='cw-notice cw-notice-bar'>{materialsError}</div>}
 <div className={`cw-body ${pane?'has-pane':''}`}>
 <div className={`cw-scroll ${nav.tab==='evidence'&&!nav.source?'cw-scroll-evidence':''}`} ref={scroll} onScroll={rememberScroll}>
 {nav.source&&selectedQuote&&runId?<div className='cw-source-wide'><button className='cw-link' onClick={()=>navigate({source:false})}><ArrowLeft size={14}/>返回证据</button><h1>{sources.find(s=>s.ref===selectedQuote.source_ref)?.title??selectedQuote.source_title}</h1><SourceText key={`${selectedQuote.source_ref}:${selectedQuote.location}`} runId={runId} card={selectedQuote} wide/></div>
 :nav.tab==='conversation'?<div className='cw-reading'>
  {turns.filter(t=>(t.turn_index??1)<turnNo).map(t=><button className='cw-old-round' key={t.run_id} onClick={()=>onOpenRun?.(t.run_id,t.status)}><span>第 {t.turn_index??1} 轮 · {t.query}</span><ArrowUpRight size={14}/></button>)}
  <div className='cw-user-question'>{turnNo>1&&<small>第 {turnNo} 轮</small>}<p>{meta?.query??view.simple?.query??view.simple?.effectiveQuery??'研究进行中'}</p></div>
  {ended&&<div className='run-meta'>
   <span className={`run-state run-state-${outcome.tone}`}>{outcome.tone==='ok'?<Check size={15}/>:outcome.tone==='stopped'?<Square size={11} fill='currentColor'/>:outcome.tone==='neutral'?null:<TriangleAlert size={14}/>}{outcome.label}</span>
   {metaParts.length>0&&<span>{metaParts.join(' · ')}</span>}
  </div>}
  {view.processMissing&&<p className='cw-notice'>{view.processMissing}</p>}
  {live&&planItems.length>0&&<section className='card plan-card'><div className='card-head'><strong>研究计划</strong><small>{planItems.filter(i=>i.status==='done').length} / {planItems.length} 完成</small></div><PlanList view={view}/></section>}
  {live&&tasks.length>0&&<section className='card plan-card'><div className='card-head'><strong>子任务</strong><small>{tasks.length} 个</small></div>{tasks.map(d=><button className='cw-agent-link' key={d.id} onClick={()=>openAgent(d.id)}><GitBranch size={14}/><span>{d.goal}</span><small>{statusName(d.status)} · {d.cardCount} 条证据</small></button>)}</section>}
  {processSegments(view).map(segment=>{
   if(segment.kind==='user')return <div className='cw-user-question cw-user-followup' key={segment.key}><small>你的补充 · {{queued:'已收到，等待处理',included:'已交给主 Agent',undelivered:'没来得及处理'}[segment.message.status]}</small><p>{segment.message.text}</p>{segment.message.reason&&<small>{segment.message.reason}</small>}</div>;
   const toolRows=segment.rows.filter(r=>r.kind==='tool');
   const searches=toolRows.filter(r=>r.kind==='tool'&&r.call.tool==='search_sources').length,reads=toolRows.filter(r=>r.kind==='tool'&&r.call.tool==='read_source').length;
   return <Fold id={`group:${segment.key}`} key={segment.key} expanded={ui.expanded} toggle={toggle} defaultOpen={live} className='cw-process'
    label={<span className='cw-process-title'>研究过程{toolRows.length?` · ${toolRows.length} 步`:''}{searches?`，搜索 ${searches} 次`:''}{reads?`，阅读 ${reads} 次`:''}</span>}>
     <ProcessList rows={segment.rows} view={view} expanded={ui.expanded} toggle={toggle} onCall={onCall} onAgent={openAgent}/>
   </Fold>})}
  {ended&&planItems.length>0&&<Fold id='plan' expanded={ui.expanded} toggle={toggle} className='cw-process' label={<span className='cw-process-title'>研究计划 · {planItems.filter(i=>i.status==='done').length}/{planItems.length} 完成</span>}><PlanList view={view}/></Fold>}
  {ended&&tasks.length>0&&<Fold id='agents' expanded={ui.expanded} toggle={toggle} className='cw-process' label={<span className='cw-process-title'>子任务 · {tasks.length} 个</span>}>{tasks.map(d=><button className='cw-agent-link' key={d.id} onClick={()=>openAgent(d.id)}><GitBranch size={14}/><span>{d.goal}</span><small>{statusName(d.status)} · {d.cardCount} 条证据</small></button>)}</Fold>}
  {live&&<p className='cw-activity'><span className='lamp lamp-current'/>{activity?.text??'正在处理…'}</p>}
  <FailedToolCalls view={view} expanded={ui.expanded} toggle={toggle} onCall={onCall}/>
  {(view.banner&&view.banner.kind!=='done'&&!(ended&&view.simple))&&<p className='cw-notice'>{view.banner.text}</p>}
  {view.simple?.scopeHistory?.at(-1)&&<p className='cw-notice'><strong>研究范围已调整：</strong>{view.simple.scopeHistory.at(-1)!.goal}<br/><small>{view.simple.scopeHistory.at(-1)!.reason}</small></p>}
  {view.inputError&&<p className='cw-warning'>{view.inputError}</p>}
  {view.simple?.draftRejected&&<p className='cw-warning'>{view.simple.draftRejectedNote??'回答没有通过引用检查，所以没有发布。'}</p>}
  {answer&&!view.simple?.draftRejected&&<article className='cw-answer cw-markdown' id='simple-answer'>{answerWithCitations(answer,buildRunIndex(view),nav.card,openCard)}</article>}
  {ended&&!answer&&!view.simple?.draftRejected&&<section className='card cw-no-answer'>
   <strong>这次没有写出回答</strong>
   <p>{view.simple?.stopReason==='cancelled'?'研究在写回答之前就停下了。':view.simple?.stopReason?`原因：${STOP_LABEL[view.simple.stopReason]?.text??view.simple.stopReason}。`:''}已经找到的证据和读过的来源都保留着，可以先看看，也可以让它基于已有线索继续。</p>
   <div className='card-actions'>
    {canFollowup&&<button className='btn btn-primary' disabled={continuing} onClick={()=>void continueFromClues()}>{continuing?'正在开始…':'基于已有线索继续'}</button>}
    {cards.length>0&&<button className='btn' onClick={()=>navigate({tab:'evidence'})}>查看 {cards.length} 条证据</button>}
   </div>
   {continueError&&<p className='cw-warning'>{continueError}</p>}
  </section>}
  <ArtifactFiles runId={runId} artifacts={view.simple?.draftRejected||view.simple?.stopReason==='cancelled'?[]:view.simple?.artifacts??[]} errors={view.simple?.artifactErrors??[]}/>
  {(view.simple?.gaps??[]).length>0&&<aside className='callout callout-warn'><Info size={16}/><div><strong>还有没查清的地方</strong><ul>{view.simple!.gaps!.map((g,i)=><li key={i}>{g}</li>)}</ul></div></aside>}
  {ended&&!cancelled&&Object.keys(view.simple?.coverage??{}).length>0&&<details className='disclosure cw-completion'><summary>回答范围与覆盖说明</summary><p className='cw-muted'>由 Agent 自报，不代表独立核验。</p>{Object.entries(view.simple?.coverage??{}).map(([id,status])=>{
   const reasons=view.simple?.coverageReasons?.[id]??[];
   const label=status==='unanswered'&&reasons.some(reason=>reason==='Lead 未报告该要求的覆盖情况')
    ?'未报告':COVERAGE_LABEL[status]??status;
   return <div key={id} className='cw-coverage-row'><strong>{view.simple?.requirements?.find(r=>r.id===id)?.text??id}</strong> · {label}{reasons.map((reason,i)=><p key={i}>{reason}</p>)}</div>;
  })}</details>}
  {!cancelled&&(view.simple?.warnings??[]).filter(w=>!(view.simple?.gaps??[]).some(g=>w===`未解决缺口：${g}`)).map((w,i)=><p key={i} className='cw-warning'>{w}</p>)}
  {ended&&runId&&<RunSkills key={runId} runId={runId} view={view}/>}
 </div>:nav.tab==='evidence'?<div className='cw-evidence-workspace'><div className='cw-evidence-list' ref={node=>{if(node)node.scrollTop=current.current.scroll['evidence:list']??0}} onScroll={e=>{current.current.scroll['evidence:list']=e.currentTarget.scrollTop}}>
  <label className='search-field'><Search size={15}/><input aria-label='搜索证据' placeholder='搜索结论、原文或来源' value={search} onChange={e=>setSearch(e.target.value)}/></label>
  {cards.filter(c=>`${c.claim} ${cardQuotes(c).map(q=>`${q.quote} ${q.source_title}`).join(' ')}`.toLowerCase().includes(search.toLowerCase())).map(c=><button key={c.card_id} className='evidence-item' aria-pressed={c.card_id===nav.card} onClick={()=>openCard(c.card_id)}>
   <span className='evidence-item-head'><span className='cite-num'>{cardNo(c.card_id)}</span><small>{hostOf(c.source_url)||c.source_title||c.source_ref}{c.origin?` · 沿用第 ${c.origin.turn_index} 轮`:''}</small></span>
   <span className='evidence-item-claim'>{c.claim}</span>
   {c.judgment&&<EvidenceJudgment judgment={c.judgment}/>}
  </button>)}
  {!cards.length&&<p className='cw-empty'>还没有保存证据。</p>}</div>
  <div className='cw-evidence-main' key={nav.card??'empty'}>{card&&runId?<EvidenceDetail key={card.card_id} card={card} sources={sources} runId={runId} onSource={quoteIndex=>navigate({source:true,quoteIndex})} onCall={onCall} onOrigin={openOrigin} view={view}/>:<p className='cw-empty'>从左边选一条证据，对照结论和原文。</p>}</div></div>
 :<>{nav.back&&<button className='cw-back cw-link' onClick={back}><ArrowLeft size={14}/>返回{nav.back.tab==='conversation'?'对话':'证据'}</button>}<RunRecords view={view} nav={nav} navigate={navigate}/></>}
 </div>
 {pane&&<aside className='cw-inspector'><header><strong>{agent?'子任务':<><span className='cite-num is-active'>{card?cardNo(card.card_id):''}</span>证据</>}</strong><button className='icon-btn' aria-label='关闭详情' onClick={closeDetail}><X size={17}/></button></header>
 {card&&runId&&<EvidenceDetail key={card.card_id} card={card} sources={sources} runId={runId} onSource={quoteIndex=>navigate({source:true,quoteIndex})} onCall={onCall} onOrigin={openOrigin} view={view}/>}
 {agent&&<AgentDetail agent={agent} view={view} expanded={ui.expanded} toggle={toggle} onCall={onCall} onAgent={openAgent}/>}</aside>}
 </div>
 {nav.tab==='conversation'&&<footer className='cw-bottom'><div className='cw-input-area'>
  {!isLatest&&<p className='cw-history-notice'>正在看之前的一轮。<button className='cw-link' onClick={()=>latest&&onOpenRun?.(latest,turns.find(t=>t.run_id===latest)?.status)}>回到最新一轮继续提问</button></p>}
  {view.replayOnly&&<p className='cw-history-notice'>当前为只读快放。<button className='cw-link' onClick={()=>runId&&onOpenRun?.(runId,meta?.status)}>打开研究结果以继续提问</button></p>}
  <ResearchComposer key={key} draftKey={key} purpose={ended?'followup':'steer'} canSend={isLatest&&(canSend||canFollowup)} state={view.inputState} messages={view.userMessages} onSend={send}/>
 </div></footer>}
 </div></RunIdentity.Provider>;
}
