import { useEffect, useState } from 'react';
import { getJSON, postJSON, request } from '../api';
import { Icon } from './Icon';
import { McpChoices } from './McpPanel';
import { Modal, Popover } from './ui';
import { sourceLabel } from '../format';

export interface SourceSelection {
  skill_selection?: string;
  source_mode: 'web' | 'library' | 'mixed';
  document_ids: string[];
  library_rerank: boolean;
  mcp_server_ids?: string[];
  blocked_sites?: string[];
}
interface Version {
  version_id: string; file_name: string; status: string; error?: string; note?: string;
  raw_chars?: number; chunks?: number; warnings?: string[];
}
interface Document {
  document_id: string; current: Version | null; latest: Version;
}
interface LibraryData { documents: Document[] }
interface SearchHit { title: string; document: {version_id: string; matches: {start:number;end:number;text:string}[]} }
const status: Record<string,string> = {queued:'排队中',parsing:'正在解析',indexing:'正在建立索引',ready:'可用',original:'原件已保存（工具读取）',failed:'导入失败'};

const MODES = [
  ['web', '仅网页', '只在网上检索和阅读'],
  ['library', '仅我的资料', '只查你上传的文件，不上网'],
  ['mixed', '网页 + 我的资料', '两边都查'],
] as const;

/** 研究来源的三个选项（下拉面板里的内容）。 */
export function SourceModeOptions({ value, onChange, disabled = false }: {
  value: SourceSelection; onChange: (value: SourceSelection) => void; disabled?: boolean;
}) {
  return <fieldset className="option-list" disabled={disabled}>
    <legend>研究来源</legend>
    {MODES.map(([id, label, hint]) => <label key={id} className={`option-row${value.source_mode === id ? ' is-on' : ''}`}>
      <input type="radio" name="source-mode" value={id} checked={value.source_mode === id}
        onChange={() => onChange({ ...value, source_mode: id, ...(id === 'library' ? { mcp_server_ids: [] } : {}) })} />
      <span><strong>{label}</strong><small>{hint}</small></span>
    </label>)}
    {value.source_mode !== 'web' && !value.document_ids.length && <p className="field-hint is-warn">还没有选资料，请到「我的资料」里勾选。</p>}
  </fieldset>;
}

/** 输入框下方的“来源”按钮：选网页 / 资料，以及要连的外部资料服务。 */
export function SourcePicker({ value, onChange, disabled = false }: {
  value: SourceSelection; onChange: (value: SourceSelection) => void; disabled?: boolean;
}) {
  return <Popover ariaLabel="研究来源" disabled={disabled} active
    label={<><Icon name="globe" size={15} />{sourceLabel(value)}<Icon name="chevron-down" size={14} className="chip-caret" /></>}>
    <SourceModeOptions value={value} onChange={onChange} disabled={disabled} />
    <McpChoices selected={value.mcp_server_ids ?? []} disabled={disabled} localOnly={value.source_mode === 'library'}
      onChange={ids => onChange({ ...value, mcp_server_ids: ids })} />
    {value.source_mode !== 'library' && <BlockedSites value={value.blocked_sites ?? []} disabled={disabled}
      onChange={sites => onChange({ ...value, blocked_sites: sites })} />}
  </Popover>;
}

/** 网站黑名单：每行一个网站，研究时不搜、不读这些网站。 */
export function BlockedSites({ value, onChange, disabled = false }: {
  value: string[]; onChange: (sites: string[]) => void; disabled?: boolean;
}) {
  // 输入框保留原样（含空行），只把整理后的列表交出去，避免打字时光标乱跳。
  const [text, setText] = useState(value.join('\n'));
  return <fieldset className="option-list" disabled={disabled}>
    <legend>不使用的网站</legend>
    <div className="blocked-sites">
      <textarea className="input textarea" rows={3} aria-label="不使用的网站" value={text}
        placeholder={'每行一个，例如\nexample.com\nmdpi.com/2227-9709/11/3/64'}
        onChange={e => {
          setText(e.target.value);
          onChange(e.target.value.split('\n').map(line => line.trim()).filter(Boolean));
        }} />
      <p className="field-hint">写域名会连同子域名一起屏蔽；写到路径只屏蔽那一部分页面。会记住，下次研究沿用。</p>
    </div>
  </fieldset>;
}

