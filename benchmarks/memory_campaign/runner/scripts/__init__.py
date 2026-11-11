"""Share the scripts namespace with the native Core tools in either import order."""

from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)
