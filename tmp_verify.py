import sys
from pathlib import Path
sys.path.insert(0, str(Path('.').resolve() / 'src'))
from mcp_server_sapbw.core.profiles import ProfileManager

pm = ProfileManager()
print('profiles', pm.names())
qa = pm.get('qa')
print('qa host', qa.host)
print('qa port', qa.port)
print('qa encrypt', qa.encrypt)
print('qa ssl_validate_certificate', qa.ssl_validate_certificate)
print('qa read_only_user', qa.read_only_user)
ecc = pm.get_ecc('ecc_qa')
print('ecc host', ecc.host)
print('ecc port', ecc.port)
print('ecc use_tls', ecc.use_tls)
print('ecc ssl_validate_certificate', ecc.ssl_validate_certificate)
