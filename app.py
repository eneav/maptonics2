# -*- coding: utf-8 -*-
"""
Maptonics — Karten zum Anfassen.
Lokales Programm: Start mit `python app.py`, oeffnet den Browser automatisch.
"""

import threading
import webbrowser

from flask import Flask, jsonify, request, send_file, send_from_directory
import io

import engine

app = Flask(__name__, static_folder="static")

# Letztes generiertes Modell (fuer den Export) im Speicher halten
_current = {"project": None}


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.post("/api/generate")
def api_generate():
    cfg = request.get_json(force=True)
    bounds = cfg["bounds"]
    params = cfg["params"]
    tile_mm = float(params.get("tile_mm", 200))
    mpt = float(params.get("meters_per_tile", 1000))

    grid = engine.plan_grid(bounds, tile_mm, mpt)
    if grid["tiles_total"] > 64:
        return jsonify({"error": f"Zu viele Kacheln ({grid['tiles_total']}). "
                                 "Kleineren Bereich wählen oder mehr Meter pro Kachel."}), 400

    # OSM fuer den gesnappten (erweiterten) Bereich laden
    exp = grid["exp_bounds"]
    try:
        data = engine.fetch_osm(exp["south"], exp["west"], exp["north"], exp["east"])
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"OSM-Abruf fehlgeschlagen: {e}"}), 502

    try:
        project = engine.build_project(data, grid, params)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Geometrie-Fehler: {e}"}), 500

    _current["project"] = project

    tiles_json = [{
        "r": t["r"], "c": t["c"],
        "layers": [{
            "name": l.name, "color": l.color,
            "verts": [round(v, 3) for v in l.verts],
            "tris": l.tris,
        } for l in t["layers"]],
    } for t in project["tiles"]]

    return jsonify({
        "cols": project["cols"], "rows": project["rows"],
        "tile_mm": project["tile_mm"],
        "exp_bounds": project["exp_bounds"],
        "stats": project["stats"],
        "tiles": tiles_json,
    })


@app.get("/api/export")
def api_export():
    project = _current.get("project")
    if project is None:
        return jsonify({"error": "Erst ein Mesh generieren."}), 400
    blob, fname, mime = engine.export_project(project)
    return send_file(io.BytesIO(blob), mimetype=mime,
                     as_attachment=True, download_name=fname)


def _open_browser():
    webbrowser.open("http://127.0.0.1:5173")


if __name__ == "__main__":
    threading.Timer(0.8, _open_browser).start()
    print("\n  Maptonics laeuft auf http://127.0.0.1:5173  (Strg+C zum Beenden)\n")
    app.run(host="127.0.0.1", port=5173, debug=False)
