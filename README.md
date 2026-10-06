# Google Drive sync for Omarchy

Two-way Google Drive sync for the Omarchy shell. Pair any number of local
folders with folders on Drive; the plugin keeps them in sync on a timer and
shortly after you change something locally. Built on
[`rclone bisync`](https://rclone.org/bisync/).

- Drive icon in the bar whose color says how the sync is doing
- Panel to connect a Google account, add, pause and remove folder pairs, sync
  now, and see conflicts
- While a folder syncs: uploads as "x / y" and the approximate time left; after
  that the last five uploaded files
- Sync every N minutes, and on local changes (inotify, debounced)
- Conflicts never lose data: both versions are kept as `.conflict` files
- Safety stops when a sync would delete many files or mirror an empty folder
- One sync engine per session, however many monitors you have

## Requirements

```sh
sudo pacman -S rclone inotify-tools
```

`rclone` 1.66 or later (bisync with `--resilient`, `--recover` and
`--conflict-resolve`). `inotify-tools` is only needed for "Sync on local
changes".

## Installation

```sh
ln -s ~/Projects/repos/software/qt/omarchy-google-drive ~/.config/omarchy/plugins/saigkill.google-drive
omarchy-shell shell rescanPlugins
omarchy plugin enable saigkill.google-drive
```

## Usage

1. Click the Drive icon and press **Connect Google account**. rclone opens
   Google's consent page in your browser and waits for the answer on
   `127.0.0.1:53682`. The token is stored in rclone's own config
   (`~/.config/rclone/rclone.conf`), never by this plugin.
2. Press **+**, choose a local folder (or type a path) and the folder on Drive
   (empty = all of *My Drive*), then **Add and sync**.
3. The first sync merges both sides: nothing is deleted, and where a file exists
   on both sides with different content, the newer copy wins.

From then on every enabled folder is synced on the timer and after local
changes. **Middle-click** on the icon syncs everything right away, as does:

```sh
omarchy-shell saigkill.google-drive sync
```

Other IPC methods: `open`, `close`, `toggle`.

To stop syncing a folder, press the trash icon at the right of its row. That
only removes the pair; no file is deleted on either side.

### Progress

While a folder syncs, its row shows what is happening:

- **Checking for changes** while bisync lists and compares both sides. On a
  large folder this can take a long time, especially on the first sync and
  with rclone's shared client id (see
  [Your own Google client id](#your-own-google-client-id)).
- **Downloading** / **3 downloaded** once files actually come down from Drive.
- **Uploading 3 / 12 · about 2 min left** while files go up to Drive. The total
  is what bisync announced before copying; the time left is rclone's estimate
  for everything still to transfer in this sync, downloads included.
- The first sync of a folder (and any resync) does not announce its uploads in
  advance, so it shows **4 uploaded** without a total.

Below that, **Recently uploaded** lists the last five files that went up to
Drive, newest first. The list is kept between syncs.

### Bar colors

| Color | Meaning |
|---|---|
| Normal | Everything is up to date |
| Accent | A sync is running or queued |
| Yellow | Conflicts are waiting to be resolved |
| Red | A sync failed, rclone is missing, or no account is connected |
| Dim | No folders yet, or all folders paused |

### Conflicts

If a file was changed on both sides since the last sync, bisync keeps both and
renames them to `name.ext.conflict1` and `name.ext.conflict2`. The panel lists
them. Merge them by hand and delete the copy you do not want; the next sync
picks that up.

### Safety stops

- **"Stopped: too many files would be deleted"**: more than half of the files
  of a folder were deleted on one side. If that is what you meant, press
  **Sync anyway** in the panel.
- **"Stopped: one side is completely empty"**: bisync never mirrors an empty
  side, because that is what an unmounted disk or an unreachable Drive looks
  like. If you really emptied the folder, empty the other side by hand.
- **"bisync lost track of this folder"**: the bisync listings are gone or
  damaged. **Resync** merges both sides again; it never deletes anything.

### What is not synced

- **Google Docs, Sheets, Slides** and other native Google files. They have no
  file behind them. rclone could export them, but bisync would then upload the
  export as a new `.docx` next to the original (`--drive-skip-gdocs`).
- Temporary files: `*.partial`, LibreOffice lock files, Office `~$` files, Vim
  swap files, `.DS_Store`, `Thumbs.db`, `.Trash-*`. The list lives in
  `~/.local/state/omarchy-google-drive/filters.txt` (filter button in the
  panel header) and uses [rclone filter syntax](https://rclone.org/filtering/).
  After you edit it, every folder reports "lost track" once; press **Resync**.

### Folders that cannot be added

`/`, your whole home directory, anything outside your home directory,
`~/.config/omarchy`, rclone's config and cache, the plugin's own state, and
any folder that overlaps an existing pair, locally or on Drive.

## Configuration

In the widget settings (`~/.config/omarchy/shell.json`):

| Key | Default | Meaning |
|---|---|---|
| `remote` | `gdrive` | rclone remote to use. Connecting creates it; an existing Google Drive remote of that name is used as it is |
| `intervalMinutes` | `15` | Minutes between full syncs (this is what picks up changes made on Drive). `0` turns the timer off |
| `watchChanges` | `true` | Sync a folder shortly after something in it changed locally |
| `debounceSeconds` | `20` | Seconds of quiet after the last local change before syncing |
| `notify` | `true` | Notify on failures, new conflicts, and recovery |

The folder pairs themselves are managed in the panel and stored in
`~/.local/state/omarchy-google-drive/pairs.json`.

### Your own Google client id

rclone's built-in client id is shared by every rclone user. Google limits how
many requests per minute it may make, so on a large folder the listing that
starts every sync keeps hitting that limit and a sync can sit at **Checking for
changes** for hours. The shared id is also being retired during 2026. With
your own client id the quota is yours alone. It costs nothing.

**1. Create the client in the Google Cloud console**

1. Open <https://console.cloud.google.com/> with the Google account whose
   Drive you sync, and create a new project (any name, e.g. "rclone").
2. Under **APIs & Services → Library**, search for **Google Drive API** and
   press **Enable**.
3. Open **Google Auth Platform** (called **OAuth consent screen** in older
   versions of the console) and set it up:
   - **Branding**: an app name (e.g. "rclone") and your e-mail address.
   - **Audience**: user type **External**. Add your own Google account under
     **Test users**.
   - **Data Access**: add the scope `https://www.googleapis.com/auth/drive`.
     This step is optional; rclone asks for the scope when it connects.
4. Under **Clients**, press **Create client**, choose application type
   **Desktop app**, and create it. Copy the **client ID** and the **client
   secret**.
5. Back under **Audience**, press **Publish app**. While the app stays in
   *Testing*, Google expires its tokens after 7 days and the sync stops with
   an authentication error every week. A published app that is not verified
   is fine for your own use; the browser only shows a warning when you
   connect ("Google hasn't verified this app"), which you can click through
   via **Advanced**.

**2. Give the client to rclone**

If the remote does not exist yet, create it before connecting from the panel:

```sh
rclone config create gdrive drive scope=drive client_id=YOUR_ID client_secret=YOUR_SECRET
```

If you already connected with the shared id, switch the existing remote over
and log in again. The old token belongs to the old client and does not work
with the new one:

```sh
rclone config update gdrive client_id=YOUR_ID client_secret=YOUR_SECRET
rclone config reconnect gdrive:
```

The plugin finds the existing remote and uses it. Use your remote name instead
of `gdrive` if you changed the **rclone remote** setting.

**3. Check it**

```sh
rclone lsf gdrive: --max-depth 1 -v
```

lists the top of your Drive. If rclone still prints *"This remote uses
rclone's shared Google Drive client_id"*, the remote has no client id of its
own yet.

rclone reads its config only when it starts. A sync that was already running
keeps using the old client until it ends; the next sync uses the new one.
Press the sync button (tooltip **Sync now**) in the panel to start it right away.

## How it works

| File | Role |
|---|---|
| `Service.qml` | Singleton: sync queue, interval timer, inotify watchers, notifications |
| `BarWidget.qml` | Bar icon, one per monitor, a view over the service |
| `Panel.qml` | Account, folder pairs, conflicts |
| `Model.js` | Pure logic: command builders, decoding, state, formatting |
| `gdrive_helper.py` | Runs rclone and inotifywait, owns the state directory |

Syncs run one at a time. Each pair has its own bisync working directory under
`~/.local/state/omarchy-google-drive/bisync/<id>` and a lock file, so two
syncs of the same folder can never overlap, even across shell restarts.

## Uninstalling

```sh
omarchy plugin disable saigkill.google-drive
rm ~/.config/omarchy/plugins/saigkill.google-drive
rm -rf ~/.local/state/omarchy-google-drive   # pairs and bisync listings
rclone config delete gdrive                  # the Google token
```

None of this touches a synced file. To revoke the access on Google's side too,
remove *rclone* under <https://myaccount.google.com/permissions>.

## Troubleshooting

**Every sync fails while offline.** That is expected; you get one notification,
and one more when it works again.

**Changes are only picked up on the timer.** Check that `inotifywait` is
installed. For very large folders the inotify watch limit may be too low:

```sh
cat /proc/sys/fs/inotify/max_user_watches
echo fs.inotify.max_user_watches=524288 | sudo tee /etc/sysctl.d/40-inotify.conf
sudo sysctl --system
```

**A sync stays at "Checking for changes" for hours.** Google is throttling
the listing. Run `rclone lsf gdrive: -R --max-depth 2 -vv 2>&1 | grep
rateLimitExceeded`; any output means the remote hits its request quota. Set up
[your own Google client id](#your-own-google-client-id), and keep build output
and caches out of the sync with the filter file. Do not restart the shell
during a long first sync: that stops it, and it starts over from the
beginning.

**Shell log:**

```sh
journalctl --user --since "-5min" | grep -i google-drive
```

**Running a sync by hand** shows exactly what bisync does:

```sh
~/.config/omarchy/plugins/saigkill.google-drive/gdrive_helper.py status gdrive
~/.config/omarchy/plugins/saigkill.google-drive/gdrive_helper.py sync gdrive <pair-id>
```

## Testing

```sh
node tests/test_model.js                       # command builders, decoding, state
python3 -m unittest discover -s tests -v       # validation, state files, token handling, real bisync
```

The end-to-end tests run the real `rclone bisync` against a remote of type
`local`, so no Google account is needed.

## License

[MIT](LICENSE) — Copyright (c) 2026 Sascha Manns.
