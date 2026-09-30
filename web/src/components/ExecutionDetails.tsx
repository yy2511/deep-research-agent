import {useState} from 'react';

export function ExecutionDetails({display}:{display:Record<string,unknown>}) {
  const [log,setLog]=useState('');const [cursor,setCursor]=useState<number|null>(0);
  const [channel,setChannel]=useState('stdout');const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  async function read(stream:string,next:number) {
    setBusy(true);setError('');
    try {
      const url=`/api/runs/${encodeURIComponent(String(display.run_id))}/execution/logs/${encodeURIComponent(String(display.execution_id))}?stream=${stream}&cursor=${next}`;
      const response=await fetch(url);if(!response.ok)throw new Error('日志读取失败');
      const data=await response.json();setLog(data.text||'（该页没有输出）');setCursor(data.next_cursor);setChannel(stream);
    } catch(e){setError(e instanceof Error?e.message:'日志读取失败');} finally{setBusy(false);}
  }
  return <details className="execution-details"><summary>命令输出与日志</summary>
    {typeof display.stdout==='string'&&display.stdout&&<pre>{display.stdout}</pre>}
    {typeof display.stderr==='string'&&display.stderr&&<pre className="execution-stderr">{display.stderr}</pre>}
    {!!(display.truncated||display.log_truncated)&&<p>显示内容已截断{display.log_truncated?'；保存日志也已达到上限':''}。</p>}
    {typeof display.execution_id==='string'&&typeof display.run_id==='string'&&<div className="artifact-actions">
      <button disabled={busy} onClick={()=>read('stdout',0)}>读取日志 stdout</button>
      <button disabled={busy} onClick={()=>read('stderr',0)}>读取日志 stderr</button>
      {log&&cursor!==null&&<button disabled={busy} onClick={()=>read(channel,cursor)}>下一页</button>}
    </div>}
    {error&&<p role="alert">{error}</p>}{log&&<pre>{log}</pre>}
  </details>;
}
