# MapStash

**A free collection downloader for osu!** Paste an [osu!Collector](https://osucollector.com) link and MapStash downloads every map in the collection from free beatmap mirrors, then sets the collection up for osu!lazer or osu!stable.

> **Unofficial.** MapStash is not affiliated with osu!Collector or ppy. If you enjoy osu!Collector, please consider supporting its creator. They run the site that makes sharing collections possible.

## Features

- Works with **osu!lazer** and **osu!stable**
- Shows **download size and free disk space** before you start, and warns if a collection won't fit
- Live progress: maps done, GB downloaded, speed and time left
- **Pause and resume**: maps you already have are skipped
- Tries several free mirrors per map (catboy.best, nerinyan, osu.direct, sayobot)
- Option to skip videos for much smaller downloads
- Builds the in-game collection for you (backs up your existing `collection.db` first)
- No account or API key needed, and no dependencies beyond Python's standard library

## Download

Grab **MapStash.exe** from the [Releases](../../releases) page and run it.

Windows SmartScreen may warn about an unknown app the first time, because the exe isn't code-signed. Click **More info → Run anyway**.

Or run it from source with Python 3.8+:

```
python mapstash.pyw
```

## How to use

1. Copy a collection link from osucollector.com, for example `https://osucollector.com/collections/24173/...`
2. Click **Paste**. MapStash checks the collection and shows the size and your free space.
3. Pick **osu!lazer** or **osu!stable**, then click **Download**.

### osu!lazer

lazer has no Songs folder, so there's one extra step:

1. Click **Open folder**, select all the `.osz` files (Ctrl+A) and drag them onto the lazer window.
2. After the import finishes, go to **Settings → Maintenance → import from stable**, choose the `lazer-collection` folder MapStash made, and tick only **Collections**.

### osu!stable

Close osu! before downloading so the collection can be saved. When you open osu! again, it imports the new maps automatically.

## Building the exe yourself

Double-click `build_exe.bat`, or run:

```
pip install pyinstaller
pyinstaller --onefile --windowed --name MapStash mapstash.pyw
```

The exe ends up in `dist/`.

## Notes

- Large collections can be tens of GB. Use **Skip videos** to save space.
- Mirrors are run by third parties and can be slow, rate-limited or down. Failed maps are listed in `failed_<id>.txt` and retried the next time you press Download.
- Please don't set parallel downloads too high. Be nice to the free mirrors.

## License

[MIT](LICENSE)
