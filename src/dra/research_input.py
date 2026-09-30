"""运行级用户收件箱。HTTP 投递，研究线程领取；与全局取消信号独立。"""
from __future__ import annotations

import copy
import re
import threading
import time

from dra.events import EventType
from dra.llm import LLMPersistenceError


class ResearchInput:
    """先持久化再确认；接收与交稿封口在同一把锁内排序。

    persist 可以取得 Web 发布锁，但调用方不能反向持有发布锁再调用这里。
    pending 是无锁读取的等待信号，避免子任务 Condition 与此锁相互嵌套。
    """

    def __init__(self, run_id, persist, *, stopped=None):
        self.run_id, self.persist = run_id, persist
        self.stopped = stopped or (lambda: False)
        self.lock = threading.Lock()
        self.pending = threading.Event()
        self.state = 'open'
        self.messages = []
        self.revision = 0
        self.event_seq = 0
        self.fatal = None

    def _publish(self, kind, **fields):
        event = {'type': EventType.RESEARCH_INPUT.value, 'run_id': self.run_id,
                 'event_id': f'input:{self.run_id}:{self.event_seq + 1}',
                 'ts': time.time(), 'kind': kind, **copy.deepcopy(fields)}
        try:
            self.persist(event)
        except Exception as exc:
            self.fatal = LLMPersistenceError('用户消息保存失败，不能确认接收或继续交稿')
            raise self.fatal from exc
        self.event_seq += 1

    def check(self):
        if self.fatal:
            raise self.fatal

    def announce(self):
        with self.lock:
            self.check()
            self._publish('state', state=self.state)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy({'state': self.state, 'revision': self.revision,
                                  'messages': self.messages})

    def accept(self, message_id, text):
        with self.lock:
            self.check()
            if not isinstance(message_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', message_id):
                raise ValueError('消息 ID 格式不正确')
            if not isinstance(text, str) or not text.strip() or len(text) > 6000:
                raise ValueError('消息须为 1–6000 字符的非空文字')
            for message in self.messages:
                if message['message_id'] == message_id:
                    if message['text'] != text:
                        raise ValueError('重复消息 ID 对应的正文不同')
                    return {'accepted': True, 'message': copy.deepcopy(message), 'state': self.state}
            if self.state != 'open' or self.stopped():
                raise ValueError('研究正在收尾或已结束，未接收新消息')
            # 有界保留原话，不用截断来伪装完整接收。
            if sum(len(m['text']) for m in self.messages) + len(text) > 60000:
                raise ValueError('本次研究的补充消息已达 60000 字符上限')
            message = {'message_id': message_id, 'text': text, 'revision': self.revision + 1,
                       'status': 'queued', 'at': time.time()}
            self._publish('accepted', message=message, state='open')
            self.revision += 1
            self.messages.append(message)
            self.pending.set()
            return {'accepted': True, 'message': copy.deepcopy(message), 'state': self.state}

    def begin_request(self, call_id, *, close=False):
        """在模型调用边界领取消息；close 用于最后一次预算收尾。"""
        with self.lock:
            self.check()
            rows = [{**m, 'status': 'included', 'call_id': call_id}
                    for m in self.messages if m['status'] == 'queued']
            state = 'closing' if close else self.state
            if rows or state != self.state:
                self._publish('included', messages=rows, state=state, call_id=call_id,
                              revision=self.revision)
            by_id = {m['message_id']: m for m in rows}
            self.messages = [by_id.get(m['message_id'], m) for m in self.messages]
            self.state = state
            self.pending.clear()
            return copy.deepcopy(rows), self.revision

    def seal(self, revision):
        with self.lock:
            self.check()
            if self.revision != revision or self.pending.is_set():
                return False
            if self.state == 'open':
                self._publish('state', state='closing')
                self.state = 'closing'
            return True

    def stop_receiving(self):
        """研究循环结束后关闭入口；已接收内容仍留给有额度的收尾请求。"""
        with self.lock:
            self.check()
            if self.state == 'open':
                self._publish('state', state='closing')
                self.state = 'closing'

    def close(self, reason):
        with self.lock:
            if self.state == 'closed':
                return
            rows = [{**m, 'status': 'undelivered', 'reason': reason}
                    for m in self.messages if m['status'] == 'queued']
            if self.fatal is None:
                self._publish('closed', messages=rows, state='closed', reason=reason)
            by_id = {m['message_id']: m for m in rows}
            self.messages = [by_id.get(m['message_id'], m) for m in self.messages]
            self.state = 'closed'
            self.pending.clear()


def replay_input(rows):
    """无存活线程时的只读投影；不会恢复执行或制造已投递事件。"""
    messages = {}
    for event in rows:
        if event.get('type') != EventType.RESEARCH_INPUT.value:
            continue
        updates = [event['message']] if 'message' in event else event.get('messages', [])
        for row in updates:
            messages[row['message_id']] = row
    ordered = sorted(messages.values(), key=lambda m: m['revision'])
    return {'state': 'closed', 'revision': max((m['revision'] for m in ordered), default=0),
            'messages': [{**m, 'status': 'undelivered', 'reason': '运行已结束，消息未进入主线请求'}
                         if m['status'] == 'queued' else m for m in ordered]}
