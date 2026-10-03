import type {SimpleSource} from '../types';

export function ComputationSourceDetails({source}:{source:SimpleSource}) {
 const data=source.computation;
 if(!data)return null;
 return <div className='cw-muted'>
  <p>计算结果 · 引用的是实际执行输出。可结合输入资料与代码核对筛选、单位和计算方法。</p>
  <details className='cw-judge-context'><summary>查看输入资料、执行命令和脚本记录</summary>
   <p>工作目录：<code>{data.cwd}</code> · 退出码：{data.exit_code}</p>
   <pre>{data.command}</pre>
   {!!data.inputs.length&&<><p>执行时已导入的资料（不表示全部被使用）：</p>{data.inputs.map((item,i)=><div key={`${item.source_ref}:${i}`}>
    <p>{item.title} · {item.source_ref}{data.declared_source_refs.includes(item.source_ref)?' · 本次指定输入':''}</p>
    <p><code>{item.path}</code><br/>SHA-256：<code>{item.sha256}</code></p>
    <a href={`/api/runs/${encodeURIComponent(item.run_id)}/sources/${item.source_ref}${item.download||item.document?'/original':''}`} target='_blank' rel='noreferrer'>查看保存的输入资料</a>
    {item.url&&<> · <a href={item.url} target='_blank' rel='noreferrer'>来源网址</a></>}
   </div>)}</>}
   {!!data.workspace_steps.length&&<details><summary>本轮先前的脚本写入、修改和执行记录</summary>
    {data.workspace_steps.map((step,i)=><div key={i}><p>{i+1}. {step.tool} · {step.ok?'成功':'未成功'}</p><pre>{JSON.stringify(step.arguments,null,2)}</pre></div>)}
   </details>}
   {data.stderr&&<><p>标准错误输出</p><pre>{data.stderr}</pre></>}
   {data.stdout_truncated&&<p className='cw-warning'>执行日志达到原有保存上限，此结果快照不完整。</p>}
  </details>
 </div>;
}
