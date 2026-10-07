import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from dra import simple_research as sr


def exchange(i, text):
    return [{'role':'assistant','content':'read','tool_calls':[{'id':f't{i}','type':'function','function':{'name':'search_sources','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':f't{i}','content':text}]


def setup_context(tmp_path):
    cfg=sr.SimpleConfig(compaction_enabled=True, context_window_tokens=18000, pressure_reserve_tokens=0,
                        estimate_chars_per_token=1, archive_mode='keep')
    ctx=sr.ToolContext(sr.RunStore(tmp_path,run_id='test',query='original goal'),cfg)
    history=[exchange(i,'old search '*2000) for i in range(3)]+[exchange(3,'recent one'),exchange(4,'recent two')]
    base=[{'role':'system','content':'rules'},{'role':'user','content':'current goal'}]
    messages=sr._build_messages(base,ctx,[],history,seconds_left=None)
    budget=sr.Budget(cfg,None);budget.identity='lead'
    return ctx,history,base,messages,budget


def summary(*a,**kw):
    return SimpleNamespace(content=json.dumps({'findings':[],'decisions':['Excluded old candidate because version is obsolete'],
        'open_questions':['Confirm latest version'], 'next_steps':['Read remaining source']}),
        incomplete=False,tool_calls=[],input_tokens=100,output_tokens=50,attempts=1)


def test_compaction_requires_explicit_capacity():
    with pytest.raises(ValueError): sr.SimpleConfig(compaction_enabled=True)


def test_reduces_recent_tail_and_reuses_notes_instead_of_squeezing_again(tmp_path):
    from dra.context_compaction import compact_context
    cfg = sr.SimpleConfig(compaction_enabled=True, context_window_tokens=20000,
        pressure_reserve_tokens=0, estimate_chars_per_token=1, archive_mode='keep', max_llm_calls=4)
    ctx = sr.ToolContext(sr.RunStore(tmp_path, run_id='tail', query='goal'), cfg)
    history = [exchange(0, 'OLD_PREFIX ' + 'o' * 12000),
               exchange(1, 'RECENT_A ' + 'a' * 4000), exchange(2, 'RECENT_B ' + 'b' * 4000)]
    messages = sr._build_messages([{'role':'system','content':'rules'}, {'role':'user','content':'goal'}],
                                 ctx, [], history, seconds_left=None)
    requests = []
    def model(messages, **kwargs):
        requests.append(json.dumps(messages))
        return SimpleNamespace(content=json.dumps({'findings':[], 'decisions':['d' * 8500 if len(requests)==1 else 'extra'],
            'open_questions':[], 'next_steps':[]}), tool_calls=[], incomplete=False)
    budget = sr.Budget(cfg, None)
    result = compact_context(ctx, messages, [], budget, call_model=model)
    doc = json.loads((ctx.store.dir/'context'/'lead'/(ctx.compaction_checkpoints[0]+'.json')).read_text())
    assert doc['retained_rounds'] == 1 and len(requests) == 2
    assert 'OLD_PREFIX' in requests[0] and 'OLD_PREFIX' not in requests[1]
    assert 'RECENT_A' in requests[1]
    assert doc['target_reached'] is False  # soft target: no extra call just to reach 60%
    assert budget.remaining_calls() == 2
    assert result[-2:] == history[-1]


def test_single_map_plus_reserved_delivery_is_enough(tmp_path):
    from dra.context_compaction import compact_context
    ctx, h, base, _, _ = setup_context(tmp_path)
    ctx.config.max_llm_calls = 2
    h[:] = [exchange(1, 'one complete round ' * 750)]
    messages = sr._build_messages(base, ctx, [], h, seconds_left=None)
    # A fresh view is needed because this is a new independent fixture history.
    ctx.message_view = None
    messages = sr._build_messages(base, ctx, [], h, seconds_left=None)
    budget = sr.Budget(ctx.config, None)
    compact_context(ctx, messages, [], budget, call_model=summary, force=True)
    assert len(ctx.compaction_checkpoints) == 1 and budget.calls_used == 1
    assert budget.remaining_calls() == 1


def test_failed_attempt_notes_are_reused_when_history_grows(tmp_path):
    from dra.context_compaction import compact_context, ContextLimit
    ctx, _, base, _, budget = setup_context(tmp_path)
    ctx.message_view = None
    history = [exchange(0, 'PREFIX_ONE ' + 'a'*10000), exchange(1, 'PREFIX_TWO ' + 'b'*10000),
               exchange(2, 'recent 1'), exchange(3, 'recent 2')]
    messages = sr._build_messages(base, ctx, [], history, seconds_left=None)
    requests = []
    def model(messages, **kwargs):
        requests.append(json.dumps(messages))
        if len(requests) == 2:
            raise RuntimeError('summary service interrupted')
        return SimpleNamespace(content=json.dumps({'findings':[], 'decisions':['keep decision'],
            'open_questions':[], 'next_steps':[]}), tool_calls=[], incomplete=False)
    with pytest.raises(ContextLimit):
        compact_context(ctx, messages, [], budget, call_model=model)
    assert ctx.latest_work_notes['decisions'] == ['keep decision']
    history.append(exchange(4, 'new recent turn'))
    messages = sr._build_messages(base, ctx, [], history, seconds_left=None)
    compact_context(ctx, messages, [], budget, call_model=model)
    assert sum('PREFIX_ONE' in r for r in requests) == 1
    doc = json.loads((ctx.store.dir/'context'/'lead'/(ctx.compaction_checkpoints[0]+'.json')).read_text())
    assert any(note['reused'] for note in doc['notes'])


def test_compaction_keeps_tail_and_does_not_reinflate(tmp_path):
    from dra.context_compaction import compact_context
    ctx,h,base,m,b=setup_context(tmp_path); original=deepcopy(h)
    result=compact_context(ctx,m,[],b,call_model=summary)
    assert len(json.dumps(result))<len(json.dumps(m))/2
    pos=next(i for i,m in enumerate(result) if m.get('tool_calls')==h[-1][0]['tool_calls'])
    assert result[pos:pos+2]==h[-1]
    assert h==original
    next_request=sr._build_messages(base,ctx,[],h,seconds_left=None)
    assert next_request==result
    assert b.calls_used>=1 and ctx.tool_calls==0
    assert ctx.store.calls()[0].role=='compact'
    checkpoint=ctx.compaction_checkpoints[-1]
    body,ok=ctx.execute('read_context_history',{'checkpoint_id':checkpoint})
    assert ok and 'old search' in body and json.loads(body)['next_cursor'] is not None
    assert not ctx.execute('read_context_history',{'checkpoint_id':'../../secrets'})[1]


def test_failure_preserves_history_and_does_not_retry_same_boundary(tmp_path):
    from dra.context_compaction import compact_context,ContextLimit
    ctx,h,base,m,b=setup_context(tmp_path);before=deepcopy(ctx.message_view.chunks)
    def empty(*a,**kw):return SimpleNamespace(content='',incomplete=False,tool_calls=[])
    with pytest.raises(ContextLimit):compact_context(ctx,m,[],b,call_model=empty)
    assert ctx.message_view.chunks==before
    with pytest.raises(ContextLimit):compact_context(ctx,m,[],b,call_model=summary)
    assert b.calls_used==1


def test_bad_reference_is_not_committed(tmp_path):
    from dra.context_compaction import compact_context,ContextLimit
    ctx,h,base,m,b=setup_context(tmp_path)
    def bad(*a,**kw):
        t=summary();d=json.loads(t.content);d['findings']=[{'text':'fact','card_ids':['c999'],'source_refs':[]}];t.content=json.dumps(d);return t
    with pytest.raises(ContextLimit):compact_context(ctx,m,[],b,call_model=bad)
    assert not ctx.compaction_checkpoints


def test_new_messages_append_after_summary_and_second_compaction(tmp_path):
    from dra.context_compaction import compact_context
    ctx,h,base,m,b=setup_context(tmp_path)
    compact_context(ctx,m,[],b,call_model=summary)
    h.append([{'role':'user','content':'NEW REQUIREMENT: exclude A'}])
    m=sr._build_messages(base,ctx,[],h,seconds_left=None)
    assert m[-1]['content']=='NEW REQUIREMENT: exclude A'
    for i in range(5,10):h.append(exchange(i,'more old search '*1500))
    h.extend([exchange(10,'recent three'),exchange(11,'recent four')])
    m=sr._build_messages(base,ctx,[],h,seconds_left=None)
    compact_context(ctx,m,[],b,call_model=summary)
    assert len(ctx.compaction_checkpoints)==2


def test_compacted_prefix_survives_read_save_and_countdown(tmp_path):
    from dra.context_compaction import compact_context
    from dra.models import RetrievedDoc
    from test_context_stability import exchange as tool_exchange
    ctx,h,base,m,b=setup_context(tmp_path)
    ctx.config.archive_mode='immediate'
    compacted=compact_context(ctx,m,[],b,call_model=summary)
    frozen=deepcopy(compacted)
    ctx.store.write_source(RetrievedDoc(title='New source',source_url='https://example.com/new',
        snippet='',raw_content='New verified fact.\n\nException applies.'))
    receipt,ok=ctx.read_source({'source_ref':'s1'})
    assert ok
    h.append(tool_exchange('read_source',receipt,'read-new'))
    read_view=sr._build_messages(base,ctx,[],h,seconds_left=90,budget=b)
    assert read_view[:len(compacted)]==compacted==frozen
    saved,ok=ctx.save_evidence({'cards':[{'claim':'New verified fact.','quote':'New verified fact.'}]})
    assert ok
    h.append(tool_exchange('save_evidence',saved,'save-new'))
    next_view=sr._build_messages(base,ctx,[],h,seconds_left=80,budget=b)
    # Compare the actual serialized prefix, including tool IDs and provider reasoning fields.
    assert json.dumps(next_view[:len(read_view)],ensure_ascii=False)==json.dumps(read_view,ensure_ascii=False)
    assert sr._build_messages(base,ctx,[],h,seconds_left=80,budget=b)==next_view
    assert json.loads(next(m['content'] for m in next_view if m.get('tool_call_id')=='read-new'))['excerpts']
    assert not any(a['action']=='context_view_archive' for a in ctx.store.state['actions'])


def test_disabled_and_below_threshold_do_not_call(tmp_path):
    from dra.context_compaction import compact_context
    ctx,h,base,m,b=setup_context(tmp_path);ctx.config.compaction_enabled=False
    assert compact_context(ctx,m,[],b,call_model=summary)==m
    ctx.config.compaction_enabled=True;ctx.config.context_window_tokens=1000000
    assert compact_context(ctx,m,[],b,call_model=summary)==m and b.calls_used==0


def test_runtime_setting_validation_and_conversion():
    from dra.runtime_config import _code_defaults,to_simple_config,RuntimeModelSettings
    d=_code_defaults().model_dump();d['compaction_enabled']=True
    with pytest.raises(ValueError):RuntimeModelSettings.model_validate(d)
    d['context_window_tokens']=64000
    cfg=to_simple_config(RuntimeModelSettings.model_validate(d))
    assert cfg.compaction_enabled and cfg.context_window_tokens==64000


@pytest.mark.parametrize('failure', ['disk', 'cancel', 'incomplete', 'stopped', 'large'])
def test_failed_candidate_never_changes_view(tmp_path, monkeypatch, failure):
    from dra import context_compaction as cc
    ctx,h,base,m,b=setup_context(tmp_path)
    before=deepcopy(ctx.message_view.chunks)
    def model(*a,**kw):
        t=summary()
        if failure=='cancel':monkeypatch.setattr(cc.llm,'cancellation_requested',lambda:True)
        if failure=='incomplete':t.incomplete=True
        if failure=='stopped':t.stopped=True
        if failure=='large':t.content=json.dumps({'findings':[],'decisions':['x'*18000],'open_questions':[],'next_steps':[]})
        return t
    if failure=='disk':
        orig=cc.atomic_json
        def broken(path,data):
            if data['status']=='committed':raise OSError('disk full')
            return orig(path,data)
        monkeypatch.setattr(cc,'atomic_json',broken)
    with pytest.raises(cc.ContextLimit):cc.compact_context(ctx,m,[],b,call_model=model)
    assert ctx.message_view.chunks==before and not ctx.compaction_checkpoints


def test_new_input_during_summary_is_not_consumed(tmp_path):
    from dra.context_compaction import compact_context
    from dra.research_input import ResearchInput
    ctx,h,base,m,b=setup_context(tmp_path)
    ctx.user_input=ResearchInput('test',lambda _:None)
    def model(*a,**kw):
        ctx.user_input.accept('u-new','Only compare current versions')
        return summary()
    compact_context(ctx,m,[],b,call_model=model)
    assert ctx.user_input.snapshot()['messages'][0]['status']=='queued'
    incoming=sr._receive_user_input(ctx,'lead-next')
    h.extend([[message] for message in incoming])
    result=sr._build_messages(base,ctx,[],h,seconds_left=None)
    assert 'Only compare current versions' in result[-1]['content']
    assert ctx.user_input.snapshot()['messages'][0]['status']=='included'


def test_evidence_and_cross_agent_permissions_are_unchanged(tmp_path):
    from dra.context_compaction import compact_context
    from dra.models import RetrievedDoc
    ctx,h,base,m,b=setup_context(tmp_path)
    ctx.store.write_source(RetrievedDoc(title='fact',source_url='https://example.com/f',snippet='',raw_content='Alpha factual statement.'))
    ctx.read_source({'source_ref':'s1'})
    _,ok=ctx.save_evidence({'cards':[{'claim':'Alpha factual statement.','quote':'Alpha factual statement.'}]})
    assert ok
    cards=deepcopy(ctx.store.state['cards']);exposures=deepcopy(ctx.read_exposures)
    m=sr._build_messages(base,ctx,[],h,seconds_left=None)
    compact_context(ctx,m,[],b,call_model=summary)
    assert ctx.store.state['cards']==cards and ctx.read_exposures==exposures
    assert 'Alpha factual statement.' in ctx.inspect_research({'view':'cards','card_id':'c1'})[0]
    child=ctx.fork_for_subagent(ctx.config,budget=None,delegation_id='sub1')
    assert not child.execute('read_context_history',{'checkpoint_id':ctx.compaction_checkpoints[0]})[1]
    assert not child.read_exposures


def test_tool_batch_and_reasoning_fields_preserved(tmp_path):
    from dra.context_compaction import compact_context
    ctx,h,base,m,b=setup_context(tmp_path)
    batch=exchange(5,'latest receipt')
    batch[0]['reasoning_content']='provider field kept verbatim'
    batch[0]['tool_calls'].append({'id':'parallel','type':'function','function':{'name':'search_sources','arguments':'{}'}})
    batch.append({'role':'tool','tool_call_id':'parallel','content':'parallel reply'})
    h.append(batch)
    m=sr._build_messages(base,ctx,[],h,seconds_left=None)
    result=compact_context(ctx,m,[],b,call_model=summary)
    p=next(i for i,item in enumerate(result) if item.get('reasoning_content'))
    assert result[p:p+3]==h[-1]


def test_budget_reserves_research_and_wrapup(tmp_path):
    from dra.context_compaction import compact_context,ContextLimit
    ctx,h,base,m,b=setup_context(tmp_path)
    ctx.config.max_llm_calls=2
    with pytest.raises(ContextLimit):compact_context(ctx,m,[],b,call_model=summary)
    assert b.calls_used==0


def test_oversized_completed_round_is_fully_summarized_and_archived(tmp_path):
    from dra.context_compaction import compact_context,ContextLimit
    ctx,h,base,m,b=setup_context(tmp_path)
    h.append(exchange(5,'x'*30000))
    m=sr._build_messages(base,ctx,[],h,seconds_left=None)
    before=deepcopy(ctx.message_view.chunks)
    result=compact_context(ctx,m,[],b,call_model=summary)
    checkpoint=ctx.store.dir/"context"/"lead"/(ctx.compaction_checkpoints[-1]+".json")
    stored=json.loads(checkpoint.read_text())
    assert stored["retained_rounds"] == 0
    assert stored["raw_exchanges"][-1] == h[-1]
    assert not any(m.get("role") == "tool" for m in result)


@pytest.mark.parametrize("overflow", [False, True])
def test_loop_compaction_has_own_call_and_delivers_new_input(tmp_path, monkeypatch, overflow):
    from dra.llm import ToolCall, ToolTurn
    from dra.research_input import ResearchInput
    cfg=sr.SimpleConfig(compaction_enabled=True,context_window_tokens=1000000 if overflow else 30000,pressure_reserve_tokens=0,
        estimate_chars_per_token=1,archive_mode='keep',max_llm_calls=20)
    store=sr.RunStore(tmp_path,run_id='loop',query='Compare research methods')
    ctx=sr.ToolContext(store,cfg);b=sr.Budget(cfg,None);b.identity='lead'
    ctx.user_input=ResearchInput('loop',lambda _:None)
    original=ctx.execute
    def execute(name,args):
        if name=='inspect_research':return 'search history '+('x'*4000),True
        return original(name,args)
    monkeypatch.setattr(ctx,'execute',execute)
    research_calls=[];compactions=[]
    def model(messages,**kw):
        if not kw['tools']:
            compactions.append(messages)
            ctx.user_input.accept('u-compact','Include evaluation limitations')
            return summary()
        research_calls.append(deepcopy(messages))
        if overflow and len(research_calls)==6 and not compactions:
            raise RuntimeError("context_length_exceeded")
        finish=bool(compactions)
        name='finish_research' if finish else 'inspect_research'
        args={'draft':'No externally verified findings. Evaluation limitations remain unresolved.'} if finish else {'view':'summary'}
        call=ToolCall(id=f't{len(research_calls)}',name=name,arguments=args,arguments_raw=json.dumps(args))
        return ToolTurn(tool_calls=[call],assistant_message={'role':'assistant','content':'',
            'tool_calls':[{'id':call.id,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]})
    schemas=[s for s in sr.LEAD_TOOL_SCHEMAS if s['function']['name'] in ('inspect_research','finish_research')]
    result=sr.run_lead_loop(store,cfg,[],budget=b,context=ctx,model_call=model,system_prompt='Research carefully.',schemas=schemas)
    assert compactions and result['draft']
    assert any('Include evaluation limitations' in m.get('content','') for m in research_calls[-1])
    assert len(store.calls())==len(research_calls)+len(compactions)
    assert len({c.call_id for c in store.calls()})==len(store.calls())
    assert ctx.tool_calls==len(research_calls)-1-int(overflow)  # finish is a control action, not a research tool


def test_failed_boundary_is_not_retried_when_budget_state_updates(tmp_path):
    from dra.context_compaction import compact_context,ContextLimit
    ctx,h,base,m,b=setup_context(tmp_path)
    with pytest.raises(ContextLimit):compact_context(ctx,m,[],b,call_model=lambda *a,**kw:summary_bad())
    m=sr._build_messages(base,ctx,[],h,seconds_left=None,budget=b)
    before=b.calls_used
    with pytest.raises(ContextLimit):compact_context(ctx,m,[],b,call_model=summary)
    assert b.calls_used==before


def summary_bad():
    t=summary();t.content='';return t


def test_cards_directory_is_paged_but_quotes_remain_complete(tmp_path):
    from dra.models import RetrievedDoc
    ctx,h,base,m,b=setup_context(tmp_path)
    ctx.store.write_source(RetrievedDoc(title='fact',source_url='https://example.com/f',snippet='',raw_content='Alpha factual statement.'))
    ctx.read_source({'source_ref':'s1'})
    ctx.save_evidence({'cards':[{'claim':'Alpha factual statement.','quote':'Alpha factual statement.'}]})
    card=ctx.store.state['cards'][0]
    ctx.store.state['cards']=[{**card,'card_id':f'c{i}'} for i in range(1,52)]
    body,ok=ctx.inspect_research({'view':'cards'})
    assert ok and len(json.loads(body)['cards'])==50 and json.loads(body)['next_cursor']==50
    body,ok=ctx.inspect_research({'view':'cards','cursor':50})
    assert ok and len(json.loads(body)['cards'])==1 and json.loads(body)['next_cursor'] is None
    assert not ctx.inspect_research({'view':'cards','cursor':-1})[1]
    assert 'Alpha factual statement.' in ctx.inspect_research({'view':'cards','card_id':'c51'})[0]


def test_http_history_only_reads_current_run_and_pages(tmp_path,monkeypatch):
    from dra import web
    from fastapi import HTTPException
    checkpoint='ctx-'+'a'*32
    folder=tmp_path/'context'/'lead';folder.mkdir(parents=True)
    (folder/(checkpoint+'.json')).write_text(json.dumps({'messages':[{'content':'x'*9000}]}))
    monkeypatch.setattr(web,'STORE',SimpleNamespace(get_meta=lambda rid:{} if rid=='run-1' else None,_dir=lambda rid:tmp_path))
    page=web.api_context_history('run-1',checkpoint)
    assert len(page['text'])==8000 and page['next_cursor']==8000
    assert web.api_context_history('run-1',checkpoint,8000)['next_cursor'] is None
    for rid,cid,cursor in [('run-1','../../secret',0),('run-1',checkpoint,-1),('another-run',checkpoint,0)]:
        with pytest.raises(HTTPException):web.api_context_history(rid,cid,cursor)


def test_child_compacts_independently_and_does_not_consume_parent_mailbox(tmp_path):
    from dra.context_compaction import compact_context
    ctx,h,base,m,b=setup_context(tmp_path)
    child_budget=sr.Budget(ctx.config,None);child_budget.identity='sub1'
    child=ctx.fork_for_subagent(ctx.config,budget=child_budget,delegation_id='sub1')
    child_messages=sr._build_messages(base,child,[],h,seconds_left=None)
    parent_before=deepcopy(ctx.message_view.chunks)
    child.delegation_messages=[{'id':'message-1','text':'retain this message'}]
    compact_context(child,child_messages,[],child_budget,call_model=summary)
    assert ctx.message_view.chunks==parent_before and b.calls_used==0
    assert child.delegation_messages==[{'id':'message-1','text':'retain this message'}]
    checkpoint=child.compaction_checkpoints[0]
    assert not ctx.execute('read_context_history',{'checkpoint_id':checkpoint})[1]
    assert child.execute('read_context_history',{'checkpoint_id':checkpoint})[1]


def test_full_tool_result_is_chunked_without_losing_tail(tmp_path):
    from dra.context_compaction import compact_context
    ctx,h,base,m,b=setup_context(tmp_path)
    h[0][1]['content'] = 'p'*25000 + 'IMPORTANT_TAIL_CONDITION'
    ctx.message_view = None
    m=sr._build_messages(base,ctx,[],h,seconds_left=None)
    seen=[]
    def model(messages, **kwargs):
        seen.extend(json.loads(messages[1]['content'])['process'])
        return summary()
    compact_context(ctx,m,[],b,call_model=model)
    fragments=[x for x in seen if isinstance(x,dict) and x.get('fragment') and x['block']==0]
    restored=''.join(x['text'] for x in fragments)
    assert json.loads(restored)==h[0]
    assert 'IMPORTANT_TAIL_CONDITION' in restored
    assert len(fragments)>1
    path=ctx.store.dir/'context'/'lead'/(ctx.compaction_checkpoints[0]+'.json')
    record=json.loads(path.read_text())
    assert len(record['calls'])==b.calls_used
    notes,ok=ctx.execute('read_context_history',{'checkpoint_id':ctx.compaction_checkpoints[0],'section':'notes'})
    assert ok and 'Excluded old candidate' in notes


def test_summary_over_6000_chars_fits_dynamic_capacity(tmp_path):
    from dra.context_compaction import compact_context
    ctx,h,base,m,b=setup_context(tmp_path)
    ctx.config.context_window_tokens = 50000
    def longer(*args, **kwargs):
        t=summary();d=json.loads(t.content);d['decisions']=['decision '*800];t.content=json.dumps(d);return t
    result=compact_context(ctx,m,[],b,call_model=longer)
    assert 'decision '*800 in json.dumps(result)
    assert ctx.compaction_checkpoints


def test_compaction_restores_catalogs_without_replaying_full_quotes(tmp_path):
    from dra.context_compaction import compact_context
    from dra.models import RetrievedDoc
    ctx,h,base,m,b=setup_context(tmp_path)
    quote='UNIQUE_QUOTE_BEGIN ' + 'exact evidence '*600
    ctx.store.write_source(RetrievedDoc(title='Archived document',source_url='https://example.com/doc',snippet='',raw_content=quote))
    ctx.read_source({'source_ref':'s1'})
    ctx.save_evidence({'cards':[{'claim':'Recorded fact','quote':quote}]})
    m=sr._build_messages(base,ctx,[],h,seconds_left=None)
    compact_context(ctx,m,[],b,call_model=summary)
    result=sr._build_messages(base,ctx,[],h,seconds_left=None)
    text=json.dumps(result,ensure_ascii=False)
    assert 'Archived document' in text and 'Recorded fact' in text
    assert 's1' in text and 'c1' in text and 'read_source' in text
    assert quote not in text
    cards,ok=ctx.inspect_research({'view':'cards','card_id':'c1'})
    assert ok and json.loads(cards)["quotes"][0]["quote"] == quote.strip()


def test_one_large_completed_batch_can_compact_again(tmp_path):
    from dra.context_compaction import compact_context
    ctx,h,base,m,b=setup_context(tmp_path)
    compact_context(ctx,m,[],b,call_model=summary)
    h.append(exchange(5,'new single batch '*3000))
    m=sr._build_messages(base,ctx,[],h,seconds_left=None)
    result=compact_context(ctx,m,[],b,call_model=summary)
    assert len(ctx.compaction_checkpoints)==2
    assert not any(message.get('role')=='tool' for message in result)
    assert sr._build_messages(base,ctx,[],h,seconds_left=None)==result


def test_summary_can_retain_discovered_leads_and_extra_work_notes(tmp_path):
    from dra.context_compaction import compact_context
    from dra.models import RetrievedDoc
    ctx,h,base,m,b=setup_context(tmp_path)
    ctx.store.register_summary(RetrievedDoc(title='Unread lead',source_url='https://example.com/lead',snippet='candidate dates'))
    assert not ctx.read_exposures
    def model(*args,**kwargs):
        t=summary();d=json.loads(t.content)
        d['process_summary']='Search result only: must read before citing.'
        d['findings']=[{'text':'Unverified search lead','card_ids':[],'source_refs':['s1'],'confidence':'unverified'}]
        t.content=json.dumps(d);return t
    compact_context(ctx,m,[],b,call_model=model)
    assert not ctx.read_exposures
    text=json.dumps(sr._build_messages(base,ctx,[],h,seconds_left=None))
    assert 'Unverified search lead' in text and 'must read before citing' in text
