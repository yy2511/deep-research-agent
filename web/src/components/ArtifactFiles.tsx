import {useState} from 'react';
import type {ArtifactFile} from '../types';

function endpoint(runId:string,artifact:ArtifactFile,action:string) {
  return `/api/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifact.artifact_id)}/${action}`;
}
export function ArtifactPreview({runId,artifact}:{runId:string;artifact:ArtifactFile}) {
  const url=endpoint(runId,artifact,'preview');
  if(['png','jpeg','webp','gif'].includes(artifact.format))return <img className="artifact-image" src={url} alt={artifact.title}/>;
  return <iframe className="artifact-preview" src={url} title={`${artifact.title}预览`}/>;
}
export function ArtifactFiles({runId,artifacts,errors}:{runId:string|null;artifacts:ArtifactFile[];errors:Array<{message:string}>}) {
  const [active,setActive]=useState<string|null>(null);
  if(!artifacts.length&&!errors.length)return null;
  return <section className="artifact-files" aria-label="研究文件">
    <h3>研究文件</h3>
    {artifacts.map(file=><article className="artifact-file" key={file.artifact_id}>
      <div className="artifact-file-head"><div><strong>{file.title}</strong><small>{file.format.toUpperCase()} · {(file.size/1024).toFixed(1)} KB · {file.filename}</small></div>
        {runId&&<div className="artifact-actions">
          {['html','pdf','svg','png','jpeg','webp','gif','csv','json','markdown','text','code'].includes(file.format)&&<button aria-expanded={active===file.artifact_id} onClick={()=>setActive(active===file.artifact_id?null:file.artifact_id)}>{active===file.artifact_id?'收起预览':'预览'}</button>}
          <a href={endpoint(runId,file,'download')} download={file.filename}>下载</a>
        </div>}
      </div>
      {runId&&active===file.artifact_id&&<ArtifactPreview runId={runId} artifact={file}/>}
    </article>)}
    {!!errors.length&&<ul className="simple-warnings">{errors.map((error,i)=><li key={i}>{error.message}</li>)}</ul>}
  </section>;
}
