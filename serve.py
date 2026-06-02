"""Production entrypoint -- serves the Flask app via waitress.

Run:  python serve.py
Env:
  APP_HOST  (default 127.0.0.1)  -- bind address. Keep on localhost or an
            internal IP; this UI has no login and runs operator-supplied SQL.
  APP_PORT  (default 8080)
  APP_THREADS (default 8)        -- waitress worker threads.

IMPORTANT: run a SINGLE process. Job status and field selections are held in
memory and shared across threads, but NOT across processes.
"""
import os

from waitress import serve

from app import app

if __name__ == "__main__":
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "8080"))
    threads = int(os.getenv("APP_THREADS", "8"))
    print(f"peopleFinder serving on http://{host}:{port} ({threads} threads)")
    serve(app, host=host, port=port, threads=threads)
