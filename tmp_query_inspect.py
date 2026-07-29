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

from mcp_server_sapbw.server import ServerRuntime
from mcp_server_sapbw.core.profiles import ProfileManager
from mcp_server_sapbw.core.connection import ReadOnlyConnectionPool
from mcp_server_sapbw.core.capabilities import CapabilityResolver
from mcp_server_sapbw.repositories.queries import QueriesRepository

rt = ServerRuntime(ProfileManager(str(root / 'profiles.yaml')), ReadOnlyConnectionPool(), CapabilityResolver())
conn = rt._connection('qa')
cap = rt.capability('qa')
qr = QueriesRepository(conn, cap)
header = qr._header('REDACTED_CUSTOMER_OBJECT')
print('header', header)
if header is not None:
    compuid = header[0]
    providers = qr._providers_list(compuid)
    print('providers', providers)
    eltuids, edges, truncated = qr._element_tree(compuid)
    print('eltuids_count', len(eltuids), 'truncated', truncated)
    print('first_20', list(eltuids)[:20])
    restrictions = qr._restrictions(list(eltuids))
    print('restriction_count', len(restrictions))
    print('infoobjects', sorted(qr._referenced_infoobjects(list(eltuids), restrictions))[:50])
