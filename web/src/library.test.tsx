import { renderToStaticMarkup } from 'react-dom/server';
import { it, expect } from 'vitest';
import { buildRunIndex,toolFacts } from './components/conversationHelpers';
import { BlockedSites, LibraryButton, LibraryManager, SourceModeOptions } from './components/LibraryPanel';
import { sourceLabel } from './format';
import { initialView } from './reducer';

it('来源选择清楚标明范围，没选资料时提醒', () => {
  const value={source_mode:'library' as const,document_ids:[],library_rerank:false};
  const html=renderToStaticMarkup(<SourceModeOptions value={value} onChange={()=>{}}/>);
  for (const text of ['仅网页','仅我的资料','网页 + 我的资料','还没有选资料']) expect(html).toContain(text);
  expect(sourceLabel({...value,source_mode:'web',mcp_server_ids:['a']})).toBe('网页 · 外部资料 1');
  expect(sourceLabel({...value,mcp_server_ids:['a']})).toBe('仅我的资料');
});

it('网站黑名单显示已填的网站，并在来源按钮上标出数量', () => {
  const html=renderToStaticMarkup(<BlockedSites value={['example.com','mdpi.com/2227-9709']} onChange={()=>{}}/>);
  for (const text of ['不使用的网站','example.com\nmdpi.com/2227-9709','子域名']) expect(html).toContain(text);
  const web={source_mode:'web' as const,document_ids:[],library_rerank:false};
  expect(sourceLabel({...web,blocked_sites:['a.com','b.com']})).toBe('网页 · 屏蔽 2 个网站');
  expect(sourceLabel({...web,mcp_server_ids:['x'],blocked_sites:['a.com']})).toBe('网页 · 外部资料 1 · 屏蔽 1 个网站');
  expect(sourceLabel({...web,source_mode:'library',blocked_sites:['a.com']})).toBe('仅我的资料');
});

it('资料弹窗说明数据去向，列表为空时给出示例入口', () => {
  const value={source_mode:'web' as const,document_ids:['d1'],library_rerank:false};
  expect(renderToStaticMarkup(<LibraryButton value={value} onChange={()=>{}}/>)).toContain('我的资料');
  const html=renderToStaticMarkup(<LibraryManager value={value} onChange={()=>{}}/>);
  for (const text of ['上传文件','导入示例资料','还没有资料']) expect(html).toContain(text);
});

it('本地检索尚未收到来源状态事件时仍展示命中文件', () => {
  const facts=toolFacts({tool:'search_sources',args:'{"query":"保留多久"}',phase:'done',ok:true,
    display:{results:[{source_ref:'s1',url:'',title:'规则.pdf',snippet:'17 days',document:{file_name:'规则.pdf'}}],total:1,
      warnings:['网页检索失败，本次仅有资料结果']}} as any,buildRunIndex(initialView()));
  expect(facts.lines[0].text).toBe('规则.pdf');
  expect(facts.lines[0].note).toContain('本地资料');
  expect(facts.warn).toContain('网页检索失败');
});
