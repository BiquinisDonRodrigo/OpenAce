import requests
from requests.adapters import HTTPAdapter

from app.utils import environment_store

_POOL_CONNECTIONS = environment_store.get_int("OPENACE_HTTP_POOL_CONNECTIONS")
_POOL_MAXSIZE = environment_store.get_int("OPENACE_HTTP_POOL_MAXSIZE")

session = requests.Session()
_adapter = HTTPAdapter(pool_connections=_POOL_CONNECTIONS, pool_maxsize=_POOL_MAXSIZE)
session.mount("http://", _adapter)
session.mount("https://", _adapter)
