"""Read-only deployment diagnostics. Never print task config, env or credentials."""
import json
import re
import subprocess
import os
from pathlib import Path
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
running = [row for row in allocations if row.get('ClientStatus') == 'running'
           and row.get('DesiredStatus') == 'run']
if running:
    latest = max(running, key=lambda row: row.get('CreateIndex', 0))
    # Use the checked-out read-only diagnostic, so observing a new job does not
    # require restarting the app or interrupting its current enrichment slice.
    runtime = subprocess.run(['nomad', 'alloc', 'exec', '-i', '-t=false', '-task', 'app',
                              latest['ID'], 'python', '-'],
                             input=Path(__file__).with_name('job_diagnostics.py').read_text(),
                             capture_output=True, text=True, timeout=60)
    if runtime.returncode:
        print('Runtime diagnostics unavailable; exit:', runtime.returncode)
    else:
        for line in runtime.stdout.splitlines():
            if line.startswith('Runtime '):
                print(line)
    if os.environ.get('AI_CHECK') == 'true':
        # Opt in only: this one calls the model (two calls at most).
        check = subprocess.run(['nomad', 'alloc', 'exec', '-i', '-t=false', '-task', 'app',
                                latest['ID'], 'python', '-'],
                               input=Path(__file__).with_name('ai_live_check.py').read_text(),
                               capture_output=True, text=True, timeout=420)
        for line in check.stdout.splitlines():
            if line.startswith('Runtime ai check'):
                print(line)
        if check.returncode:
            tail = (check.stderr.strip().splitlines() or [''])[-1]
            print('AI check exit:', check.returncode, safe(tail))
for row in sorted(allocations, key=lambda x:x.get('CreateIndex',0), reverse=True)[:3]:
    print('Allocation:', row['ID'], row.get('ClientStatus'), row.get('DesiredStatus'))
    allocation = command('alloc', 'status', '-json', row['ID']) or {}
    for task, state in allocation.get('TaskStates', {}).items():
        print('Task:', task, state.get('State'), 'failed:', state.get('Failed'))
        for event in state.get('Events', [])[-4:]:
            print('Event:', task, event.get('Type'), safe(event.get('DisplayMessage') or event.get('Message')))
# How much memory and CPU the running tasks use right now.
if running:
    try:
        req = urllib.request.Request(
            os.environ['NOMAD_ADDR'].rstrip('/') + f"/v1/client/allocation/{latest['ID']}/stats",
            headers={'X-Nomad-Token': os.environ['NOMAD_TOKEN']})
        with urllib.request.urlopen(req, timeout=20) as response:
            stats = json.load(response)
        for task, use in (stats.get('Tasks') or {}).items():
            ru = use.get('ResourceUsage') or {}
            mem = ru.get('MemoryStats') or {}
            print('Task usage:', json.dumps({
                'task': task, 'rss_mb': round((mem.get('RSS') or 0) / 2**20),
                'usage_mb': round((mem.get('Usage') or 0) / 2**20),
                'max_usage_mb': round((mem.get('MaxUsage') or 0) / 2**20),
                'cpu_mhz': round((ru.get('CpuStats') or {}).get('TotalTicks') or 0),
                'cpu_percent': round((ru.get('CpuStats') or {}).get('Percent') or 0, 1)}))
    except Exception as exc:
        print('Task usage unavailable:', type(exc).__name__)
# What the cluster can offer a job: CPU, memory and any GPUs per ready node.
for node in (command('node', 'status', '-json') or [])[:12]:
    if node.get('Status') != 'ready':
        continue
    detail = command('node', 'status', '-json', node['ID']) or {}
    res = detail.get('NodeResources') or {}
    gpus = [f"{d.get('Vendor')}/{d.get('Name')} x{len(d.get('Instances') or [])}"
            for d in (res.get('Devices') or []) if d.get('Type') == 'gpu']
    print('Cluster node:', json.dumps({
        'name': node.get('Name'), 'class': node.get('NodeClass'),
        'cpu_mhz': (res.get('Cpu') or {}).get('CpuShares'),
        'memory_mb': (res.get('Memory') or {}).get('MemoryMB'), 'gpus': gpus}))
request = urllib.request.Request(os.environ['NOMAD_ADDR'].rstrip('/') + '/v1/job/bellwether/evaluations',
                                 headers={'X-Nomad-Token':os.environ['NOMAD_TOKEN']})
with urllib.request.urlopen(request, timeout=20) as response:
    evaluations = json.load(response)
for full in sorted(evaluations, key=lambda x:x.get('CreateIndex',0), reverse=True)[:4]:
    print('Evaluation:', full['ID'], full.get('Status'), safe(full.get('StatusDescription')))
    for group, failures in (full.get('FailedTGAllocs') or {}).items():
        print('Placement:', group, {key:failures.get(key) for key in
              ('NodesEvaluated','NodesFiltered','NodesExhausted','DimensionExhausted','ConstraintFiltered')})
