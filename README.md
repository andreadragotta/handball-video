# Handball Video

Editor semplice per l'analisi video delle partite di pallamano (Windows e Mac).

## Come si usa
1. **Apri video** della partita.
2. Porta il video all'inizio dell'azione → **Inizio (I)**, alla fine → **Fine (O)**, scrivi un nome → **Aggiungi clip** (o Invio).
3. Seleziona una clip nella **Sequenza**, metti in pausa nel punto giusto, scegli uno strumento
   (Freccia, Passaggio tratteggiato, Linea, Riquadro, Cerchio, Testo) e disegna sul video.
   Ogni disegno appare per 4 secondi: puoi cambiare quando appare/sparisce nel pannello a destra.
4. **＋ Copertina** inserisce una schermata con sfondo pieno, titolo e sottotitolo prima dell'elemento selezionato.
5. Trascina gli elementi per riordinarli, poi **Esporta**: elemento selezionato, tutto in un video, oppure un file per elemento.

Il progetto si salva come file `.hvproj` (contiene solo tagli e disegni, non il video).

Scorciatoie: Spazio play/pausa · ←/→ fotogramma · Shift+←/→ 1 s · J/L 5 s · I/O inizio/fine · Ctrl/Cmd+Z annulla disegno · Esc puntatore.

## Sviluppo
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py
```

## Creare l'installabile
```bash
.venv/bin/pip install pyinstaller
.venv/bin/python build.py        # → dist/
```
PyInstaller crea l'app solo per il sistema su cui gira: per Windows usa un PC Windows oppure
la GitHub Action `.github/workflows/build.yml` (avvio manuale o tag `v*`), che produce gli zip
per Windows, Mac Apple Silicon e Mac Intel. FFmpeg è incluso nell'app.

Note per la distribuzione:
- **Mac**: senza firma e notarizzazione Apple (account sviluppatore, 99 $/anno) al primo avvio serve
  tasto destro → Apri.
- **Windows**: SmartScreen può mostrare un avviso; per un installer vero si può aggiungere Inno Setup.

## Struttura
- `handballvideo/model.py` — progetto, clip, copertine, annotazioni (salvataggio JSON)
- `handballvideo/render.py` — disegno di annotazioni e copertine (usato da anteprima ed export)
- `handballvideo/exporter.py` — taglio, sovrapposizioni e unione con FFmpeg
- `handballvideo/video_view.py` — player con livello di disegno
- `handballvideo/main_window.py` — interfaccia