/** 输入框下方的“我的资料”按钮，点开是资料弹窗。 */
export function LibraryButton({ value, onChange, disabled = false }: {
  value: SourceSelection; onChange: (value: SourceSelection) => void; disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const count = value.document_ids.length;
  return <>
    <button type="button" className={`chip-btn${count ? ' is-active' : ''}`} disabled={disabled} onClick={() => setOpen(true)}>
      <Icon name="book" size={15} />我的资料{count ? <span className="chip-count">{count}</span> : null}
    </button>
    {open && <Modal title="我的资料" onClose={() => setOpen(false)}
      description="文件保存在本机；研究时，读到的片段会发给研究模型。文字可检索；表格、图片和其他原件可交给工具读取。单个最大 10 MB。"
      footer={<>
        <span className="modal-foot-note">{count
          ? `已选 ${count} 份，${value.source_mode === 'library' ? '本次只查这些资料' : '本次会同时查网页和这些资料'}`
          : '勾选的资料会用于这次研究'}</span>
        <button type="button" className="btn btn-primary" onClick={() => setOpen(false)}>完成</button>
      </>}>
      <LibraryManager value={value} onChange={onChange} disabled={disabled} />
    </Modal>}
  </>;
}

/** 资料列表：上传、勾选、试查、更新和移出。 */
export function LibraryManager({value,onChange,disabled=false}: {
  value: SourceSelection; onChange:(value:SourceSelection)=>void; disabled?:boolean;
}) {
  const [data,setData]=useState<LibraryData>({documents:[]});
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const [query,setQuery]=useState('');
  const [hits,setHits]=useState<SearchHit[]|null>(null);
  const [searching,setSearching]=useState(false);
  const [searchError,setSearchError]=useState('');
  const [notice,setNotice]=useState('');
  async function refresh() { setData(await getJSON<LibraryData>('/api/library')); }
  useEffect(()=>{
    let mounted=true;
    const load=async()=>{try {const next=await getJSON<LibraryData>('/api/library');if(mounted)setData(next);}catch(e){if(mounted)setError(String(e));}};
    void load();
    const timer=setInterval(()=>{void load();},2500);
    return ()=>{mounted=false;clearInterval(timer);};
  },[]);
  function select(id:string,checked:boolean) {
    const document_ids=checked?[...value.document_ids,id]:value.document_ids.filter(v=>v!==id);
    // 勾选资料时如果还是“仅网页”，自动改成“网页 + 我的资料”，免得选了却没用上。
    onChange({...value,document_ids,...(checked&&value.source_mode==='web'?{source_mode:'mixed' as const}:{})});
    setHits(null);
  }
  async function upload(files:FileList|null,replace?:string) {
    if(!files?.length)return;
    setBusy(true);setError('');setNotice('');
    try {
      for(const file of Array.from(files)) {
        if(file.size>10*1024*1024)throw new Error(`${file.name} 超过 10 MB`);
        const params=new URLSearchParams({name:file.name,...(replace?{replace}:{})});
        await request(`/api/library/documents?${params}`,{method:'POST',body:file,headers:{'Content-Type':'application/octet-stream'}});
      }
      setNotice('文件已上传，处理完成后就能勾选。');await refresh();
    } catch(e){setError(e instanceof Error?e.message:String(e));await refresh();}
    finally{setBusy(false);}
  }
  async function demo() {
    setBusy(true);setError('');
    try {await postJSON('/api/library/demo',{});setNotice('示例资料已导入：公开技术资料，以及明确标注为模拟的内部文件。');await refresh();}
    catch(e){setError(e instanceof Error?e.message:String(e));}
    finally{setBusy(false);}
  }
  async function remove(id:string) {
    setBusy(true);setError('');
    try {await request(`/api/library/documents/${id}`,{method:'DELETE'});select(id,false);await refresh();}
    catch(e){setError(e instanceof Error?e.message:String(e));}
    finally{setBusy(false);}
  }
  async function search() {
    setSearching(true);setSearchError('');setHits(null);
    try {
      const result=await postJSON<{results:SearchHit[]}>('/api/library/search',{query,document_ids:value.document_ids,rerank:value.library_rerank});
      setHits(result.results);
    }catch(e){setSearchError(e instanceof Error?e.message:String(e));}
    finally{setSearching(false);}
  }
  return <section className="library" aria-label="我的资料">
    <div className="library-actions">
      <label className="btn btn-sm library-upload"><Icon name="upload" size={14} />上传文件<input aria-label="上传资料" type="file" multiple disabled={busy||disabled} onChange={e=>{void upload(e.target.files);e.target.value='';}}/></label>
      <button type="button" className="btn btn-sm btn-ghost" disabled={busy||disabled} onClick={()=>void demo()}>导入示例资料</button>
    </div>
    {error && <p className="field-hint is-bad" role="alert">{error}</p>}
    {notice && <p className="field-hint" role="status">{notice}</p>}
    {!data.documents.length && <p className="empty-note">还没有资料。可以先导入示例资料体验一下。</p>}
    {data.documents.length>0 && <ul className="library-documents">{data.documents.map(doc=>{
      const current=doc.current,latest=doc.latest;
      const processing=['queued','parsing','indexing'].includes(latest.status);
      return <li key={doc.document_id} className={!current?'is-disabled':''}>
        <label className="library-document-head">
          <input type="checkbox" aria-label={`选择 ${latest.file_name}`} checked={value.document_ids.includes(doc.document_id)} disabled={!current||disabled} onChange={e=>select(doc.document_id,e.target.checked)}/>
          <span className="library-document-name">
            <strong>{latest.file_name}</strong>
            <small>{current
              ? `${current.status==='original'?'尚无文字索引':`${current.raw_chars?.toLocaleString()} 字`}${latest.version_id!==current.version_id?' · 新版本处理中，暂用上一版':''}`
              : processing ? '处理完成后才能选' : ''}</small>
          </span>
          <span className={`status-text is-${latest.status}`}>{processing&&<Icon name="loading" className="icon-spin" size={13}/>}{status[latest.status]??latest.status}</span>
        </label>
        {latest.note&&<p className="field-hint">{latest.note}</p>}
        {latest.error && <p className="field-hint is-bad">{latest.error}</p>}
        {(current?.warnings??[]).map(w=><p className="field-hint" key={w}>{w}</p>)}
        <div className="library-document-actions">
          {current && <>{current.status!=="original"&&<a href={`/api/library/versions/${current.version_id}/text`} target="_blank" rel="noreferrer">预览解析文本</a>}<a href={`/api/library/versions/${current.version_id}/original`} target="_blank" rel="noreferrer">打开原件</a></>}
          <label className="library-replace">{latest.status==='failed'?'重新导入':'更新版本'}<input aria-label={`更新 ${latest.file_name}`} type="file" disabled={busy||disabled||processing} onChange={e=>{void upload(e.target.files,doc.document_id);e.target.value='';}}/></label>
          <button type="button" className="link-btn" disabled={busy||disabled} onClick={()=>void remove(doc.document_id)}>移出</button>
        </div>
      </li>;
    })}</ul>}
    {data.documents.length>0 && <p className="field-hint">移出只影响之后的研究，已经用过的版本和引用会保留。</p>}
    {value.document_ids.length>0 && <div className="library-search">
      <div className="field-label">先试查一下，看看选中的资料能不能找到相关内容</div>
      <div className="inline-form"><input className="input" aria-label="试查所选资料" placeholder="例如：主从切换的步骤" value={query} onChange={e=>setQuery(e.target.value)}/><button type="button" className="btn" disabled={searching||!query.trim()} onClick={()=>void search()}>{searching?'查找中…':'试查'}</button></div>
      <label className="check-line"><input type="checkbox" checked={value.library_rerank} disabled={disabled} onChange={e=>onChange({...value,library_rerank:e.target.checked})}/>本地重排（结果更准，但更慢）</label>
      {searchError && <p className="field-hint is-bad" role="alert">{searchError}</p>}
      {hits && <div className="library-hits"><p className="field-hint">{hits.length?`在 ${hits.length} 份文件里找到相关片段（找到不代表已经回答了问题）。`:'没有找到相关内容。'}</p>{hits.map(hit=><details key={hit.document.version_id}><summary>{hit.title} · {hit.document.matches.length} 处</summary>{hit.document.matches.map(m=><blockquote key={`${m.start}-${m.end}`}><small>第 {m.start}–{m.end} 字</small><p>{m.text}</p></blockquote>)}</details>)}</div>}
    </div>}
  </section>;
}
