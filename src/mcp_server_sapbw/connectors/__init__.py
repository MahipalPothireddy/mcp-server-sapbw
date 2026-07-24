"""Connector layer: pluggable external-BI metadata connectors.

Tableau (Metadata API / workgroup repository) and BOBJ (Query Builder / RESTful RaaS)
metadata does not exist in BW. These connectors are defined behind an interface separate
from the BW core so scenarios 9.7 / 9.8 can be populated later without touching the core.
When no connector is configured, dependent analyzers emit templates with an explicit
"connector not configured" result. Implemented in build prompt B9.
"""
