import {useEffect,useState} from 'react';
import {getJSON,postJSON,putJSON} from '../api';
import {updateMcpTool,type AllowedMcpTool as AllowedTool,type McpTool} from '../mcpModel';
import {Icon} from './Icon';
import {Switch} from './ui';

interface McpServer {id:string;name:string;url:string;enabled:boolean;headers_env:Record<string,string>;timeout_s:number;tools:AllowedTool[]}
interface McpSettings {schema_version:1;servers:McpServer[]}

function useMcpSettings() {
 const [settings,setSettings]=useState<McpSettings>({schema_version:1,servers:[]});
 const [loading,setLoading]=useState(true),[error,setError]=useState('');
 useEffect(()=>{let alive=true;getJSON<McpSettings>('/api/mcp/config').then(data=>{if(alive)setSettings(data)}).catch(e=>{if(alive)setError(e.message)}).finally(()=>{if(alive)setLoading(false)});return()=>{alive=false}},[]);
 return {settings,setSettings,loading,error,setError};
}

export function McpToolPicker({tools,selected,onChange,disabled=false}:{tools:McpTool[];selected:AllowedTool[];onChange:(value:AllowedTool[])=>void;disabled?:boolean}) {
 return <div className='mcp-tool-picker'><p className='field-hint'>勾选允许 Agent 使用的只读工具。Agent 实际看到的返回内容可以直接引用，来源会保留。</p>
  {tools.map(tool=>{const chosen=selected.find(t=>t.name===tool.name),writes=tool.annotations?.readOnlyHint===false;
   return <div className={`mcp-tool${writes?' is-disabled':''}`} key={tool.name}>
    <label className='check-line'><input type='checkbox' checked={!!chosen} disabled={disabled||writes||(!chosen&&selected.length>=16)} onChange={e=>onChange(updateMcpTool(selected,tool.name,e.target.checked))}/><code>{tool.name}</code><small>{writes?'写入工具 · 不能启用':tool.annotations?.readOnlyHint?'服务标注只读':'需确认只读'}</small></label>
    {tool.description&&<p className='field-hint'>{tool.description}</p>}
   </div>;
  })}
  {!tools.length&&<p className='field-hint'>服务没有提供工具。</p>}
 </div>;
}

/** 新建研究时勾选这次要连的外部资料服务（在“来源”面板里）。 */
export function McpChoices({selected,onChange,disabled=false,localOnly=false}:{selected:string[];onChange:(ids:string[])=>void;disabled?:boolean;localOnly?:boolean}) {
 const {settings,loading,error}=useMcpSettings();
 const usable=settings.servers.filter(s=>s.enabled);
 // 设置里停用或删掉的服务，从本次选择里去掉。
 const stale=!loading&&!error&&selected.some(id=>!usable.some(s=>s.id===id&&s.tools.length));
 useEffect(()=>{if(stale)onChange(selected.filter(id=>usable.some(s=>s.id===id&&s.tools.length)))},[stale]);// eslint-disable-line react-hooks/exhaustive-deps
 function select(id:string,checked:boolean) {onChange(checked?[...selected,id]:selected.filter(s=>s!==id))}
 if(loading)return null;
 return <fieldset className='option-list' disabled={disabled||localOnly}>
  <legend>外部资料（MCP）</legend>
  {localOnly&&<p className='field-hint'>“仅我的资料”不连接外部服务。</p>}
  {usable.map(s=><label key={s.id} className='option-row'>
   <input type='checkbox' checked={selected.includes(s.id)} disabled={!s.tools.length||(!selected.includes(s.id)&&selected.length>=4)} onChange={e=>select(s.id,e.target.checked)}/>
   <span><strong>{s.name}</strong><small>{s.tools.length?`${s.tools.length} 个可用工具`:'还没有选工具'}</small></span>
  </label>)}
  {!usable.length&&<p className='field-hint'>还没有连接。可以在「设置 → 外部资料」里添加。</p>}
  {error&&<p className='field-hint is-bad' role='alert'>{error}</p>}
 </fieldset>;
}

