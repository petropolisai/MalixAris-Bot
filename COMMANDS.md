# MalixAris Bot slash-command catalog (78 commands)

## Staff applications (3)

The application panel now offers **Staff** and **Dropper** applications. The existing interview/review workflow is retained.

```text
/apply panel
/apply closeticket
/apply diagnose
```

## Guess-number and race events (8)

```text
/gtn number start
/gtn number status
/gtn number cancel
/gtn number reveal
/gtn race start
/gtn race status
/gtn race cancel
/gtn help
```

## Vouch and reputation (12)

```text
/vouch add
/vouch view
/vouch edit
/vouch remove
/vouch import
/vouch blacklist add
/vouch blacklist remove
/vouch profile
/vouch leaderboard
/vouch stats
/vouch setup
/vouch help
```

The existing text shortcuts are also retained:

```text
+vouch @user [reason]
-vouch @user [reason]
```

## Support tickets (1)

`/ticket panel` posts a separate ticket dropdown. It offers **General, Claim, Reward Claim, Support, Partnership, and Other**. Tickets are private channels in the existing configured ticket category and use the existing staff roles and transcript log.

```text
/ticket panel
```

## Render state backups (3)

Administrators can inspect status, create a snapshot immediately, or choose an older ZIP from the configured mod channel. Backups are triggered by successful state writes and coalesced for eight seconds; there is no periodic polling and no automatic restore. `/backup restore` manually imports the selected snapshot; the newest 25 files are retained.

```text
/backup status
/backup now
/backup restore
```

Archives contain runtime data and allow-listed non-secret settings only. `.env`, bot/API credentials, and source code are never uploaded.

## MalixAris Bot AI (51)

### Direct AI commands (23)

```text
/ai chat
/ai ask
/ai reset
/ai roast
/ai vibe
/ai mode
/ai whoami
/ai about
/ai describe
/ai testvision
/ai serverinfo
/ai summarize
/ai rules
/ai askrules
/ai notify
/ai pingsettings
/ai ping
/ai help
/ai stats
/ai persona
/ai resetpersona
/ai models
/ai dashboard
```

### AI server configuration (13)

```text
/ai server setrules
/ai server addrule
/ai server removerule
/ai server clearrules
/ai server setaichannel
/ai server removeaichannel
/ai server listaichannels
/ai server pingpolicy
/ai server pingprotect
/ai server pingunprotect
/ai server setdailylimit
/ai server setlogchannel
/ai server removelogchannel
```

### AI administration (15)

```text
/ai admin addowner
/ai admin removeowner
/ai admin owners
/ai admin clear
/ai admin addpremium
/ai admin removepremium
/ai admin blacklist
/ai admin unblacklist
/ai admin userinfo
/ai admin setmodel
/ai admin resetmodel
/ai admin diagnose
/ai admin apistatus
/ai admin maintenance
/ai admin broadcast
```

**Total: 3 + 8 + 12 + 1 + 3 + 51 = 78 slash-command handlers.** Group headings such as `/gtn number` are not counted as handlers.
