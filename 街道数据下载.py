# 街道数据下载.py
import os
import sys
import shutil
import time

# =======================================================
# 🌟 核心环境自修复补丁 (必须在 import osmnx 之前执行！)
# 强行注入 proj.db 的路径，解决 epsg:4326 报错
# =======================================================
conda_env_path = sys.prefix
proj_lib_path = os.path.join(conda_env_path, 'Library', 'share', 'proj')

if os.path.exists(proj_lib_path):
    os.environ['PROJ_DATA'] = proj_lib_path
    os.environ['PROJ_LIB'] = proj_lib_path
    try:
        # 双重保险：直接调用 pyproj 的内部接口设置路径
        import pyproj
        pyproj.datadir.set_data_dir(proj_lib_path)
    except:
        pass
    print(f"🔧 已自动修复投影数据库路径，准备就绪！")
else:
    print("⚠️ 警告: 未能自动定位 proj 数据库。")

# --- 现在可以安全导入地理库了 ---
import osmnx as ox

# ==========================================
# 1. 强制清理旧缓存，防止读取到之前的错位数据
# ==========================================
if os.path.exists('cache'):
    try:
        shutil.rmtree('cache')
        print("🗑️ 已清理历史错误缓存。")
    except:
        pass

# ==========================================
# 2. 核心配置
# ==========================================
ox.settings.log_console = True
ox.settings.use_cache = True
ox.settings.timeout = 3600

# 绝对正确的地理边界
NORTH = 22.65
SOUTH = 21.60
EAST = 114.52
WEST = 112.85

def build_and_save_network(level_name, filename, network_type=None, custom_filter=None):
    if os.path.exists(filename):
        print(f"\n🟢 检测到 {filename} 已存在，跳过构建。")
        return

    print(f"\n{'=' * 60}")
    print(f"🚀 单线程稳健模式：开始构建【{level_name}】路网")
    print(f"📂 目标保存文件: {filename}")
    print(f"{'=' * 60}\n")

    start_time = time.time()
    try:
        # 自动适配不同版本的 OSMnx 参数规范
        v_major = int(ox.__version__.split('.')[0])
        if v_major >= 2:
            print(f"📡 检测到 OSMnx v{ox.__version__}，应用 (西, 南, 东, 北) 规范...")
            bbox_v2 = (WEST, SOUTH, EAST, NORTH)
            if custom_filter:
                G = ox.graph_from_bbox(bbox=bbox_v2, custom_filter=custom_filter)
            else:
                G = ox.graph_from_bbox(bbox=bbox_v2, network_type=network_type)
        else:
            print(f"📡 检测到 OSMnx v{ox.__version__}，应用 (北, 南, 东, 西) 规范...")
            if custom_filter:
                G = ox.graph_from_bbox(NORTH, SOUTH, EAST, WEST, custom_filter=custom_filter)
            else:
                G = ox.graph_from_bbox(NORTH, SOUTH, EAST, WEST, network_type=network_type)

        print(f"\n✅ 路网下载与拓扑简化完成！正在序列化保存到本地硬盘...")
        ox.io.save_graphml(G, filename)

        elapsed = time.time() - start_time
        nodes = len(G.nodes)
        edges = len(G.edges)

        print(f"\n🎉 完美收官！")
        print(f"⏱️ 耗时: {elapsed / 60:.2f} 分钟.")
        print(f"📊 节点数(路口): {nodes:,} 个 | 边数(路段): {edges:,} 条")
        print(f"💾 文件 {filename} 已生成，主程序将实现秒开！\n")

    except Exception as e:
        print(f"\n❌ 构建彻底失败: {e}")

if __name__ == '__main__':
    print("准备开始执行离线调度系统的核心路网数据抽取...")

    # 1. 构建主干道级路网 (只需一两分钟)
    build_and_save_network(
        level_name="主干道级 (国道/省道/主路)",
        filename="zhuhai_network_main.graphml",
        custom_filter='["highway"~"primary|secondary|tertiary|trunk|motorway"]'
    )

    # 2. 构建街道级路网 (数据量大，大概需要 5~15 分钟，请耐心等待终端绿字滚动)
    build_and_save_network(
        level_name="街道级 (全量机动车道)",
        filename="zhuhai_network_drive.graphml",
        network_type='drive_service'
    )

    print("=" * 60)
    print("🏁 所有路网数据已准备完毕！现在你可以直接去运行主程序了，地图路网将瞬间加载！")