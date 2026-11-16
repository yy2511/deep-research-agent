/** Offline visual fixture. All research, evidence and source text below are synthetic.
 * No model, search, real preview data or backend requests are used.
 * Serve with the existing QA Vite config; this is not a product entry point.
 */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { Brand } from '../src/components/Brand';
import { ConversationWorkspace } from '../src/components/ConversationWorkspace';
import { initialView } from '../src/reducer';
import type { SimpleCard, SimpleSource } from '../src/types';
import '../src/styles.css';
import '../src/theme.css';
import '../src/workspace-theme.css';
import '../src/settings-theme.css';
import '../src/management-theme.css';

const runId = 'synthetic-research-workspace';
const query = '如何评估社区图书馆延时开放试点？';
const sourceTexts = [
  '社区图书馆服务试点方案（合成资料）\n\n一、试点目标\n本资料仅用于界面验收，不对应任何真实机构或研究结论。试点将围绕居民使用需求、场馆运行条件和人员保障开展观察，优先选择交通方便、现有设备完善的社区图书馆。\n\n二、开放安排\n试点馆拟在每周五和周六延长开放至 21:00，连续观察八周。延长时段保留图书借还、自习和咨询服务，活动室预约另行记录。试点期间应在入口公示服务时间，并记录到馆人数、座位使用和安全巡查情况。\n\n三、复盘原则\n工作人员每周汇总运行记录，发现异常时说明具体时间、影响范围与处理方式。八周结束后，对使用需求与运行负担共同进行评估，不将单一的人流数字作为唯一依据。',
  '居民需求访谈纪要（合成资料）\n\n资料说明\n以下访谈内容和人数均为人工编写的验收样例，不代表实际调查结果。样例用于观察中文长段落、证据引文和来源信息的排版。\n\n需求记录\n受访者主要提到下班后自习、亲子阅读和周末借还书三个场景。部分居民希望保留安静阅读区域，也有人关注晚间离馆后的公交接驳。访谈没有采用随机抽样，不能直接推算整个社区的需求比例。\n\n后续工作\n试点前应补充工作日与周末分时段观察，并分别记录首次到访者和重复到访者。对于未能参与访谈的老年读者，应提供线下反馈渠道，以避免只听到活跃使用者的意见。',
  '延时服务评估清单（合成资料）\n\n记录口径\n本清单是离线界面测试数据。实际项目需要由相关机构确认指标、采集方式和个人信息保护安排。\n\n指标建议\n评估应同时记录新增到馆人次、晚间座位使用率、人员加班时长和安全事件。新增人次应与同馆、同星期的原开放时段进行比较，避免将节假日或临时活动带来的波动误认为延时服务效果。\n\n决策建议\n若新增使用与运营投入不匹配，应先调整开放日期或服务范围，再决定是否扩大试点。最终复盘应同时展示原始记录、统计口径和仍未解释的差异，保留对试点方案作出修订的空间。',
];
const quotes = [
  '试点馆拟在每周五和周六延长开放至 21:00，连续观察八周。',
  '受访者主要提到下班后自习、亲子阅读和周末借还书三个场景。',
  '评估应同时记录新增到馆人次、晚间座位使用率、人员加班时长和安全事件。',
];
const titles = ['社区图书馆服务试点方案（合成）', '居民需求访谈纪要（合成）', '延时服务评估清单（合成）'];
const claims = ['先用八周试点观察周五、周六晚间开放需求', '区分自习、亲子阅读和借还书等使用场景', '把服务使用与运营投入放在一起评估'];
const sources: SimpleSource[] = sourceTexts.map((text, i) => ({
  ref: `s${i + 1}`, url: `https://synthetic-${i + 1}.example.test/library-pilot`, title: titles[i],
  snapshot_kind: 'text', raw_chars: Array.from(text).length, published_at: '2026-11-01', fetched_at: 1793577600,
}));
const cards: SimpleCard[] = sourceTexts.map((text, i) => {
  const start = Array.from(text.slice(0, text.indexOf(quotes[i]))).length;
  return { card_id: `c${i + 1}`, claim: claims[i], quote: quotes[i], source_ref: sources[i].ref,
    source_url: sources[i].url, source_title: sources[i].title,
    location: `chars=${start}-${start + Array.from(quotes[i]).length}` };
});
const view = initialView();
view.startedAtMs = 1793577600000;
view.endedAtMs = view.startedAtMs + 83000;
view.simple = { mode: 'simple', phase: 'finished', delivery: 'delivered', stopReason: 'finished', query, cards, sources, cardCount: cards.length,
  draft: `## 先做小范围试点，再决定是否扩大\n\n建议以八周为一个观察周期，先验证居民是否会在新增时段持续使用图书馆，再结合人员投入决定后续安排。样例方案选择周五、周六开放至 21:00，为评估提供清晰的时间范围。[c1]\n\n### 观察三个具体问题\n\n1. **谁在使用？** 分别记录自习、亲子阅读和借还书等场景，避免只看总人次。访谈中的需求可以用来设计观察表，但不能直接当作整个社区的需求比例。[c2]\n2. **新增服务是否值得持续？** 同时看晚间座位使用与人员加班，建立固定口径，每周复盘一次。[c3]\n3. **什么情况下需要调整？** 若某个日期使用偏低，先调整开放日期或服务范围，再比较变化。\n\n### 建议的复盘表\n\n| 观察维度 | 每周记录 | 决策用途 |\n| --- | --- | --- |\n| 服务使用 | 新增到馆人次、座位使用率 | 判断是否存在持续需求 |\n| 运营投入 | 加班时长、安全事件 | 评估运行条件 |\n| 读者反馈 | 使用场景、未满足的需求 | 改进开放安排 |\n\n**还需要确认：** 实际人流基线、值班安排和试点预算。以上均为合成内容，仅用于验证报告阅读、证据切换与原文定位界面。`,
};
const meta = { run_id: runId, conversation_id: runId, query, turn_index: 1, status: 'done', finished_at: 1793577683 };

