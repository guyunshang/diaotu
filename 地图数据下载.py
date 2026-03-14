import sqlite3
import requests
import math
import time
import os
import sys
import threading
import random  # 新增：用于随机延时
from concurrent.futures import ThreadPoolExecutor, as_completed

# === 1. 你指定的绝对经纬度范围 ===
LAT_MAX, LAT_MIN = 22.65, 21.6
LON_MAX, LON_MIN = 114.52, 112.85

# === 2. 配置参数 ===
ZOOM_LEVELS = list(range(1, 19))  # 下载 1 到 18 级
DB_NAME = "zhuhai_custom_area.mbtiles"

# --- 核心提速参数 ---
MAX_WORKERS = 10      # 并发线程数（适当提高，但仍需遵守 OSM 使用政策）
MAX_RETRIES = 3       # 失败重试次数
RETRY_DELAY = 1.0     # 重试等待时间
# --------------------

# 线程安全的数据库写锁
db_lock = threading.Lock()


def deg2num(lat_deg, lon_deg, zoom):
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return (xtile, ytile)


def print_progress_bar(iteration, total, start_time, prefix='极速下载中', length=40, fill='█'):
    """控制台动态进度条"""
    if total == 0: return
    percent = f"{100 * (iteration / float(total)):.1f}"
    filled_length = int(length * iteration // total)
    bar = fill * filled_length + '-' * (length - filled_length)

    elapsed_time = time.time() - start_time
    if iteration > 0:
        avg_time = elapsed_time / iteration
        rem_time = avg_time * (total - iteration)
        m, s = divmod(rem_time, 60)
        h, m = divmod(m, 60)
        time_str = f"剩余: {int(h):02d}h{int(m):02d}m{int(s):02d}s"
    else:
        time_str = "计算中..."

    sys.stdout.write(f'\r{prefix} |{bar}| {percent}% ({iteration}/{total}) {time_str}  ')
    sys.stdout.flush()
    if iteration == total:
        print()


def init_mbtiles_db():
    is_new = not os.path.exists(DB_NAME)
    # check_same_thread=False 允许在多线程中共享连接
    conn = sqlite3.connect(DB_NAME, check_same_thread=False, timeout=15.0)
    cursor = conn.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS metadata (name text, value text)''')
    cursor.execute(
        '''CREATE TABLE IF NOT EXISTS tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob)''')
    cursor.execute('''CREATE UNIQUE INDEX IF NOT EXISTS tile_index on tiles (zoom_level, tile_column, tile_row)''')

    if is_new:
        metadata = [
            ('name', 'Zhuhai Custom Area 112.7-114.5'),
            ('type', 'baselayer'),
            ('version', '1.0'),
            ('description', 'Custom bounding box map'),
            ('format', 'png'),
            ('bounds', f"{LON_MIN},{LAT_MIN},{LON_MAX},{LAT_MAX}")
        ]
        cursor.executemany('INSERT OR IGNORE INTO metadata VALUES (?, ?)', metadata)
    else:
        print(f"🟢 检测到已有数据库 {DB_NAME}，准备合并数据...")

    conn.commit()
    return conn


def get_missing_tasks(conn):
    """【秒级预检】计算所有任务，并与数据库比对，直接返回缺失的任务列表"""
    cursor = conn.cursor()
    print("⏳ 正在读取本地数据库已存瓦片 (这可能需要几秒钟)...")
    cursor.execute("SELECT zoom_level, tile_column, tile_row FROM tiles")
    existing_records = cursor.fetchall()
    # 转换为 Set，查询时间复杂度降为 O(1)
    existing_set = set(existing_records)
    print(f"   => 数据库中已有 {len(existing_set):,} 张有效图片。")

    print("⏳ 正在生成全局坐标矩阵矩阵...")
    all_tasks = []
    total_theoretical = 0
    for z in ZOOM_LEVELS:
        x_min, y_max = deg2num(LAT_MIN, LON_MIN, z)
        x_max, y_min = deg2num(LAT_MAX, LON_MAX, z)
        if x_min > x_max: x_min, x_max = x_max, x_min
        if y_min > y_max: y_min, y_max = y_max, y_min

        for x in range(x_min, x_max + 1):
            for y in range(y_min, y_max + 1):
                tms_y = (1 << z) - 1 - y
                total_theoretical += 1
                # 核心过滤：只有数据库里没有的，才加入下载队列
                if (z, x, tms_y) not in existing_set:
                    all_tasks.append((z, x, y, tms_y))

    return total_theoretical, all_tasks


def download_worker(task, conn):
    """单个线程的工作函数（优化版：使用 Session + 随机延时）"""
    z, x, y, tms_y = task
    url = f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"

    # 随机延时 0.1~0.3 秒，平滑请求
    time.sleep(random.uniform(0.1, 0.3))

    # 使用 Session 复用 TCP 连接
    with requests.Session() as session:
        session.headers.update({"User-Agent": f"MBTiles_FastWorker_Thread_{threading.get_ident()}"})

        for attempt in range(MAX_RETRIES):
            try:
                res = session.get(url, timeout=8)
                if res.status_code == 200:
                    # 获取到图片后，加锁写入 SQLite
                    with db_lock:
                        cursor = conn.cursor()
                        cursor.execute('INSERT OR IGNORE INTO tiles VALUES (?, ?, ?, ?)',
                                       (z, x, tms_y, sqlite3.Binary(res.content)))
                    return True
                elif res.status_code in [403, 404]:
                    return False  # 明确无图，直接放弃
            except requests.exceptions.RequestException:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
        return False


def run_fast_downloader():
    conn = init_mbtiles_db()

    total_theoretical, tasks_to_download = get_missing_tasks(conn)
    num_tasks = len(tasks_to_download)

    print("=" * 50)
    print("🚀 极速多线程下载引擎已就绪")
    print("=" * 50)
    print(f"📍 理论总计: {total_theoretical:,} 张")
    print(f"⏭️ 瞬间跳过: {total_theoretical - num_tasks:,} 张 (已在库中)")
    print(f"🎯 本次需下载: {num_tasks:,} 张")
    print("=" * 50)

    if num_tasks == 0:
        print("\n🎉 你的地图已经是完美状态，没有需要补充的瓦片！")
        conn.close()
        return

    choice = input("\n⚠️ 确认启动并发下载吗？(y/n): ").strip().lower()
    if choice not in ['y', 'yes']:
        sys.exit(0)

    start_time = time.time()
    success_count = 0
    completed_tasks = 0

    # 启动线程池
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 将任务分发给工人
        future_to_task = {executor.submit(download_worker, task, conn): task for task in tasks_to_download}

        # 只要有一个工人完成，就更新一次进度条
        for future in as_completed(future_to_task):
            completed_tasks += 1
            if future.result():
                success_count += 1

            # 每 50 个任务批量提交一次事务（保持原逻辑）
            if completed_tasks % 50 == 0:
                with db_lock:
                    conn.commit()

            # 每 10 个任务刷新一次进度条（保持原逻辑）
            if completed_tasks % 10 == 0 or completed_tasks == num_tasks:
                print_progress_bar(completed_tasks, num_tasks, start_time)

    # 最终提交
    conn.commit()
    conn.close()

    print(f"\n\n🎉 极速任务完成！耗时: {time.time() - start_time:.1f} 秒")
    print(f"✅ 本次成功抓取: {success_count:,} 张，无效/空图: {num_tasks - success_count:,} 张。")


if __name__ == "__main__":
    run_fast_downloader()