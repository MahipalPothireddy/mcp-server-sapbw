import os
import sys
from pathlib import Path

root = Path('.').resolve()
sys.path.insert(0, str(root / 'src'))

# Load .env manually (same format as the server bootstrap)
for path in [root / '.env', root / 'profiles.yaml']:
    pass

env = {}
for line in (root / '.env').read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if not line or line.startswith('#') or '=' not in line:
        continue
    k, v = line.split('=', 1)
    env[k.strip()] = v.strip().strip('"').strip("'")
for k, v in env.items():
    os.environ.setdefault(k, v)

from mcp_server_sapbw.core.profiles import ProfileManager

pm = ProfileManager(str(root / 'profiles.yaml'))
qa = pm.get('qa')
ecc = pm.get_ecc('ecc_qa')
print('BW profile: host=', qa.host, 'port=', qa.port, 'encrypt=', qa.encrypt)
print('ECC profile: host=', ecc.host, 'port=', ecc.port, 'use_tls=', ecc.use_tls)

try:
    import hdbcli.dbapi as dbapi
    conn = dbapi.connect(
        address=qa.host,
        port=qa.port,
        user=qa.user,
        password=qa.password.get_secret_value(),
        encrypt=qa.encrypt,
        sslValidateCertificate=qa.ssl_validate_certificate,
    )
    cur = conn.cursor()
    cur.execute('SELECT CURRENT_USER FROM DUMMY')
    rows = cur.fetchall()
    print('BW_HANA_OK', rows)
    cur.close()
    conn.close()
except Exception as exc:
    print('BW_HANA_FAIL', type(exc).__name__, exc)

try:
    import httpx
    client = httpx.Client(
        base_url=f"{'https' if ecc.use_tls else 'http'}://{ecc.host}:{ecc.port}",
        auth=(ecc.user, ecc.password.get_secret_value()),
        timeout=10.0,
        verify=ecc.ssl_validate_certificate,
        follow_redirects=False,
    )
    response = client.get('/sap/bc/adt/discovery', params={'sap-client': ecc.client})
    print('ECC_ADT_STATUS', response.status_code)
    print('ECC_ADT_BODY', response.text[:400])
    client.close()
except Exception as exc:
    print('ECC_ADT_FAIL', type(exc).__name__, exc)
