# Compatibility shim for legacy tests
"""This module re‑exports the FastAPI app, the MongoDB database handle, and a shimmed
index‑creation function from the consolidated location `app/main.py`.  The original
test suite expects a top‑level `main` module containing `app`, `db`, and
`create_indexes`.  By providing a thin wrapper we keep that public interface
unchanged while the application code lives under `app/`.

The shim also registers a dummy `helpers.helper` module exposing the `IST`
timezone constant, satisfying legacy imports.
"""

import importlib, sys, types

# Load the underlying application module
_app_module = importlib.import_module('app.main')

# Re‑export the FastAPI app and IST constant
app = _app_module.app
IST = _app_module.IST

# Initialise shim ``db`` with the real MongoClient used by the application.
# Tests will replace ``main.db`` with a ``FakeDB``; a proxy is installed into
# ``app.main`` so that all route handlers transparently see the current object.
db = _app_module.db  # mutable reference

class _DBProxy:
    """Proxy that forwards attribute access to the current ``db`` variable.

    The proxy is assigned to ``app.main.db``.  When tests later reassign
    ``main.db`` to a ``FakeDB``, the proxy will automatically delegate to the
    new object because it reads the global ``db`` name each time.
    """

    def __getattr__(self, name):
        return getattr(db, name)

    def __setattr__(self, name, value):
        setattr(db, name, value)

# Install the proxy in the application module so all handlers use it.
_app_module.db = _DBProxy()

def create_indexes() -> None:
    """Create required MongoDB indexes.

    This implementation forwards the index‑creation calls to whatever ``db``
    object is currently bound (the real ``MongoClient`` in production, a
    ``FakeDB`` in the test suite).  It mirrors the logic from ``app/main.py``.
    """
    # Employees indexes
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    # Attendance logs indexes
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    # Additional required indexes per contract (R5‑R7)
    db.attendance_logs.create_index([("status", 1), ("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1)])

# Register a dummy `helpers.helper` module for backward compatibility with tests
helpers_pkg = types.ModuleType('helpers')
helper_mod = types.ModuleType('helpers.helper')
helper_mod.IST = IST
helpers_pkg.helper = helper_mod
sys.modules['helpers'] = helpers_pkg
sys.modules['helpers.helper'] = helper_mod

__all__ = ["app", "db", "create_indexes"]
