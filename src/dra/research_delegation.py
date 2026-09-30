"""Run-owned background research. Workers publish records, never mutate model messages."""
from __future__ import annotations

import contextvars
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from dra.llm import LLMPersistenceError

ACTIVE = {'queued', 'running', 'cancel_requested'}


class CombinedCancel:
    def __init__(self, *signals):
        self.signals = [s for s in signals if s is not None]

    def is_set(self):
        return any(s.is_set() for s in self.signals)


class DelegationManager:
    def __init__(self, runner, persist, *, workers=3, queue_limit=3, cancel=None, deadline=None):
        self.runner, self.persist = runner, persist
        self.limit = workers + queue_limit
        self.parent_cancel, self.deadline = cancel, deadline
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='research-child')
        self.cv = threading.Condition(threading.RLock())
        self.tasks, self.batches, self.signals, self.inboxes = {}, {}, {}, {'parent': []}
        self.closed = False
        self.fatal = None
        self.seq = 0

    def _publish(self, kind, task=None, **payload):
        self.seq += 1
        event = {'kind': kind, 'message_id': f'dm{self.seq}', 'at': time.time(), **payload}
        if task is not None:
            event['task'] = copy.deepcopy(task)
        try:
            self.persist(event)
        except BaseException as exc:
            self.fatal = exc
            for signal in self.signals.values():
                signal.set()
            self.cv.notify_all()
            raise
        self.cv.notify_all()
        return event

    def check(self):
        if self.fatal is not None:
            raise self.fatal

    def stopped(self):
        return ((self.parent_cancel is not None and self.parent_cancel.is_set())
                or (self.deadline is not None and time.monotonic() >= self.deadline))

    def submit(self, tasks, call_id):
        with self.cv:
            self.check()
            if call_id in self.batches:
                return copy.deepcopy(self.batches[call_id])
            if self.closed or self.stopped():
                raise ValueError('research_closed')
            if len(tasks) + sum(t['status'] in ACTIVE for t in self.tasks.values()) > self.limit:
                raise ValueError('capacity: too many active/queued research tasks; wait for results')
            rows=[]
            for goal, context in tasks:
                task_id=f'sub{len(self.tasks)+1}'
                task={'id':task_id,'delegation_id':task_id,'parent_call_id':call_id,
                      'goal':goal,'context':context,'status':'queued','accepts_messages':True,'created_at':time.time()}
                self.signals[task_id]=threading.Event()
                self.inboxes[task_id]=[]
                self.tasks[task_id]=task
                self._publish('state',task)
                rows.append(copy.deepcopy(task))
            self.batches[call_id]=rows
            for row in rows:
                ctx=contextvars.copy_context()
                self.pool.submit(ctx.run,self._run,row['id'])
            return copy.deepcopy(rows)

    def _run(self, task_id):
        with self.cv:
            task=self.tasks[task_id]
            if self.fatal is not None or task['status'] not in ACTIVE:
                return
            if self.stopped() or self.signals[task_id].is_set():
                self._settle(task, {}, 'timed_out' if self.deadline is not None and time.monotonic() >= self.deadline else 'cancelled')
                return
            task.update(status='running',started_at=time.time())
            self._publish('state',task)
        cancel=CombinedCancel(self.parent_cancel,self.signals[task_id])
        try:
            result=self.runner(copy.deepcopy(task),cancel)
            expired = self.deadline is not None and time.monotonic() >= self.deadline
            user_cancelled = ((self.parent_cancel is not None and self.parent_cancel.is_set())
                              or task['status'] == 'cancel_requested')
            if user_cancelled:
                status = 'cancelled'
            elif expired or result.get('status', '').startswith(('deadline', 'request_timeout')):
                status = 'timed_out'
            elif cancel.is_set() or result.get('status') == 'cancelled':
                status = 'cancelled'
            elif result.get('status', '').startswith(('failed', 'protocol_error')):
                status = 'failed'
            else:
                status = 'completed'

        except LLMPersistenceError as exc:
            with self.cv:
                self.fatal=exc
                for signal in self.signals.values(): signal.set()
                self.cv.notify_all()
            return
        except Exception as exc:
            result={'findings':'','gaps':[],'status':'failed',
                    'error':{'stage':'execution','type':type(exc).__name__,'message':str(exc)[:2000]}}
            status='cancelled' if cancel.is_set() else 'failed'
        with self.cv:
            if self.fatal is None:
                self._settle(task,result,status)

    def _settle(self,task,result,status):
        if task['status'] not in ACTIVE:
            return
        result = {'findings':'', 'gaps':[], 'card_ids':[], 'summary_available':False, 'error':None, **result}
        task.update(status=status,accepts_messages=False,ended_at=time.time(),result=result)
        pending = self.inboxes[task['id']]
        if pending:
            task['undelivered_messages'] = [m['message_id'] for m in pending]
        event=self._publish('result',task,result=result,sender=task['id'],target='parent')
        self.inboxes['parent'].append(event)
        self.cv.notify_all()

    def inspect(self,task_id=None):
        with self.cv:
            self.check()
            if task_id is not None and task_id not in self.tasks:
                raise ValueError('unknown_task')
            return copy.deepcopy([self.tasks[task_id]] if task_id else list(self.tasks.values()))

    def active(self):
        return [t for t in self.inspect() if t['status'] in ACTIVE]

    def send(self,sender,target,message):
        with self.cv:
            self.check()
            if not isinstance(message,str) or not message.strip() or len(message)>6000:
                raise ValueError('message must contain 1–6000 characters')
            child=target if sender=='parent' else sender
            if (sender!='parent' and target!='parent') or child not in self.tasks:
                raise ValueError('only parent/direct child messages are allowed')
            if (self.closed or self.stopped() or self.tasks[child]['status'] not in {'queued','running'}
                    or not self.tasks[child]['accepts_messages']):
                raise ValueError('task_closed')
            event=self._publish('message',self.tasks[child],sender=sender,target=target,message=message)
            self.inboxes[target].append(event)
            self.cv.notify_all()
            return {'accepted':True,'message_id':event['message_id'],'delivery':'next_model_request'}

    def seal(self, task_id):
        """Atomically stop message admission only if no unconsumed message remains."""
        with self.cv:
            self.check()
            if self.inboxes[task_id]:
                return False
            self.tasks[task_id]['accepts_messages'] = False
            return True

    def drain(self,target):
        with self.cv:
            self.check()
            result=copy.deepcopy(self.inboxes.get(target,[])[:20])
            if result:
                self._publish('delivered',target=target,message_ids=[m['message_id'] for m in result])
                del self.inboxes[target][:len(result)]
            return result

    def ready_to_finish(self, target):
        with self.cv:
            self.check()
            return not self.inboxes.get(target) and (target != 'parent' or
                not any(t['status'] in ACTIVE for t in self.tasks.values()))

    def wait(self,task_ids=None,timeout=30,*,ignore_inbox=False,wakeup=None):
        timeout=max(0,min(30,float(timeout)))
        end=time.monotonic()+timeout
        with self.cv:
            self.check()
            if task_ids and any(i not in self.tasks for i in task_ids):
                raise ValueError('unknown_task')
            version=self.seq
            while not self.stopped() and time.monotonic()<end:
                if wakeup is not None and wakeup.is_set(): break
                wanted=[t for t in self.tasks.values() if not task_ids or t['id'] in task_ids]
                if not any(t['status'] in ACTIVE for t in wanted): break
                if not ignore_inbox and any(not task_ids or m.get('sender') in task_ids for m in self.inboxes['parent']): break
                if self.seq!=version: break
                self.cv.wait(min(.1,max(0,end-time.monotonic())))
                self.check()
            return self.inspect()

    def cancel(self,task_id,reason):
        with self.cv:
            self.check()
            if task_id not in self.tasks: raise ValueError('unknown_task')
            task=self.tasks[task_id]
            if task['status'] in ACTIVE:
                self.signals[task_id].set()
                task['cancel_reason']=str(reason)
                if task['status']=='queued': self._settle(task,{},'cancelled')
                else:
                    task['status']='cancel_requested'
                    self._publish('state',task)
            return copy.deepcopy(task)

    def close(self,cancel=True):
        with self.cv:
            self.closed=True
            if cancel:
                for task_id in self.tasks:
                    self.signals[task_id].set()
            self.cv.notify_all()
        self.pool.shutdown(wait=True)
        self.check()
