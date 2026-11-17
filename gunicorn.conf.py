"""Gunicorn settings for running LabRat from a checkout.

It listens on loopback only; pass ``-b`` for another address, and add that
host to ``[server] allowed_hosts``. Code reloading is for development and
stays off unless ``LABRAT_RELOAD=1`` is set, since it watches every source
file and restarts workers on any change.
"""

import os

bind = "127.0.0.1:8000"
workers = 2
reload = os.environ.get("LABRAT_RELOAD") == "1"
accesslog = "-"
errorlog = "-"
