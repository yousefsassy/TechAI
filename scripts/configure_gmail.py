"""Configure Gmail SMTP locally without putting an app password in shell history."""
import argparse
import getpass
import os
import re
import tempfile
from pathlib import Path

from email_validator import validate_email, EmailNotValidError

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / '.env'

def update_env(values):
    if not ENV.is_file():
        raise SystemExit('Missing .env. Copy .env.example to .env first.')
    lines = ENV.read_text(encoding='utf-8').splitlines()
    found = set()
    result = []
    for line in lines:
        match = re.match(r'^([A-Z][A-Z0-9_]*)=', line)
        if match and match[1] in values:
            key = match[1]
            if key not in found:
                result.append(f'{key}={values[key]}')
                found.add(key)
            continue
        result.append(line)
    result.extend(f'{key}={value}' for key, value in values.items() if key not in found)
    fd, temp = tempfile.mkstemp(prefix='.env-gmail-', dir=ROOT)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write('\n'.join(result) + '\n')
        os.chmod(temp, 0o600)
        os.replace(temp, ENV)
    finally:
        if os.path.exists(temp): os.unlink(temp)

def main():
    parser = argparse.ArgumentParser(description='Configure Gmail SMTP for email sign-in codes')
    parser.add_argument('--prepare', action='store_true', help='Set Gmail host/port without activating email delivery')
    args = parser.parse_args()
    if args.prepare:
        update_env({'SMTP_HOST': 'smtp.gmail.com', 'SMTP_PORT': '587'})
        print('Gmail SMTP host and port are prepared. Run this script without --prepare to enter your address and app password locally.')
        return
    address = input('Gmail sender address: ').strip().lower()
    try: address = validate_email(address, check_deliverability=False).normalized
    except EmailNotValidError: raise SystemExit('Enter a valid Gmail sender address.') from None
    if not address.endswith('@gmail.com'):
        raise SystemExit('This setup script expects a @gmail.com sender. Configure Google Workspace SMTP manually.')
    password = ''.join(getpass.getpass('Gmail app password (hidden): ').split())
    if len(password) != 16 or not password.isascii():
        raise SystemExit('A Gmail app password has 16 characters. No settings were changed.')
    update_env({'EMAIL_MODE':'real', 'SMTP_HOST':'smtp.gmail.com', 'SMTP_PORT':'587',
                'SMTP_USER':address, 'SMTP_PASSWORD':password, 'SMTP_FROM':address,
                'SMTP_ALLOWED_RECIPIENTS':address})
    print('Gmail delivery configured for this address. Restart the backend. The app password was not printed.')

if __name__ == '__main__':
    main()
