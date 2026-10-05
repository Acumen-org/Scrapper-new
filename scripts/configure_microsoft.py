"""Provision sign-in from a deployment secret passed on stdin, never argv."""
import json
import sys
from prospect import db, settings, msauth


def main():
    values = json.load(sys.stdin)
    for name in ('tenant_id', 'client_id', 'client_secret', 'public_url'):
        if not isinstance(values.get(name), str) or not values[name].strip():
            raise SystemExit('Microsoft configuration is incomplete; no changes made.')
    if not all(msauth.GUID_RE.fullmatch(values[k]) for k in ('tenant_id', 'client_id')):
        raise SystemExit('Microsoft configuration IDs are invalid.')
    if values['public_url'] != 'https://bellwether.pmx.acumen-strategy.com':
        raise SystemExit('Unexpected public URL; no changes made.')
    c = db.connect()
    settings.init(c)
    c.close()
    for key, value in {
        'ms.tenant_id': values['tenant_id'], 'ms.client_id': values['client_id'],
        'ms.client_secret': values['client_secret'], 'app.public_url': values['public_url'],
        'auth.allowed_domains': 'acumen-strategy.com',
        'auth.admins': 'rahul.gopan@acumen-strategy.com',
    }.items():
        settings.set(key, value, by='deployment')
    print('Microsoft sign-in configured. Credentials are encrypted at rest.')


if __name__ == '__main__':
    main()
