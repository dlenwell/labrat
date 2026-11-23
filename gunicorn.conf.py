"""Gunicorn settings for running LabRat from a checkout.

It listens on loopback only; pass ``-b`` for another address, and add that
host to ``[server] allowed_hosts``. Code reloading is for development and
stays off unless ``LABRAT_RELOAD=1`` is set, since it watches every source
file and restarts workers on any change.

The hooks below look after LabRat's startup gate (``labrat/startup.py``):
each worker starts its startup thread as soon as it boots, with the
master's pid so all workers share one web lease, and the master gives the
lease up when it exits so a successor need not wait for it to go stale.
"""

import os

bind = "127.0.0.1:8000"
workers = 2
reload = os.environ.get("LABRAT_RELOAD") == "1"
accesslog = "-"
errorlog = "-"


def on_starting(server):
    """Forget a fork left by an earlier run of this server.

    Args:
        server: The Gunicorn arbiter.
    """
    from labrat.instance import holder_id
    from labrat.startup import clear_fork_marker

    clear_fork_marker(holder_id(os.getpid()))


def post_worker_init(worker):
    """Start the startup gate in a freshly booted worker.

    Args:
        worker: The Gunicorn worker, whose ``wsgi`` is ``labrat.app:app``.
    """
    start = getattr(worker.wsgi, "start", None)
    if callable(start):
        start(server_pid=worker.ppid)


def on_exit(server):
    """Release the web lease as the server shuts down.

    Args:
        server: The Gunicorn arbiter.
    """
    from labrat.instance import holder_id
    from labrat.startup import release_web_lease

    release_web_lease(holder_id(os.getpid()))
