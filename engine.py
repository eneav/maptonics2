# -*- coding: utf-8 -*-
"""
Maptonics Engine
OSM-Daten (Overpass) -> 2D-Geometrie (shapely) -> extrudierte 3D-Meshes -> 3MF.

Jede Ebene (Grundplatte, Gebaeude, Strassen, Wasser, Gruen, Herz) wird als
eigenes Objekt mit eigener Farbe in die 3MF geschrieben, damit man in
Bambu Studio pro Objekt ein Filament zuweisen kann.
"""

import io
import math
import time
import zipfile

import numpy as np
import requests
import mapbox_earcut

# Firmen-Laptops (TLS-Inspection durch Proxy/VPN): System-Zertifikatsspeicher
# nutzen statt certifi, sonst scheitert HTTPS mit CERTIFICATE_VERIFY_FAILED.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass
from shapely.geometry import box, LineString, Polygon, MultiPolygon
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

# ----------------------------------------------------------------------
# Konfiguration
# ----------------------------------------------------------------------

LAYERS = [
    # key, Anzeigename, Farbe (Hex, fuer 3MF + Vorschau)
    ("base",      "Grundplatte",   "#E7E0D4"),
    ("buildings", "Gebaeude",      "#B9B3AA"),
    ("roads",     "Strassen",      "#2B2B2B"),
    ("water",     "Wasser",        "#6AA9E0"),
    ("green",     "Gruenflaechen", "#7FD6A4"),
    ("heart",     "Herz",          "#F0568A"),
]

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

# Gesamtzeit fuer alle Overpass-Versuche zusammen (Sekunden).
OVERPASS_BUDGET_S = 250

# Hoehen der duennen Ebenen ueber der Grundplatte (mm).
# Leicht gestaffelt: Strassen liegen oben, damit sie Gruenflaechen kreuzen.
Z_GREEN = 0.34
Z_WATER = 0.44
Z_ROAD = 0.60
Z_HEART = 1.60


def road_width_m(tags: dict) -> float:
    """Strassenbreite in Metern nach highway-Klasse."""
    hw = tags.get("highway", "")
    table = [
        (("motorway", "trunk"), 14.0),
        (("primary",), 11.0),
        (("secondary",), 9.0),
        (("tertiary",), 7.5),
        (("residential", "unclassified", "living_street", "service"), 5.5),
        (("pedestrian",), 5.0),
        (("cycleway",), 2.5),
        (("footway", "path", "steps", "track"), 2.0),
    ]
    for keys, w in table:
        if any(k in hw for k in keys):
            return w
    return 5.0


def building_height_m(tags: dict) -> float:
    """Gebaeudehoehe aus OSM-Tags, mit Fallbacks."""
    h = tags.get("height")
    if h:
        try:
            return max(2.0, float(str(h).replace(",", ".").replace("m", "").strip()))
        except ValueError:
            pass
    levels = tags.get("building:levels")
    if levels:
        try:
            return max(2.0, float(levels) * 3.2)
        except ValueError:
            pass
    return 9.0


# ----------------------------------------------------------------------
# OSM laden
# ----------------------------------------------------------------------

