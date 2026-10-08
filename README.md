# Sous-titres

Traduit les sous-titres déjà présents dans un film (ou un fichier `.srt` / `.ass` / `.vtt`) dans une autre langue, en gardant le minutage d'origine.

- Dépose un **MKV / MP4** : les pistes de sous-titres texte sont extraites avec ffmpeg, tu choisis laquelle traduire.
- Ou dépose directement un **fichier de sous-titres**.
- Option pour retirer les descriptions pour malentendants (SDH) : `Musique intense`, `[PORTE QUI CLAQUE]`, `JEAN:`…
- Traduction par **Google Traduction** (gratuit), ou **DeepL** si une clé est configurée.

## Lancer

```bash
docker compose up -d --build
```

Puis http://localhost:8000.

### Variables d'environnement

| Variable        | Défaut  | Rôle                                                            |
|-----------------|---------|-----------------------------------------------------------------|
| `MAX_UPLOAD_MB` | `20000` | Taille max d'un fichier envoyé                                  |
| `DEEPL_API_KEY` | —       | Si définie, DeepL remplace Google (clé gratuite `…:fx` acceptée) |

## Rien n'est stocké

- **MKV / WebM / TS** : la vidéo est envoyée en flux directement dans ffmpeg, elle n'est jamais écrite.
- **MP4 / MOV / AVI** : l'index peut être en fin de fichier, la vidéo passe donc par `/tmp` le temps de l'extraction, puis elle est supprimée. Dans le conteneur, `/tmp` est un tmpfs (en RAM).
- Les sous-titres extraits et les traductions restent **en mémoire uniquement**, et sont effacés au bout de 30 minutes ou quand on recommence.
- Le conteneur tourne en **lecture seule**, sans volume, sans log d'accès.

### Derrière un reverse proxy

Nginx garde par défaut les gros envois **sur disque**. Pour que la vidéo ne touche pas le serveur :

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    client_max_body_size 0;
    proxy_request_buffering off;
    proxy_read_timeout 600s;
}
```

Avec Traefik ou Caddy, les envois sont transmis en flux par défaut.

## Développement

```bash
pip install -r requirements.txt   # ffmpeg et ffprobe doivent être dans le PATH
uvicorn app.main:app --reload
```

```
app/
  main.py         API (envoi, jobs de traduction, téléchargement)
  subtitles.py    extraction ffmpeg, lecture des formats, nettoyage SDH
  translator.py   traduction par lots en parallèle (Google / DeepL)
  store.py        stockage en mémoire avec expiration
  static/         interface web (HTML/CSS/JS, sans build)
```
