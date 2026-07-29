import os
import socket
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

print('BW_HANA_HOST', qa.host)
print('BW_HANA_PORT', qa.port)
print('BW_HANA_USER', qa.user)
print('BW_APP_HOST', os.environ.get('BW_QA_ABAP_ASHOST'))
print('BW_APP_SYSNR', os.environ.get('BW_QA_ABAP_SYSNR'))
print('BW_APP_USER', os.environ.get('BW_QA_ABAP_USER'))
print('ECC_HOST', ecc.host)
print('ECC_PORT', ecc.port)
print('ECC_USE_TLS', ecc.use_tls)
print('ECC_ALLOW_PLAIN_HTTP', ecc.allow_plain_http)

# BW HANA probe
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
    print('BW_HANA_RESULT', rows)
    cur.close()
    conn.close()
except Exception as exc:
    print('BW_HANA_ERROR', type(exc).__name__, str(exc))

# BW ABAP application-layer probe: RFC gateway port heuristic
try:
    host = os.environ['BW_QA_ABAP_ASHOST']
    sysnr = os.environ['BW_QA_ABAP_SYSNR']
    port = int(sysnr) + 3200
    with socket.create_connection((host, port), timeout=10):
        print('BW_APP_SOCKET_OK', host, port)
except Exception as exc:
    print('BW_APP_SOCKET_ERROR', type(exc).__name__, str(exc))

# ECC ADT probe
try:
    import httpx
    client = httpx.Client(
        base_url=f"http://{ecc.host}:{ecc.port}",
        auth=(ecc.user, ecc.password.get_secret_value()),
        timeout=15.0,
        verify=False,
        follow_redirects=False,
    )
    response = client.get('/sap/bc/adt/discovery', params={'sap-client': ecc.client})
    print('ECC_STATUS', response.status_code)
    print('ECC_BODY', response.text[:400])
    client.close()
except Exception as exc:
    print('ECC_ERROR', type(exc).__name__, str(exc))
