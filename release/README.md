# Builds for testing

None of these have been tested inside Free Download Manager yet.

| File | Version | What's in it |
|---|---|---|
| `plexo-multi-network-1.1.0.fda` | 1.1.0 | **Latest.** The add-on, plus the bridge can be used as FDM's proxy (HTTP, HTTPS, SOCKS5) for every download, torrents included. |
| `plexo-bridge-standalone-1.1.0.zip` | 1.1.0 | The bridge without the add-on, with `Start Plexo Bridge.cmd`. For proxy mode and torrents. Needs Python 3.10+. |
| `plexo-multi-network-1.0.1.fda` | 1.0.1 | The add-on, which logs each run to `addon.log` for troubleshooting. |
| `plexo-multi-network-1.0.0.fda` | 1.0.0 | First build: the add-on for direct links only. |

Install a `.fda` from FDM's **Add-ons** menu. Magnet links and `.torrent` files never reach add-ons; use the standalone bridge in proxy mode for those (see the main README).
