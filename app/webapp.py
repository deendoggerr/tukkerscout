import atexit, threading, webbrowser
from datetime import datetime
from flask import Flask, jsonify, redirect, render_template, request, url_for, flash, session
from .core import *

app=Flask(__name__)
app.secret_key="tukkerscout-local"
STOP_EVENT=threading.Event()

@app.template_filter("prettytime")
def prettytime(v):
    if not v:return "Nog niet"
    try:return datetime.fromisoformat(v).strftime("%d-%m-%Y %H:%M:%S")
    except:return str(v)

@app.get("/")
def dashboard():
    cfg=load_config(); hours=int(request.args.get("hours",cfg.get("default_window_hours",48)))
    return render_template("dashboard.html",groups=get_groups(hours),hours=hours,status_info=STATUS)

@app.get("/sources")
def source_page():
    return render_template("sources.html",sources=sources_status(),status_info=STATUS)

@app.route("/persons",methods=["GET","POST"])
def persons_page():
    preview=session.pop("selection_preview",None)
    preview_text=session.pop("selection_text","")
    if request.method=="POST":
        action=request.form.get("action")
        try:
            if action=="add":
                add_person(request.form.get("name",""),request.form.get("role","Speler"),request.form.get("active")=="1",request.form.get("follow")=="1")
                flash("Persoon toegevoegd.")
            elif action=="initial_import":
                added,skipped=bulk_add_persons(request.form.get("bulk_text",""))
                flash(f"{added} spelers geïmporteerd, {skipped} overgeslagen.")
            elif action=="preview_selection":
                text=request.form.get("selection_text","")
                session["selection_preview"]=compare_player_selection(text)
                session["selection_text"]=text
            elif action=="apply_selection":
                result=apply_player_selection(request.form.get("selection_text",""))
                flash(f"Selectie bijgewerkt: {len(result['new'])} nieuw, {len(result['missing'])} op inactief gezet.")
        except Exception as exc:
            flash(str(exc))
        return redirect(url_for("persons_page"))
    q=request.args.get("q","").strip(); role=request.args.get("role","").strip()
    active=request.args.get("active",""); follow=request.args.get("follow","")
    return render_template("persons.html",persons=list_persons(q,role,active,follow),
        person_count=count_persons(),q=q,role=role,active=active,follow=follow,
        preview=preview,preview_text=preview_text)

@app.post("/persons/<int:person_id>/update")
def person_update(person_id):
    update_person(person_id,request.form.get("role","Speler"),request.form.get("active")=="1",request.form.get("follow")=="1")
    return redirect(request.referrer or url_for("persons_page"))

@app.post("/persons/<int:person_id>/delete")
def person_delete(person_id):
    delete_person(person_id)
    return redirect(url_for("persons_page"))

@app.post("/check")
def check():
    result=run_check()
    if request.headers.get("X-Requested-With")=="fetch": return jsonify(result)
    return redirect(url_for("dashboard"))

def start():
    setup_logging(); cfg=load_config(); host=cfg.get("host","127.0.0.1"); port=int(cfg.get("port",8765))
    threading.Thread(target=scheduler_loop,args=(STOP_EVENT,),daemon=True).start()
    if cfg.get("open_browser_on_start",True): threading.Timer(1.3,lambda:webbrowser.open(f"http://{host}:{port}")).start()
    atexit.register(STOP_EVENT.set)
    print(f"TukkerScout 3.0 draait op http://{host}:{port}")
    app.run(host=host,port=port,debug=False,use_reloader=False)
