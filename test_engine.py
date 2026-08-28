# -*- coding: utf-8 -*-
"""Engine-Test mit synthetischen OSM-Daten (kein Netz noetig)."""
import sys
sys.path.insert(0, ".")
from engine import plan_grid, build_project, export_project
import trimesh, io, zipfile

def node(i, lat, lon): return {"type": "node", "id": i, "lat": lat, "lon": lon}
els = [node(1,51.4350,6.7500), node(2,51.4350,6.7800),
       {"type":"way","id":1000,"nodes":[1,2],"tags":{"highway":"primary"}},
       node(3,51.4360,6.7560), node(4,51.4360,6.7568),
       node(5,51.4366,6.7568), node(6,51.4366,6.7560),
       {"type":"way","id":2000,"nodes":[3,4,5,6,3],"tags":{"building":"yes","height":"25"}}]
data = {"elements": els}
params = {"tile_mm":200,"meters_per_tile":1000,"base_mm":2,"height_factor":1,
          "layers":{"buildings":True,"roads":True,"water":True,"green":True,"heart":True},
          "heart":{"lat":51.4350,"lon":6.7648}}

# 1 Kachel
grid = plan_grid({"south":51.4345,"west":6.7640,"north":51.4356,"east":6.7655}, 200, 1000)
proj = build_project(data, grid, params)
blob, fn, mt = export_project(proj)
sc = trimesh.load(io.BytesIO(blob), file_type="3mf")
print(f"1 Kachel: {grid['cols']}x{grid['rows']} -> {fn} "
      f"wasserdicht={all(g.is_watertight for g in sc.geometry.values())}")

# Raster
grid2 = plan_grid({"south":51.4310,"west":6.7540,"north":51.4390,"east":6.7760}, 200, 600)
proj2 = build_project(data, grid2, params)
blob2, fn2, mt2 = export_project(proj2)
zf = zipfile.ZipFile(io.BytesIO(blob2))
n3mf = [n for n in zf.namelist() if n.endswith(".3mf")]
allwt = all(all(g.is_watertight for g in trimesh.load(io.BytesIO(zf.read(n)), file_type="3mf").geometry.values()) for n in n3mf)
print(f"Raster: {grid2['cols']}x{grid2['rows']} -> {fn2} ({len(n3mf)} 3mf) alle_wasserdicht={allwt}")
print("OK" if allwt else "FEHLER")
