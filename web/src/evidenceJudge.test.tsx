import {renderToStaticMarkup} from 'react-dom/server';
import {describe, expect, it} from 'vitest';
import {EvidenceJudgment} from './components/EvidenceJudgment';
import {settingsUpdate, type ConfigPayload} from './components/SettingsPage';
import {initialView, reduce} from './reducer';

describe('Jev 证据状态',()=>{
 it('已显示的卡片收到判定后触发材料刷新，重复 SSE 不重复刷新',()=>{
  const event={type:'evidence_judged',seq:1,results:{c1:{status:'complete',relation:'insufficient'}}};
  const view=reduce(initialView(),event,100);
  expect(view.simple?.evidenceRevision).toBe(1);
  expect(reduce(view,event,101).simple?.evidenceRevision).toBe(1);
 });
 it.each([
  [undefined,'未核验'],
  [{status:'pending'},'核验中'],
  [{status:'timeout',error_code:'timeout'},'未核验'],
  [{status:'complete',relation:'supported',model:'jev-1.13.0'},'引文支持'],
  [{status:'complete',relation:'insufficient',model:'jev-1.13.0'},'依据不足'],
  [{status:'complete',relation:'contradicted',model:'jev-1.13.0'},'引文矛盾'],
 ] as const)('如实展示 %j', (judgment,label)=>{
  const html=renderToStaticMarkup(<EvidenceJudgment judgment={judgment} detail/>);
  expect(html).toContain(label);
  expect(html).not.toContain('事实已核实');
 });
 it('开关可保存、密钥配置摘要不回传',()=>{
  const body=settingsUpdate({evidence_judge:{enabled:true},evidence_judge_status:{key_configured:true}} as ConfigPayload);
  expect(body.evidence_judge).toEqual({enabled:true});
  expect(body).not.toHaveProperty('evidence_judge_status');
 });
});

it('重试成功仍展示前次未知消耗和尝试记录',()=>{
 const html=renderToStaticMarkup(<EvidenceJudgment detail judgment={{status:'complete',relation:'supported',attempted:true,elapsed_ms:1200,
  usage:{input_tokens:120,output_tokens:0},usage_unknown:true,
  attempts:[{number:1,status:'error',error_code:'connect_error',elapsed_ms:20},
   {number:2,status:'complete',elapsed_ms:300}]}}/>);
 expect(html).toContain('请求 2 次');
 expect(html).toContain('另有请求用量未知');
 expect(html).toContain('连接建立失败');
 expect(html).toContain('核验请求记录');
});

it('排队超时显示尚未发送请求',()=>{
 const html=renderToStaticMarkup(<EvidenceJudgment detail judgment={{status:'timeout',error_code:'queue_timeout',attempted:false,elapsed_ms:300,queue_ms:300}}/>);
 expect(html).toContain('排队超时');
 expect(html).toContain('未发送请求');
 expect(html).not.toContain('用量未知');
});