def fetch_osm(south: float, west: float, north: float, east: float) -> dict:
    """Laedt Gebaeude, Strassen, Wasser und Gruenflaechen via Overpass."""
    bbox = f"{south},{west},{north},{east}"
    query = f"""[out:json][timeout:180];
(
  way["building"]({bbox});
  way["highway"]({bbox});
  way["natural"="water"]({bbox});
  way["waterway"="riverbank"]({bbox});
  way["leisure"~"^(park|garden|playground|pitch)$"]({bbox});
  way["landuse"~"^(grass|forest|meadow|recreation_ground|village_green|cemetery)$"]({bbox});
);
(._;>;);
out body;"""
    # Gesamtbudget: lieber nach gut 4 Minuten mit klarer Meldung aufgeben,
    # als das Frontend minutenlang haengen lassen.
    deadline = time.monotonic() + OVERPASS_BUDGET_S
    reasons: dict[str, str] = {}

    # Zwei Runden: 502/504/Timeout sind bei Overpass meist nur Last und
    # gehen beim zweiten Versuch durch.
    for _ in range(2):
        for url in OVERPASS_ENDPOINTS:
            host = url.split("/")[2]
            for verify in (True, False):
                # Verbindungsaufbau muss schnell scheitern, damit ein toter
                # Endpoint das Failover nicht blockiert. Lesen darf dauern,
                # weil Overpass Anfragen in eine Warteschlange legt — aber
                # nie laenger als das Restbudget.
                read_budget = deadline - time.monotonic()
                if read_budget < 10:
                    reasons.setdefault(host, "nicht mehr versucht (Zeitbudget)")
                    break
                try:
                    if not verify:
                        # Letzter Ausweg hinter TLS-inspizierenden Firmen-Proxys.
                        import urllib3
                        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
                    r = requests.post(url, data={"data": query},
                                      timeout=(7, min(150, read_budget)),
                                      headers={"User-Agent": "Maptonics/1.0"},
                                      verify=verify)
                    r.raise_for_status()
                    return r.json()
                except requests.exceptions.SSLError as e:
                    reasons[host] = f"TLS: {type(e).__name__}"
                    continue  # gleicher Endpoint, ohne Verifizierung
                except requests.exceptions.HTTPError as e:
                    reasons[host] = f"HTTP {e.response.status_code} (ueberlastet)"
                    break  # naechster Endpoint
                except requests.exceptions.Timeout:
                    reasons[host] = "Zeitueberschreitung"
                    break
                except Exception as e:  # noqa: BLE001 - Endpoint-Fallback
                    reasons[host] = type(e).__name__
                    break

    detail = ", ".join(f"{h}: {w}" for h, w in reasons.items())
    raise RuntimeError(
        "Alle Overpass-Server sind gerade belegt oder nicht erreichbar "
        f"({detail}). Das liegt nicht an Maptonics — in ein paar Minuten "
        "nochmal versuchen, oder einen kleineren Bereich waehlen.")


# ----------------------------------------------------------------------
# Mesh-Bau
# ----------------------------------------------------------------------

class LayerMesh:
    """Sammelt Dreiecksgeometrie einer Ebene."""

    def __init__(self, key, name, color):
        self.key = key
        self.name = name
        self.color = color
        self.verts: list[float] = []   # x,y,z, x,y,z, ...
        self.tris: list[int] = []      # Indizes, 3 pro Dreieck

    def empty(self) -> bool:
        return not self.tris


def _triangulate(poly: Polygon):
    """Trianguliert ein shapely-Polygon (inkl. Loecher) mit earcut.
    Rueckgabe: (punkte Nx2, dreiecks-indizes M)"""
    rings = [np.asarray(poly.exterior.coords)[:-1]]
    rings += [np.asarray(r.coords)[:-1] for r in poly.interiors]
    pts = np.concatenate(rings).astype(np.float64)
    ring_ends = np.cumsum([len(r) for r in rings]).astype(np.uint32)
    tris = mapbox_earcut.triangulate_float64(pts, ring_ends)
    return pts, np.asarray(tris, dtype=np.int64)


def _ring_ccw(coords: np.ndarray) -> np.ndarray:
    """Erzwingt Gegenuhrzeigersinn (positives signiertes Flaecheninhalt)."""
    x, y = coords[:, 0], coords[:, 1]
    area = np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y)
    return coords if area > 0 else coords[::-1]


def extrude_polygon(poly: Polygon, z0: float, z1: float, layer: LayerMesh):
    """Extrudiert ein Polygon (mit Loechern) zu einem geschlossenen Prisma."""
    if poly.is_empty or poly.area < 1e-6:
        return
    poly = poly.buffer(0)  # selbstschneidende Ringe reparieren
    if isinstance(poly, MultiPolygon):
        for p in poly.geoms:
            extrude_polygon(p, z0, z1, layer)
        return
    if not isinstance(poly, Polygon) or poly.is_empty:
        return
    # Aussenring CCW, Loecher CW -> definierte Wicklung
    poly = orient(poly, sign=1.0)

    pts, tris = _triangulate(poly)
    if len(tris) == 0:
        return

    base = len(layer.verts) // 3
    n = len(pts)
    # Deck- und Bodenvertices
    for x, y in pts:
        layer.verts.extend((float(x), float(y), z1))
    for x, y in pts:
        layer.verts.extend((float(x), float(y), z0))

    # earcut folgt der Ring-Orientierung: Aussenring CCW -> Deckel-Normale +z
    for i in range(0, len(tris), 3):
        a, b, c = int(tris[i]), int(tris[i + 1]), int(tris[i + 2])
        layer.tris.extend((base + a, base + b, base + c))             # Deckel
        layer.tris.extend((base + n + a, base + n + c, base + n + b)) # Boden

    # Waende entlang aller Ringe (Aussen CCW, Loecher CW -> Normalen aussen)
    offset = 0
    ring_list = [np.asarray(poly.exterior.coords)[:-1]]
    ring_list += [np.asarray(r.coords)[:-1] for r in poly.interiors]
    for ring in ring_list:
        m = len(ring)
        for i in range(m):
            j = (i + 1) % m
            ti, tj = base + offset + i, base + offset + j
            bi, bj = ti + n, tj + n
            layer.tris.extend((ti, bj, tj))
            layer.tris.extend((ti, bi, bj))
        offset += m


