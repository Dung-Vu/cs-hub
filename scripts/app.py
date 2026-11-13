#!/usr/bin/env python3
"""CS Hub — one link for the CS team: delivery report printing, ZNS dashboard,
and a link to the CS KPI dashboard (now on bi.bonstu.site/cs)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))
from odoo_core import connect_rpc, rpc_execute  # noqa: E402

ENV_PATH = BASE_DIR / ".env"
TEMPLATES_DIR = str(BASE_DIR / "templates")
app = Flask(__name__, template_folder=TEMPLATES_DIR)


def load_env():
    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip().strip('"').strip("'")


_odoo_conn = None


def get_odoo():
    global _odoo_conn
    if _odoo_conn is None:
        load_env()
        _odoo_conn = connect_rpc("prod")
    return _odoo_conn


load_env()
CS_DASHBOARD_URL = os.environ.get("CS_DASHBOARD_URL", "https://bi.bonstu.site/cs")


@app.route("/")
def hub_home():
    """CS Hub home: one link to every CS tool."""
    return render_template("hub.html", cs_dashboard_url=CS_DASHBOARD_URL)


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


from hub_auth import init_auth  # noqa: E402
from delivery_report import create_blueprint as create_delivery_blueprint  # noqa: E402
from zns_proxy import create_blueprint as create_zns_blueprint  # noqa: E402

init_auth(app, get_odoo=get_odoo, rpc=rpc_execute)
app.register_blueprint(create_delivery_blueprint(get_odoo, rpc_execute))
app.register_blueprint(create_zns_blueprint())


if __name__ == "__main__":
    print("CS Hub — http://localhost:5100")
    app.run(host="0.0.0.0", port=5100, debug=True)
