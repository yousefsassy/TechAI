from fastapi.testclient import TestClient
from backend.main import app
from backend.services import auth

def test_email_uses_configured_smtp_with_tls(monkeypatch):
    sent=[]
    class FakeSMTP:
        def __init__(self,host,port,timeout): sent.append((host,port,timeout))
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def starttls(self): sent.append('tls')
        def login(self,user,password): sent.append(('login',user))
        def send_message(self,message): sent.append((message['To'],message['Subject'],message.get_content()))
    monkeypatch.setattr(auth.smtplib,'SMTP',FakeSMTP)
    monkeypatch.setattr(auth.settings,'smtp_host','smtp.example.com')
    monkeypatch.setattr(auth.settings,'smtp_port',587)
    monkeypatch.setattr(auth.settings,'smtp_user','sender@example.com')
    monkeypatch.setattr(auth.settings,'smtp_password','test-only-value')
    auth._send_code('buyer@example.com','123456')
    assert sent[0] == ('smtp.example.com',587,10)
    assert sent[1] == 'tls'
    assert sent[-1][0] == 'buyer@example.com' and '123456' in sent[-1][2]

def test_email_code_rejected_without_real_smtp():
    with TestClient(app) as client:
        result=client.post('/shopping/auth/email/register',json={'email':'mfa@example.com','password':'very-long-password'})
        assert result.status_code == 503
        assert 'EMAIL_MODE=real' in result.json()['detail']

def test_email_registration_login_and_one_time_code(monkeypatch):
    monkeypatch.setattr(auth.settings,'email_mode','real')
    monkeypatch.setattr(auth.settings,'smtp_host','smtp.example.com')
    monkeypatch.setattr(auth.settings,'smtp_user','sender@example.com')
    monkeypatch.setattr(auth.settings,'smtp_password','test-only-value')
    monkeypatch.setattr(auth.settings,'smtp_allowed_recipients','mfa@example.com')
    sent=[]
    monkeypatch.setattr(auth,'_send_code',lambda email,code:sent.append((email,code)))
    with TestClient(app) as client:
        started=client.post('/shopping/auth/email/register',json={'email':'mfa@example.com','password':'very-long-password'})
        assert started.status_code == 200, started.text
        assert 'code' not in started.json() and len(sent)==1
        challenge=started.json()['challenge']
        bad=client.post('/shopping/auth/email/verify',json={'challenge':challenge,'code':'999999' if sent[0][1]!='999999' else '888888'})
        assert bad.status_code==401
        success=client.post('/shopping/auth/email/verify',json={'challenge':challenge,'code':sent[0][1]})
        assert success.status_code==200, success.text
        assert client.post('/shopping/auth/email/verify',json={'challenge':challenge,'code':sent[0][1]}).status_code==401
        bearer={'Authorization':'Bearer '+success.json()['token']}
        assert client.get('/shopping/cart',headers=bearer).status_code==200
        incorrect=client.post('/shopping/auth/email/login',json={'email':'mfa@example.com','password':'bad-password-123'})
        assert incorrect.status_code==401
        login=client.post('/shopping/auth/email/login',json={'email':'mfa@example.com','password':'very-long-password'})
        assert login.status_code==200
        assert client.post('/shopping/auth/email/verify',json={'challenge':login.json()['challenge'],'code':sent[-1][1]}).status_code==200

def test_email_signup_allows_other_recipient_with_one_sender(monkeypatch):
    monkeypatch.setattr(auth.settings,'email_mode','real')
    monkeypatch.setattr(auth.settings,'smtp_host','smtp.gmail.com')
    monkeypatch.setattr(auth.settings,'smtp_user','sender@gmail.com')
    monkeypatch.setattr(auth.settings,'smtp_password','test-only-value')
    monkeypatch.setattr(auth.settings,'smtp_allowed_recipients','sender@gmail.com')
    sent=[]
    monkeypatch.setattr(auth,'_send_code',lambda address,code:sent.append(address))
    with TestClient(app) as client:
        result=client.post('/shopping/auth/email/register',json={'email':'different@example.com','password':'very-long-password'})
        assert result.status_code==200, result.text
        assert sent==['different@example.com']
