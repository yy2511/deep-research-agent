import {useEffect, useState} from 'react';
import {getJSON, putJSON} from '../api';
import type {RunView} from '../types';
import {Icon} from './Icon';
import {Popover, Switch} from './ui';

interface Skill {
  name: string; title?: string; description?: string; enabled: boolean;
  text?: string; references?: Record<string,string>; error?: string; builtin?: boolean;
}
interface LoadedSkill {name:string;title?:string;hash:string;agent_id:string;source:string;path?:string|null;content:string}
const message = (e:unknown) => e instanceof Error ? e.message : String(e);
const freshText = '---\nname: custom-method\ndescription: 说明适合什么研究任务，以及何时使用。\nmetadata:\n  display-name: 我的研究方法\n---\n\n# 研究方法\n写下步骤、判断原则与交付要求。\n';

export function SkillChoices({skills,selected,onChange,disabled=false}:{skills:Skill[];selected:string[];onChange:(names:string[])=>void;disabled?:boolean}) {
  return <fieldset className="option-list" disabled={disabled}><legend>技能</legend>
    <p className="field-hint">不勾选时，Agent 会按问题自动选用。勾选的技能从澄清和制定计划开始就会用上。</p>
    {skills.filter(s=>s.enabled&&!s.error).map(s=><label key={s.name} className={`option-row${selected.includes(s.name)?' is-on':''}`}>
      <input type="checkbox" checked={selected.includes(s.name)} onChange={e=>onChange(e.target.checked?[...selected,s.name]:selected.filter(n=>n!==s.name))}/>
      <span><strong>{s.title||s.name}</strong><small>{s.description}</small></span>
    </label>)}
    {!skills.some(s=>s.enabled&&!s.error)&&<p className="field-hint">没有启用的技能，可以在「设置 → 技能」里添加。</p>}
  </fieldset>;
}

function useSkills() {
  const [skills,setSkills]=useState<Skill[]>([]);
  const [error,setError]=useState('');
  useEffect(()=>{
    let alive=true;
    const load=()=>getJSON<{skills:Skill[]}>('/api/skills').then(r=>{if(alive){setSkills(r.skills);setError('')}}).catch(e=>{if(alive)setError(message(e))});
    void load();window.addEventListener('skills-changed',load);
    return ()=>{alive=false;window.removeEventListener('skills-changed',load)};
  },[]);
  return {skills,setSkills,error,setError};
}

/** 输入框下方的“技能”按钮。 */
export function SkillPicker(props:{selected:string[];onChange:(names:string[])=>void;disabled?:boolean}) {
  const {skills,error}=useSkills();
  const count=props.selected.length;
  return <Popover ariaLabel="选择技能" disabled={props.disabled} active={count>0}
    label={<><Icon name="sparkles" size={15}/>{count?`技能：已选 ${count} 个`:'技能：自动选择'}</>}>
    {error&&<p role="alert" className="field-hint is-bad">{error}</p>}
    <SkillChoices {...props} skills={skills}/>
  </Popover>;
}

