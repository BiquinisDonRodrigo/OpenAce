import os

import requests
from requests.adapters import HTTPAdapter

_POOL_CONNECTIONS = int(os.environ.get("OPENACE_HTTP_POOL_CONNECTIONS", "64"))
_POOL_MAXSIZE = int(os.environ.get("OPENACE_HTTP_POOL_MAXSIZE", "128"))

session = requests.Session()
_adapter = HTTPAdapter(pool_connections=_POOL_CONNECTIONS, pool_maxsize=_POOL_MAXSIZE)
session.mount("http://", _adapter)
session.mount("https://", _adapter)
