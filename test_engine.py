# -*- coding: utf-8 -*-
"""Engine-Test mit synthetischen OSM-Daten (kein Netz noetig).

Pruefung der Wasserdichtheit ohne Zusatz-Abhaengigkeit: jede gerichtete Kante
darf genau einmal vorkommen, jede ungerichtete genau zweimal. Ist trimesh
installiert, wird zusaetzlich dessen is_watertight gegengeprueft.
"""
import collections
import io
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, ".")
from engine import plan_grid, build_project, export_project

try:
    import trimesh
except ImportError:
    trimesh = None


def node(i, lat, lon):
    return {"type": "node", "id": i, "lat": lat, "lon": lon}


els = [node(1, 51.4350, 6.7500), node(2, 51.4350, 6.7800),
       {"type": "way", "id": 1000, "nodes": [1, 2], "tags": {"highway": "primary"}},
       node(3, 51.4360, 6.7560), node(4, 51.4360, 6.7568),
       node(5, 51.4366, 6.7568), node(6, 51.4366, 6.7560),
       {"type": "way", "id": 2000, "nodes": [3, 4, 5, 6, 3],
        "tags": {"building": "yes", "height": "25"}},
       node(7, 51.4340, 6.7600), node(8, 51.4340, 6.7640),
       node(9, 51.4348, 6.7640), node(10, 51.4348, 6.7600),
       {"type": "way", "id": 3000, "nodes": [7, 8, 9, 10, 7],
        "tags": {"leisure": "park"}}]
data = {"elements": els}
params = {"tile_mm": 200, "meters_per_tile": 1000, "base_mm": 2, "height_factor": 1,
          "layers": {"buildings": True, "roads": True, "water": True,
                     "green": True, "heart": True},
          "heart": {"lat": 51.4350, "lon": 6.7648}}


def manifold(layer) -> bool:
    """Geschlossene Oberflaeche? Kantenpaarung pruefen."""
    directed = collections.Counter()
    undirected = collections.Counter()
    t = layer.tris
    for i in range(0, len(t), 3):
        a, b, c = t[i], t[i + 1], t[i + 2]
        for p, q in ((a, b), (b, c), (c, a)):
            directed[(p, q)] += 1
            undirected[(min(p, q), max(p, q))] += 1
    return (all(v == 1 for v in directed.values())
            and all(v == 2 for v in undirected.values()))


def check_project(proj, label) -> bool:
    ok, tris = True, 0
    for t in proj["tiles"]:
        for layer in t["layers"]:
            tris += len(layer.tris) // 3
            if not manifold(layer):
                ok = False
                print(f"  !! nicht wasserdicht: Kachel r{t['r']}c{t['c']} "
                      f"Ebene {layer.key}")
    print(f"{label}: {proj['cols']}x{proj['rows']} Kacheln, {tris} Dreiecke, "
          f"wasserdicht={ok}")
    return ok


def check_3mf(blob: bytes) -> bool:
    """3MF-Paket liest sich als gueltiges XML?"""
    ET.fromstring(zipfile.ZipFile(io.BytesIO(blob)).read("3D/3dmodel.model"))
    if trimesh is not None:
        sc = trimesh.load(io.BytesIO(blob), file_type="3mf")
        return all(g.is_watertight for g in sc.geometry.values())
    return True


# --- 1 Kachel ---------------------------------------------------------------
grid = plan_grid({"south": 51.4345, "west": 6.7640,
                  "north": 51.4356, "east": 6.7655}, 200, 1000)
proj = build_project(data, grid, params)
ok1 = check_project(proj, "1 Kachel")
blob, fn, mt = export_project(proj)
ok1 &= check_3mf(blob)
print(f"  Export: {fn} ({len(blob) / 1024:.1f} KB), stats={proj['stats']}")

# --- Raster -----------------------------------------------------------------
grid2 = plan_grid({"south": 51.4310, "west": 6.7540,
                   "north": 51.4390, "east": 6.7760}, 200, 600)
proj2 = build_project(data, grid2, params)
ok2 = check_project(proj2, "Raster  ")
blob2, fn2, mt2 = export_project(proj2)
zf = zipfile.ZipFile(io.BytesIO(blob2))
n3mf = [n for n in zf.namelist() if n.endswith(".3mf")]
ok2 &= all(check_3mf(zf.read(n)) for n in n3mf)
ok2 &= "ZUSAMMENBAU.txt" in zf.namelist()
print(f"  Export: {fn2} ({len(n3mf)} 3MF + Anleitung)")

# --- Grenzfaelle ------------------------------------------------------------
empty = build_project({"elements": []}, grid, {**params, "heart": None})
ok3 = len(empty["tiles"][0]["layers"]) == 1          # nur Grundplatte
off = build_project(data, grid, {**params, "layers": {}, "heart": None})
ok3 &= len(off["tiles"][0]["layers"]) >= 1
big = plan_grid({"south": 51.0, "west": 6.0, "north": 52.0, "east": 7.0}, 200, 1000)
ok3 &= big["tiles_total"] > 64                        # app.py blockt das
print(f"Grenzfaelle: leer/aus/riesig ok={ok3}")

if trimesh is None:
    print("(trimesh nicht installiert — eigene Kantenpruefung verwendet)")
print("OK" if (ok1 and ok2 and ok3) else "FEHLER")
sys.exit(0 if (ok1 and ok2 and ok3) else 1)
