import {describe, it, expect} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {SkillChoices, SkillSnapshot} from './components/SkillsPanel';
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
