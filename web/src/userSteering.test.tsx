import { renderToStaticMarkup } from 'react-dom/server';
import { expect, it } from 'vitest';
import { ConversationWorkspace as ResearchWorkspace } from "./components/ConversationWorkspace";
import { ResearchComposer } from './components/ResearchComposer';
import { activityState } from './components/conversationHelpers';
import { initialView, mergeResearchInput, mergeSimpleResult, reduce } from './reducer';

const message = { message_id: 'u1', text: '只查齿科，价格不用比较', revision: 1, status: 'queued' as const, at: 1 };

it('HTTP 回执与 SSE 无论先后都只有一条用户消息，迟到回执不降级状态', () => {
  let v = reduce(initialView(), { type: 'research_input', kind: 'included', state: 'open',
    messages: [{ ...message, status: 'included', call_id: 'lead-2' }] }, 2000);
  v = mergeResearchInput(v, { state: 'open', messages: [message] });
  v = reduce(v, { type: 'research_input', kind: 'accepted', state: 'open', message }, 1000);
  expect(v.userMessages).toHaveLength(1);
  expect(v.userMessages?.[0].status).toBe('included');
  expect(v.timeline.filter(i => i.t === 'user')).toHaveLength(1);
  v = mergeResearchInput(v, { state: 'closed', messages: [] });
  v = mergeResearchInput(v, { state: 'open', messages: [message] });
  expect(v.inputState).toBe('closed');
});

it('实时与刷新保留用户输入、有效目标、范围变化及最终覆盖', () => {
  const scope = { revision: 1, goal: '齿科面扫软件兼容', requirements: [{ id: 'software', text: '软件兼容', kind: 'goal' }],
    user_message_ids: ['u1'], reason: '用户撤销价格比较', at: 2 };
  let v = reduce(initialView(), { type: 'research_input', kind: 'accepted', state: 'open', message }, 1000);
  v = reduce(v, { type: 'research_plan_updated', items: [{ id: 'price', text: '查价格', status: 'dropped' }], scope }, 2000);
  const refreshed = mergeSimpleResult(v, { effective_query: scope.goal, scope_history: [scope],
    requirements: scope.requirements, research_coverage: { software: 'partial' } }, null);
  for (const view of [v, refreshed]) {
    const html = renderToStaticMarkup(<ResearchWorkspace view={view} runId='r' />);
    expect(html).toContain(message.text);
    expect(html).toContain('齿科面扫软件兼容');
    expect(html).toContain('用户撤销价格比较');
    expect(view.timeline.filter(i => i.t === 'user')).toHaveLength(1);
  }
});

it('历史/收尾禁用输入，运行中显示真实的处理边界', () => {
  const send = async () => {};
  const live = renderToStaticMarkup(<ResearchComposer canSend onSend={send} state='open' />);
  expect(live).toContain('补充要求，或调整研究方向');
  expect(live).toContain('当前整批工具');
  const closing = renderToStaticMarkup(<ResearchComposer canSend={false} onSend={send} state='closing' />);
  expect(closing).toContain('正在收尾');
  expect(closing).toMatch(/textarea[^>]*disabled/);
});

it('异常结束的待处理消息明确显示未处理，已有投递记录不抹掉', () => {
  let v = mergeResearchInput(initialView(), { state: 'open', messages: [message,
    { ...message, message_id: 'u2', revision: 2, status: 'included' }] });
  v = reduce(v, { type: 'end' }, 3000);
  expect(v.inputState).toBe('closed');
  expect(v.userMessages?.map(m => m.status)).toEqual(['undelivered', 'included']);
});

it('新输入拦下旧交稿后继续研究，不显示已交稿正在保存', () => {
  const v = reduce(initialView(), { type: 'subagent_tool_call', mode: 'simple', tool: 'finish_research',
    call_id: 'old-finish', call_no: 1, phase: 'done', ok: false,
    error: '有新的用户消息到达' }, 1000);
  expect(activityState(v)?.text).not.toContain('已提交答案');
});

it('HTTP 回执先到也按原时刻展示，损坏消息日志不冒充缺失全部过程', () => {
  const v = mergeResearchInput(initialView(), {state:'open',messages:[{...message,at:2}],
    error:'部分消息日志损坏'});
  v.simpleTools = {earlier:{callNo:1,tool:'search_sources',ok:true,phase:'done',status:'ok',
    args:'{"query":"更早的检索"}',result:'',atMs:1000}};
  v.timeline.push({t:'tool',key:'earlier',atMs:1000});
  const html = renderToStaticMarkup(<ResearchWorkspace view={v} runId='r'/>);
  expect(html).toContain('更早的检索');
  expect(html.indexOf('更早的检索')).toBeLessThan(html.indexOf(message.text));
  expect(html).toContain('部分消息日志损坏');
  expect(html).not.toContain('不重放过程');
});
