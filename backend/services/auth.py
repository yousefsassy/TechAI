"""Password + RFC 6238 authenticator login for shopping accounts."""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
import uuid
import smtplib
from email.message import EmailMessage
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from cryptography.fernet import Fernet
from sqlalchemy import select
from backend.config import settings
from backend.database import Session
from backend.models import ShoppingUser, ShoppingAuthChallenge, ShoppingAuthSession, ShoppingEmailChallenge, now

def _key_file():
    return Path(settings.database_url.removeprefix('sqlite:///')).with_suffix('.auth.key')

def _cipher():
    path = _key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(Fernet.generate_key())
    return Fernet(path.read_bytes())

def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def _password_hash(password):
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + ':' + key.hex()

def _password_matches(password, saved):
    try:
        salt, expected = saved.split(':')
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False

def totp(secret, counter):
    key = base64.b32decode(secret, casefold=True)
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = struct.unpack('>I', digest[offset:offset+4])[0] & 0x7fffffff
    return f'{value % 1000000:06d}'

def _challenge(db, user, purpose):
    token = secrets.token_urlsafe(32)
    db.add(ShoppingAuthChallenge(token_hash=_digest(token), user_id=user.id,
                                 purpose=purpose, expires_at=now()+timedelta(minutes=5)))
    return token

def register(email, password):
    normalized = str(email).strip().lower()
    with Session.begin() as db:
        if db.scalar(select(ShoppingUser).where(ShoppingUser.email == normalized)):
            raise ValueError('Account already exists')
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
        user = ShoppingUser(id=uuid.uuid4().hex, email=normalized,
                            password_hash=_password_hash(password),
                            totp_secret_encrypted=_cipher().encrypt(secret.encode()).decode(),
                            mfa_enabled=False, last_totp_counter=-1, recovery_hashes=[], failed_attempts=0)
        db.add(user)
        db.flush()
        challenge = _challenge(db, user, 'enroll')
    return dict(challenge=challenge, secret=secret,
                otpauth_uri=f'otpauth://totp/{quote("TechStock:" + normalized)}?secret={secret}&issuer=TechStock&algorithm=SHA1&digits=6&period=30')

def login(email, password):
    normalized = str(email).strip().lower()
    with Session.begin() as db:
        user = db.scalar(select(ShoppingUser).where(ShoppingUser.email == normalized))
        if not user: return None
        if user.locked_until and user.locked_until > now(): return None
        if not _password_matches(password, user.password_hash):
            _failure(user)
            return None
        # Unfinished enrollment requires setup again using the original secret.
        if not user.mfa_enabled:
            challenge = _challenge(db, user, 'enroll')
            secret = _cipher().decrypt(user.totp_secret_encrypted.encode()).decode()
            return dict(challenge=challenge, enrollment=True, secret=secret,
                        otpauth_uri=f'otpauth://totp/{quote("TechStock:" + normalized)}?secret={secret}&issuer=TechStock&algorithm=SHA1&digits=6&period=30')
        return dict(challenge=_challenge(db, user, 'login'), enrollment=False)

def _failure(user):
    user.failed_attempts += 1
    if user.failed_attempts >= 5:
        user.locked_until = now() + timedelta(minutes=15)
        user.failed_attempts = 0

def verify(challenge, code):
    """Consume a TOTP step or one recovery code; return a session only once."""
    with Session.begin() as db:
        pending = db.get(ShoppingAuthChallenge, _digest(challenge))
        if not pending or pending.expires_at <= now() or pending.attempts >= 5: return None
        user = db.get(ShoppingUser, pending.user_id)
        if not user or user.locked_until and user.locked_until > now(): return None
        counter = None
        if len(code) == 6 and code.isascii() and code.isdigit():
            secret = _cipher().decrypt(user.totp_secret_encrypted.encode()).decode()
            current = int(time.time() // 30)
            for candidate in (current, current-1):
                if candidate > user.last_totp_counter and hmac.compare_digest(totp(secret, candidate), code):
                    counter = candidate
                    break
        recovery_digest = _digest(code.upper().replace('-', ''))
        recovery_used = (pending.purpose == 'login' and user.mfa_enabled
                         and recovery_digest in user.recovery_hashes)
        if counter is None and not recovery_used:
            pending.attempts += 1
            _failure(user)
            return None
        db.delete(pending)
        user.failed_attempts = 0
        if counter is not None: user.last_totp_counter = counter
        recovery_codes = None
        if pending.purpose == 'enroll':
            user.mfa_enabled = True
            recovery_codes = [secrets.token_hex(8).upper() for _ in range(8)]
            user.recovery_hashes = [_digest(c) for c in recovery_codes]
        elif recovery_used:
            user.recovery_hashes = [c for c in user.recovery_hashes if c != recovery_digest]
        token = secrets.token_urlsafe(32)
        db.add(ShoppingAuthSession(token_hash=_digest(token), user_id=user.id,
                                   expires_at=now()+timedelta(hours=24)))
    return dict(token=token, email=user.email, recovery_codes=recovery_codes)

def authenticated_user(token):
    if not token or len(token) < 40: return None
    with Session() as db:
        user = db.execute(
            select(ShoppingUser.id, ShoppingUser.email)
            .join(ShoppingAuthSession, ShoppingAuthSession.user_id == ShoppingUser.id)
            .where(ShoppingAuthSession.token_hash == _digest(token),
                   ShoppingAuthSession.expires_at > now(), ShoppingUser.mfa_enabled.is_(True))
        ).one_or_none()
        return dict(id=user.id, email=user.email) if user else None

def logout(token):
    with Session.begin() as db:
        active = db.get(ShoppingAuthSession, _digest(token))
        if active: db.delete(active)

def _email_ready(email):
    if (settings.email_mode != 'real' or not settings.smtp_host or not settings.smtp_user
            or not settings.smtp_password or not settings.smtp_from):
        raise ValueError('Real email delivery is not configured. Set EMAIL_MODE=real and SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM in .env.')

def _send_code(email, code):
    message = EmailMessage()
    message['From'] = settings.smtp_from
    message['To'] = email
    message['Subject'] = 'TechStock sign-in code'
    message.set_content(f'Your TechStock sign-in code is {code}. It expires in 5 minutes. If you did not request it, ignore this message.')
    if settings.smtp_port == 465:
        transport = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10)
    else:
        transport = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
    with transport as smtp:
        if settings.smtp_port != 465: smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)

