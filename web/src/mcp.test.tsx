import {renderToStaticMarkup} from 'react-dom/server';
import {expect,it} from 'vitest';
import { buildRunIndex,toolFacts } from './components/conversationHelpers';
import {McpToolPicker} from './components/McpPanel';
import {toolCallLabel} from './displayLabels';
import {updateMcpTool,type McpTool} from './mcpModel';
import {initialView} from './reducer';

it('只勾选允许的工具，重复选择不重复登记，取消时保留其他工具',()=>{
 const a=updateMcpTool([], 'read_doc', true);
 const b=updateMcpTool(a, 'search', true);
 expect(b).toEqual([{name:'read_doc'},{name:'search'}]);
 expect(updateMcpTool(b,'search',true)).toEqual(b);
 expect(updateMcpTool(b,'read_doc',false)).toEqual([{name:'search'}]);
});

it('已选工具不再要求设置用途，写工具仍不能勾选',()=>{
 const tools:McpTool[]=[{name:'search',description:'查找资料',annotations:{readOnlyHint:true}},
  {name:'delete_file',description:'删除',annotations:{readOnlyHint:false}}];
 const html=renderToStaticMarkup(<McpToolPicker tools={tools} selected={[{name:'search'}]} onChange={()=>{}}/>);
 expect(html).toContain('只读');expect(html).toContain('写入工具');
 expect(html).not.toContain('<select');expect(html).not.toContain('返回内容的用途');
 expect(html).toContain('实际看到');expect(html).toContain('引用');
 expect(html).toMatch(/disabled=""[^>]*|disabled/);
});

it('研究调用显示外部服务与资料数量，不裸露哈希工具名',()=>{
 const call={callNo:1,tool:'mcp_notes_read_ab123456',ok:true,args:'{}',result:'{}',atMs:0,
  display:{mcp:{server_id:'notes',server_name:'研究资料',tool:'read_document'},elapsed_ms:84,sources:[{source_ref:'s1',title:'资料正文'}],total:1}};
 expect(toolCallLabel(call)).toBe('研究资料 · read_document');
 const facts=toolFacts(call,buildRunIndex(initialView()));
 expect(facts.counters.join(' ')).toContain('1 份资料');
 expect(facts.counters.join(' ')).toContain('84 ms');
});
