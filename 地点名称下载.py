import os
import osmnx as ox
import pandas as pd
import warnings
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# 忽略 geopandas 的一些投影警告，保持控制台整洁
warnings.filterwarnings("ignore")

ox.settings.log_console = True
ox.settings.log_level = 20
ox.settings.timeout = 1800  # 延长超时时间


def download_and_save_layer(bbox, tags, filename, layer_name, keep_cols):
    """
    带断点续传与自动重试的下载函数
    """
    # 确定保存路径
    current_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(current_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    save_path = os.path.join(data_dir, filename)

    # ==========================================
    # 💡 智能断点续传：检测到文件存在直接跳过
    # ==========================================
    if os.path.exists(save_path):
        print(f"\n⏩ 检测到【{layer_name}】({filename}) 已下载，直接跳过！")
        return

    print(f"\n" + "=" * 50)
    print(f"📡 正在向服务器请求【{layer_name}】数据...")
    print("=" * 50)

    # ==========================================
    # 💡 自动重试机制：应对 SSL 与网络闪断
    # ==========================================
    max_retries = 3
    for attempt in range(max_retries):
        try:
            gdf = ox.features_from_bbox(bbox=bbox, tags=tags)

            if gdf.empty:
                print(f"\n⚠️ 未找到【{layer_name}】的相关数据。")
                return

            print(f"\n🔄 【{layer_name}】数据已下载，正在清洗瘦身...")

            existing_cols = [col for col in keep_cols if col in gdf.columns]
            if 'geometry' not in existing_cols:
                existing_cols.append('geometry')

            # 如果源数据有 name 列，就补充保留
            if 'name' in gdf.columns and 'name' not in existing_cols:
                existing_cols.append('name')

            gdf_clean = gdf[existing_cols].copy()

            # 如果是POI图层，过滤掉没有名字的数据
            if layer_name == "POI与建筑地名" and 'name' in gdf_clean.columns:
                gdf_clean = gdf_clean[gdf_clean['name'].notna()]

            gdf_clean.to_file(save_path, driver="GeoJSON")
            print(f"✅ 【{layer_name}】处理完成！共提取 {len(gdf_clean)} 条数据。")
            print(f"💾 成功保存至: {save_path}")

            return  # 成功则退出函数

        except Exception as e:
            print(f"\n❌ 第 {attempt + 1} 次下载【{layer_name}】失败: {e}")
            if attempt < max_retries - 1:
                print("⏳ 休息 10 秒后准备重试...")
                time.sleep(10)
            else:
                print(f"🚨 【{layer_name}】彻底失败，请稍后排查网络。")


def main():
    # 修复 Anaconda 虚拟环境下 pyproj 的报错
    proj_path = r'D:\Anaconda\conda\envs\aaa\Library\share\proj'
    if os.path.exists(proj_path):
        os.environ['PROJ_LIB'] = proj_path
        os.environ['PROJ_DATA'] = proj_path
        import pyproj
        pyproj.datadir.set_data_dir(proj_path)

    bbox = (112.85, 21.60, 114.52, 22.65)

    print("🚀 开始执行珠海市 GIS 补充数据下载任务（多线程强壮版）...")

    # ==========================================
    # 💡 解决 18.3GB 内存溢出：采用精确属性制导
    # ==========================================
    safe_poi_tags = {
        'building': True,
        'amenity': True,
        'shop': True,
        'office': True,
        'leisure': True,
        'historic': True,
        'tourism': True
    }

    tasks = [
        (safe_poi_tags, "zhuhai_pois.geojson", "POI与建筑地名", ['name', 'amenity', 'building', 'shop']),
        ({'landuse': True}, "zhuhai_landuse.geojson", "土地利用(商业/工业/居民区等)", ['name', 'landuse']),
        ({'natural': True, 'water': True, 'waterway': True}, "zhuhai_natural.geojson", "自然景观与水系",
         ['name', 'natural', 'water', 'waterway'])
    ]

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = []
        for tags, filename, layer_name, keep_cols in tasks:
            future = executor.submit(download_and_save_layer, bbox, tags, filename, layer_name, keep_cols)
            futures.append(future)

        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"线程执行严重异常: {e}")

    print("\n" + "🎉" * 15)
    print("🎉 所有任务均已跑完（请检查上方是否全是绿勾✅）！ 🎉")
    print("🎉" * 15)


if __name__ == "__main__":
    main()