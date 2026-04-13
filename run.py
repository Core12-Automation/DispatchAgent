"""
run.py

Entry point for the DispatchAgent portal.

    python run.py

Then open http://localhost:5000
"""

import logging
import os

from app import create_app

app = create_app()

log = logging.getLogger(__name__)

if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", "5000"))
    log.info("DispatchAgent Portal starting — http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False, threaded=True)
