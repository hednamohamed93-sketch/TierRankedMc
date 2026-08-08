# FranceTier Queue Bot

Bot Discord de queue + tickets automatiques.

## Fonctionnement
- `/queue setup` : crée le panneau avec les boutons Rejoindre/Quitter.
- Les joueurs sont classés par ordre d'arrivée.
- Le panneau affiche le nom, la position et la date/heure d'arrivée.
- Quand quelqu'un devient #1, un ticket privé est créé dans la catégorie Discord `1524417786602848518`.
- Le bot ne crée jamais deux tickets pour la même personne.
- Quand le #1 quitte la queue ou que son ticket est fermé, le joueur suivant devient #1 et reçoit son ticket.
- Les administrateurs ou membres ayant `Gérer les salons` peuvent fermer un ticket.
- La queue est sauvegardée dans `queue.db`, donc un redémarrage ne supprime pas la queue.

## Railway
Start command:
`python bot.py`

Variable:
`DISCORD_BOT_TOKEN=TON_NOUVEAU_TOKEN`

Place `bot.py` et `requirements.txt` à la racine du dépôt.
