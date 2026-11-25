from scripts import configure_gmail

def test_gmail_configuration_preserves_existing_keys(tmp_path, monkeypatch):
    env = tmp_path / '.env'
    env.write_text('OPENAI_API_KEY=keep-this-key\nEMAIL_MODE=sandbox\nSMTP_HOST=localhost\nSMTP_PORT=1025\n')
    monkeypatch.setattr(configure_gmail, 'ENV', env)
    monkeypatch.setattr(configure_gmail, 'ROOT', tmp_path)
    configure_gmail.update_env({'EMAIL_MODE':'real', 'SMTP_HOST':'smtp.gmail.com',
                                'SMTP_PORT':'587', 'SMTP_USER':'sender@gmail.com'})
    contents = env.read_text()
    assert 'OPENAI_API_KEY=keep-this-key' in contents
    assert 'SMTP_HOST=smtp.gmail.com' in contents
    assert 'EMAIL_MODE=real' in contents
    assert 'SMTP_USER=sender@gmail.com' in contents
