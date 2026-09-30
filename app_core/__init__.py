"""RootED application package.

Importing a utility submodule must not initialize the Flask application or a
database. The WSGI ``app`` export remains available lazily for compatibility.
"""


def __getattr__(name):
    if name == "app":
        from .dashboard_v5 import app

        return app
    raise AttributeError(name)
