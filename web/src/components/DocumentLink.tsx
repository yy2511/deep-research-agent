import type { SimpleSource } from '../types';

export function DocumentLink({runId,source,location}:{runId:string|null;source?:SimpleSource;location?:string}) {
  if(!runId||!source?.document||source.snapshot_kind!=='full')return null;
  const start=Number(/^chars=(\d+)-/.exec(location??'')?.[1]??0);
  const page=source.document.pages.find(p=>p.start<=start&&start<p.end)?.page??1;
  const pdf=source.document.suffix==='.pdf';
  return <a href={`/api/runs/${runId}/sources/${source.ref}/original${pdf?`#page=${page}`:''}`} target="_blank" rel="noreferrer">
    {pdf?`打开原 PDF 第 ${page} 页`:'打开保存原件'}
  </a>;
}
