import type {SimpleSource} from '../types';

export function McpSourceDetails({source,runId}:{source:SimpleSource;runId:string|null}) {
 const mcp=source.mcp;
 if(!mcp)return null;
 const original=mcp.run_id??source.origin?.run_id??runId;
 return <div className='cw-muted mcp-source-details'>
  <p>MCP 返回资料 · {mcp.server_name} / {mcp.tool}<br/>引用定位于服务返回的{mcp.format==='structured_json'?'结构化数据':'文本'}，不代表网页全文或事实已核实。</p>
  {mcp.uri&&<p>资源标识：<code>{mcp.uri}</code></p>}
  {original&&/^[a-f0-9]{32}$/.test(mcp.receipt_id)&&<a href={`/api/runs/${encodeURIComponent(original)}/mcp-results/${mcp.receipt_id}`} target='_blank' rel='noreferrer'>查看 MCP 原始回执</a>}
 </div>;
}
