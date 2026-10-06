# AGENTS.md

Guidance for AI agents working in this repository. The README documents *what*
the plugin does; this file records *how* to change it without breaking it.

## What this is

An Omarchy shell plugin (Quickshell/QML) that drives `rclone bisync` to keep
local folders and Google Drive folders in sync. Kinds `service` and
`bar-widget`, id `saigkill.google-drive`.

## The rules that matter most

**A sync must never delete files the user did not delete.** That is why:

- the first sync of a pair, and every recovery, is `--resync --resync-mode
  newer`, which only ever adds files;
- logout and remove-pair throw away the bisync listings. Listings from another
  account or an old pair would make bisync think every file was deleted on one
  side and delete it on the other;
- `--force` is only ever sent after the user confirmed it in the panel;
- bisync's own safety checks (max-delete, empty side) stay on.

`tests/test_helper.py` has end-to-end tests for each of these against a real
rclone. Do not weaken them to make a new design pass.

**The panel never sees a credential.** The token lives in rclone's config.
`gdrive_helper.py` swallows the stdout of `rclone config create` (it prints the
token) and never runs `config dump`/`config show`. A test plants a token in a
fake rclone's output and checks it does not come out.

**Exactly one sync engine.** The shell creates a bar widget per monitor.
Everything that does work (timer, watchers, queue, notifications) lives in
`Service.qml`; `BarWidget.qml` and `Panel.qml` only call into it.

## Before you change anything

```sh
node tests/test_model.js
python3 -m unittest discover -s tests
```

Both must be green before and after.

## Editing QML

Count the braces after every insertion (must print 0 per file):

```sh
for f in *.qml; do python3 - "$f" <<'EOF'
import re,sys
depth=0
for l in open(sys.argv[1],encoding='utf-8').read().split('\n'):
    s=re.sub(r'"(\\.|[^"\\])*"','""',l); s=re.sub(r'//.*','',s)
    for c in s: depth+=(c=='{')-(c=='}')
print(sys.argv[1],depth)
EOF
done
```

`qmllint` is not a substitute: the `qs.*` imports give false positives. Use the
shell log.

- Invisible items still take space in a `Column`: use `height: visible ?
  implicitHeight : 0`.
- Do not name a property `state` on an `Item`; it is Item's state machine. The
  service uses `syncState`.
- The watcher `Instantiator` is bound to `watchIds`, which only changes when the
  set of ids changes. Binding it to `status.pairs` restarts every inotifywait
  after every sync, because each result replaces the array.
- Commands are built in `Model.js` only. Every builder returns an array starting
  with the helper path, with no shell. When you add one, add it to `COMMANDS`
  in `tests/test_model.js`.

## Editing gdrive_helper.py

- A refused request exits non-zero with an **empty stdout** and the reason on
  stderr. `sync` exits 0 whenever bisync ran. While bisync runs it prints
  `{"progress": …}` lines (at most one per second); the outcome is always the
  **last** line, which is what `Model.lastJsonLine` and the tests read.
- Progress comes from bisync's own log (`ProgressTracker`): Path1 is local and
  Path2 is Drive, so only copies in the "to Path2" phase are uploads. Match the
  phase lines, not file names.
- Do not set a umask: rclone inherits it, and downloaded files would come out
  0600. State files get their mode from `write_private()`.
- Children are spawned with `PR_SET_PDEATHSIG`, so a shell reload that kills
  the helper also stops rclone and inotifywait. Keep using `spawn()`.
- bisync's messages are matched in `parse_bisync_log()`. Match on the specific
  phrase: "retryable without --resync" contains `--resync` but means the
  opposite of "Must run --resync".
- New local-path restrictions go in `normalize_local()`, with a test.

## Deploying to test

`~/.config/omarchy/plugins/saigkill.google-drive` is a symlink to this repo.
Writing there reloads the plugin; with many plugins installed a rescan keeps the
shell busy for a while, so IPC calls may time out in the meantime.

```sh
omarchy plugin validate .
omarchy-shell shell rescanPlugins
OMARCHY_SHELL_IPC_TIMEOUT=10s omarchy-shell saigkill.google-drive open
journalctl --user --since "-1m" | grep -i google-drive
```

Never edit anything under `~/.local/share/omarchy/` or `/usr/share/omarchy/`.

## Conventions

2-space indent, no semicolons in QML/JS, `var` in functions, `root.` prefix.
Theme values from `qs.Commons`, components from `qs.Ui`. Code, comments and docs
in English. Bump `version` in `manifest.json` for user-visible changes; a new
setting goes into `barWidget.defaults`, `barWidget.schema`, `Model.parseSettings`,
`BarWidget.pushSettings` and the README table.
