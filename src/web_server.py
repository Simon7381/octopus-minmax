import logging
import secrets
from functools import wraps

from flask import Flask, Response, abort, flash, redirect, render_template, request

import config
import config_manager
from comparison_preview import ComparisonPreview
from diagnostics import log_failure

logger = logging.getLogger("octobot.web_server")

app = Flask(__name__)
app.secret_key = "octobot-tool"
comparison_preview = ComparisonPreview()
preview_token = secrets.token_urlsafe(32)


def is_ingress_request():
    # Skip auth for ingress requests
    return bool(
        request.headers.get("X-Ingress-Path") or request.headers.get("X-Hassio-Ingress")
    )


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if is_ingress_request():
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or not (
            auth.username == config.WEB_USERNAME
            and auth.password == config.WEB_PASSWORD
        ):
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="OctoBot Login Required"'},
            )
        return f(*args, **kwargs)

    return decorated


@app.route("/")
@require_auth
def index():
    """Homepage - Dashboard with navigation buttons"""
    return render_template("index.html", preview=comparison_preview.status(), preview_token=preview_token)


@app.route("/preview", methods=["POST"])
@require_auth
def start_preview():
    if not secrets.compare_digest(request.form.get("preview_token", "").encode(), preview_token.encode()):
        abort(400, "Reload the dashboard before starting a preview.")
    try:
        started = comparison_preview.start()
        flash("Comparison preview started. No tariff changes will be made." if started
              else "A comparison preview is already running.", "success")
    except Exception as exc:
        log_failure(logger, "Could not start comparison preview", exc)
        flash("Could not start the preview. See Logs for diagnostics.", "error")
    # Relative URLs preserve the Home Assistant ingress prefix.
    return redirect(".", code=303)


@app.route("/preview-status")
@require_auth
def preview_status():
    response = app.json.response(comparison_preview.status())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/config", methods=["GET", "POST"])
@require_auth
def config_page():
    if request.method == "POST":
        # Validate input
        errors = config_manager.validate_config(request.form.to_dict())
        if errors:
            for error in errors:
                flash(error, "error")
        else:
            # Update config
            try:
                config_manager.update_config(request.form.to_dict())
                logger.info("Configuration updated successfully.")

                flash(
                    "Configuration updated successfully! (Will reset on container restart)",
                    "success",
                )
            except Exception as e:
                flash(f"Error updating config: {e!s}", "error")
                logger.error(f"Config update failed: {e}")

        return redirect("config")

    current_config = config_manager.get_config()
    return render_template("config.html", config=current_config)


@app.route("/logs")
@require_auth
def logs():
    log_lines = tail_file("logs/octobot.log", None)  # None = read entire file
    log_entries = group_log_entries(log_lines)
    return render_template("logs.html", log_entries=log_entries)


def tail_file(filepath, n):
    """Read last n lines from file, or entire file if n is None"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
            if n is None:
                return lines  # Return entire file
            return lines[-n:] if len(lines) > n else lines
    except FileNotFoundError:
        return ["Log file not found. The bot may not have started yet."]
    except Exception as e:
        logger.error(f"Error reading log file: {e}")
        return [f"Error reading log file: {e!s}"]


def group_log_entries(log_lines):
    """Group log lines into entries based on timestamp pattern"""
    import re

    # Pattern matches timestamp
    timestamp_pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    entries = []
    current_entry = []

    for line in log_lines:
        if timestamp_pattern.match(line):
            # New log entry starts
            if current_entry:
                entries.append("".join(current_entry))
            current_entry = [line]
        else:
            # Continuation of previous entry
            if current_entry:
                current_entry.append(line)
            else:
                # Edge case: file starts without timestamp
                current_entry.append(line)

    if current_entry:
        entries.append("".join(current_entry))

    return entries


def run_server():
    logger.info(f"Web server starting on http://localhost:{config.WEB_PORT}")
    app.run(host="0.0.0.0", port=config.WEB_PORT, debug=False, use_reloader=False)
