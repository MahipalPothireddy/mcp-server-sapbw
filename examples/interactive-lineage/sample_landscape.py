"""A synthetic BW landscape, shaped like a real one.

**Every name here is invented.** No part of this file came from a customer system. The shape,
though, is deliberately realistic, because an example built on a tidy three-box flow would not
demonstrate the thing that makes the real output worth reading: a report sitting on a
CompositeProvider that unions five stores, two of which are loaded out of HANA calculation views
that read other BW objects and hand the result back as a full load.

Naming avoids the customer namespace (no token starting ``Z`` or ``Y``) and the generated-table
form (no ``/BIC/<name>`` literal) so the repository's customer-metadata scan passes on it. The
SAP-delivered ``0*`` InfoObjects are real SAP names, identical on every BW system, and carry no
customer information.

Field names mirror what the server actually returns, so this doubles as a reference for the shape
of a lineage payload.
"""

from __future__ import annotations

from typing import Any

GENERATED = "2026-10-03"

QUERY = {
    "name": "Q_SALES_MARGIN",
    "description": "Sales Margin by Customer and Material",
    "provider": "SALES_OVERVIEW_CP",
    "origin": "designed",
    "last_used": "2026-09-28",
    "days_since_used": 5,
}

PROVIDER = {
    "name": "SALES_OVERVIEW_CP",
    "description": "Sales Overview",
    "kind": "compositeprovider",
    "part_count": 5,
}

#: name -> (kind, description, origin). ``origin`` is the description subsystem's own verdict:
#: ``stored`` is BW's text verbatim, ``stored_augmented`` means the long text was synthesized
#: because what BW held added nothing. The page marks anything not purely stored.
OBJECTS: dict[str, tuple[str, str, str]] = {
    # source systems
    "ERP_PROD": ("src", "ERP production", "generated"),
    "LEGACY_DW": ("src", "legacy data warehouse", "generated"),
    "FLAT FILE": ("src", "file interface, no system of record", "generated"),
    # DataSources
    "DS_ORDER_ITEM": ("ds", "Sales order item", "stored"),
    "DS_ORDER_HEADER": ("ds", "Sales order header", "stored"),
    "DS_BILLING_ITEM": ("ds", "Billing document item", "stored"),
    "DS_DELIVERY_ITEM": ("ds", "Delivery item", "stored"),
    "DS_REBATE_ITEM": ("ds", "Rebate settlement item", "stored"),
    "DS_LEGACY_SALES": ("ds", "Legacy sales history", "stored"),
    "DS_BUDGET_FILE": ("ds", "Budget upload", "stored"),
    # staging and calc-view bases
    "STG_ORDER": ("adso", "Order staging", "stored_augmented"),
    "ORDER_HEADER": ("adso", "Order Header", "stored_augmented"),
    "DELIVERY_ITEM": ("adso", "Delivery Item", "stored_augmented"),
    "ORDER_COND": ("adso", "Order Conditions", "stored_augmented"),
    # the nested CompositeProvider a calculation view reads
    "ORDER_COND_CP": ("cp", "Order Conditions", "stored_augmented"),
    # calculation views
    "ACME.SALES/CV_OPEN_ORDERS": ("calc", "Open order quantity and open order value", "generated"),
    "ACME.SALES/CV_FREE_OF_CHARGE": (
        "calc",
        "Free-of-charge order lines with conditions",
        "generated",
    ),
    # the CompositeProviders that expose those views to BW
    "OPEN_ORDER_CP": ("cp", "Open Order Item", "stored"),
    "FOC_ORDER_CP": ("cp", "Free of Charge View", "stored"),
    # the five parts
    "ORDER_ITEM": ("adso", "Order Item", "stored_augmented"),
    "BILLING_ITEM": ("adso", "Billing Item", "stored_augmented"),
    "REBATE_ITEM": ("adso", "Rebate Item", "stored_augmented"),
    "LEGACY_SALES": ("adso", "Legacy Sales History", "stored"),
    "BUDGET_PLAN": ("adso", "Budget Plan", "stored"),
    # reporting layer
    "SALES_OVERVIEW_CP": ("cp", "Sales Overview", "stored_augmented"),
    "Q_SALES_MARGIN": ("query", "Sales Margin by Customer and Material", "stored"),
    # lookup targets
    "0CUSTOMER": ("iobj", "Customer", "stored_augmented"),
    "0MATERIAL": ("iobj", "Material", "stored_augmented"),
    "0PAYER": ("iobj", "Payer", "stored_augmented"),
    "0PLANT": ("iobj", "Plant", "stored_augmented"),
    "0COMP_CODE": ("iobj", "Company code", "stored_augmented"),
    "PARTNER_FN": ("adso", "Partner Functions", "stored_augmented"),
    "COST_CENTRE": ("adso", "Cost Centre Assignment", "stored"),
}

