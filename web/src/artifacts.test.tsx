import {renderToStaticMarkup} from 'react-dom/server';
import {expect,it} from 'vitest';
import {ArtifactFiles,ArtifactPreview} from './components/ArtifactFiles';
import { initialView,mergeSimpleResult } from './reducer';

const file={artifact_id:'a_123',title:'研究报告',filename:'report.html',format:'html',mime:'text/html',size:2048,sha256:'abc',kind:'report',requirement_ids:['goal']};
it('shows file downloads and preserves them across history merge',()=>{
 const view=mergeSimpleResult(initialView(),{artifacts:[file],artifact_errors:[{message:'PDF 未生成'}]},null);
 const html=renderToStaticMarkup(<ArtifactFiles runId='r1' artifacts={view.simple?.artifacts??[]} errors={view.simple?.artifactErrors??[]}/>);
 expect(html).toContain('/api/runs/r1/artifacts/a_123/download');expect(html).toContain('PDF 未生成');
 expect(html).toContain('研究报告');
});
it('previews interactive HTML without blocking its scripts or resources',()=>{
 const html=renderToStaticMarkup(<ArtifactPreview runId='r1' artifact={file}/>);
 expect(html).toContain('/api/runs/r1/artifacts/a_123/preview');
 expect(html).not.toContain('sandbox=');expect(html).not.toContain('srcDoc');
});

it.each(['jpeg','webp','gif'])('previews %s as an image',format=>{
 const html=renderToStaticMarkup(<ArtifactPreview runId='r1' artifact={{...file,format}}/>);
 expect(html).toContain('<img');expect(html).not.toContain('<iframe');
});

it('offers PDF preview and download, but unknown formats only download',()=>{
 const pdf=renderToStaticMarkup(<ArtifactFiles runId='r1' artifacts={[{...file,format:'pdf'}]} errors={[]}/>);
 expect(pdf).toContain('预览');expect(pdf).toContain('/download');
 const binary=renderToStaticMarkup(<ArtifactFiles runId='r1' artifacts={[{...file,format:'bin'}]} errors={[]}/>);
 expect(binary).not.toContain('预览');expect(binary).toContain('/download');
});

it('renders files in the active conversation workspace',async()=>{
 const {ConversationWorkspace}=await import('./components/ConversationWorkspace');
 const view=mergeSimpleResult(initialView(),{draft:'正文',artifacts:[file]},null);
 const html=renderToStaticMarkup(<ConversationWorkspace view={view} runId='r1'/>);
 expect(html).toContain('/api/runs/r1/artifacts/a_123/download');
});
