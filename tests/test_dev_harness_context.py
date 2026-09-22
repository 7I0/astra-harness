import json

import pytest

from scripts import dev_harness as h
from scripts import dev_harness_context as c


def put(root, name, body):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def test_inventory_deterministic_private_and_literal(tmp_path):
    text = 'Keep missing evidence unknown and preserve source files.\n'
    put(tmp_path, 'global.md', text)
    put(tmp_path, 'project.md', text + 'Domain rules.\n')
    put(tmp_path, 'config.toml', 'model = "gpt-6-astra"\nservice_tier = "priority"\napi_key = "SECRET"\n[agents]\ndefault_subagent_model = "gpt-6-sol"\n')
    manifest = {'files': [{'path': x, 'kind': 'config' if x.endswith('toml') else 'instructions'}
                          for x in ('global.md', 'project.md', 'config.toml')]}
    result = c.guidance_inventory(tmp_path, manifest)
    assert result == c.guidance_inventory(tmp_path, manifest)
    assert result['overlap'][0]['shared_normalized_lines'] == 1
    assert result['observed_runtime'] is None
    config_record = next(record for record in result['files'] if record['path'].endswith('/config.toml'))
    assert {"section": "agents", "key": "default_subagent_model", "value": "gpt-6-sol"} in config_record['literal_configured_settings']
    serialized = json.dumps(result)
    assert 'SECRET' not in serialized and text.strip() not in serialized
    assert 'priority' in serialized


def test_inventory_catalog_is_explicit_and_size_only(tmp_path):
    put(tmp_path, 'SKILL.md', '---\nname: test\ndescription: long body\n---\nSecret prose')
    result = c.guidance_inventory(tmp_path, {'files': [{'path': 'SKILL.md', 'kind': 'skill'}],
        'catalog': {'tools': ['private_tool'], 'skill_descriptions': ['private description']}})
    assert result['catalog']['supplied_tools'] == 1
    assert result['files'][0]['frontmatter_characters'] > 0
    assert 'private_tool' not in json.dumps(result)
    assert 'private description' not in json.dumps(result)


@pytest.mark.parametrize('exit_status', [0, 1, None])
def test_output_buried_failure_and_warning_do_not_become_pass(tmp_path, exit_status):
    raw = '\n'.join(['successful α'] * 120 + ['ERROR hidden failure', 'Warning: something'] + ['passed'] * 120)
    path = put(tmp_path, 'raw.log', raw)
    result = c.output_view(tmp_path, {'path': 'raw.log', 'command': 'pytest', 'exit_status': exit_status,
                                     'format': 'pytest', 'max_lines': 20})
    excerpt = '\n'.join(row['text'] for row in result['excerpt'])
    assert 'ERROR hidden failure' in excerpt and 'Warning: something' in excerpt
    assert result['semantic_outcome'] == 'not_assessed'
    assert result['exit_status'] == exit_status
    assert result['omitted_lines'] > 0
    assert path.read_text() == raw
    assert result['decoded_with_replacement'] is False


def test_output_preserves_failure_block_and_marks_overflow(tmp_path):
    raw = '\n'.join(['setup', '====== ERRORS ======', '_________ test_setup _________'] +
                    ['context'] * 30 + ['E   ValueError: bad', '====== short test summary info ======'])
    put(tmp_path, 'raw.log', raw)
    manifest = {'path': 'raw.log', 'command': 'pytest', 'exit_status': 1, 'format': 'pytest'}
    full = c.output_view(tmp_path, manifest)
    assert full['omitted_lines'] == 0
    short = c.output_view(tmp_path, {**manifest, 'max_lines': 12})
    assert short['omitted_failure_context_lines'] > 0


def test_raw_fallback_has_source_pointer_and_bad_unicode_flag(tmp_path):
    (tmp_path / 'raw.log').write_bytes(b'\xff\n' + b'line\n' * 100)
    result = c.output_view(tmp_path, {'path': 'raw.log', 'command': 'unknown', 'exit_status': None,
                                     'format': 'raw', 'max_lines': 8})
    assert result['decoded_with_replacement']
    assert result['raw']['bytes'] == 502
    assert len(result['excerpt']) == 8


def test_map_dirty_untracked_callers_scope_and_missing(tmp_path):
    put(tmp_path, 'a.py', 'from b import foo\ndef run():\n    return foo()\n')
    put(tmp_path, 'b.py', 'def foo():\n    return 1\n')
    snapshot = c.context_map(tmp_path, {'scope': ['b.py', 'a.py'], 'revision': 'same-head'})
    assert snapshot['files'][0]['calls'] == [{'name': 'foo', 'line': 3}]
    assert c.context_check(tmp_path, snapshot, {'scope': ['a.py', 'b.py']})['fresh']
    put(tmp_path, 'a.py', 'from b import foo\ndef run():\n    return foo() + 1\n')
    assert c.context_check(tmp_path, snapshot, {'scope': ['a.py', 'b.py']})['changed'] == ['a.py']
    put(tmp_path, 'new.py', 'untracked = True\n')
    assert c.context_check(tmp_path, snapshot, {'scope': ['a.py', 'b.py', 'new.py']})['added_scope'] == ['new.py']
    (tmp_path / 'b.py').unlink()
    assert c.context_check(tmp_path, snapshot, {'scope': ['a.py', 'b.py']})['missing'] == ['b.py']


def test_map_hash_workspace_binding_and_parse_limits(tmp_path):
    put(tmp_path, 'bad.py', 'def broken(')
    put(tmp_path, 'plain.txt', 'no parser')
    snapshot = c.context_map(tmp_path, {'scope': ['bad.py', 'plain.txt']})
    assert snapshot['files'][0]['parse_error'] == 'SyntaxError'
    assert snapshot['files'][1]['parse_error'].startswith('non-Python')
    other = tmp_path / 'other'
    other.mkdir()
    with pytest.raises(h.HarnessError, match='another workspace'):
        c.context_check(other, snapshot, {'scope': ['bad.py']})
    snapshot['files'][0]['symbols'].append({'fake': 'changed'})
    with pytest.raises(h.HarnessError, match='hash mismatch'):
        c.context_check(tmp_path, snapshot, {'scope': ['bad.py']})


def test_context_rejects_escape_and_bad_manifest(tmp_path):
    with pytest.raises(h.HarnessError):
        c.context_map(tmp_path, {'scope': ['../secret.py']})
    with pytest.raises(h.HarnessError):
        c.guidance_inventory(tmp_path, {'files': [None]})
    with pytest.raises(h.HarnessError):
        c.output_view(tmp_path, [])


def test_output_long_line_is_bounded_with_explicit_loss(tmp_path):
    put(tmp_path, 'raw.log', 'x' * 10000 + ' ERROR hidden near end')
    result = c.output_view(tmp_path, {'path': 'raw.log', 'command': 'report', 'exit_status': 1,
        'format': 'report', 'max_line_characters': 80})
    assert result['alert_lines'] == 1
    assert result['truncated_excerpt_lines'] == 1
    assert len(result['excerpt'][0]['text']) == 80
    assert result['excerpt'][0]['omitted_characters'] > 9900