#: Which logical system each DataSource extracts from.
DS_SYSTEM = {
    "DS_ORDER_ITEM": "ERP_PROD",
    "DS_ORDER_HEADER": "ERP_PROD",
    "DS_BILLING_ITEM": "ERP_PROD",
    "DS_DELIVERY_ITEM": "ERP_PROD",
    "DS_REBATE_ITEM": "ERP_PROD",
    "DS_LEGACY_SALES": "LEGACY_DW",
    "DS_BUDGET_FILE": "FLAT FILE",
}

PARTS = ["ORDER_ITEM", "BILLING_ITEM", "REBATE_ITEM", "LEGACY_SALES", "BUDGET_PLAN"]

#: Explicit layer and within-layer order. Assigned rather than computed, because a readable
#: layered diagram needs a human decision about ordering that longest-path layout will not make.
LAYERS = [
    ("Source system", ["ERP_PROD", "LEGACY_DW", "FLAT FILE"]),
    (
        "DataSource",
        [
            "DS_ORDER_ITEM",
            "DS_ORDER_HEADER",
            "DS_BILLING_ITEM",
            "DS_DELIVERY_ITEM",
            "DS_REBATE_ITEM",
            "DS_LEGACY_SALES",
            "DS_BUDGET_FILE",
        ],
    ),
    ("Staging / calc-view base", ["STG_ORDER", "ORDER_HEADER", "DELIVERY_ITEM", "ORDER_COND"]),
    ("Nested CompositeProvider", ["ORDER_COND_CP"]),
    ("HANA calculation view", ["ACME.SALES/CV_OPEN_ORDERS", "ACME.SALES/CV_FREE_OF_CHARGE"]),
    (
        "Lookups / exposing CP",
        [
            "OPEN_ORDER_CP",
            "FOC_ORDER_CP",
            "0CUSTOMER",
            "0MATERIAL",
            "0PAYER",
            "0PLANT",
            "0COMP_CODE",
            "PARTNER_FN",
            "COST_CENTRE",
        ],
    ),
    ("Parts of SALES_OVERVIEW_CP", PARTS),
    ("Reporting CompositeProvider", ["SALES_OVERVIEW_CP"]),
    ("BEx query", ["Q_SALES_MARGIN"]),
]

#: Objects the diagram hides until the reader asks for them. Keeping the default view to the
#: spine is what makes a 40-node graph legible; the real reports hide far more.
LOOKUP_GROUP = {
    "0CUSTOMER",
    "0MATERIAL",
    "0PAYER",
    "0PLANT",
    "0COMP_CODE",
    "PARTNER_FN",
    "COST_CENTRE",
}

