"""Inventory verdict-dependent events and card unlocks in public tasks."""
import argparse
import ast
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cohbench.runner.runner import verdict_dependent_events
from cohbench.tasks import generate
from cohbench.tasks.suite import options


def inspect(name, doc):
    events = doc.get('events', [])
    names = verdict_dependent_events([SimpleNamespace(name=e['name'], trigger=e['trigger']) for e in events])
    return {'task':name, 'events':[{k:e[k] for k in ('name','trigger','action')} for e in events if e['name'] in names],
            'card_unlocks':[{'item':item['id'], 'event':item['available_after']}
                            for items in doc['user']['card'].values() for item in items
                            if isinstance(item,dict) and item.get('available_after') in names]}


def regression_events():
    rows=[]
    for path in sorted(Path('tests/runner').glob('test_*.py')):
        tree=ast.parse(path.read_text())
        for function in tree.body:
            if not isinstance(function, ast.FunctionDef):
                continue
            events=[]
            for node in ast.walk(function):
                if not isinstance(node, ast.Dict):
                    continue
                fields={key.value:value for key,value in zip(node.keys,node.values)
                        if isinstance(key,ast.Constant) and isinstance(key.value,str)}
                if not {'name','trigger','action'} <= fields.keys():
                    continue
                try:
                    events.append({key:ast.literal_eval(fields[key]) for key in ('name','trigger','action')})
                except (ValueError,TypeError):
                    continue
            names=verdict_dependent_events([SimpleNamespace(name=e['name'],trigger=e['trigger']) for e in events])
            for event in events:
                if event['name'] in names:
                    rows.append({'test':f'{path.as_posix()}::{function.name}', 'event':event['name'],
                                 'trigger':event['trigger'],'action':event['action']['type'],'card_unlocks':[]})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out',type=Path,required=True)
    args=ap.parse_args()
    generated=[(o.name(),generate(o).task) for o in options()]
    fixtures=[(p.as_posix(),json.loads(p.read_text())) for p in sorted(Path('tests/runner/fixtures').rglob('task.json'))]
    report={'date':'2026-11-23'}
    for label,documents in [('generated',generated),('fixtures',fixtures)]:
        rows=[inspect(name,doc) for name,doc in documents]
        report[label]={'tasks':len(rows),'planted_events':sum(len(doc.get('events',[])) for _,doc in documents),
                       'dependencies':[r for r in rows if r['events'] or r['card_unlocks']]}
    report['dynamic_regression_fixtures']=regression_events()
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,sort_keys=True))


if __name__=='__main__':
    main()