def heart_polygon(cx: float, cy: float, size_mm: float) -> Polygon:
    """Parametrisches Herz, ca. size_mm breit, Mittelpunkt (cx, cy)."""
    s = size_mm / 32.0
    t = np.linspace(0, 2 * math.pi, 64, endpoint=False)
    x = cx + 16 * np.sin(t) ** 3 * s
    y = cy + (13 * np.cos(t) - 5 * np.cos(2 * t) - 2 * np.cos(3 * t) - np.cos(4 * t)) * s
    return Polygon(np.column_stack([x, y]))


def plan_grid(bounds: dict, tile_mm: float, meters_per_tile: float) -> dict:
    """
    Plant aus dem gezeichneten Bereich ein Kachel-Raster bei FESTEM Massstab.
    Eine Kachel = tile_mm (z.B. 200 mm = 20 cm) = meters_per_tile reale Meter.
    Der gezeichnete Bereich wird auf volle Kacheln aufgerundet und zentriert.
    """
    s, w = bounds["south"], bounds["west"]
    n, e = bounds["north"], bounds["east"]
    lat0 = (s + n) / 2
    m_lon = 111320.0 * math.cos(math.radians(lat0))
    m_lat = 110540.0

    width_m = max(1.0, (e - w) * m_lon)
    height_m = max(1.0, (n - s) * m_lat)
    cols = max(1, math.ceil(width_m / meters_per_tile - 1e-6))
    rows = max(1, math.ceil(height_m / meters_per_tile - 1e-6))

    total_w_m = cols * meters_per_tile
    total_h_m = rows * meters_per_tile
    clat, clon = (s + n) / 2, (w + e) / 2
    half_w_deg = (total_w_m / 2) / m_lon
    half_h_deg = (total_h_m / 2) / m_lat
    exp = {
        "south": clat - half_h_deg, "north": clat + half_h_deg,
        "west": clon - half_w_deg, "east": clon + half_w_deg,
    }
    return {
        "cols": cols, "rows": rows, "exp_bounds": exp,
        "m_lon": m_lon, "m_lat": m_lat,
        "tiles_total": cols * rows,
        "total_w_cm": cols * tile_mm / 10.0,
        "total_h_cm": rows * tile_mm / 10.0,
    }