# (source, target, kind, label, basis, note)
#
# `kind` drives the stroke; `basis` is the honest part. `declared` means a metadata row states the
# relationship. `advisory` means it was parsed out of ABAP or resolved from a generated name, so
# the set of such edges is a lower bound rather than a complete answer.
EDGES: list[tuple[str, str, str, str, str, str]] = [
    # source system -> DataSource
    *[
        (
            DS_SYSTEM[ds],
            ds,
            "declared",
            "extracts from",
            "declared",
            "logical system read from the DataSource header",
        )
        for ds in DS_SYSTEM
    ],
    # DataSource -> staging / parts
    ("DS_ORDER_ITEM", "STG_ORDER", "declared", "loads (delta)", "declared", ""),
    ("DS_ORDER_HEADER", "ORDER_HEADER", "declared", "loads (delta)", "declared", ""),
    ("DS_DELIVERY_ITEM", "DELIVERY_ITEM", "declared", "loads (delta)", "declared", ""),
    ("DS_BILLING_ITEM", "BILLING_ITEM", "declared", "loads (delta)", "declared", ""),
    ("DS_REBATE_ITEM", "REBATE_ITEM", "declared", "loads (delta)", "declared", ""),
    ("DS_LEGACY_SALES", "LEGACY_SALES", "declared", "loads (delta)", "declared", ""),
    ("DS_BUDGET_FILE", "BUDGET_PLAN", "declared", "loads (full)", "declared", ""),
    ("STG_ORDER", "ORDER_ITEM", "declared", "loads (delta)", "declared", ""),
    (
        "ORDER_COND",
        "ORDER_COND_CP",
        "part",
        "part [J1]",
        "declared",
        "declared in the CompositeProvider's stored model",
    ),
    (
        "ORDER_HEADER",
        "ORDER_COND_CP",
        "part",
        "part [J2]",
        "declared",
        "declared in the CompositeProvider's stored model",
    ),
    (
        "0PAYER",
        "ORDER_COND_CP",
        "part",
        "part [J3]",
        "declared",
        "an InfoObject used as a join partner inside the CompositeProvider",
    ),
    # calc-view bases. The CompositeProvider base is the hop a /BIC/ table walk cannot see.
    (
        "ORDER_ITEM",
        "ACME.SALES/CV_OPEN_ORDERS",
        "calcbase",
        "read by the view",
        "declared",
        "direct base, confirmed against the activated view definition",
    ),
    (
        "BILLING_ITEM",
        "ACME.SALES/CV_OPEN_ORDERS",
        "calcbase",
        "read by the view",
        "declared",
        "direct base, confirmed against the activated view definition",
    ),
    (
        "DELIVERY_ITEM",
        "ACME.SALES/CV_OPEN_ORDERS",
        "calcbase",
        "read by the view",
        "declared",
        "direct base, confirmed against the activated view definition",
    ),
    ("ORDER_ITEM", "ACME.SALES/CV_FREE_OF_CHARGE", "calcbase", "read by the view", "declared", ""),
    (
        "ORDER_HEADER",
        "ACME.SALES/CV_FREE_OF_CHARGE",
        "calcbase",
        "read by the view",
        "declared",
        "",
    ),
    (
        "DELIVERY_ITEM",
        "ACME.SALES/CV_FREE_OF_CHARGE",
        "calcbase",
        "read by the view",
        "declared",
        "",
    ),
    (
        "ORDER_COND_CP",
        "ACME.SALES/CV_FREE_OF_CHARGE",
        "calcbase",
        "read by the view (CompositeProvider)",
        "declared",
        "a CompositeProvider has no generated table, so only the generated per-provider view "
        "resolves it",
    ),
    # calc view -> the CompositeProvider that exposes it -> the full load back into BW
    ("ACME.SALES/CV_OPEN_ORDERS", "OPEN_ORDER_CP", "expose", "exposed to BW", "declared", ""),
    ("ACME.SALES/CV_FREE_OF_CHARGE", "FOC_ORDER_CP", "expose", "exposed to BW", "declared", ""),
    (
        "OPEN_ORDER_CP",
        "ORDER_ITEM",
        "full",
        "loads (full)",
        "declared",
        "a full load out of a HANA view: content is replaced by whatever the view returns",
    ),
    (
        "FOC_ORDER_CP",
        "BILLING_ITEM",
        "full",
        "loads (full)",
        "declared",
        "a full load out of a HANA view: content is replaced by whatever the view returns",
    ),
    # self re-processing
    *[
        (
            p,
            p,
            "self",
            "re-processing (full)",
            "declared",
            "source and target are the same object, so the result depends on load order",
        )
        for p in ("ORDER_ITEM", "BILLING_ITEM", "REBATE_ITEM")
    ],
    # declared lookups
    (
        "0CUSTOMER",
        "ORDER_ITEM",
        "lookup",
        "declared lookup",
        "declared",
        "recorded in BW's typed rule-step tables, so it appears in BW's own where-used list",
    ),
    ("0MATERIAL", "ORDER_ITEM", "lookup", "declared lookup", "declared", ""),
    ("0PAYER", "BILLING_ITEM", "lookup", "declared lookup", "declared", ""),
    ("0COMP_CODE", "BILLING_ITEM", "lookup", "declared lookup", "declared", ""),
    ("0PLANT", "ORDER_ITEM", "lookup", "declared lookup", "declared", ""),
    ("0MATERIAL", "LEGACY_SALES", "lookup", "declared lookup", "declared", ""),
    # routine-derived reads: real, but a lower bound
    (
        "PARTNER_FN",
        "BILLING_ITEM",
        "routine",
        "read inside an end routine",
        "advisory",
        "parsed from a SELECT in ABAP - real, but the set of such edges is a lower bound",
    ),
    (
        "COST_CENTRE",
        "REBATE_ITEM",
        "routine",
        "read inside an end routine",
        "advisory",
        "parsed from a SELECT in ABAP - real, but the set of such edges is a lower bound",
    ),
    ("PARTNER_FN", "LEGACY_SALES", "routine", "read inside an end routine", "advisory", ""),
    ("ORDER_ITEM", "REBATE_ITEM", "routine", "read inside an end routine", "advisory", ""),
    # parts -> reporting CP -> query
    *[
        (
            p,
            "SALES_OVERVIEW_CP",
            "part",
            "part",
            "declared",
            "declared in the CompositeProvider's stored model",
        )
        for p in PARTS
    ],
    ("SALES_OVERVIEW_CP", "Q_SALES_MARGIN", "read", "read by the query", "declared", ""),
]

