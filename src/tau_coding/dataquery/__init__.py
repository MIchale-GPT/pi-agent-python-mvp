"""Evidence-bound DWS read-only data-query extension (bundled, opt-in).

The public entry point is :func:`tau_coding.dataquery.extension.setup`, which
registers the four domain tools only when the deployment is fully configured
(decision 20). Internal modules are imported lazily by the extension so core
Tau sessions never pay for sqlglot or driver imports unless the extension is
actually enabled.
"""