def _email_challenge(db, user):
    token = secrets.token_urlsafe(32)
    code = f'{secrets.randbelow(1000000):06d}'
    db.add(ShoppingEmailChallenge(token_hash=_digest(token), user_id=user.id,
                                  code_hash=_digest(token+':'+code),
                                  expires_at=now()+timedelta(minutes=5)))
    return token, code

def register_email(email, password):
    normalized = str(email).strip().lower()
    _email_ready(normalized)
    with Session.begin() as db:
        if db.scalar(select(ShoppingUser).where(ShoppingUser.email == normalized)):
            raise ValueError('Account already exists. Sign in instead.')
        # Keep a random encrypted TOTP seed for compatibility with existing accounts.
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
        user = ShoppingUser(id=uuid.uuid4().hex, email=normalized,
                            password_hash=_password_hash(password),
                            totp_secret_encrypted=_cipher().encrypt(secret.encode()).decode(),
                            mfa_enabled=False, last_totp_counter=-1, recovery_hashes=[], failed_attempts=0)
        db.add(user); db.flush()
        token, code = _email_challenge(db, user)
    try:
        _send_code(normalized, code)
    except (OSError, smtplib.SMTPException):
        with Session.begin() as db:
            challenge = db.get(ShoppingEmailChallenge, _digest(token))
            if challenge: db.delete(challenge)
        raise ValueError('Could not send the email code. Check SMTP settings and try signing in again.') from None
    return dict(challenge=token, delivery='email')

def login_email(email, password):
    normalized = str(email).strip().lower()
    _email_ready(normalized)
    with Session.begin() as db:
        user = db.scalar(select(ShoppingUser).where(ShoppingUser.email == normalized))
        if not user or user.locked_until and user.locked_until > now(): return None
        if not _password_matches(password, user.password_hash):
            _failure(user)
            return None
        if user.failed_attempts >= 4: return None
        recent = db.scalar(select(ShoppingEmailChallenge).where(
            ShoppingEmailChallenge.user_id == user.id,
            ShoppingEmailChallenge.expires_at > now()+timedelta(minutes=4)
        ).limit(1))
        if recent: raise ValueError('Please wait one minute before requesting another email code.')
        token, code = _email_challenge(db, user)
    try:
        _send_code(normalized, code)
    except (OSError, smtplib.SMTPException):
        with Session.begin() as db:
            challenge = db.get(ShoppingEmailChallenge, _digest(token))
            if challenge: db.delete(challenge)
        raise ValueError('Could not send the email code. Check SMTP settings and try again.') from None
    return dict(challenge=token, delivery='email')

def verify_email(challenge, code):
    if len(code) != 6 or not code.isascii() or not code.isdigit(): return None
    with Session.begin() as db:
        pending = db.get(ShoppingEmailChallenge, _digest(challenge))
        if not pending or pending.expires_at <= now() or pending.attempts >= 5: return None
        user = db.get(ShoppingUser, pending.user_id)
        if not user or user.locked_until and user.locked_until > now(): return None
        if not hmac.compare_digest(pending.code_hash, _digest(challenge+':'+code)):
            pending.attempts += 1
            _failure(user)
            return None
        db.delete(pending)
        user.mfa_enabled = True
        user.failed_attempts = 0
        token = secrets.token_urlsafe(32)
        db.add(ShoppingAuthSession(token_hash=_digest(token),user_id=user.id,
                                   expires_at=now()+timedelta(hours=24)))
    return dict(token=token,email=user.email)
