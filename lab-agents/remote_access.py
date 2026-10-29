"""LAN password gate using the browser's native login dialog."""
import hmac
import ipaddress
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit
from flask import Response, request


def is_loopback(value):
    try:return ipaddress.ip_address(value).is_loopback
    except ValueError:return value.lower()=='localhost'


def local_request():
    # Never trust Forwarded/X-Forwarded-For for the local password exemption.
    # Checking Host too prevents a LAN hostname from borrowing this exemption.
    host=urlsplit('http://'+request.host).hostname or ''
    return is_loopback(request.remote_addr or '') and is_loopback(host)


def remote_password(root):
    """Use an operator-provided secret, or persist a generated one privately."""
    configured=os.getenv('LAB_REMOTE_PASSWORD')
    path=Path(root)/'.remote-access-password'
    if configured is not None:
        password=configured
    else:
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:
            if path.is_symlink():raise ValueError('Remote password file must not be a symlink')
            password=path.read_text().strip()
            path.chmod(0o600)
        else:
            password=secrets.token_urlsafe(18)
            with os.fdopen(fd,'w') as out:out.write(password+'\n')
    if len(password)<12:raise ValueError('LAB_REMOTE_PASSWORD must contain at least 12 characters')
    return password, None if configured is not None else path


def install_remote_access(app,password=None):
    secret=password if password is not None else os.getenv('LAB_REMOTE_PASSWORD')
    if secret is not None and len(secret)<12:raise ValueError('Remote password must contain at least 12 characters')

    @app.before_request
    def password_gate():
        if local_request():return None
        if not secret:
            return Response('Remote access is not configured. Start with --host 0.0.0.0.\n',503,
                            headers={'Cache-Control':'no-store'})
        auth=request.authorization
        username=auth.username if auth and auth.type=='basic' else ''
        supplied=auth.password if auth and auth.type=='basic' else ''
        if not (hmac.compare_digest((username or '').encode(),b'lab') and
                hmac.compare_digest((supplied or '').encode(),secret.encode())):
            return Response('Enter the remote-access username and password.\n',401,
                            headers={'WWW-Authenticate':'Basic realm="Lab Agents", charset="UTF-8"',
                                     'Cache-Control':'no-store'})
        return None

    @app.after_request
    def private_response(response):
        if not local_request():
            response.vary.add('Authorization')
            response.headers['Cache-Control']='no-store'
        return response
