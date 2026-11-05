"""iOS entry point for Aurum. The original trading analyzer is imported unchanged."""
import os
import threading
from http.server import ThreadingHTTPServer

import toga

from . import aurum_bot


class AurumApp(toga.App):
    def startup(self):
        # Keep persistent FVG state in the app's writable iOS data directory.
        data_dir = str(self.paths.data)
        os.makedirs(data_dir, exist_ok=True)
        aurum_bot.STATE_FILE = os.path.join(data_dir, "fvgs_state.json")
        aurum_bot.load_state()

        # Serve the existing HTML interface locally, inside the native WebView.
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), aurum_bot.Handler)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            name="AurumLocalServer",
            daemon=True,
        )
        self.server_thread.start()

        local_url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        self.main_window = toga.MainWindow(title="Aurum")
        self.main_window.content = toga.WebView(url=local_url)
        self.main_window.show()

    def on_exit(self):
        try:
            aurum_bot.persist_state(force=True)
        finally:
            if getattr(self, "server", None):
                self.server.shutdown()
                self.server.server_close()
        return True


def main():
    return AurumApp("Aurum", "com.example.aurum")
