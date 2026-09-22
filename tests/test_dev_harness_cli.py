"""Exercise the portable entry point from an unrelated working directory."""
import json
import subprocess
import sys
from pathlib import Path

CLI = Path(__file__).resolve().parents[1] / 'scripts/dev_harness_cli.py'


def write(root, name, value):
    (root / name).write_text(json.dumps(value))


def run(root, *args, ok=True):
    process = subprocess.run([sys.executable, str(CLI), '--root', str(root), *args],
                             cwd=root.parent, capture_output=True, text=True)
    assert (process.returncode == 0) is ok, process.stderr
    return json.loads(process.stdout if ok else process.stderr)


def test_portable_commands_and_read_only_checks(tmp_path):
    (tmp_path / 'source.py').write_text('def work():\n    return 1\n')
    task = {'version': 2, 'task_id': 'cli-test', 'task_class': 'implementation',
            'objective': 'Keep selected Astra', 'inputs': [{'path': 'source.py', 'reason': 'source'}],
            'allowed_edits': ['source.py'], 'acceptance': ['verified'], 'invariants': ['offline'],
            'stop_conditions': ['ambiguity'], 'max_attempts': 2,
            'routing': {'model': 'gpt-6-astra', 'reasoning_effort': 'ultra', 'reason': 'selected',
                        'uncertainty': 'integration', 'escalation_condition': 'failure', 'policy_version': 'test'}}
    write(tmp_path, 'task.json', task)
    assert run(tmp_path, 'prepare', 'task.json', '--out', 'packet')['ok']
    assert run(tmp_path, 'verify', 'packet/packet.json')['ok']
    manifest = {'scope': ['source.py']}
    write(tmp_path, 'map.json', manifest)
    snapshot = run(tmp_path, 'context-map', 'map.json')
    write(tmp_path, 'snapshot.json', snapshot)
    assert run(tmp_path, 'context-check', 'snapshot.json', 'map.json')['fresh']
    write(tmp_path, 'inventory.json', {'files': [{'path': 'source.py', 'kind': 'instructions'}]})
    assert run(tmp_path, 'inventory', 'inventory.json')['observed_runtime'] is None
    (tmp_path / 'raw.log').write_text('ERROR setup failed\n')
    write(tmp_path, 'output.json', {'path': 'raw.log', 'command': 'pytest', 'exit_status': 1, 'format': 'pytest'})
    assert run(tmp_path, 'output-view', 'output.json')['exit_status'] == 1
    state = {'phase': 'review', 'evidence': [], 'active_workers': [], 'attempts_used': 1,
             'next_action': 'read evidence', 'state': 'active', 'unresolved_mutations': []}
    write(tmp_path, 'state-input.json', state)
    assert run(tmp_path, 'checkpoint-init', 'state.json', 'packet/packet.json', 'state-input.json')['revision'] == 0
    assert run(tmp_path, 'checkpoint-update', 'state.json', 'packet/packet.json', 'state-input.json', '--expected-revision', '0')['revision'] == 1
    assert run(tmp_path, 'checkpoint-inspect', 'state.json', '--packet', 'packet/packet.json')['status'] == 'found'
    assert not run(tmp_path, 'checkpoint-update', 'state.json', 'packet/packet.json', 'state-input.json', '--expected-revision', '0', ok=False)['ok']
    review = {'scope': ['source.py'], 'assumptions': {'oracle': 'unchanged'},
              'groups': [{'id': 'source', 'files': ['source.py'], 'dependencies': [], 'assumptions': ['oracle']}]}
    write(tmp_path, 'review-input.json', review)
    write(tmp_path, 'review.json', run(tmp_path, 'review-snapshot', 'review-input.json'))
    write(tmp_path, 'review-check.json', {key: review[key] for key in ('scope', 'assumptions')})
    assert run(tmp_path, 'review-check', 'review.json', 'review-check.json')['coverage_complete']
    (tmp_path / 'source.py').write_text('def work():\n    return 2\n')
    assert not run(tmp_path, 'context-check', 'snapshot.json', 'map.json')['fresh']
    assert not run(tmp_path, 'review-check', 'review.json', 'review-check.json')['coverage_complete']
    assert not run(tmp_path, 'inventory', '../outside.json', ok=False)['ok']


def test_cli_usage_unknown_and_help(tmp_path):
    write(tmp_path, 'usage.json', {'version': 1, 'task_id': 'usage', 'coverage_complete': False,
        'attempts': [{'attempt_id': 'a1', 'status': 'interrupted', 'coverage_complete': False, 'agents': []}]})
    report = run(tmp_path, 'usage-manifest', 'usage.json')
    assert report['cost_usd'] is None and not report['coverage_complete']
    process = subprocess.run([sys.executable, str(CLI), '--help'], capture_output=True, text=True)
    assert process.returncode == 0
    assert 'context-map' in process.stdout and 'prepare' in process.stdout
