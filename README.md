# Plexo Multi-Network for FDM

An add-on for [Free Download Manager](https://www.freedownloadmanager.org/) (FDM) 6 that downloads one file over **all of your internet connections at the same time**, such as Wi-Fi plus a phone tethered over USB, or two Ethernet lines from different providers. It uses the technique from [Plexo](https://github.com/anmolkapil/plexo).

You keep using FDM as usual. For large files, the add-on routes the download through a small helper on your computer (the **bridge**), which fetches parts of the file over every network at once and passes them to FDM in order.

## How it works

FDM add-ons can't change how FDM opens connections. They can look at a link, run a bundled Python script, and give FDM a different link to download. This add-on uses that:

```text
                                      ┌── Wi-Fi ───────────┐
FDM ──→ http://127.0.0.1:47913/d/… ──→│                    │──→ example.com/big.iso
        (the bridge, on your PC)      └── iPhone USB ──────┘
```

1. You add a link to FDM. If the file is large (16 MB or more by default), FDM asks the add-on about it.
2. The add-on starts the bridge if it isn't running, and the bridge asks the server for the file's first byte. A `206 Partial Content` answer means the server can send any part of the file. It also gives the file's size, name and version.
3. If splitting will help (at least two networks are online, the server supports ranges, and the file is big enough), the bridge gives FDM a local link. Otherwise the add-on declines and FDM downloads the link the normal way.
4. While FDM downloads from the local link, the bridge does what Plexo does:
   - It cuts the file into blocks of 0.5–4 MB, and each network runs 8 streams.
   - Each stream is pinned to one network: it is bound to that network's IP address, and on Linux also to the device itself (`SO_BINDTODEVICE`).
   - A stream takes the next free block, fetches it with an HTTP range request, then takes another. So **faster networks fetch more of the file**, and if one network drops, the others carry on.
   - Near the end, an idle stream **races** a block that another stream is fetching too slowly, starting from where that stream got to. Whichever finishes first wins, so there's no slow finish.
   - If a server refuses some streams (429/503), that network drops to the streams the server accepted, then gets one more back each minute.
   - If the file changes on the server partway through (a new ETag or size), the bridge refuses to continue rather than mix two versions.
5. Bytes reach FDM in order as soon as they arrive. FDM's own connections, pause/resume, and the download list all work as usual.

## Requirements

- Free Download Manager 6 with add-on support (6.32 or later recommended)
- Python 3.10 or later. FDM offers to install it when you install the add-on, as it does for other add-ons. The bridge uses only Python's standard library.
- At least **two networks that each reach the internet through their own connection**. Home Wi-Fi plus a USB-tethered phone works. Wi-Fi and Ethernet plugged into the *same* router share one connection, so they won't be faster together.

Windows only: Windows turns Wi-Fi off when Ethernet is plugged in. To keep both on, open `gpedit.msc`, go to *Computer Configuration › Administrative Templates › Network › Windows Connection Manager*, and set **Minimize the number of simultaneous connections to the Internet or a Windows domain** to **Disabled**. Plexo's README covers more setups, including [an Android phone over USB on macOS](https://github.com/anmolkapil/plexo#use-an-android-phone-over-usb-on-macos).

## Install

1. Build `plexo-multi-network.fda` (see below), or download it from this repository's Releases or CI artifacts.
2. In FDM, open the main menu › **Add-ons**, choose to install an add-on from a file, and pick `plexo-multi-network.fda`.
3. Allow the **launchPython** permission when FDM asks, and let FDM install Python if it offers.

## Use

- **Automatically:** add any large download to FDM as usual. FDM offers it as a download with one format, labelled something like "Plexo: Wi-Fi + iPhone USB". Download it, and the bridge splits it across your networks.
- **Force it for one link:** add `#plexo` to the end of the link, for example `https://example.com/file.zip#plexo`. That link goes through the bridge even if it's small, if automatic mode is off, or if only one network is online.
- **Status page:** open <http://127.0.0.1:47913/> while the bridge is running. It shows each network's speed and how much of each download it fetched. There you can:
  - switch individual networks on or off
  - change the minimum file size, the number of networks required, and the streams per network
  - turn automatic mode off
  - turn on **Start the bridge when I log in**

### Resuming after a restart

FDM only runs the add-on when a link is added. If you pause a download and later restart the computer, the bridge has to be running again before FDM can resume. Either turn on **Start the bridge when I log in** on the status page, or add any link to FDM, which starts the bridge. Downloads in progress are remembered for 60 days.

## Command line

The bridge is a normal Python program, which helps with troubleshooting. From the folder FDM installed the add-on into, or from `addon/` in this repository:

```bash
python bridge/plexo_fdm.py networks   # each network found, and whether it reaches the internet
python bridge/plexo_fdm.py start      # start the bridge in the background
python bridge/plexo_fdm.py status     # what it's doing, as JSON
python bridge/plexo_fdm.py ui         # open the status page
python bridge/plexo_fdm.py stop
python bridge/plexo_fdm.py serve --verbose   # run in the foreground, logging to the terminal
```

Settings, the log (`bridge.log`) and the download records are kept in:
- Windows: `%LOCALAPPDATA%\PlexoFDM`
- macOS: `~/Library/Application Support/PlexoFDM`
- Linux: `~/.local/share/plexo-fdm`

## Limitations

- **Direct HTTP/HTTPS downloads only.** Torrents, and video sites handled by other add-ons, still use FDM's own engine. The add-on never takes web pages, so a video add-on still gets them.
- **The server has to support byte ranges.** Most file hosts and CDNs do. When a server doesn't, FDM downloads the file normally.
- **FDM proxy settings:** if FDM is set to use a proxy for a link, the add-on leaves that link to FDM, because the bridge connects directly through each network.
- **Linux:** each stream is pinned to its network with `SO_BINDTODEVICE`, which works without root on Linux 5.7 and later. On older kernels, the bridge only binds source addresses, which needs one route per network.
- **Memory:** blocks wait in memory until FDM reads them, up to 256 MB per download (`memory_limit_mb` in `config.json`).
- **Not yet tested inside FDM itself.** FDM has no public add-on documentation. The add-on uses the same API as FDM's published add-ons ([Elephant](https://github.com/meowcateatrat/elephant) and others): a media parser that runs a bundled Python script with `launchPythonScript` and returns the link to download. The bridge has been tested against simulated networks and against real downloads. Please report how it behaves in your FDM version.

## Develop

```bash
python -m unittest tests.test_bridge -v   # the bridge, against a local test server (Linux)
node --test tests/addon.test.mjs          # the add-on's JavaScript, in a stand-in for FDM
./build/build.sh                          # dist/plexo-multi-network.fda (Windows: .\build\build.ps1)
```

The bridge tests use loopback addresses as stand-in networks. A test server paces each source address as its own link, for example a 2 MB/s network and a 6 MB/s one. The tests check that:

- the combined speed beats the faster network alone
- the fast network fetches more of the file
- every byte matches
- FDM-style split connections waste little
- stalled networks get raced
- refused streams recover
- a changed file is refused

Layout:

```text
addon/                      what goes in the .fda
  manifest.json             the add-on's manifest (media parser, launchPython permission)
  plexo.js, msparser.js     the parser FDM calls: decides which links to offer the bridge
  bridge/plexo_fdm.py       the command the add-on runs (register, serve, status, …)
  bridge/plexofdm/
    engine.py               blocks, streams, racing: Plexo's technique
    routing.py              connections pinned to one network
    netif.py                finding networks and checking they reach the internet
    probe.py                the first-byte probe, cookies, file names
    server.py               the local server FDM downloads from, its API and status page
    ui.html                 the status page
    autostart.py            starting the bridge at login
tests/                      the tests above
build/                      packaging scripts
```

## Credits

The download technique comes from [Plexo](https://github.com/anmolkapil/plexo) by [Anmol Kapil](https://github.com/anmolkapil), MIT License. The add-on format follows FDM's published add-ons, such as [Elephant](https://github.com/meowcateatrat/elephant).

## License

MIT. See [LICENSE](LICENSE).