// Every fetch is handled in memory. Unknown requests and all mutations fail closed.
window.fetch = async (input, init) => {
  const request = input instanceof Request ? input : null;
  const url = new URL(request?.url ?? String(input), location.origin);
  if (url.origin !== location.origin || (init?.method ?? request?.method ?? 'GET').toUpperCase() !== 'GET') {
    throw new Error('离线验收夹具禁止网络写入和外部请求');
  }
  const path = url.pathname;
  let body: unknown;
  if (path === `/api/runs/${runId}`) body = { meta };
  else if (path === `/api/conversations/${runId}`) body = { turns: [meta], latest_run_id: runId, next_cursor: null };
  else if (path === `/api/runs/${runId}/materials`) body = { cards, sources };
  else if (path === `/api/runs/${runId}/skills`) body = { loaded: [], catalog_recorded: false };
  else {
    const index = sources.findIndex(source => path === `/api/runs/${runId}/sources/${source.ref}`);
    if (index < 0) throw new Error(`未模拟的离线请求：${path}`);
    return new Response(sourceTexts[index], { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
  }
  return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
};

createRoot(document.getElementById('root')!).render(<StrictMode>
  <div style={{ height: '100dvh', display: 'flex', flexDirection: 'column', minWidth: 0 }}>
    <header style={{ display: 'flex', alignItems: 'center', gap: 16, flexWrap: 'wrap', flex: 'none', padding: '12px 18px', borderBottom: '1px solid var(--line)', background: 'var(--surface)' }}>
      <Brand /><span style={{ color: 'var(--faint)', fontSize: 12 }}>合成研究样例 · 离线视觉验收</span>
    </header>
    <ConversationWorkspace view={view} runId={runId} readOnly />
  </div>
</StrictMode>);
