"""External-BI connectors (Tableau, BOBJ) — interface now, live implementation deferred (task 38).

Scenarios 9.7 (report schedules vs. chain completion) and 9.8 (dashboards reading calc views
directly) need metadata that only the BI tools hold:

- **Tableau** — the Metadata API (or ``workgroup`` repository read access) for published-datasource
  and workbook lineage plus extract-refresh schedules.
- **BOBJ** — the Query Builder / RESTful RaaS interface for report schedules.

Neither is reachable over the BW HANA connection. Until implemented, both classes report "not
configured" so the analyzers emit their timeline/parallel-path template with the connector named.
The BW side of 9.7 (feeding-chain p95 completion) is derivable without any connector.
"""

from __future__ import annotations

from typing import Literal

from .base import ConnectorStatus


class TableauConnector:
    """Deferred Tableau connector (Metadata API / workgroup). Never configured yet."""

    kind: Literal["tableau"] = "tableau"

    def is_configured(self) -> bool:
        return False

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(
            kind=self.kind,
            configured=False,
            detail="Tableau connector implementation deferred (Metadata API / workgroup repo)",
        )


class BobjConnector:
    """Deferred BusinessObjects connector (Query Builder / RESTful RaaS). Never configured yet."""

    kind: Literal["bobj"] = "bobj"

    def is_configured(self) -> bool:
        return False

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(
            kind=self.kind,
            configured=False,
            detail="BOBJ connector implementation deferred (Query Builder / RESTful RaaS)",
        )
