"""Owner bootstrap/recovery previews only synthetic identity before confirmation."""
from dra import admin
from dra.api.services import auth, user_management
from dra.db_tables import User


def test_cli_previews_identity_before_confirmation(database, monkeypatch, capsys):
    user = auth.create_user(database, 'synthetic-admin@example.test', 'synthetic-password', role='admin')
    with database.session() as session:
        session.get(User, user.id).display_name = '合成管理员'
    monkeypatch.setattr(admin, '_ready_database', lambda: database)
    def decline(_prompt):
        output = capsys.readouterr().out
        assert '合成管理员' in output and 'synthetic-admin@example.test' in output
        assert '角色=admin' in output and '状态=active' in output
        return 'n'
    monkeypatch.setattr('builtins.input', decline)
    assert admin.main(['bootstrap-owner', '--user-id', str(user.id)]) == 1
    with database.session() as session:
        assert session.get(User, user.id).role == 'admin'


def test_cli_invalid_target_is_rejected_before_prompt(database, monkeypatch):
    user = auth.create_user(database, 'synthetic-user@example.test', 'synthetic-password', role='user')
    monkeypatch.setattr(admin, '_ready_database', lambda: database)
    monkeypatch.setattr('builtins.input', lambda _: (_ for _ in ()).throw(AssertionError('must not prompt')))
    assert admin.main(['bootstrap-owner', '--user-id', str(user.id)]) == 2


def test_recover_previews_both_accounts(database, monkeypatch, capsys):
    old = auth.create_user(database, 'synthetic-old@example.test', 'synthetic-password', role='admin')
    new = auth.create_user(database, 'synthetic-new@example.test', 'synthetic-password', role='admin')
    user_management.bootstrap_owner(database, old.id)
    monkeypatch.setattr(admin, '_ready_database', lambda: database)
    def decline(_prompt):
        output = capsys.readouterr().out
        assert 'synthetic-old@example.test' in output and 'synthetic-new@example.test' in output
        assert '角色=owner' in output and '角色=admin' in output and '合成恢复' in output
        return 'n'
    monkeypatch.setattr('builtins.input', decline)
    assert admin.main(['recover-owner', '--old-owner-id', str(old.id), '--user-id', str(new.id), '--reason', '合成恢复']) == 1
