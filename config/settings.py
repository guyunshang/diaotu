import os
import sys
import warnings

# 设置多线程及数学库的底层环境变量，避免冲突
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
os.environ["OMP_NUM_THREADS"] = "1"

# 静默处理 pyproj 在 Anaconda 环境下常见的内部数据库路径警告，避免污染控制台日志
warnings.filterwarnings("ignore", message="pyproj unable to set database path")

# 配置地理空间投影库（PROJ）的环境变量
conda_env_path = sys.prefix
proj_lib_path = os.path.join(conda_env_path, 'Library', 'share', 'proj')
if os.path.exists(proj_lib_path):
    os.environ['PROJ_DATA'] = proj_lib_path
    os.environ['PROJ_LIB'] = proj_lib_path
    try:
        import pyproj
        pyproj.datadir.set_data_dir(proj_lib_path)
    except Exception:
        pass

# 项目根目录获取（基于当前 config 文件夹向上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 定义统一的数据存储目录
DATA_DIR = os.path.join(PROJECT_ROOT, 'data')

# 静态资源与数据文件路径
MBTILES_PATH = os.path.join(DATA_DIR, 'zhuhai_custom_area.mbtiles')
DB_PATH = os.path.join(DATA_DIR, 'logistics_system.db')

# 业务常量定义
URBAN_COORDS = [
    (22.2510, 113.5674), (22.2304, 113.5391), (22.2530, 113.5210),
    (22.2480, 113.5185), (22.2555, 113.5757), (22.2455, 113.5601), (22.2386, 113.5422),
    (22.2217, 113.5353), (22.2431, 113.5492), (22.2589, 113.5455), (22.2620, 113.5256),
    (22.2301, 113.5157), (22.2403, 113.5528), (22.2403, 113.5309), (22.2684, 113.5550),
    (22.2312, 113.5457), (22.2506, 113.5752), (22.2298, 113.5301), (22.2260, 113.5369),
    (22.2577, 113.5685), (22.2550, 113.5106), (22.2421, 113.5722), (22.2697, 113.5348)
]

DEMAND = [0, 0, 0] + [0.1, 0.4, 1.2, 1.5, 0.8, 1.3, 1.7, 0.6, 1.2, 0.4,
                      0.9, 1.3, 1.3, 1.9, 1.7, 1.1, 1.5, 1.6, 1.7, 1.5]


# --- 高德地图 API 配置 ---
AMAP_WEB_KEY = "dc64d01a39a64209713939de099b534d"  # Web 服务 API Key