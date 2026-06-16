# reMarkable Toolkit

Beliebige Inhalte an die reMarkable-Cloud **senden** und Dokumente/Notizen
**abziehen** — per Skript oder über die Claude-Skill `/remarkable`.

Engine: [`rmapi`](https://github.com/ddvk/rmapi) (Cloud-API, sync15) +
Headless-Chrome (HTML→PDF) + [`rmc`](https://github.com/ricklupton/rmc) /
[`rmscene`](https://github.com/ricklupton/rmscene) (Handschrift-Rendering).

## Aufbau

```
bin/rmapi          Transport-Engine (Cloud-API)
.venv/             Python-Umgebung (markdown, pygments, trafilatura, rmc, rmscene)
.rmapi.conf        Device-Token (Geheimnis, .gitignore)
scripts/
  rmlib.py         gemeinsame Bausteine: Pfade + Render-Kette (MD/HTML → PDF)
  send.py          Datei | URL | Markdown  →  PDF/EPUB  →  Gerät
  pull.py          list | get | backup | render
  md2html.py       Standalone: Markdown → bildschirmoptimiertes HTML
```

## Setup (frischer Checkout)

```bash
bash scripts/setup.sh   # lädt rmapi, baut .venv, installiert die /remarkable-Skill
```
Danach einmalig anmelden (8-Zeichen-Code von https://my.remarkable.com/device/browser/connect):
```bash
echo <CODE> | RMAPI_CONFIG=$PWD/.rmapi.conf bin/rmapi ls
```

Läuft auf **macOS & Linux**. PDF-Seitenformat per `RM_PAGE_SIZE` einstellbar
(Default = Paper Pro Move 7,3″; z. B. `RM_PAGE_SIZE="157mm 210mm"` für rM/rM2).

## Nutzung

```bash
PY=.venv/bin/python

# Senden
$PY scripts/send.py datei.pdf                       # PDF/EPUB direkt
$PY scripts/send.py notiz.md --dest /Lesestoff      # Markdown → bildschirm-PDF
$PY scripts/send.py "https://…/artikel" --name "X"  # Web-Artikel → PDF

# Abziehen
$PY scripts/pull.py list
$PY scripts/pull.py backup ./backup                 # alles sichern
$PY scripts/pull.py render "Schnellnotiz" -o note.pdf
```

## Grenzen

- Gerät akzeptiert nativ nur **PDF/EPUB**; alles andere wird zu PDF konvertiert.
- **Handschrift** → Bild (kein OCR). Bei neuen Farbgeräten (Paper Pro/Move) kann
  **Farbe fehlen** oder eine Seite nicht rendern, solange `rmscene` neue Blocktypen
  nicht abdeckt (`render` meldet übersprungene Seiten); `pip install -U rmc rmscene`.
- **Free-Tier:** Cloud hält nur ~50 Tage; ältere Notizbücher nur lokal (USB/SSH).

Neu-Anmeldung bei abgelaufenem Token: siehe `~/.claude/skills/remarkable/SKILL.md`.
