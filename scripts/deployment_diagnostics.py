"""Read-only deployment diagnostics. Never print task config, env or credentials."""
import json
import re
import subprocess
import os
import urllib.request


def command(*args):
    r = subprocess.run(['nomad', *args], capture_output=True, text=True, timeout=30)
    if r.returncode:
        print('Nomad query failed:', args[0], r.returncode)
        return None
    return json.loads(r.stdout)


def safe(message):
    # Task events should not contain credentials, but omit tokens defensively.
    text = re.sub(r'[A-Za-z0-9_~+/.=-]{40,}', '[long value omitted]', str(message))
    return text[:500]


allocations = command('job', 'allocs', '-json', 'bellwether') or []
for row in sorted(allocations, key=lambda x:x.get('CreateIndex',0), reverse=True)[:3]:
    print('Allocation:', row['ID'], row.get('ClientStatus'), row.get('DesiredStatus'))
    allocation = command('alloc', 'status', '-json', row['ID']) or {}
    for task, state in allocation.get('TaskStates', {}).items():
        print('Task:', task, state.get('State'), 'failed:', state.get('Failed'))
        for event in state.get('Events', [])[-4:]:
            print('Event:', task, event.get('Type'), safe(event.get('DisplayMessage') or event.get('Message')))
request = urllib.request.Request(os.environ['NOMAD_ADDR'].rstrip('/') + '/v1/job/bellwether/evaluations',
                                 headers={'X-Nomad-Token':os.environ['NOMAD_TOKEN']})
with urllib.request.urlopen(request, timeout=20) as response:
    evaluations = json.load(response)
for full in sorted(evaluations, key=lambda x:x.get('CreateIndex',0), reverse=True)[:4]:
    print('Evaluation:', full['ID'], full.get('Status'), safe(full.get('StatusDescription')))
    for group, failures in (full.get('FailedTGAllocs') or {}).items():
        print('Placement:', group, {key:failures.get(key) for key in
              ('NodesEvaluated','NodesFiltered','NodesExhausted','DimensionExhausted','ConstraintFiltered')})
