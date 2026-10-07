import {describe, it, expect} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {RunSkillsView, SkillChoices, SkillSnapshot, runSkillsState} from './components/SkillsPanel';
import {initialView, reduce} from './reducer';

describe('research skills', () => {
  it('shows enabled methods with explicit automatic/default semantics', () => {
    const html=renderToStaticMarkup(<SkillChoices skills={[{name:'compare',title:'方案对比',description:'Compare.',enabled:true},{name:'disabled',description:'Hidden',enabled:false}]} selected={['compare']} onChange={()=>{}}/>);
    expect(html).toContain('方案对比');
    expect(html).toContain('checked');
    expect(html).not.toContain('Hidden');
    expect(html).toContain('自动');
  });
  it('renders historical text as text and distinguishes actor', () => {
    const html=renderToStaticMarkup(<SkillSnapshot loaded={[{name:'compare',title:'方案对比',hash:'abcdef',agent_id:'sub1',content:'<script>old</script>',source:'automatic'}]}/>);
    expect(html).toContain('sub1');
    expect(html).toContain('&lt;script&gt;');
    expect(html).not.toContain('<script>');
    expect(html).toContain('已加载');
  });
  it('retains activation identities in replay', () => {
    const v=reduce(initialView(),{type:'skill_loaded',name:'compare',agent_id:'sub1',hash:'abc',ts:1},1000);
    expect(v.timeline.some(i=>i.t==='note'&&i.card.kind==='skill_loaded'&&i.card.agent_id==='sub1')).toBe(true);
  });
});

describe('本轮技能状态', () => {
  const loaded = [{name:'compare',title:'方案对比',hash:'abcdef1234567890',agent_id:'lead',content:'body',source:'manual'},
    {name:'compare',title:'方案对比',hash:'abcdef1234567890',agent_id:'sub2',content:'ref',source:'automatic',path:'references/guide.md'}];
  it('loaded=[] 不隐藏，说明未加载并列出当时可用的技能', () => {
    const state=runSkillsState({catalog:{skills:{compare:{title:'方案对比'}},selected:[]},catalog_recorded:true,loaded:[]});
    expect(state.kind).toBe('none');
    const html=renderToStaticMarkup(<RunSkillsView state={state}/>);
    expect(html).toContain('本轮技能');
    expect(html).toContain('未加载');
    expect(html).toContain('当时可用 1 个：方案对比');
  });
  it('手选或目录启用不等于已加载', () => {
    const html=renderToStaticMarkup(<RunSkillsView state={runSkillsState({catalog:{skills:{compare:{title:'方案对比'}},selected:['compare']},catalog_recorded:true,loaded:[]})}/>);
    expect(html).toContain('未加载');
    expect(html).toContain('你手动指定了：方案对比，但没有加载记录');
    expect(html).not.toContain('已加载');
  });
  it('没有技能记录的运行如实说明，不冒充空目录', () => {
    const html=renderToStaticMarkup(<RunSkillsView state={runSkillsState({catalog:{skills:{},selected:[]},catalog_recorded:false,loaded:[]})}/>);
    expect(html).toContain('这一轮没有技能记录');
    expect(html).toContain('本轮技能 · <span class="run-skills-state">无记录</span>');
    expect(html).not.toContain('未加载');
  });
  it('旧接口没有 catalog_recorded：目录为空时无法确认，目录非空时才算未加载', () => {
    const unknown=renderToStaticMarkup(<RunSkillsView state={runSkillsState({catalog:{skills:{},selected:[]},loaded:[]})}/>);
    expect(unknown).toContain('无记录');
    expect(unknown).toContain('无法确认当时是否提供过技能');
    expect(unknown).not.toContain('未加载');
    const known=renderToStaticMarkup(<RunSkillsView state={runSkillsState({catalog:{skills:{compare:{title:'方案对比'}},selected:[]},loaded:[]})}/>);
    expect(known).toContain('未加载');
    expect(known).toContain('当时可用 1 个：方案对比');
  });
  it('读取中与接口错误分开显示，错误不当成空列表', () => {
    expect(renderToStaticMarkup(<RunSkillsView state={{kind:'loading'}}/>)).toContain('读取中');
    const html=renderToStaticMarkup(<RunSkillsView state={{kind:'error',message:'run 不存在'}} onRetry={()=>{}}/>);
    expect(html).toContain('读取失败');
    expect(html).toContain('run 不存在');
    expect(html).toContain('重新读取');
    expect(html).not.toContain('未加载');
  });
  it('已加载保留主/子 Agent、版本和参考文件', () => {
    const state=runSkillsState({catalog:{skills:{compare:{title:'方案对比'}},selected:['compare']},catalog_recorded:true,loaded});
    const html=renderToStaticMarkup(<RunSkillsView state={state}/>);
    expect(html).toContain('已加载 1 个，参考文件 1 份');
    expect(html).toContain('主 Agent');
    expect(html).toContain('子任务 sub2');
    expect(html).toContain('参考文件 references/guide.md');
    expect(html).toContain('版本 abcdef123456');
    expect(html).toContain('手动选择');
  });
});
