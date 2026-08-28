# Maptonics — Karten zum Anfassen

OpenStreetMap-Ausschnitt → 3D-druckbares Multicolor-Modell (3MF).
Jede Ebene (Grundplatte, Gebäude, Straßen, Wasser, Grünflächen, Herz-Marker)
wird als **eigenes Objekt** exportiert → in Bambu Studio pro Objekt einfach
ein Filament zuweisen (Rechtsklick → Filament ändern). Kein Painting nötig.

## Fester Maßstab + Kacheln (neu)

Eine Kachel ist **immer 20×20 cm** (Kachelgröße einstellbar) und zeigt **immer
dieselbe reale Fläche** ("Meter pro Kachel" = dein Zoom, wird gespeichert).

- Kleiner Bereich → 1 Kachel → ideal für den Bilderrahmen (20×20 cm).
- Großer Bereich (z.B. ganz Köln) → Raster aus Kacheln. Export = ZIP mit einer
  3MF je Kachel. Einzeln drucken, dann Kante an Kante zusammensetzen — durch den
  festen Maßstab passen alle Kacheln exakt aneinander.
- Button "Aktuelle Auswahl = 1 Kachel" übernimmt den aktuellen Karten-Ausschnitt
  als Maßstab.

## Start

```
pip install -r requirements.txt
python app.py
```

Browser öffnet sich automatisch auf http://127.0.0.1:5173

## Workflow für den 20×20-Rahmen

1. Adresse suchen
2. "Rechteck zeichnen" → euren Spot grob aufziehen
3. "Aktuelle Auswahl = 1 Kachel" → Maßstab festlegen (oder "Meter pro Kachel" direkt eingeben)
4. Optional: "Herz-Position setzen"
5. "Mesh generieren" → 3D-Vorschau prüfen (sollte 1 × 1 Kachel sein)
6. "3MF exportieren" → in Bambu Studio öffnen

## Druck-Tipps

- Straßen/Wasser/Grün sind 0,3–0,6 mm dick (1–3 Layer bei 0,2 mm) — ideal für
  flache, gerahmte Karten.
- Gebäudehöhen-Slider auf ×1,5–2 wirkt im Rahmen dramatischer.
- Alle Meshes sind wasserdicht; überlappende Straßen werden vereinigt.
- Firmen-Laptop mit Proxy/VPN: truststore nutzt den Windows-Zertifikatsspeicher,
  bei SSL-Fehler wird automatisch ein unverifizierter Fallback versucht.

## Als .exe paketieren (Windows)

```
pip install pyinstaller
pyinstaller --onefile --add-data "static;static" --name Maptonics app.py
```

## Lizenz / Attribution (wichtig für kommerziellen Verkauf!)

Kartendaten: © OpenStreetMap-Mitwirkende, ODbL. Gedruckte Modelle sind
"Produced Works" und dürfen kommerziell verkauft werden, solange die
Attribution angegeben wird (Produktseite und/oder Rahmen-Rückseite).
Steht auch in den 3MF-Metadaten und der ZUSAMMENBAU.txt.
