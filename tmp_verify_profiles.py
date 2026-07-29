import os
import sys
from pathlib import Path

root = Path('.').resolve()
sys.path.insert(0, str(root / 'src'))
for line in (root / '.env').read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if line and '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
from mcp_server_sapbw.core.profiles import ProfileManager
pm = ProfileManager(str(root / 'profiles.yaml'))
qa = pm.get('qa')
ecc = pm.get_ecc('ecc_qa')
print('BW_OK', qa.host, qa.port, qa.user)
print('ECC_OK', ecc.host, ecc.port, ecc.use_tls, ecc.allow_plain_http)
