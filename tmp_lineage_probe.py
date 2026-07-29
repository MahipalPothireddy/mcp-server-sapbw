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

rt = ServerRuntime(ProfileManager(str(root / 'profiles.yaml')), ReadOnlyConnectionPool(), CapabilityResolver())
svc = rt.lineage('qa')
name = 'REDACTED_CUSTOMER_OBJECT'
results = {}
results['lineage'] = svc.get_lineage(name, direction='both', depth=6).model_dump()
results['trace_to_source'] = svc.trace_to_source(name, depth=8).model_dump()
results['impact_analysis'] = svc.impact_analysis(name, depth=3).model_dump()
Path('output/lineage_probe.json').write_text(__import__('json').dumps(results, indent=2), encoding='utf-8')
print('wrote', root / 'output' / 'lineage_probe.json')
