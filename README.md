Triple J House Party RSS Feed
Automatisch gegenereerde RSS-feed voor de Triple J House Party show.
Hoe het werkt
GitHub Actions draait elke vrijdag- en zaterdagnacht automatisch een Python script dat:
De Triple J House Party-pagina scrapet op nieuwe afleveringen
De directe audio-URL per aflevering ophaalt via `yt-dlp`
Een RSS-feed (`docs/feed.xml`) genereert die door Lyrion (of elke podcast-app) gelezen kan worden
Setup
Stap 1: Fork of clone deze repo naar jouw GitHub-account
Stap 2: Zet GitHub Pages aan
Ga naar Settings → Pages
Stel de source in op: Branch: main / folder: /docs
Sla op
Stap 3: Jouw feed-URL
Na het instellen is jouw feed beschikbaar op:
```
https://JOUW_GEBRUIKERSNAAM.github.io/houseparty-feed/feed.xml
```
Stap 4: Toevoegen aan Lyrion
Ga in Lyrion naar My Music → Podcasts → Add podcast
Plak de bovenstaande URL
Handmatig de feed vernieuwen
Ga in GitHub naar Actions → Generate Triple J House Party RSS Feed → Run workflow
Licentie
Puur voor persoonlijk gebruik. Audio-content is eigendom van ABC/triple j.
