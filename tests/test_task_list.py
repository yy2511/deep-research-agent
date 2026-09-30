from dra import simple_research as sr
import json


def test_initial_plan_is_published_before_first_model_call(tmp_path, monkeypatch):
    seen=[]
    captured=[]
    monkeypatch.setattr(sr.events,'emit',lambda event,**payload:seen.append((str(event),payload)))
    def capture(*args,**kwargs):
        captured.append(args[0])
        plans=[p for _,p in seen if 'items' in p and 'diff' in p]
        assert plans and plans[0]['items'][0]['status']=='todo'
        assert 'r1' in str(args[0])
        raise RuntimeError('stop offline probe')
    sr.run_simple_research('问题',run_dir=tmp_path,initial_route=[{'id':'r1','text':'核对官方资料'}],model_call=capture)
    assert captured and "r1" in str(captured[0])
    ledger=json.loads((tmp_path / "run.json").read_text())
    assert any(a.get("action")=="research_plan" for a in ledger["actions"])
    # 主循环会收口模型异常，额外断言确保模型内失败不被吞掉。
    assert any(p.get('items')==[{'id':'r1','text':'核对官方资料','status':'todo'}] for _,p in seen)


def test_current_plan_is_in_every_model_context(tmp_path):
    store=sr.RunStore(tmp_path,run_id='r',query='q')
    ctx=sr.ToolContext(store,sr.SimpleConfig())
    ctx.plan_items=[{'id':'r1','text':'检索新版本','status':'doing'},{'id':'r2','text':'排除旧路线','status':'dropped'}]
    messages=sr._build_messages([],ctx,[],[],seconds_left=10)
    assert 'r1' in messages[0]['content']
    assert 'doing' in messages[0]['content']
    assert 'dropped' in messages[0]['content']
