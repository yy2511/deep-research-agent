from pathlib import Path
import pytest
from dra import skills

TEXT = '---\nname: compare\ndescription: >\n  Compare options\n  with evidence.\nmetadata:\n  display-name: 方案对比\n---\nCompare costs and uncertainty.\n'

@pytest.fixture
def repo(tmp_path):
    return skills.SkillRepository(tmp_path, builtin_dir=tmp_path / 'builtin')


def test_yaml_and_reference_snapshot_survives_edit(repo):
    repo.save('compare', TEXT, {'references/check.md': 'Check units.'})
    repo.set_enabled(['compare'])
    bundle = repo.prepare(['compare'])
    repo.save('compare', TEXT.replace('Compare costs', 'Changed costs'), {})
    restored = repo.resolve(bundle['id'])
    assert restored == bundle
    assert restored['skills']['compare']['references']['references/check.md'] == 'Check units.'
    assert 'Compare costs' in restored['skills']['compare']['body']
    assert 'Compare options with evidence.' in restored['skills']['compare']['description']
    assert repo.prepare([])['id'] != bundle['id']


def test_bad_package_isolated_and_disabled_not_disclosed(repo):
    repo.save('compare', TEXT, {})
    bad = repo.root / 'broken'
    bad.mkdir(parents=True)
    (bad / 'SKILL.md').write_text('---\nname: broken\n---\nBody')
    listing = repo.list()
    assert len(listing['skills']) == 2
    assert next(s for s in listing['skills'] if s['name'] == 'broken')['error']
    assert repo.prepare([])['skills'] == {}
    with pytest.raises(skills.SkillError):
        repo.prepare(['compare'])


@pytest.mark.parametrize('path', ['../escape.md', '/tmp/escape.md', 'references/../../escape.md', 'scripts/run.py'])
def test_reference_paths_rejected(repo, path):
    with pytest.raises(skills.SkillError):
        repo.save('compare', TEXT, {path: 'no'})


def test_symlink_and_invalid_yaml(repo, tmp_path):
    repo.root.mkdir(parents=True)
    (repo.root / 'compare').symlink_to(tmp_path / 'outside', target_is_directory=True)
    with pytest.raises(skills.SkillError):
        repo.save('compare', TEXT, {})
    for text in [TEXT.replace('name: compare', 'name: wrong'), TEXT.replace('name: compare', 'name: compare\nname: compare')]:
        with pytest.raises(skills.SkillError):
            skills.parse_skill('compare', text, {})


def test_configured_data_directory_may_have_symlink_ancestors(tmp_path):
    actual = tmp_path / 'actual'
    actual.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(actual, target_is_directory=True)
    repo = skills.SkillRepository(alias, builtin_dir=tmp_path / 'none')
    repo.save('compare', TEXT, {})
    assert not repo.list()['skills'][0].get('error')


def test_edit_preserves_unmanaged_assets(repo):
    repo.save('compare', TEXT, {})
    asset = repo.root / 'compare' / 'assets' / 'template.bin'
    asset.parent.mkdir()
    asset.write_bytes(b'keep this asset')
    repo.save('compare', TEXT.replace('Compare costs', 'Changed costs'), {})
    assert asset.read_bytes() == b'keep this asset'


def test_bad_yaml_keeps_editable_draft_and_references(repo):
    repo.save('compare', TEXT, {'references/check.md':'keep me'})
    (repo.root / 'compare' / 'SKILL.md').write_text(TEXT.replace('name: compare','name: wrong'))
    entry = repo.list()['skills'][0]
    assert entry['error']
    assert 'name: wrong' in entry['text']
    assert entry['references']['references/check.md'] == 'keep me'


def test_failed_replacement_restores_existing_package(repo, monkeypatch):
    repo.save('compare', TEXT, {})
    original = Path.rename
    def fail_package(path, target):
        if path.name == 'package':
            raise OSError('simulated replace failure')
        return original(path, target)
    monkeypatch.setattr(Path, 'rename', fail_package)
    with pytest.raises(OSError):
        repo.save('compare', TEXT.replace('Compare costs','Changed costs'), {})
    assert repo.list()['skills'][0]['text'] == TEXT


def test_metadata_changes_version_and_reference_symlinks_rejected(repo, tmp_path):
    repo.save('compare', TEXT, {})
    repo.set_enabled(['compare'])
    before = repo.prepare([])
    repo.save('compare', TEXT.replace('Compare options', 'Choose options'), {})
    after = repo.prepare([])
    assert before['skills']['compare']['hash'] != after['skills']['compare']['hash']
    refs = repo.root / 'compare' / 'references'
    refs.mkdir()
    (refs / 'outside.md').symlink_to(tmp_path / 'outside.md')
    assert repo.list()['skills'][0]['error']


def test_edit_builtin_preserves_default_enabled_state(tmp_path):
    builtin = tmp_path / 'builtin' / 'compare'
    builtin.mkdir(parents=True)
    (builtin / 'SKILL.md').write_text(TEXT)
    repo = skills.SkillRepository(tmp_path / 'data', builtin_dir=builtin.parent)
    assert repo.list()['skills'][0]['enabled']
    repo.save('compare', TEXT.replace('Compare costs','Changed costs'), {})
    assert repo.list()['skills'][0]['enabled']