/** 设置页：添加、测试、编辑外部资料服务。 */
export function McpManager() {
 const {settings,setSettings,loading,error,setError}=useMcpSettings();
 const [draft,setDraft]=useState<McpServer|null>(null),[editingId,setEditingId]=useState<string|null>(null);
 const [tools,setTools]=useState<McpTool[]|null>(null),[testing,setTesting]=useState(false),[saving,setSaving]=useState(false),[notice,setNotice]=useState('');
 const busy=testing||saving;
 function edit(server?:McpServer) {
  setDraft(server?{...server,headers_env:{...server.headers_env},tools:[...server.tools]}:{id:`source_${crypto.randomUUID().slice(0,8)}`,name:'',url:'',enabled:true,headers_env:{},timeout_s:30,tools:[]});
  setEditingId(server?.id??null);setTools(null);setError('');setNotice('');
 }
 function changeConnection(patch:Partial<McpServer>) {
  setDraft(d=>d?{...d,...patch,tools:[]}:d);setTools(null);setNotice('');
 }
 async function test() {
  if(!draft)return;setTesting(true);setError('');setNotice('');setTools(null);
  try {const result=await postJSON<{ok:boolean;tools:McpTool[]}>('/api/mcp/test',draft);
   setTools(result.tools);setDraft(d=>d?{...d,tools:d.tools.filter(t=>result.tools.some(r=>r.name===t.name&&r.annotations?.readOnlyHint!==false))}:d);
   setNotice(`连接成功，发现 ${result.tools.length} 个工具。请勾选允许使用的资料工具。`);
  }catch(e){setError(e instanceof Error?e.message:String(e))}finally{setTesting(false)}
 }
 async function persist(servers:McpServer[]) {
  const data=await putJSON<McpSettings>('/api/mcp/config',{...settings,servers});setSettings(data);
 }
 async function save() {
  if(!draft)return;setSaving(true);setError('');setNotice('');
  try {await persist([...settings.servers.filter(s=>s.id!==editingId),draft]);
   setDraft(null);setTools(null);setNotice('已保存。新建研究时在「来源」里勾选它就能用。');
  }catch(e){setError(e instanceof Error?e.message:String(e))}finally{setSaving(false)}
 }
 async function toggle(server:McpServer,enabled:boolean) {
  setSaving(true);setError('');
  try {await persist(settings.servers.map(s=>s.id===server.id?{...s,enabled}:s))}
  catch(e){setError(e instanceof Error?e.message:String(e))}finally{setSaving(false)}
 }
 return <div className='settings-section'>
  <div className='section-head'>
   <div><h2>外部资料（MCP）</h2><p>连接已有的 MCP 服务，让 Agent 读取里面的资料。只会启用只读工具，调用和返回内容会随研究一起保存。</p></div>
   {!draft&&<button type='button' className='btn' disabled={busy||settings.servers.length>=8} onClick={()=>edit()}><Icon name='plus' size={15}/>添加服务</button>}
  </div>
  {loading&&<p className='field-hint'>正在读取连接配置…</p>}
  {error&&<p className='field-hint is-bad' role='alert'>{error}</p>}
  {notice&&<p className='field-hint' role='status'>{notice}</p>}
  {settings.servers.length>0&&<div className='list-card'>{settings.servers.map(s=><div key={s.id} className='list-row'>
   <Switch checked={s.enabled} label={`启用 ${s.name}`} disabled={busy||(!s.enabled&&!s.tools.length)} onChange={v=>void toggle(s,v)}/>
   <div className='list-row-main'><strong>{s.name}</strong><code>{s.url}</code></div>
   <span className={`status-text${s.enabled?' is-ready':''}`}>{s.enabled?`${s.tools.length} 个可用工具`:'已停用'}</span>
   <button type='button' className='btn btn-sm' disabled={busy||!!draft} onClick={()=>edit(s)}>编辑</button>
  </div>)}</div>}
  {!loading&&!settings.servers.length&&!draft&&<p className='empty-note'>还没有连接任何服务。</p>}
  {draft&&<div className='form-card'>
   <h3>{editingId?`编辑：${draft.name||'服务'}`:'添加服务'}</h3>
   <label className='field'><span className='field-label'>名称</span><input className='input' aria-label='MCP 服务名称' value={draft.name} maxLength={80} disabled={busy} placeholder='例如 GitHub 资料' onChange={e=>setDraft({...draft,name:e.target.value})}/></label>
   <label className='field'><span className='field-label'>服务地址</span><input className='input mono' aria-label='MCP 服务地址' value={draft.url} disabled={busy} placeholder='https://example.com/mcp' onChange={e=>changeConnection({url:e.target.value})}/><small className='field-hint'>支持 Streamable HTTP 服务。</small></label>
   <details className='disclosure'><summary>认证与超时</summary>
    <p className='field-hint'>凭据写在项目的 .env 里，这里只填变量名，例如请求头 Authorization 对应 GITHUB_MCP_AUTH（变量值是完整的 Bearer 凭据）。改完 .env 需要重启后端。</p>
    {Object.entries(draft.headers_env).map(([header,variable],index)=><div className='mcp-header-row' key={index}><label className='field'><span className='field-label'>请求头</span><input className='input' aria-label='认证请求头' disabled={busy} value={header} onChange={e=>{const pairs=Object.entries(draft.headers_env).map(([k,v])=>[k===header?e.target.value:k,v]);changeConnection({headers_env:Object.fromEntries(pairs)})}}/></label><label className='field'><span className='field-label'>环境变量名</span><input className='input mono' aria-label='凭据环境变量' disabled={busy} value={variable} placeholder='GITHUB_MCP_AUTH' onChange={e=>changeConnection({headers_env:{...draft.headers_env,[header]:e.target.value}})}/></label><button type='button' className='btn btn-sm btn-ghost' disabled={busy} onClick={()=>changeConnection({headers_env:Object.fromEntries(Object.entries(draft.headers_env).filter(([k])=>k!==header))})}>移除</button></div>)}
    <button type='button' className='btn btn-sm' disabled={busy||Object.keys(draft.headers_env).length>=8} onClick={()=>{let header='Authorization';let index=1;while(header in draft.headers_env){header=index===1?'X-Api-Key':`X-Api-Key-${index}`;index++}changeConnection({headers_env:{...draft.headers_env,[header]:''}})}}>添加认证请求头</button>
    <label className='field field-inline'><span className='field-label'>单次超时（秒）</span><input className='input input-num' type='number' min={1} max={60} value={draft.timeout_s} disabled={busy} onChange={e=>setDraft({...draft,timeout_s:Number(e.target.value)})}/></label>
   </details>
   <label className='check-line'><input type='checkbox' checked={draft.enabled} disabled={busy} onChange={e=>setDraft({...draft,enabled:e.target.checked})}/>启用这个服务</label>
   <div className='form-actions form-actions-start'><button type='button' className='btn' disabled={busy||!draft.url.trim()||!draft.name.trim()} onClick={()=>void test()}>{testing?'连接中…':'测试连接并列出工具'}</button><span className='field-hint'>只读取工具目录，不会调用工具。</span></div>
   {tools&&<McpToolPicker tools={tools} selected={draft.tools} onChange={v=>setDraft({...draft,tools:v})} disabled={busy}/>}
   {!tools&&draft.tools.length>0&&<p className='field-hint'>已保存 {draft.tools.length} 个工具；测试连接后可以调整。</p>}
   <div className='form-actions'><button type='button' className='btn btn-ghost' disabled={busy} onClick={()=>{setDraft(null);setTools(null);setError('')}}>取消</button><button type='button' className='btn btn-primary' disabled={busy||!draft.name.trim()||!draft.url.trim()||(draft.enabled&&(!tools||!draft.tools.length))} onClick={()=>void save()}>{saving?'保存中…':'保存'}</button></div>
  </div>}
 </div>;
}
