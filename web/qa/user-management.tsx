/** Offline component acceptance harness. All API responses are synthetic and in-memory. */
import { StrictMode, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { UserManagement } from '../src/components/UserManagement';
import { OwnerManagement } from '../src/components/OwnerManagement';
import type { ManagedUser } from '../src/userManagement';
import '../src/styles.css';
const roles = ['owner', 'admin', 'user', 'guest'] as const;
const names = ['合成负责人', '合成管理员', '合成普通用户', '合成访客'];
const users: ManagedUser[] = roles.map((role, i) => ({
 id: i + 1, email: `synthetic-${i + 1}@example.test`, display_name: names[i], role, status: 'active', management_version: 1,
 created_at: '2026-11-12T00:00:00Z', quota: role === 'user' ? { mode: 'limited', daily_runs_limit: '5', daily_briefs_limit: '10', version: 1 } : null,
 usage: { mode: role === 'guest' ? 'global' : role === 'user' ? 'limited' : 'unlimited', runs_used: 2, briefs_used: 1, runs_limit: role === 'user' ? '5' : role === 'guest' ? '3' : null, briefs_limit: role === 'user' ? '10' : null, runs_left: role === 'user' ? '3' : null, briefs_left: role === 'user' ? '9' : null, timezone: 'Asia/Shanghai', resets_at: '2026-11-13T00:00:00+08:00' },
}));
let nextStatus = 200; let writes = 0;
window.fetch = async (input, init) => {
 const url = new URL(String(input), location.origin); const path = url.pathname;
 if (!path.startsWith('/api/admin/')) throw new Error('验收环境禁止非模拟 API 请求');
 const isWrite = init?.method && init.method !== 'GET';
 await new Promise(resolve => setTimeout(resolve, 250));
 if (nextStatus !== 200) { const status = nextStatus; nextStatus = 200; return new Response(JSON.stringify({ detail: status === 409 ? '模拟版本冲突' : '模拟权限失效' }), { status }); }
 let data: unknown;
 if (isWrite) {
   writes++; document.querySelector('#writes')!.textContent = String(writes);
   const body = JSON.parse(String(init?.body ?? '{}'));
   const match = path.match(/^\/api\/admin\/users\/(\d+)\/(quota|status|role)$/);
   if (match) { const user = users.find(item => item.id === Number(match[1]))!;
     if (match[2] === 'quota') { user.quota = { ...body, version: (user.quota?.version ?? 0) + 1 }; user.usage!.runs_limit = body.daily_runs_limit; }
     else { Object.assign(user, match[2] === 'status' ? { status: body.status } : { role: body.role, ...(body.quota ? { quota: { ...body.quota, version: 1 } } : {}) }); user.management_version++; }
     data = user;
   } else if (path.endsWith('/verify')) data = { target_id: 2, expires_at: '2099-01-01T00:00:00Z' };
   else data = { ok: true };
 } else if (path === '/api/admin/users') { const q = url.searchParams.get('q') ?? ''; data = { items: users.filter(u => (!q || `${u.email} ${u.display_name}`.includes(q)) && (!url.searchParams.get('role') || u.role === url.searchParams.get('role')) && (!url.searchParams.get('status') || u.status === url.searchParams.get('status'))), next_cursor: null, governance: { owner_user_id: 1, version: 1 } }; }
 else if (/\/users\/\d+$/.test(path)) data = users.find(u => u.id === Number(path.split('/').at(-1)));
 else if (path.endsWith('/governance')) data = { owner_user_id: 1, version: 1, reauth_methods: ['password'], reauth_ttl_seconds: 300 };
 else if (path.endsWith('/owner-transfer/status')) data = { ready: false };
 else if (path.endsWith('/quota-timezone')) data = { timezone: 'Asia/Shanghai', pending_timezone: null, effective_at: null, version: 1 };
 else if (path.endsWith('/audit-events')) data = { items: [{ id: 1, actor_id: 1, target_id: 3, action: 'quota_update', created_at: '2026-11-12T00:00:00Z', reason: '合成测试记录' }], next_cursor: null };
 return new Response(JSON.stringify(data ?? {}), { status: 200, headers: { 'Content-Type': 'application/json' } });
};
function Harness() {
 const [role, setRole] = useState<'owner' | 'admin'>('owner'); const [page, setPage] = useState('users');
 const account = { id: role === 'owner' ? 1 : 2, email: 'synthetic@example.test', role };
 return <main style={{ maxWidth: 1120, margin: '24px auto', padding: 16 }}>
 <header style={{ borderBottom: '1px solid #bbb', paddingBottom: 16, marginBottom: 24 }}><h1>用户管理验收 · 仅合成数据</h1><p>无真实账号、无后端连接。成功写入模拟次数：<span id="writes">0</span></p><div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
 <button className="btn" onClick={() => setRole(role === 'owner' ? 'admin' : 'owner')}>切换为{role === 'owner' ? '管理员' : '负责人'}</button>
 <button className="btn" onClick={() => setPage('users')}>用户管理</button><button className="btn" onClick={() => setPage('owner')}>负责人管理</button><button className="btn" onClick={() => setPage('away')}>离开页面</button>
 <button className="btn" onClick={() => { nextStatus = 409; }}>下次请求模拟409</button><button className="btn" onClick={() => { nextStatus = 403; }}>下次请求模拟403</button>
 </div></header>
 {page === 'users' ? <UserManagement key={role} account={account} onTrial={() => setPage('trial')} /> : page === 'owner' ? <OwnerManagement key={role} account={account} /> : <p>{page === 'trial' ? '平台通用试用配置（合成占位，不修改生产配置）' : '已离开管理页面。'}</p>}
 </main>;
}
createRoot(document.getElementById('root')!).render(<StrictMode><Harness /></StrictMode>);