def build_project(data: dict, grid: dict, params: dict) -> dict:
    """
    Baut das gesamte Raster bei festem Massstab. Jede Kachel ist ein eigenes,
    tile_mm x tile_mm grosses Modell mit eigenen Ebenen-Meshes, sodass Kacheln
    nebeneinander gedruckt und zusammengesetzt werden koennen.
    """
    exp = grid["exp_bounds"]
    m_lon, m_lat = grid["m_lon"], grid["m_lat"]
    cols, rows = grid["cols"], grid["rows"]

    tile_mm = float(params.get("tile_mm", 200.0))
    mpt = float(params.get("meters_per_tile", 1000.0))
    scale = tile_mm / mpt                       # mm pro Meter (FEST)
    base_h = float(params.get("base_mm", 2.0))
    hf = float(params.get("height_factor", 1.0))
    enabled = params.get("layers", {})

    sw_lat, sw_lon = exp["south"], exp["west"]

    def g(lat, lon):
        """lat/lon -> globale mm-Koordinate (SW-Ecke des Rasters = 0,0)."""
        return ((lon - sw_lon) * m_lon * scale, (lat - sw_lat) * m_lat * scale)

    Wtot, Htot = cols * tile_mm, rows * tile_mm
    full = box(0, 0, Wtot, Htot)

    nodes = {el["id"]: (el["lat"], el["lon"])
             for el in data.get("elements", []) if el["type"] == "node"}
    ways = [el for el in data.get("elements", []) if el["type"] == "way"]

    # --- Globale 2D-Geometrie EINMAL bauen ---------------------------------
    buildings = []           # (poly_global, hoehe_mm)
    road_polys, water_polys, green_polys = [], [], []
    for way in ways:
        tags = way.get("tags", {})
        coords = [g(*nodes[i]) for i in way.get("nodes", []) if i in nodes]
        if len(coords) < 2:
            continue
        closed = way["nodes"][0] == way["nodes"][-1] and len(coords) >= 4

        if "building" in tags and enabled.get("buildings", True) and closed:
            poly = Polygon(coords).buffer(0)
            if not poly.is_empty:
                bh = min(max(building_height_m(tags) * scale * hf, 0.6), 120.0)
                buildings.append((poly, bh))
        elif "highway" in tags and enabled.get("roads", True):
            half = max(0.28, road_width_m(tags) * scale / 2)
            road_polys.append(LineString(coords).buffer(
                half, cap_style="round", join_style="round"))
        elif (tags.get("natural") == "water" or tags.get("waterway") == "riverbank") \
                and enabled.get("water", True) and closed:
            water_polys.append(Polygon(coords).buffer(0))
        elif (("leisure" in tags) or ("landuse" in tags)) \
                and enabled.get("green", True) and closed:
            green_polys.append(Polygon(coords).buffer(0))

    roads_u = unary_union(road_polys).intersection(full) if road_polys else None
    water_u = unary_union(water_polys).intersection(full) if water_polys else None
    green_u = unary_union(green_polys).intersection(full) if green_polys else None

    heart_g = None
    heart = params.get("heart")
    if heart and enabled.get("heart", True):
        hx, hy = g(heart["lat"], heart["lon"])
        if 0 <= hx <= Wtot and 0 <= hy <= Htot:
            heart_g = heart_polygon(hx, hy, max(6.0, tile_mm * 0.05))

    # --- Pro Kachel zuschneiden und extrudieren ----------------------------
    from shapely.affinity import translate as _translate

    tiles = []
    totals = {"buildings": 0, "roads": 0, "water": 0, "green": 0, "heart": False}

    for r in range(rows):
        for c in range(cols):
            ox, oy = c * tile_mm, r * tile_mm
            tbox = box(ox, oy, ox + tile_mm, oy + tile_mm)

            layers = {k: LayerMesh(k, nm, col) for k, nm, col in LAYERS}
            extrude_polygon(box(0, 0, tile_mm, tile_mm), 0.0, base_h, layers["base"])
            building_tops = []

            for poly, bh in buildings:
                inter = poly.intersection(tbox)
                if inter.is_empty:
                    continue
                inter = _translate(inter, xoff=-ox, yoff=-oy)
                extrude_polygon(inter, base_h, base_h + bh, layers["buildings"])
                building_tops.append((inter, base_h + bh))
                totals["buildings"] += 1

            for src, key, ztop, cnt in (
                (roads_u, "roads", Z_ROAD, "roads"),
                (water_u, "water", Z_WATER, "water"),
                (green_u, "green", Z_GREEN, "green"),
            ):
                if src is None:
                    continue
                inter = src.intersection(tbox)
                if inter.is_empty:
                    continue
                inter = _translate(inter, xoff=-ox, yoff=-oy)
                extrude_polygon(inter, base_h, base_h + ztop, layers[key])
                totals[cnt] += 1

            if heart_g is not None:
                hp = heart_g.intersection(tbox)
                if not hp.is_empty:
                    hp = _translate(hp, xoff=-ox, yoff=-oy)
                    top_under = base_h
                    for bpoly, btop in building_tops:
                        if hp.intersects(bpoly):
                            top_under = max(top_under, btop)
                    heart_top = max(base_h + Z_HEART, top_under + 1.4)
                    extrude_polygon(hp, base_h, heart_top, layers["heart"])
                    totals["heart"] = True

            used = [layers[k] for k, _, _ in LAYERS if not layers[k].empty()]
            tiles.append({"r": r, "c": c, "layers": used})

    return {
        "tiles": tiles, "cols": cols, "rows": rows,
        "tile_mm": tile_mm, "meters_per_tile": mpt,
        "exp_bounds": exp, "stats": totals,
    }


