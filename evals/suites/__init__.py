"""The individual evaluation suites.

Each module exposes ``run(...) -> Suite``. Suites never raise: a suite that cannot
complete returns ``status="error"`` with the traceback in ``error``, so one broken
suite does not cost you the report.
"""