/** 设置页：启用、新建、编辑技能。 */
export function SkillsPanel() {
  const {skills,setSkills,error,setError}=useSkills();
  const [notice,setNotice]=useState('');
  const [busy,setBusy]=useState(false);
  const [editing,setEditing]=useState(false);
  const [isNew,setIsNew]=useState(false);
  const [name,setName]=useState('custom-method');
  const [text,setText]=useState(freshText);
  const [references,setReferences]=useState<Record<string,string>>({});
  const [refPath,setRefPath]=useState('references/guide.md');
  async function action(work:()=>Promise<void>) {
    setBusy(true);setError('');setNotice('');
    try {await work()} catch(e){setError(message(e))} finally {setBusy(false)}
  }
  function edit(skill?:Skill) {
    setEditing(true);setIsNew(!skill);setName(skill?.name??'custom-method');setText(skill?.text??freshText.replace('custom-method',skill?.name??'custom-method'));
    setReferences(skill?.references??{});setError('');setNotice('');
  }
  async function save() {
    if(isNew&&skills.some(s=>s.name===name))throw new Error("这个技能标识已存在，请编辑已有技能或换一个标识。");
    await putJSON(`/api/skills/packages/${encodeURIComponent(name)}`,{text,references});
    setSkills((await getJSON<{skills:Skill[]}>('/api/skills')).skills);
    setEditing(false);setNotice(isNew?'已保存。新建的技能还需要打开开关才会启用。':'已保存。已经开始的研究继续用原来的版本。');
    window.dispatchEvent(new Event('skills-changed'));
  }
  async function enable(skill:Skill) {
    const enabled=skills.filter(s=>s.enabled&&s.name!==skill.name).map(s=>s.name);
    if(!skill.enabled)enabled.push(skill.name);
    setSkills((await putJSON<{skills:Skill[]}>('/api/skills/enabled',{enabled})).skills);
    window.dispatchEvent(new Event('skills-changed'));
  }
  return <section className="settings-section" aria-label="技能管理">
    <div className="section-head">
      <div><h2>技能</h2><p>技能（Skill）是一份写好的研究套路，Agent 会按问题自动加载；新建研究时也可以手动指定。技能只指导怎么查，事实仍要靠取证。</p></div>
      <div className="section-head-actions">
        <button className="btn btn-ghost" disabled={busy} onClick={()=>void action(async()=>{setSkills((await getJSON<{skills:Skill[]}>('/api/skills')).skills);window.dispatchEvent(new Event('skills-changed'))})}><Icon name="refresh" size={14}/>刷新</button>
        <button className="btn" disabled={busy||editing} onClick={()=>edit()}><Icon name="plus" size={15}/>新建技能</button>
      </div>
    </div>
    {error&&<p className="field-hint is-bad" role="alert">{error}</p>}{notice&&<p className="field-hint" role="status">{notice}</p>}
    {skills.length>0&&<div className="list-card">{skills.map(s=><div className="list-row list-row-top" key={s.name}>
      <Switch checked={s.enabled} label={`启用 ${s.title||s.name}`} disabled={busy||!!s.error} onChange={()=>void action(()=>enable(s))}/>
      <div className="list-row-main">
        <strong>{s.title||s.name}<span className="tag">{s.builtin?'内置':'自定义'}</span></strong>
        <p>{s.description}</p>
        {s.error&&<p className="field-hint is-bad">{s.error}</p>}
      </div>
      <button className="btn btn-sm" disabled={busy||editing||!!s.error&&!s.text} onClick={()=>edit(s)}>编辑</button>
    </div>)}</div>}
    {editing&&<div className="form-card"><h3>{isNew?'新建技能':`编辑技能：${name}`}</h3><fieldset className="form-fieldset" disabled={busy}>
      <label className="field"><span className="field-label">技能标识</span><input className="input mono" aria-label="技能标识" value={name} disabled={!isNew} onChange={e=>{const next=e.target.value;setText(t=>t.replace(/^name: .*$/m,`name: ${next}`));setName(next)}}/></label>
      <label className="field"><span className="field-label">技能正文（SKILL.md）</span><textarea className="input textarea mono" aria-label="技能正文" rows={14} value={text} onChange={e=>setText(e.target.value)}/>
        <small className="field-hint">保留顶部的 name 和 description；正文可以引用 references/ 下的文件。编辑内置技能会另存为你自己的版本。</small></label>
      {Object.entries(references).map(([path,content])=><div className="field" key={path}><div className="field-label-row"><span className="field-label mono">{path}</span><button type="button" className="link-btn" onClick={()=>setReferences(Object.fromEntries(Object.entries(references).filter(([p])=>p!==path)))}>移除</button></div><textarea className="input textarea mono" aria-label={path} rows={5} value={content} onChange={e=>setReferences({...references,[path]:e.target.value})}/></div>)}
      <div className="inline-form"><input className="input mono" aria-label="参考文件路径" value={refPath} onChange={e=>setRefPath(e.target.value)} placeholder="references/guide.md"/><button type="button" className="btn" onClick={()=>{if(!Object.hasOwn(references,refPath))setReferences({...references,[refPath]:''})}}>添加参考文件</button></div>
      <div className="form-actions"><button type="button" className="btn btn-ghost" onClick={()=>setEditing(false)}>取消</button><button type="button" className="btn btn-primary" onClick={()=>void action(save)}>保存</button></div>
    </fieldset></div>}
  </section>;
}

export function SkillSnapshot({loaded}:{loaded:LoadedSkill[]}) {
  return <div className="skill-snapshot">{loaded.length===0?<p className="field-hint">这一轮没有加载技能。</p>:loaded.map((item,i)=><details key={`${item.agent_id}:${item.name}:${item.path??''}:${i}`}>
    <summary>{item.title||item.name}{item.path?` / ${item.path}`:''} · {item.agent_id==='lead'?'主 Agent':item.agent_id} · 已加载</summary>
    <small className="field-hint">{item.source==='manual'?'手动选择':'自动加载'} · 版本 {item.hash.slice(0,12)} · 这里记录的是 Agent 收到的内容，不代表执行得好不好</small><pre>{item.content}</pre>
  </details>)}</div>;
}

export function RunSkills({runId,view}:{runId:string;view:RunView}) {
  const [loaded,setLoaded]=useState<LoadedSkill[]>([]);
  const [error,setError]=useState('');
  const count=view.timeline.filter(i=>i.t==='note'&&i.card.kind==='skill_loaded').length;
  useEffect(()=>{let alive=true;setError('');getJSON<{loaded:LoadedSkill[]}>(`/api/runs/${runId}/skills`).then(r=>{if(alive)setLoaded(r.loaded)}).catch(e=>{if(alive)setError(message(e))});return()=>{alive=false}},[runId,count,view.endedAtMs]);
  if(!error&&!loaded.length)return null;
  return <details className="disclosure run-skills"><summary>本轮用到的技能{loaded.length?` · ${loaded.length} 个`:''}</summary>{error?<p role="alert" className="field-hint is-bad">{error}</p>:<SkillSnapshot loaded={loaded}/>}</details>;
}
