import sqlite3
import json
from config.settings import DB_PATH

class DatabaseManager:
    """
    持久化数据管理模块
    负责与本地 SQLite 数据库的交互，处理调度点及道路异常状态的存取。
    """
    def __init__(self, db_path=DB_PATH):
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self.create_tables()

    def create_tables(self):
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lat REAL,
                lng REAL,
                demand REAL,
                type INTEGER,
                is_active INTEGER DEFAULT 1,
                name TEXT
            )
        ''')
        try:
            self.cursor.execute("ALTER TABLE locations ADD COLUMN population REAL DEFAULT 0")
        except:
            pass
        try:
            self.cursor.execute("ALTER TABLE locations ADD COLUMN area REAL DEFAULT 0")
        except:
            pass
        try:
            self.cursor.execute("ALTER TABLE locations ADD COLUMN importance INTEGER DEFAULT 2")
        except:
            pass

        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS road_conditions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT,
                lat REAL,
                lng REAL,
                desc TEXT
            )
        ''')

        # 新增：算法参数配置表
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS algorithm_params (
                key TEXT PRIMARY KEY,
                value REAL
            )
        ''')
        self.conn.commit()

    def save_data(self, depots_data, customers_data, road_conditions, algo_params):
        self.cursor.execute("DELETE FROM locations")
        for i, d in enumerate(depots_data):
            self.cursor.execute(
                "INSERT INTO locations (lat, lng, demand, type, is_active, name, population, area, importance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (d['pos'][0], d['pos'][1], 0.0, 0, d['active'], d.get('name', '未命名'), 0, 0, 0))
        for i, c in enumerate(customers_data):
            self.cursor.execute(
                "INSERT INTO locations (lat, lng, demand, type, is_active, name, population, area, importance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (c['pos'][0], c['pos'][1], c['demand'], 1, c['active'], c.get('name', '未命名'), c.get('pop', 0),
                 c.get('area', 0), c.get('imp', 2)))

        self.cursor.execute("DELETE FROM road_conditions")
        import json
        for r in road_conditions:
            desc_val = json.dumps({
                'name': r.get('name', '未知路段'),
                'lat2': r.get('lat2', r['lat']),
                'lng2': r.get('lng2', r['lng']),
                'geom': r.get('geom', [])
            })
            self.cursor.execute("INSERT INTO road_conditions (type, lat, lng, desc) VALUES (?, ?, ?, ?)",
                                (r['type'], r['lat'], r['lng'], desc_val))

        self.cursor.execute("DELETE FROM algorithm_params")
        for k, v in algo_params.items():
            self.cursor.execute("INSERT INTO algorithm_params (key, value) VALUES (?, ?)", (k, v))

        self.conn.commit()

    def load_data(self):
        try:
            self.cursor.execute(
                "SELECT lat, lng, demand, type, is_active, population, area, importance, name FROM locations ORDER BY type, id")
            rows = self.cursor.fetchall()
            depots = [{'pos': (r[0], r[1]), 'active': r[4], 'name': r[8]} for r in rows if r[3] == 0]
            customers = [{'pos': (r[0], r[1]), 'demand': r[2], 'active': r[4], 'pop': r[5], 'area': r[6], 'imp': r[7],
                          'name': r[8]} for r in rows if r[3] == 1]

            self.cursor.execute("SELECT type, lat, lng, desc FROM road_conditions")
            road_rows = self.cursor.fetchall()
            import json
            roads = []
            for r in road_rows:
                try:
                    data = json.loads(r[3])
                    roads.append({
                        'type': r[0], 'lat': r[1], 'lng': r[2],
                        'name': data.get('name', '未知路段'),
                        'lat2': data.get('lat2', r[1]),
                        'lng2': data.get('lng2', r[2]),
                        'geom': data.get('geom', [])
                    })
                except:
                    roads.append(
                        {'type': r[0], 'lat': r[1], 'lng': r[2], 'name': r[3], 'lat2': r[1], 'lng2': r[2], 'geom': []})

            self.cursor.execute("SELECT key, value FROM algorithm_params")
            params = {row[0]: row[1] for row in self.cursor.fetchall()}

            return depots, customers, roads, params
        except sqlite3.OperationalError:
            return [], [], [], {}

