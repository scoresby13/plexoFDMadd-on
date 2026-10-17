Plexo bridge, standalone

Lets Free Download Manager use all your internet connections at once, for every download,
torrents included, through FDM's proxy setting. Works with or without the FDM add-on.

1. Install Python 3.10 or later from https://www.python.org/downloads/ if you don't have it
   (on Windows, tick "Add python.exe to PATH").
2. Windows: double-click "Start Plexo Bridge.cmd". macOS/Linux: run start-plexo-bridge.sh.
   The status page opens at http://127.0.0.1:47913/
3. In FDM, open Settings > Network, choose a manual proxy, and enter
     HTTP and HTTPS proxy: 127.0.0.1, port 47913
     SOCKS5 proxy (used for torrents): 127.0.0.1, port 47913
4. On the status page, turn on "Start the bridge when I log in", so FDM can always connect.

To stop using it, set FDM's proxy back to "No proxy".
