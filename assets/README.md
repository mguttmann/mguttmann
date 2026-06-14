# assets/

Eigene, **lokal gehostete** Grafik-Fallbacks für das Profil-README. Diese Dateien sind **optional** und nur dann nötig, wenn ein externer Render-Dienst (z. B. `capsule-render`, `readme-typing-svg`) ausfällt oder du dich von externen Abrufen unabhängig machen willst.

## Inhalt

- **`.gitkeep`** — hält den Ordner im Git-Tree, falls sonst leer.
- **`divider.svg`** — dezenter, horizontaler Trenn-Strich (GitHub-Grün, transluzenter Verlauf). Reines Inline-SVG, **kein** externer Abruf, **kein** Tracking, dark/light-neutral. Einsatz im README per:

  ```markdown
  ![](assets/divider.svg)
  ```

  (Pfad relativ zum Repo-Root, also nach dem Push nach `mguttmann/mguttmann` direkt nutzbar.)

## Warum (zunächst) so schlank?

Die **bewegten** Elemente des READMEs kommen aus zwei Quellen, für die **keine** eigenen Bilder nötig sind:

1. **Snake-Animation** → wird von der GitHub Action (`snake.yml`) erzeugt und im Branch `output` abgelegt; das README bindet sie über `raw.githubusercontent.com/.../output/...` ein. Kein lokales Asset erforderlich.
2. **Typing-Header & Banner/Divider** → externe SVG-Dienste (`readme-typing-svg`, `capsule-render`), die zur Anzeigezeit gerendert werden.

`divider.svg` liegt hier als **robuster Offline-Fallback** bereit, falls einer dieser externen Dienste nicht erreichbar ist. Wird er nicht gebraucht, kann der `assets/`-Ordner beim Push weggelassen werden (siehe `DEPLOY.md`, Schritt 2).