# --------------------------------------------------------------------- findings
# code, severity, headline, detail, evidence
FINDINGS: list[tuple[str, str, str, str, list[str]]] = [
    (
        "F1",
        "High",
        "Two closed BW &rarr; HANA &rarr; BW loops feed this report, and one reads a "
        "CompositeProvider.",
        "ORDER_ITEM is full-loaded from OPEN_ORDER_CP, backed by "
        "<code>ACME.SALES/CV_OPEN_ORDERS</code>; BILLING_ITEM from FOC_ORDER_CP, backed by "
        "<code>ACME.SALES/CV_FREE_OF_CHARGE</code>. The second view's direct bases include the "
        "CompositeProvider <b>ORDER_COND_CP</b>, which adds a layer and pulls in ORDER_COND. BW "
        "raises no where-used warning when either view changes, and both loads are full - so there "
        "is no delta to inspect afterwards.",
        ["SYS.OBJECT_DEPENDENCIES", "_SYS_REPO.ACTIVE_OBJECT", "RSOHCPR", "RSBKDTP"],
    ),
    (
        "F2",
        "High",
        "ORDER_ITEM is both upstream and downstream of the same calculation view.",
        "<code>CV_OPEN_ORDERS</code> reads ORDER_ITEM and feeds OPEN_ORDER_CP, which full-loads "
        "ORDER_ITEM. Load order therefore determines the result and the object cannot be reloaded "
        "independently of the view. A failed request cannot simply be re-run.",
        ["SYS.OBJECT_DEPENDENCIES", "RSBKDTP", "RSTRAN"],
    ),
    (
        "F3",
        "Medium",
        "The logic is in the self-referencing transformations, not in the loads from the source "
        "system.",
        "None of the seven DataSource-fed transformations carries an end routine. 1,146 lines of "
        "end-routine ABAP sit in the three transformations that read a store and write back to it. "
        "A change review that reads the inbound transformations sees almost nothing.",
        ["RSTRAN", "RSTRANRULE", "RSAABAP"],
    ),
    (
        "F4",
        "Medium",
        "BILLING_ITEM's end routine reads PARTNER_FN through a SELECT, which BW's where-used list "
        "does not show.",
        "Routine-derived dependencies are invisible to BW's own impact analysis, so a change to "
        "PARTNER_FN reaches this report with nothing in BW to flag it. Four such edges were "
        "found on this path, and that count is a <b>lower bound</b>: dynamic SQL, "
        "function-module calls and class methods are not followed.",
        ["RSAABAP", "RSTRAN"],
    ),
    (
        "F5",
        "Low",
        "BUDGET_PLAN is fed only by a flat file.",
        "No system of record this server can reach, so its content cannot be reconciled upstream. "
        "It is also the only part loaded full from its source.",
        ["RSDS", "RSBKDTP"],
    ),
]

