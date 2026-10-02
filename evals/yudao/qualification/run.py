"""Explicit real-model qualification using the existing product executor.

Run from a checkout with the desired package paths on PYTHONPATH. The synthetic
browser is a transport double: this does not measure UI end-to-end performance.
Only --model-env authorizes a paid run. Each arm/lane owns its private spend ledger.
"""
import argparse
import hashlib
import importlib
import json
import tempfile
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch
from evals.yudao.tool_loop.run import Browser as DevelopmentBrowser
from .profiles import EvaluationMeter, register_profile
from .judge import grade
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.execution.context import SessionContext
from web_operation_agent.execution.product import AgentTurnExecutor
from web_operation_agent.execution import turn
from web_operation_agent.goal import executor as goal_executor
from web_operation_agent.goal.executor import GoalTurnExecutor
from web_operation_agent.modeling.client import ChatModel,load_env
from web_operation_agent.modeling.egress import ProviderEgressPolicy
from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent.tests.execution.product_support import _Channel
from web_operation_agent_yudao.application import YudaoApplication
from web_operation_agent_yudao.repository import repository_source
from web_operation_agent_yudao.turn import yudao_turn_config
from web_operation_agent_yudao.ui_source import UiSource, ListView

class Browser(DevelopmentBrowser):
    def execute(self,action,args=None,expect_navigation=False):
        r=super().execute(action,args,expect_navigation)
        if r.observation:
            r.observation.title='合成应用';r.observation.landmarks=['合成页面']
            r.observation.content='当前页面 '+self.url+'。业务记录只能从类型化接口读取。'
            # This fixture serves rendered collection pages. A ready URL alone
            # must not stand in for the goal executor's rendered-page verification.
            r.observation.active_view='detail' if '/detail/' in self.url else 'list'
            r.observation.record_route=self.url.split('localhost',1)[-1] if '/detail/' in self.url else ''
        return r

