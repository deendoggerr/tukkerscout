import atexit
import threading
import webbrowser
from datetime import datetime

from flask import (
    Flask, jsonify, redirect, render_template,
    request, url_for, flash, session
)

from .core import *

app = Flask(__name__)
app.secret_key = "tukkerscout-local"
STOP_EVENT = threading.Event()


@app.template_filter("prettytime")
def prettytime(value):
    if not value:
        return "Nog niet"
    try:
        return datetime.fromisoformat(value).strftime("%d-%m-%Y %H:%M:%S")
    except Exception:
        return str(value)


@app.get("/")
def dashboard():
    config = load_config()
    hours = int(
        request.args.get(
            "hours",
            config.get("default_window_hours", 48),
        )
    )
    latest = latest_check()
    latest_id = latest["id"] if latest else None

    return render_template(
        "dashboard.html",
        new_groups=get_groups(hours, latest_id) if latest_id else [],
        all_groups=get_groups(hours),
        hours=hours,
        status_info=STATUS,
        latest=latest,
        latest_runs=latest_check_runs(latest_id),
    )


@app.get("/sources")
def source_page():
    return render_template(
        "sources.html",
        sources=sources_status(),
        status_info=STATUS,
    )


@app.route("/x", methods=["GET", "POST"])
def x_page():
    if request.method == "POST":
        token = request.form.get("bearer_token", "")
        accounts_text = request.form.get("accounts", "")
        accounts = [
            line.strip()
            for line in accounts_text.replace(",", "\n").splitlines()
            if line.strip()
        ]
        save_x_config(token, accounts)
        flash("X-instellingen lokaal opgeslagen.")
        return redirect(url_for("x_page"))

    x_config = load_x_config()
    return render_template(
        "x.html",
        x_config=x_config,
    )


@app.route("/persons", methods=["GET", "POST"])
def persons_page():
    preview = session.pop("selection_preview", None)
    preview_text = session.pop("selection_text", "")

    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "add":
                add_person(
                    request.form.get("name", ""),
                    request.form.get("role", "Speler"),
                    request.form.get("active") == "1",
                    request.form.get("follow") == "1",
                )
                flash("Persoon toegevoegd.")
            elif action == "initial_import":
                added, skipped = bulk_add_persons(
                    request.form.get("bulk_text", "")
                )
                flash(
                    f"{added} spelers geïmporteerd, "
                    f"{skipped} overgeslagen."
                )
            elif action == "preview_selection":
                text = request.form.get("selection_text", "")
                session["selection_preview"] = compare_player_selection(text)
                session["selection_text"] = text
            elif action == "apply_selection":
                result = apply_player_selection(
                    request.form.get("selection_text", "")
                )
                flash(
                    f"Selectie bijgewerkt: "
                    f"{len(result['new'])} nieuw, "
                    f"{len(result['missing'])} op inactief gezet."
                )
        except Exception as exc:
            flash(str(exc))
        return redirect(url_for("persons_page"))

    q = request.args.get("q", "").strip()
    role = request.args.get("role", "").strip()
    active = request.args.get("active", "")
    follow = request.args.get("follow", "")

    return render_template(
        "persons.html",
        persons=list_persons(q, role, active, follow),
        person_count=count_persons(),
        q=q,
        role=role,
        active=active,
        follow=follow,
        preview=preview,
        preview_text=preview_text,
    )


@app.post("/persons/<int:person_id>/update")
def person_update(person_id):
    update_person(
        person_id,
        request.form.get("role", "Speler"),
        request.form.get("active") == "1",
        request.form.get("follow") == "1",
    )
    return redirect(request.referrer or url_for("persons_page"))


@app.post("/persons/<int:person_id>/delete")
def person_delete(person_id):
    delete_person(person_id)
    return redirect(url_for("persons_page"))


@app.post("/cleanup")
def cleanup():
    removed = cleanup_irrelevant_articles()
    flash(f"{removed} irrelevante oude berichten verwijderd.")
    return redirect(url_for("dashboard"))


@app.post("/check")
def check():
    result = run_check()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(result)
    return redirect(url_for("dashboard"))


def start():
    setup_logging()
    config = load_config()
    host = config.get("host", "127.0.0.1")
    port = int(config.get("port", 8765))

    if config.get("open_browser_on_start", True):
        threading.Timer(
            1.3,
            lambda: webbrowser.open(f"http://{host}:{port}"),
        ).start()

    atexit.register(STOP_EVENT.set)

    print(f"TukkerScout 3.2.1 draait op http://{host}:{port}")
    print("Nieuws wordt alleen gecontroleerd wanneer jij op Update nieuws klikt.")

    app.run(
        host=host,
        port=port,
        debug=False,
        use_reloader=False,
    )