# --------------------------------------------------------------------- routines
# target, source, fields, rule mix, routines, lines, reads, anti-patterns
TRANSFORMATIONS: list[dict[str, Any]] = [
    {
        "target": "ORDER_ITEM",
        "source": "ORDER_ITEM",
        "self": True,
        "fields": 164,
        "rules": {"direct": 71, "constant": 62, "master-data read": 19, "DSO lookup": 12},
        "routines": "end x1, global x2",
        "lines": 612,
        "reads": ["0CUSTOMER", "0PLANT", "PARTNER_FN"],
        "anti": ["recordset delete x1", "nested loop x2"],
        "unresolved": ["CONVERT_TO_FOREIGN_CURRENCY (function module)"],
    },
    {
        "target": "BILLING_ITEM",
        "source": "BILLING_ITEM",
        "self": True,
        "fields": 98,
        "rules": {"direct": 44, "constant": 33, "master-data read": 21},
        "routines": "end x1, global x2",
        "lines": 387,
        "reads": ["0PAYER", "PARTNER_FN"],
        "anti": ["missing FOR ALL ENTRIES x1"],
        "unresolved": ["CONVERT_TO_FOREIGN_CURRENCY (function module)"],
    },
    {
        "target": "REBATE_ITEM",
        "source": "REBATE_ITEM",
        "self": True,
        "fields": 76,
        "rules": {"direct": 38, "constant": 25, "master-data read": 13},
        "routines": "start x1, end x1",
        "lines": 147,
        "reads": ["COST_CENTRE", "ORDER_ITEM"],
        "anti": [],
        "unresolved": [],
    },
    {
        "target": "ORDER_ITEM",
        "source": "OPEN_ORDER_CP",
        "self": False,
        "fields": 42,
        "rules": {"direct": 40, "constant": 2},
        "routines": "none",
        "lines": 0,
        "reads": [],
        "anti": [],
        "unresolved": [],
    },
    {
        "target": "ORDER_ITEM",
        "source": "DS_ORDER_ITEM",
        "self": False,
        "fields": 118,
        "rules": {"direct": 101, "formula": 16, "time conv.": 1},
        "routines": "none",
        "lines": 0,
        "reads": [],
        "anti": [],
        "unresolved": [],
    },
    {
        "target": "LEGACY_SALES",
        "source": "DS_LEGACY_SALES",
        "self": False,
        "fields": 64,
        "rules": {"direct": 30, "constant": 32, "formula": 2},
        "routines": "end x1",
        "lines": 203,
        "reads": ["0MATERIAL", "PARTNER_FN"],
        "anti": ["hardcoded value x2"],
        "unresolved": ["CONVERSION_EXIT_MATN1_INPUT (function module)"],
    },
]

ROUTINE_BASIS = (
    "Field counts and rule types are read from the transformation and rule tables, active version "
    "only. Table dependencies and anti-patterns come from a <b>static parse</b> of the ABAP and "
    "are a lower bound: dynamic SQL, function-module calls and class methods are not followed, "
    "which is why the last column exists rather than being omitted. A routine reported with no "
    "anti-patterns has none <i>of the kinds the parser detects</i>."
)