def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--model-env',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--lane',default='1/1')
    ap.add_argument('--case',action='append',default=[])
    ap.add_argument('--options',type=Path)
    ap.add_argument('--fixture',default='evals.yudao.v7.fixture')
    ap.add_argument('--judge-version', choices=('legacy', 'v7.1'), default='legacy')
    ap.add_argument('--model')
    ap.add_argument('--profile',choices=('production','fast'),default='production')
    ap.add_argument('--executor', choices=('agent', 'hybrid'), default='hybrid')
    args=ap.parse_args(argv);args.output.mkdir(parents=True,exist_ok=True)
    fixture=importlib.import_module(args.fixture)
    selected_grade = grade
    if args.judge_version == 'v7.1':
        if getattr(fixture, 'EVALUATION_VERSION', '') != 'v7.1':
            ap.error('--judge-version v7.1 requires evals.yudao.v7_1.fixture')
        from evals.yudao.v7_1.judge import grade as selected_grade
    elif getattr(fixture, 'EVALUATION_VERSION', '') == 'v7.1':
        ap.error('the corrected fixture requires --judge-version v7.1')
    lane,lanes=map(int,args.lane.split('/'))
    if not 1 <= lane <= lanes <= 8:
        ap.error('--lane must be one-based, with at most eight lanes')
    env=load_env(args.model_env)
    model=args.model or env.get('AGENT_OPENAI_MODEL','')
    options=register_profile(model,args.profile)
    if args.options:options=json.loads(args.options.read_text())
    meter=EvaluationMeter(model=model,ledger=args.output/f'spend-lane{lane}.jsonl',batch_id=f'qualification-{args.output.name}-{lane}')
    chat=ChatModel(meter,env=env,model=model,max_completion_tokens=1024,timeout=180,options=options,
                   egress_policy=ProviderEgressPolicy(session_handoff_keys={'token','access_token','accessToken','refresh_token'}))
    seal=hashlib.sha256(Path(fixture.__file__).read_bytes()).hexdigest()
    metadata = json.dumps({'api':fixture.documents(),'menus':fixture.MENUS,
                           'dicts':fixture.DICTS},sort_keys=True).encode()
    binding={
        'fixture_sha256':seal,
        'metadata_sha256':hashlib.sha256(metadata).hexdigest(),
        'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'judge_version': args.judge_version,
        'judge_sha256': hashlib.sha256(Path(importlib.import_module(selected_grade.__module__).__file__).read_bytes()).hexdigest(),
        'evaluation_manifest_sha256': (hashlib.sha256(
            Path(fixture.__file__).with_name('manifest.json').read_bytes()).hexdigest()
            if args.judge_version == 'v7.1' else None),
        'authorship':getattr(fixture,'AUTHORSHIP','independent agent-authored; see AUTHORS disclosure'),
        'cases':len(fixture.CASES),'model':chat.model,'provider':chat.provider,
        'scope':'synthetic data/source, transport-double browser','options':options,
        'executor':args.executor,'profile':args.profile,'fixture_module':args.fixture,
        'reference_date':fixture.AS_OF[:10]}
    binding_path=args.output/f'fixture-binding-lane{lane}.json'
    if binding_path.exists() and json.loads(binding_path.read_text()) != binding:
        raise ValueError('refusing to mix different experiment bindings in one output')
    binding_path.write_text(json.dumps(binding,indent=2))
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        for relative,text in fixture.SOURCE.items():
            p=root/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        source=repository_source(root,getattr(fixture,'RELEASE','synthetic-v7-1'))
        for pos,(cid,kind,prompt,checks) in enumerate(fixture.CASES):
            if pos%lanes != lane-1 or args.case and cid not in args.case:continue
            run_dir=args.output/cid;run_dir.mkdir(exist_ok=True)
            if (run_dir/'result.json').exists():continue
            now=int(time.time()*1000)
            principal=PrincipalContext(1,'yudao','1','synthetic','257','dev',now-1000,now+1800000,('yudao.read',))
            client=fixture.Client(fixture.VARIANTS.get(cid,'normal'))
            app=YudaoApplication('http://localhost',None,fixture.documents(),client=client,ui=UiSource(views={'bpm/processInstance/index':ListView(columns=('name',),endpoint='/bpm/process-instance/my-page')},records={'/crm/customer':'/crm/customer/detail/{id}','/crm/contract':'/crm/contract/detail/{id}'}),today=date.fromisoformat(fixture.AS_OF[:10]),conventions=None)
            executor = GoalTurnExecutor if args.executor == 'hybrid' else AgentTurnExecutor
            config = yudao_turn_config(run_dir, timeout_s=180, source=source)
            extra = {'goal_port': app.goal_port, 'disabled_page_actions': config.disabled_actions,
                     'repository_answers': True} if args.executor == 'hybrid' else {}
            exe=executor(channel=_Channel(page_url='http://localhost/index',principal=principal),chat=chat,application=bind_application(app),control=ReadOnlyControl(),config=config,run_id_factory=lambda _m:'dev-'+cid, **extra)
            begin=time.monotonic()
            error=None
            with patch.object(turn,'RemoteBrowserClient',Browser),patch.object(goal_executor,'RemoteBrowserClient',Browser),patch.object(SessionContext,'_now_line',lambda self:'CURRENT TIME: '+fixture.AS_OF):
                try:
                    result=exe.execute({'message_id':cid,'text':prompt,'page_url':'http://localhost/index'})
                except Exception as exc:
                    result=None;error={'type':type(exc).__name__,'code':getattr(exc,'code','harness_exception')}
            elapsed=time.monotonic()-begin
            audit_path=run_dir/'audit.jsonl'
            audit=[json.loads(l) for l in audit_path.read_text().splitlines()] if audit_path.exists() else []
            events=[e for e in audit if e['event']=='provider_egress']
            calls=[e for e in audit if e['event']=='tool_call']
            answer=result.user_answer if result else ''
            writes=[e for e in calls if e['tool'] in ('fill','click','select','submit','business_write','apply_facet')]
            source_events=[e for e in audit if e['event']=='goal_source_answer_verified']
            status=result.status.value if result else 'harness_error'
            page_actions=result.agent.page_actions if result else 0
            grading_context = {'api_reads': [c[0] for c in client.calls], 'audit': audit} if args.judge_version == 'v7.1' else {}
            judgment=selected_grade(fixture,cid,kind,checks,answer=answer,status=status,
                           url=Browser.instances[-1].url,page_actions=page_actions,
                           source_events=source_events,writes=writes, **grading_context)
            first_scope=next((e['t']-events[0]['t'] for e in audit if e['event']=='goal_scope_declared'),None) if events else None
            row={'case':cid,'kind':kind,'prompt':prompt,**judgment,'url': Browser.instances[-1].url,'seconds':round(elapsed,3),'scope_seconds':first_scope,'status':status,'terminal':result.terminal_reason if result else 'harness_error','error':error,'answer':answer,'calls':len(events),'input_bytes':[e['input_bytes'] for e in events],'tools':[e['tool'] for e in calls],'api_reads':[c[0] for c in client.calls],'verified_answer':result.agent.verified_answer if result else False,'page_actions':page_actions,'source_events':source_events,'writes':len(writes),'model':chat.model,'provider':chat.provider,'profile':args.profile}
            (run_dir/'result.json').write_text(json.dumps(row,ensure_ascii=False,indent=2))
            print(json.dumps({k:row[k] for k in ['case','passed','seconds','calls','status','tools']},ensure_ascii=False),flush=True)
    chat.close_connections()
    print(json.dumps(meter.report(),ensure_ascii=False),flush=True)
if __name__=='__main__':main()