# ----------------------------------------------------------------------
# 3MF schreiben
# ----------------------------------------------------------------------

def write_3mf(model) -> bytes:
    """Schreibt ein Modell als 3MF. `model` ist entweder eine Layer-Liste
    (eine Kachel) oder ein dict mit Schluessel 'layers'. Ein Objekt pro Ebene."""
    layers = model["layers"] if isinstance(model, dict) else model
    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write('<model unit="millimeter" xml:lang="de" '
              'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n')
    out.write('<metadata name="Application">Maptonics</metadata>\n')
    out.write('<metadata name="Copyright">Kartendaten: (c) OpenStreetMap contributors (ODbL)</metadata>\n')
    out.write('<resources>\n<basematerials id="1">\n')
    for layer in layers:
        out.write(f'<base name="{layer.name}" displaycolor="{layer.color.upper()}FF"/>\n')
    out.write('</basematerials>\n')

    for idx, layer in enumerate(layers):
        oid = idx + 2
        out.write(f'<object id="{oid}" type="model" name="{layer.name}" '
                  f'pid="1" pindex="{idx}">\n<mesh>\n<vertices>\n')
        v = layer.verts
        out.write("\n".join(
            f'<vertex x="{v[i]:.3f}" y="{v[i+1]:.3f}" z="{v[i+2]:.3f}"/>'
            for i in range(0, len(v), 3)))
        out.write("\n</vertices>\n<triangles>\n")
        t = layer.tris
        out.write("\n".join(
            f'<triangle v1="{t[i]}" v2="{t[i+1]}" v3="{t[i+2]}"/>'
            for i in range(0, len(t), 3)))
        out.write("\n</triangles>\n</mesh>\n</object>\n")

    out.write('</resources>\n<build>\n')
    for idx in range(len(layers)):
        out.write(f'<item objectid="{idx + 2}"/>\n')
    out.write('</build>\n</model>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?>\n'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?>\n'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
                   '<Relationship Target="/3D/3dmodel.model" Id="rel-1" '
                   'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
                   '</Relationships>')
        z.writestr("3D/3dmodel.model", out.getvalue())
    return buf.getvalue()


def export_project(project: dict) -> tuple[bytes, str, str]:
    """Exportiert das Projekt. 1 Kachel -> einzelne .3mf.
    Mehrere Kacheln -> .zip mit einer .3mf je Kachel + Zusammenbau-Hinweis.
    Rueckgabe: (bytes, dateiname, mimetype)."""
    tiles = project["tiles"]
    cols, rows = project["cols"], project["rows"]

    if len(tiles) == 1:
        return write_3mf(tiles[0]["layers"]), "maptonics_modell.3mf", "model/3mf"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for t in tiles:
            # Reihen von oben (Norden) nach unten benennen, wie man sie aufhaengt
            name = f"kachel_z{rows - t['r']}_s{t['c'] + 1}.3mf"
            z.writestr(name, write_3mf(t["layers"]))
        tile_cm = project["tile_mm"] / 10.0
        z.writestr("ZUSAMMENBAU.txt",
                   "Maptonics — Kachel-Raster\n"
                   "==========================\n\n"
                   f"Raster: {cols} Spalten x {rows} Reihen = {len(tiles)} Kacheln\n"
                   f"Jede Kachel: {tile_cm:.0f} x {tile_cm:.0f} cm\n"
                   f"Gesamtbild: {cols * tile_cm:.0f} x {rows * tile_cm:.0f} cm\n\n"
                   "Dateiname = kachel_z<Reihe von oben>_s<Spalte von links>.3mf\n"
                   "Kacheln nach dem Druck Kante an Kante zusammenstecken/kleben.\n"
                   "Massstab ist fest, alle Kacheln passen exakt aneinander.\n\n"
                   "Kartendaten: (c) OpenStreetMap contributors (ODbL)\n")
    return buf.getvalue(), f"maptonics_raster_{cols}x{rows}.zip", "application/zip"
