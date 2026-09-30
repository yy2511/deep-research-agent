import { useEffect, useRef, useState } from 'react';
import type { ResearchInputState, ResearchUserMessage } from '../types';
import { Icon } from './Icon';

const drafts = new Map<string,{text:string;attempt:{id:string;text:string}|null}>();

export function ResearchComposer({ canSend, state, messages = [], onSend, purpose="steer", draftKey }: {
  draftKey?: string; purpose?: "steer"|"followup";
  canSend: boolean; state?: ResearchInputState; messages?: ResearchUserMessage[];
  onSend: (messageId: string, text: string) => Promise<void>;
}) {
  const [text, setText] = useState(draftKey ? drafts.get(draftKey)?.text ?? '' : '');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');
  const alive = useRef(true);
  const composing = useRef(false);
  const busy = useRef(false);
  const attempt = useRef<{ id: string; text: string } | null>(draftKey ? drafts.get(draftKey)?.attempt ?? null : null);
  useEffect(()=>{if(draftKey)drafts.set(draftKey,{text,attempt:attempt.current})},[text,draftKey,sending]);
  const known = useRef(messages);
  known.current = messages;
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    const pending = attempt.current;
    if (!sending && pending && messages.some(m => m.message_id === pending.id)) {
      setText(current => current === pending.text ? '' : current);
      setError('');
      attempt.current = null;
    }
  }, [messages, sending]);

  async function send() {
    if (!canSend || busy.current || !text.trim()) return;
    const payload = attempt.current?.text === text ? attempt.current : { id: crypto.randomUUID(), text };
    attempt.current = payload;
    busy.current = true;
    setSending(true); setError('');
    try {
      await onSend(payload.id, payload.text);
      // 跳到新轮次会先卸载旧输入框；成功回执仍要清掉旧轮已发送的草稿。
      if (draftKey && drafts.get(draftKey)?.text === payload.text) {
        drafts.set(draftKey, {text: '', attempt: null});
      }
      if (!alive.current) return;
      setText(current => current === payload.text ? '' : current);
      attempt.current = null;
    } catch (cause) {
      if (!alive.current) return;
      // 请求超时但 SSE 已确认接收时，不再提示失败或重复发送。
      if (known.current.some(m => m.message_id === payload.id)) {
        setText(current => current === payload.text ? '' : current);
        attempt.current = null;
      } else {
        setError(cause instanceof Error ? cause.message : '发送失败，请重试');
      }
    } finally {
      busy.current = false;
      if (alive.current) setSending(false);
    }
  }
  const hint = purpose==='followup'&&canSend ? '继续提问会开始新一轮研究，前面的回答和证据都会保留。' : state === 'closing' ? '正在收尾，暂不接收新要求'
    : !canSend && state === 'open' ? '暂时无法发送，正在等待连接恢复'
    : !canSend ? '这一轮已经结束或正在回放，不能在这里继续发送'
    : '当前整批工具执行完后，主 Agent 会处理你的补充。';
  return <form className='research-composer' aria-label={purpose==='followup'?'继续研究':'补充研究要求'} onSubmit={e => { e.preventDefault(); void send(); }}>
    <div className='composer composer-inline'>
      <label className='sr-only' htmlFor='research-message'>{purpose==='followup'?'继续提问':'补充研究要求'}</label>
      <textarea id='research-message' className='composer-input' value={text} maxLength={6000} rows={1} disabled={!canSend}
        placeholder={purpose==='followup'?'继续追问，或换一个角度…':'补充要求，或调整研究方向…'} onChange={e => { setText(e.target.value); setError(''); }}
        onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }}
        onKeyDown={e => {
          if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && !composing.current && e.keyCode !== 229) {
            e.preventDefault(); void send();
          }
        }} />
      <button type='submit' className='btn btn-primary btn-icon' aria-label={sending ? '发送中' : '发送'} disabled={!canSend || sending || !text.trim()}>
        {sending ? <Icon name='loading' className='icon-spin' size={16}/> : <Icon name='arrow-up' size={16}/>}
      </button>
    </div>
    <small className='composer-note' aria-live='polite'>{sending ? '正在发送…' : hint}</small>
    {error && <p className='field-hint is-bad' role='alert'>{error}，文字已保留。</p>}
  </form>;
}
