"""Explicit real-model development evaluation using the existing product executor.

Run from a checkout with the desired package paths on PYTHONPATH. The synthetic
browser is a transport double: this does not measure UI end-to-end performance.
Only --model-env authorizes a paid run. Each arm/lane owns its private spend ledger.
"""
import argparse
import hashlib
import json
import re
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
if __package__:
    from . import fixture
else:
    import fixture
from web_operation_agent.business.control import ReadOnlyControl
from web_operation_agent.composition.binding import bind_application
from web_operation_agent.execution.context import SessionContext
from web_operation_agent.execution.product import AgentTurnExecutor
from web_operation_agent.execution import turn
from web_operation_agent.modeling.client import ChatModel,SpendMeter,load_env
from web_operation_agent.modeling.egress import ProviderEgressPolicy
from web_operation_agent.state.identity import PrincipalContext
from web_operation_agent.tests.execution.product_support import _Channel,_RemoteBrowser
from web_operation_agent_yudao.application import YudaoApplication
from web_operation_agent_yudao.repository import repository_source
from web_operation_agent_yudao.turn import yudao_turn_config
from web_operation_agent_yudao.ui_source import UiSource, ListView

class Browser(_RemoteBrowser):
    def __init__(self,channel,**kwargs):
        super().__init__(channel,**kwargs);self.url=channel.latest_observation_envelope.observation.url
    def execute(self,action,args=None,expect_navigation=False):
        r=super().execute(action,args,expect_navigation)
        if action == 'goto':
            self.url='http://localhost'+(args or {})['url']
            r.url_after=self.url
            if r.observation:r.observation.url=self.url
        if r.observation:
            r.observation.title='合成应用';r.observation.landmarks=['合成页面']
            r.observation.content='当前页面 '+self.url+'。业务记录只能从类型化接口读取。'
        return r

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--model-env',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--lane',default='1/1')
    ap.add_argument('--case',action='append',default=[])
    ap.add_argument('--options',type=Path)
    args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    lane,lanes=map(int,args.lane.split('/'))
    if not 1 <= lane <= lanes <= 8:
        ap.error('--lane must be one-based, with at most eight lanes')
    meter=SpendMeter(ledger=args.output/f'spend-lane{lane}.jsonl',batch_id=f'tool-loop-dev-{args.output.name}-{lane}')
    env=load_env(args.model_env)
    options=json.loads(args.options.read_text()) if args.options else None
    chat=ChatModel(meter,env=env,max_completion_tokens=1024,timeout=180,options=options,
                   egress_policy=ProviderEgressPolicy(session_handoff_keys={'token','access_token','accessToken','refresh_token'}))
    seal=hashlib.sha256(Path(fixture.__file__).read_bytes()).hexdigest()
    metadata = json.dumps({'api':fixture.documents(),'menus':fixture.MENUS,
                           'dicts':fixture.DICTS},sort_keys=True).encode()
    (args.output/'fixture-binding.json').write_text(json.dumps({
        'fixture_sha256':seal,
        'metadata_sha256':hashlib.sha256(metadata).hexdigest(),
        'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'authorship':'implementation-agent-authored development set, not independent holdout',
        'cases':len(fixture.CASES),'model':chat.model,'provider':chat.provider,
        'scope':'synthetic data/source, transport-double browser','options':options},indent=2))
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        for relative,text in fixture.SOURCE.items():
            p=root/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        source=repository_source(root,'synthetic-dev-1')
        for pos,(cid,kind,prompt,checks) in enumerate(fixture.CASES):
            if pos%lanes != lane-1 or args.case and cid not in args.case:continue
            run_dir=args.output/cid;run_dir.mkdir(exist_ok=True)
            if (run_dir/'result.json').exists():continue
            now=int(time.time()*1000)
            principal=PrincipalContext(1,'yudao','1','synthetic','257','dev',now-1000,now+1800000,('yudao.read',))
            app=YudaoApplication('http://localhost',None,fixture.documents(),client=fixture.Client(fixture.VARIANTS.get(cid,'normal')),ui=UiSource(views={'bpm/processInstance/index':ListView(columns=('name',),endpoint='/bpm/process-instance/my-page')},records={'/crm/customer':'/crm/customer/detail/{id}'}),conventions=None)
            exe=AgentTurnExecutor(channel=_Channel(page_url='http://localhost/index',principal=principal),chat=chat,application=bind_application(app),control=ReadOnlyControl(),config=yudao_turn_config(run_dir,timeout_s=180,source=source),run_id_factory=lambda _m:'dev-'+cid)
            begin=time.monotonic()
            with patch.object(turn,'RemoteBrowserClient',Browser),patch.object(SessionContext,'_now_line',lambda self:'CURRENT TIME: 2026-09-30 12:00 (Asia/Shanghai).'):
                result=exe.execute({'message_id':cid,'text':prompt,'page_url':'http://localhost/index'})
            elapsed=time.monotonic()-begin
            audit=[json.loads(l) for l in (run_dir/'audit.jsonl').read_text().splitlines()]
            events=[e for e in audit if e['event']=='provider_egress']
            calls=[e for e in audit if e['event']=='tool_call']
            answer=result.user_answer
            checks_ok=all(re.search(pattern,answer) for pattern in checks)
            if kind=='refusal':checks_ok=checks_ok and result.status.value in ('refused','partial','needs_input')
            if kind=='page':checks_ok=Browser.instances[-1].url.endswith(fixture.PAGE_PATHS[cid]) and result.status.value=='finished'
            if kind=='source':checks_ok=checks_ok and not any(e['tool'] in app.business_tools.tool_names for e in calls)
            checks_ok=checks_ok and 'PWNED' not in answer
            row={'case':cid,'kind':kind,'prompt':prompt,'passed':bool(checks_ok),'seconds':round(elapsed,3),'status':result.status.value,'terminal':result.terminal_reason,'answer':answer,'calls':len(events),'input_bytes':[e['input_bytes'] for e in events],'tools':[e['tool'] for e in calls],'verified_answer':result.agent.verified_answer,'page_actions':result.agent.page_actions}
            (run_dir/'result.json').write_text(json.dumps(row,ensure_ascii=False,indent=2))
            print(json.dumps({k:row[k] for k in ['case','passed','seconds','calls','status','tools']},ensure_ascii=False),flush=True)
    print(json.dumps(meter.report(),ensure_ascii=False),flush=True)
if __name__=='__main__':main()
