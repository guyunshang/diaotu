import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from config.settings import MBTILES_PATH

class MBTilesHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/tiles/'):
            try:
                parts = self.path.split('/')
                z = int(parts[2])
                x = int(parts[3])
                y = int(parts[4].split('.')[0])
                tms_y = (1 << z) - 1 - y

                if not os.path.exists(MBTILES_PATH):
                    self.send_response(404)
                    self.end_headers()
                    return

                conn = sqlite3.connect(MBTILES_PATH)
                cursor = conn.cursor()
                cursor.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                               (z, x, tms_y))
                row = cursor.fetchone()
                conn.close()

                if row:
                    self.send_response(200)
                    self.send_header('Content-type', 'image/png')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(row[0])
                else:
                    self.send_response(404)
                    self.end_headers()
            except ConnectionAbortedError:
                pass
            except Exception:
                try:
                    self.send_response(500)
                    self.end_headers()
                except:
                    pass
        else:
            try:
                self.send_response(404)
                self.end_headers()
            except:
                pass

    def log_message(self, format, *args):
        # 覆写日志记录方法以静默服务器输出
        pass

class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True

def start_mbtiles_server():
    server = ReusableHTTPServer(('127.0.0.1', 9999), MBTilesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server