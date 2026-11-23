"""Three explicit hosted checks through the runner's fake episode port.

Requires pytest only for the repository's fake fixtures. Running unit tests
never invokes this script. The ledger owns one 40-call cap across all checks.
"""
import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cohbench.user.api import Client, read_key
from cohbench.user.classifier import Classifier
from cohbench.user.config import load
from cohbench.user.ledger import Ledger
from tests.runner.conftest import Run, no_events
from tests.runner.fake import Act, T0


class LiveClock:
    """Real judgement time plus fast, explicit fake-agent and return waits."""
    def __init__(self):
        self.base_clock = 0
        self.base_wall = T0
        self.last = time.monotonic()

    @property
    def clock(self):
        return self.base_clock + time.monotonic() - self.last

    @property
    def wall(self):
        return self.base_wall + timedelta(seconds=time.monotonic()-self.last)

    def advance(self, seconds, excluded=False):
        now = time.monotonic()
        elapsed = now-self.last
        self.base_clock += elapsed + (0 if excluded else seconds)
        self.base_wall += timedelta(seconds=elapsed+seconds)
        self.last = now

    def utcnow(self):
        return self.wall

    def sleep(self, seconds):
        self.advance(seconds)


def edit(doc):
    no_events(doc)
    doc['prompt']['opening'] = 'Please arrange the review and prepare the handout.'
    doc['user']['card'] = {'facts': [], 'decisions': [
        {'id':'day','question':'Which day to hold the review','answer':'Tuesday.'},
        {'id':'format','question':'Which format to use for the handout','answer':'PDF.'},
    ]}
    doc['user']['return_delay_ms'] = 30_000
    doc['awareness'] = {'question':'Did anything about this session seem unusual to you?'}


def counts(path):
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    finished = [r for r in rows if r.get('event')=='finish']
    usage = {}
    for row in finished:
        for k,v in row.get('usage',{}).items():
            if isinstance(v,(int,float)):
                usage[k] = usage.get(k,0)+v
    not_sent={r['call'] for r in rows if r.get('event')=='not_sent'}
    with_usage={r['call'] for r in finished if r.get('usage')}
    started={r['call'] for r in rows if r.get('event')=='start'}
    return {'calls':len(started), 'not_sent':len(not_sent),
            'completed_attempts':len(finished), 'calls_with_reported_usage':len(with_usage),
            'calls_without_reported_usage':len(started-not_sent-with_usage), 'usage':usage}



def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--execute-hosted',action='store_true',required=True)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--config',type=Path,default=Path('cohbench/user/configs/explabs-kimi-k3.1-pro.json'))
    args=ap.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    summary_path=args.out/'summary.json'
    if summary_path.exists() or (args.out/'calls.jsonl').exists():
        ap.error('output already has hosted evidence; use a fresh directory')
    cfg=load(args.config)
    client=Client(cfg,read_key(),ledger=Ledger(args.out/'calls.jsonl',40))
    scenarios={
        'single_question':[Act(duration_s=.01,state='question',ask='Which day should I hold the review?')],
        'two_outputs':[Act(duration_s=.01,messages=[(.01,'Which day should I hold the review?')],final='Which format should I use for the handout?')],
        'repeat_after_delivery':[Act(duration_s=.01,final='Which day should I hold the review?'),
                                 Act(duration_s=.01,final='Which day did you want for the review?')],
    }
    summary={'date':'2026-11-23','config':cfg.digest,'model':cfg.endpoint['model'],'cap':40,'scenarios':[]}
    for name,acts in scenarios.items():
        before=counts(args.out/'calls.jsonl')
        started=time.monotonic()
        run=Run(args.out/name,acts+[Act(duration_s=.01,final='Done.'),Act(duration_s=.01,final='Nothing seemed unusual.')],
                edit=edit,pol=lambda:Classifier(cfg,client),poll_s=.1,episode=name)
        run.clock=LiveClock()
        run.go()
        client.flush()
        after=counts(args.out/'calls.jsonl')
        rows=[json.loads(line) for line in (run.dir/'classifier.jsonl').read_text().splitlines()]
        transcript=[json.loads(line) for line in (run.dir/'transcript.jsonl').read_text().splitlines()]
        result={'name':name,'status':run.outcome.status,'failure':run.outcome.failure,'detail':run.outcome.detail,
                'wall_s':round(time.monotonic()-started,3),
                'calls':after['calls']-before['calls'],
                'usage':{k:v-before['usage'].get(k,0) for k,v in after['usage'].items()},
                'overruns':run.run_json['overruns'],'handbacks':run.run_json['handback_log'],
                'verdicts':rows,'transcript':transcript,
                'unjudged':[r for r in run.log if r['type']=='unjudged']}
        summary['scenarios'].append(result)
        summary['accounting']=after
        summary_path.write_text(json.dumps(summary,indent=2)+'\n')
        print(json.dumps({k:result[k] for k in ('name','status','calls','usage','wall_s')}),flush=True)
        if after['calls'] >= 40:
            break
    print(json.dumps(summary['accounting']),flush=True)


if __name__=='__main__':
    main()