# --------------------------------------------------------------- calc view logic
CALC_VIEWS: dict[str, dict[str, Any]] = {
    "ACME.SALES/CV_OPEN_ORDERS": {
        "produces": "Open order quantity and open order value, in document and group currency",
        "named_by": "OPEN_ORDER_CP, the CompositeProvider that exposes it, is called "
        '"Open Order Item" in BW',
        "how": "Order items are filtered to live business - no rejection reason, document "
        "category C or I. Two branches then run in parallel. One aggregates delivered "
        "quantity from DELIVERY_ITEM (goods issue posted only) and computes "
        "<code>open = ordered + target - delivered</code>. The other aggregates invoiced "
        "quantity from BILLING_ITEM and computes the same measure against invoiced "
        "instead. The branches are unioned with the opposing measure nulled out, and open "
        "value is pro-rated from net value.",
        "reading": 'This is the order backlog, measured two ways because "not yet delivered" '
        'and "not yet invoiced" are different questions and the view declines to '
        "choose. The result full-loads into ORDER_ITEM.",
        "watch": "Three measures exist only here - no BW transformation computes them and no BW "
        "where-used list mentions them. The union means a line can appear on both "
        "branches, so an aggregate over the result double-counts unless the branch is "
        "constrained.",
        "nodes": [
            ("Order", "projection", "ORDER_ITEM", "rejection reason empty, category C or I"),
            ("Delivered", "aggregation", "DELIVERY_ITEM", "goods-issue date set"),
            ("Invoiced", "aggregation", "BILLING_ITEM", "none"),
            ("Join_1", "join leftOuter", "Order + Delivered", "none"),
            ("Join_2", "join leftOuter", "Order + Invoiced", "none"),
            ("Union_1", "union", "Join_1 + Join_2", "none"),
            ("Final", "projection (final)", "Union_1", "none"),
        ],
        "calculated": [
            ("open_quantity", "Join_1", "ordered + target - delivered"),
            ("open_quantity", "Join_2", "ordered + target - invoiced"),
            ("open_value", "Final", "abs(open_quantity / (ordered + target)) * net_value"),
        ],
    },
    "ACME.SALES/CV_FREE_OF_CHARGE": {
        "produces": "Free-of-charge order lines, with their delivery and their pricing conditions",
        "named_by": "FOC_ORDER_CP, the CompositeProvider that exposes it, is called "
        '"Free of Charge View" in BW',
        "how": "Order items and order headers are both filtered to the free-of-charge document "
        "type. Delivery items are re-keyed onto the order and joined with an "
        "<b>inner</b> join, so an order line with no delivery row is dropped. The order "
        "header is then left-joined, and finally the conditions from ORDER_COND_CP - "
        "condition type, value and activation flag - are left-joined on order item.",
        "reading": "A free-of-charge line is revenue-neutral but not cost-neutral: it consumes "
        "product and carries condition values. This view assembles the full picture "
        "of one, and the result full-loads into BILLING_ITEM.",
        "watch": 'The inner join is the thing to know. The view answers "free-of-charge lines '
        'that were delivered", not "free-of-charge lines". An undelivered line is '
        "absent by construction, and nothing downstream says so.",
        "nodes": [
            ("Order", "projection", "ORDER_ITEM", "document type = free of charge"),
            ("Delivery", "projection", "DELIVERY_ITEM", "none"),
            ("Item", "join inner", "Order + Delivery", "none"),
            ("Header", "projection", "ORDER_HEADER", "document type = free of charge"),
            ("Join_2", "join leftOuter", "Item + Header", "none"),
            ("Conditions", "projection", "ORDER_COND_CP", "none"),
            ("Join_3", "join leftOuter (final)", "Join_2 + Conditions", "none"),
        ],
        "calculated": [],
    },
}

CALC_VIEW_BASIS = (
    "Read from each view's activated definition: filters, join types, union mappings and "
    "calculated-column formulas are stated there rather than inferred. The business reading is "
    "the analysis's own, anchored to the BW description of the CompositeProvider that exposes the "
    "view. An inactive change in the modeller is not visible, and the runtime view is what "
    "queries execute."
)
