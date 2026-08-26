"""Bundled evidence-bound data-query extension entry (PRD).

The implementation lives in ``tau_coding.dataquery``; this file is the loader
entry point so the extension can be packaged, discovered and reloaded like any
user extension. Tools register only when the deployment is fully configured
(decision 20).
"""

from tau_coding.dataquery.extension import setup

__all__ = ["setup"]
