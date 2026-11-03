"""用途、用户、服务器及端点绑定的 Fernet 凭据；不读本机 SSH 文件。"""

import base64
import binascii
import json

from cryptography.fernet import Fernet, InvalidToken

from app.errors import ServerCredentialDecryptionError
from app.models.server_connection import validate_credentials
from app.validators import validate_positive_integer


def validate_server_ciphertext(value):
    try:
        if not isinstance(value, str) or not 100 <= len(value) <= 131072:
            raise ValueError('invalid length')
        encoded = value.encode('ascii')
        decoded = base64.b64decode(encoded, altchars=b'-_', validate=True)
        if (len(decoded) < 73 or decoded[0] != 0x80 or (len(decoded) - 57) % 16
                or base64.urlsafe_b64encode(decoded) != encoded):
            raise ValueError('invalid envelope')
    except (ValueError, UnicodeError, binascii.Error):
        raise ValueError('服务器凭据必须是 Fernet 密文。') from None
    return value


class ServerCredentialCipher:
    def __init__(self, master_key):
        self._fernet = Fernet(master_key.encode('ascii'))

    def encrypt(self, user_id, profile, credentials):
        user_id = validate_positive_integer(user_id, '用户 ID')
        credentials = validate_credentials(profile.auth_method, **credentials)
        payload = {'version': 1, 'purpose': 'ssh-server-credential', 'user_id': user_id,
                   'binding': profile.binding(), 'credentials': credentials}
        return self._fernet.encrypt(json.dumps(payload, ensure_ascii=True, separators=(',', ':')).encode()).decode('ascii')

    def decrypt(self, user_id, profile):
        user_id = validate_positive_integer(user_id, '用户 ID')
        try:
            token = profile.encrypted_credentials
            if not isinstance(token, str) or not 100 <= len(token) <= 131072:
                raise ValueError('invalid ciphertext')
            payload = json.loads(self._fernet.decrypt(token.encode('ascii')))
            if (type(payload) is not dict or type(payload.get('version')) is not int or payload['version'] != 1
                    or payload.get('purpose') != 'ssh-server-credential' or type(payload.get('user_id')) is not int
                    or payload['user_id'] != user_id or payload.get('binding') != profile.binding()
                    or type(payload.get('credentials')) is not dict):
                raise ValueError('invalid binding')
            return validate_credentials(profile.auth_method, **payload['credentials'])
        except (InvalidToken, ValueError, TypeError, UnicodeError) as error:
            raise ServerCredentialDecryptionError('服务器凭据无法解密或归属不匹配。') from error
