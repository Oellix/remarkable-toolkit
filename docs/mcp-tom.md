# reMarkable MCP für Tom (Hermes) — noch NICHT scharf

> **Status: Tom-Exposure ist bewusst NICHT scharfgeschaltet.** Dieses Dokument
> beschreibt, *wie* der reMarkable-MCP-Server an Toms Hermes-Gateway angebunden
> würde, und welche **zwei Voraussetzungen** vorher erfüllt sein müssen. Solange
> H2 und H3 (unten) nicht entschieden/umgesetzt sind, wird der Server in Toms
> `~/.hermes/config.yaml` **nicht** registriert.

## Wie es registriert würde

Der Server wird genauso eingehängt wie der bestehende `homeassistant`-MCP-Server
in Toms `~/.hermes/config.yaml` — ein stdio-Prozess unter `mcp_servers:` mit
`command`, optionalen `args`, einer `env`-Map und `enabled`. Schema-Form ist
gegen Toms Live-Config verifiziert (homeassistant + obsidian Einträge).

```yaml
# ~/.hermes/config.yaml  (auf host 'tom' / mac-mini-von-tom)
mcp_servers:
  # ... bestehende Einträge (homeassistant, obsidian) ...

  remarkable:
    command: /Users/tom/remarkable/.venv/bin/python   # venv-Python im Repo-Klon AUF tom
    args:
      - /Users/tom/remarkable/scripts/mcp_server.py     # absoluter Pfad (Hermes-cwd ≠ Repo)
    env:
      # CONFINEMENT: NICHT 'ALL'. Tom darf nur in /HERMES schreiben (fail-closed).
      RM_ALLOWED_PREFIX: /HERMES
      # Per-Agent-Token: EIGENE .rmapi.conf für Tom, chmod 600, NICHT im Agent-CWD.
      RMAPI_CONFIG: /Users/tom/.config/remarkable/tom.rmapi.conf
    enabled: true
```

Anmerkungen:
- **Absoluter `args`-Pfad.** Auf Alex' Maschine (`.mcp.json`) ist der Pfad
  cwd-relativ und funktioniert, weil der Server seine Repo-Wurzel aus `__file__`
  auflöst. Für Tom trotzdem absolut schreiben — der Hermes-cwd ist nicht das
  Repo, und `command`/`args` werden ohne Repo-cwd gestartet.
- **`RM_ALLOWED_PREFIX: /HERMES`** schaltet das Schreib-Confinement scharf
  (fail-closed): jeder Upload/`mkdir`/`mv`/`rm` außerhalb von `/HERMES` wird vom
  Server abgelehnt (`isError=true`, `error: precondition_failed`). Unset/`""` ⇒
  Schreiben komplett verweigert. Nur das Sentinel `ALL` hebt das Confinement auf
  (das wäre der Alex-Fall, **nicht** Tom).
- **Eigene `.rmapi.conf` für Tom** (eigenes Device-Token) → unabhängig
  widerrufbar, ohne Alex' Token anzufassen.

## Voraussetzung H2 — SSH/Client-Env propagiert NICHT, Kopplung an die Token-Identität nötig

Das `env:`-Feld oben gibt Tom-**lokal** den Prefix mit. Das ist nur sicher,
**solange Tom (Hermes) den Server lokal als Kindprozess startet** und die `env`
aus der Config kommt — also nicht vom *Client* frei wählbar ist.

Problematisch wird es, sobald der Aufruf über eine Grenze geht (SSH, Remote-MCP,
ein Proxy): **`RM_ALLOWED_PREFIX` und `RMAPI_CONFIG` dürfen nicht per Client-Env
einstellbar sein** — sonst setzt ein kompromittierter/irrender Client einfach
`RM_ALLOWED_PREFIX=ALL` und das Confinement ist weg. SSH propagiert per Default
ohnehin keine beliebigen Env-Variablen (`AcceptEnv`/`SendEnv`), d. h. ein naiver
„Env durchreichen"-Ansatz **trägt nicht** über SSH.

**Anforderung:** Prefix **und** Token-Conf müssen **serverseitig an die
Token-IDENTITÄT gekoppelt** werden, nicht frei am Client hängen. Konkret:
- eine **root-owned Map** `Token-/Agent-Identität → {prefix, rmapi_conf}` auf
  dem Host, die der Server beim Start liest (Client kann sie nicht überstimmen),
  **oder**
- der Server läuft als ein dedizierter, unprivilegierter User, dessen
  Environment (Prefix + Conf-Pfad) fix in der LaunchAgent-/systemd-Unit steht
  und vom Client nicht erreichbar ist.

Bis das steht, ist „Tom schreibt confined nach /HERMES" nur so stark wie die
Annahme, dass niemand Toms `env` umbiegen kann.

## Voraussetzung H3 — ENTSCHEIDUNG: Lese-Tools geben Tom Zugriff auf den GANZEN Account

`rm_list`, `rm_get`, `rm_render`, `rm_backup` laufen über `pull.py` und sind
bewusst **read-only** — sie respektieren `RM_ALLOWED_PREFIX` **NICHT**.
reMarkable-Device-Tokens sind nicht ordner-scopebar; ein Read-Confinement müsste
(wie der Write-Guard) clientseitig in diesen Befehlen erzwungen werden und
existiert heute nicht. Das heißt:

> **Mit den Lese-Tools sieht Tom den GESAMTEN reMarkable-Account** — jedes
> Notizbuch, jedes PDF, und `rm_backup` zieht **alles** herunter. Das
> Schreib-Confinement (`/HERMES`) ändert daran nichts.

**Es ist eine bewusste Entscheidung nötig — eine der beiden:**

| Option | Was tun | Folge |
|--------|---------|-------|
| **A — bewusst zulassen** | Lese-Tools registrieren | Tom kann den ganzen Account lesen/sichern. Nur akzeptabel, wenn Tom als voll vertrauenswürdig gilt UND H2 steht. |
| **B — Lese-Tools NICHT registrieren** | `rm_list/rm_get/rm_render/rm_backup` für Tom weglassen (nur `rm_send`), oder ein read-confined `pull.py` mit Prefix-Filter ergänzen, bevor Lesen freigeschaltet wird | Tom kann nur senden (confined nach /HERMES), aber nichts vom Account abziehen. |

Eine MCP-Allowlist auf Tool-Ebene (nur ausgewählte Tools registrieren) ist der
saubere Weg für Option B; der Server selbst exponiert alle fünf Tools.

## Honest-Box (warum „noch nicht scharf")

- Der Schreib-Guard schützt vor **versehentlichen** out-of-prefix-Writes eines
  **korrekt konfigurierten** Wrappers. Er hat **~null Adversary-Resistance**:
  Wer Toms `.rmapi.conf` oder das nackte `bin/rmapi` erreicht, hat trotzdem
  Vollzugriff (Token unscoped). Echtes Confinement = Per-Agent-Token +
  Hermes-Tool-Allowlist, **raw `rmapi` nie exponieren**.
- Solange **H2** (Identitäts-Kopplung) und **H3** (Read-Scope-Entscheidung)
  offen sind, bleibt der Eintrag oben eine Vorlage — **nicht** in Toms aktive
  Config übernehmen.

Verwandt: `README.md` → Abschnitt **MCP** (Trusted-Local-Fall, `.mcp.json`),
`scripts/rmlib.py` (Guard, Notizen M1), `scripts/pull.py` (Read-Confinement-Note H3).
