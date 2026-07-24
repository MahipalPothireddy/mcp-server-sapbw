"""ECC extractor-enhancement connector — interface now, live implementation deferred (task 38).

Scenario 9.6 needs the enhancement *logic* (CMOD/BAdI ABAP and the extractor definition in
``ROOSOURCE``/``ROOSFIELD``), which lives in the **ECC source system** — a separate database on
**SQL Server, not HANA**. A live connector would therefore use a distinct driver (``pyodbc`` with
*ODBC Driver 18 for SQL Server*), schema typically ``SAP<SID>``, with every table name validated at
connect time exactly as the BW capability resolver does, and scoped to the enhancement **inventory
only** (``ROOSOURCE``, ``ROOSFIELD``, ``DD02L``/``DD03L`` append structures, ``MODSAP``/``MODACT``,
``SXS_ATTR``/``SXC_EXIT``, ``ENHHEADER``/``ENHOBJ``). It cannot read ABAP source: ``REPOSRC.DATA``
is compressed on every platform, so exit source needs a source bundle (task 37), not this connector.

Until implemented, this class reports "not configured" so scenario 9.6 documents the gap. The
BW-side 9.6 heuristic (customer-namespace fields in ``RSDSSEGFD``) runs without any connector.
"""

from __future__ import annotations

from typing import Literal

from .base import ConnectorStatus


class EccConnector:
    """Deferred ECC connector. Satisfies the connector interface but is never configured yet."""

    kind: Literal["ecc"] = "ecc"

    def is_configured(self) -> bool:
        return False

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(
            kind=self.kind,
            configured=False,
            detail=(
                "ECC connector implementation deferred (SQL Server / pyodbc, enhancement inventory "
                "only; ABAP source needs a source bundle)"
            ),
        )
