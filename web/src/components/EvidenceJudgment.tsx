import type {EvidenceJudgmentData} from '../types';

const labels={supported:'引文支持',contradicted:'引文矛盾',insufficient:'依据不足'};
const errors:Record<string,string>={missing_key:'服务尚未配置密钥',timeout:'等待超时',cancelled:'研究已取消',
 network:'服务连接失败',invalid_response:'服务响应无效',client_error:'请求未完成',http_401:'密钥验证失败',
 http_429:'服务限流',http_529:'服务繁忙',http_422:'服务未接受请求',queue_timeout:'排队超时',
 batch_timeout:'本批核验等待已达上限',research_deadline:'研究时间已用尽',request_timeout:'单次请求超时',
 connect_timeout:'建立连接超时',read_timeout:'等待响应超时',write_timeout:'发送请求超时',pool_timeout:'等待连接超时',
 connect_error:'连接建立失败',read_error:'响应读取失败',write_error:'请求发送失败',remote_protocol_error:'服务连接协议异常'};
const errorLabel=(code?:string|null)=>errors[code??'']??(code?.startsWith('http_')?`服务返回 HTTP ${code.slice(5)}`:'核验未完成');
const stopLabels:Record<string,string>={attempt_limit:'已达尝试次数上限',insufficient_time:'剩余时间不足以等待再次尝试',research_deadline:'研究时间已用尽',batch_timeout:'本批核验等待已达上限',cancelled:'研究已取消',client_error:'请求未完成'};

export function EvidenceJudgment({judgment:j,detail=false}:{judgment?:EvidenceJudgmentData|null;detail?:boolean}) {
 const complete=j?.status==='complete'&&j.relation;
 const label=complete?labels[j.relation!]:j?.status==='pending'?'核验中':'未核验';
 const tone=complete?j.relation:j?.status==='pending'?'pending':'unknown';
 return <div className={`cw-judgment ${detail?'detail':''}`}>
  <span className={`cw-judgment-badge ${tone}`}>{j?'Jev · ':''}{label}</span>
  {detail&&<>
   <p className='cw-muted'>{complete
    ? `${j.model??'Jev'} ${j.prompt_version==='evidence-support-v2'?'联合判断全部引文及补充上下文是否支持主张。':'仅判断这段引文是否支持主张。'}${j.relation==='supported'?'来源真实性与时效仍需核对。':'已反馈给 Agent，需补读或修正后重新存证。'}`
    : j?.status==='pending'?'正在等待 Jev 返回判断。'
    : j?`${errorLabel(j.error_code)}；没有作出支持与否的判断。`:'这张卡没有 Jev 判断记录。'}</p>
   {j&&(j.attempted||Boolean(j.elapsed_ms))&&<small className='cw-muted'>等待 {((j.elapsed_ms??0)/1000).toFixed(2)} 秒（含排队与退避） · {j.usage?`${j.usage.input_tokens} 已知输入 token${j.usage_unknown?'，另有请求用量未知':''}`:j.attempted?'用量未知':'未发送请求'}{j.attempts?.length?` · 请求 ${j.attempts.length} 次`:''}</small>}
   {j?.retry_stop_reason&&<p className='cw-muted'>{stopLabels[j.retry_stop_reason]??'补判已停止'}。</p>}
   {!!j?.attempts?.length&&<details><summary>核验请求记录</summary>
    <p className='cw-muted'>累计排队 {((j.queue_ms??0)/1000).toFixed(2)} 秒</p>
    <ul>{j.attempts.map(a=><li key={a.number}>第 {a.number} 次：{a.status==='complete'?'取得有效判断':errorLabel(a.error_code)} · {((a.elapsed_ms??0)/1000).toFixed(2)} 秒</li>)}</ul>
   </details>}
  </>}
 </div>;
}
