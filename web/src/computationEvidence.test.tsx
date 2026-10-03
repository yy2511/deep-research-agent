import {expect, it} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {ComputationSourceDetails} from './components/ComputationSourceDetails';
import type {SimpleSource} from './types';

it('计算来源展示实际命令、脚本记录和原始输入链接；历史来源仍定位原运行', () => {
 const source:SimpleSource={ref:'s8',url:'',title:'计算结果',snapshot_kind:'full',raw_chars:10,
  origin:{run_id:'later',turn_index:2},
  computation:{run_id:'original',execution_id:'exec_test',tool_call_id:'call_test',cwd:'/workspace/work',
   command:'python filter.py',exit_code:0,declared_source_refs:['s5'],stdout_truncated:false,stderr:'',
   inputs:[{run_id:'original',source_ref:'s5',title:'counties.csv',url:'https://example.org/counties.csv',path:'/workspace/input/counties.csv',sha256:'abc123',size:321,download:{}}],
   workspace_steps:[{tool:'write_file',arguments:{path:'/workspace/work/filter.py',content:'print(60)'},ok:true}]}};
 const html=renderToStaticMarkup(<ComputationSourceDetails source={source}/>);
 expect(html).toContain('计算结果');
 expect(html).toContain('python filter.py');
 expect(html).toContain('print(60)');
 expect(html).toContain('abc123');
 expect(html).toContain('本次指定输入');
 expect(html).toContain('/api/runs/original/sources/s5/original');
 expect(html).not.toContain('/api/runs/later/');
});
