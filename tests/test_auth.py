import time
from fastapi.testclient import TestClient
from backend.main import app
from backend.services.auth import totp

def create_account(client, email):
    started = client.post('/shopping/auth/register', json={'email':email,'password':'a-long-test-password'})
    assert started.status_code == 200, started.text
    data = started.json()
    code = totp(data['secret'], int(time.time() // 30))
    verified = client.post('/shopping/auth/verify', json={'challenge':data['challenge'],'code':code})
    assert verified.status_code == 200, verified.text
    return data, verified.json()

def test_mfa_protects_shopping_and_recovery_is_one_time():
    with TestClient(app) as client:
        anonymous = {'Authorization':'Bearer '+client.post('/sessions').json()['token']}
        assert client.get('/shopping/cart',headers=anonymous).status_code == 401
        enrolled, session = create_account(client,'buyer@example.com')
        token = session['token']; headers = {'Authorization':'Bearer '+token}
        assert len(session['recovery_codes']) == 8
        assert client.get('/shopping/cart',headers=headers).status_code == 200
        assert client.get('/shopping/auth/me',headers=headers).json()['email'] == 'buyer@example.com'
        assert client.post('/shopping/auth/verify',json={'challenge':enrolled['challenge'],'code':totp(enrolled['secret'],int(time.time()//30))}).status_code == 401
        login = client.post('/shopping/auth/login',json={'email':'buyer@example.com','password':'a-long-test-password'}).json()
        assert login.get('challenge') and login['enrollment'] is False
        first = session['recovery_codes'][0]
        assert client.post('/shopping/auth/verify',json={'challenge':login['challenge'],'code':first}).status_code == 200
        another = client.post('/shopping/auth/login',json={'email':'buyer@example.com','password':'a-long-test-password'}).json()
        assert client.post('/shopping/auth/verify',json={'challenge':another['challenge'],'code':first}).status_code == 401
        assert client.post('/shopping/auth/verify',json={'challenge':another['challenge'],'code':session['recovery_codes'][1]}).status_code == 200
        assert client.post('/shopping/auth/logout',headers=headers).status_code == 200
        assert client.get('/shopping/cart',headers=headers).status_code == 401

def test_mfa_rejects_wrong_password_and_isolates_accounts():
    with TestClient(app) as client:
        _, first = create_account(client,'first@example.com')
        _, other = create_account(client,'second@example.com')
        assert client.post('/shopping/auth/login',json={'email':'first@example.com','password':'wrong-password-123'}).status_code == 401
        a = {'Authorization':'Bearer '+first['token']}
        b = {'Authorization':'Bearer '+other['token']}
        assert client.get('/shopping/conversations',headers=a).json() == []
        assert client.get('/shopping/conversations',headers=b).json() == []
