import os
import json
import random
import copy
import pickle
import warnings
import requests
import numpy as np
import osmnx as ox
import pandas as pd
import osmnx.distance as ox_dist
import networkx as nx
import geopandas as gpd
from shapely.geometry import Point, LineString
from shapely.ops import substring
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QTextEdit, QCheckBox, QLabel, QGroupBox,
                             QSplitter, QFrame, QTableWidget, QTableWidgetItem,
                             QMessageBox, QAbstractItemView, QComboBox, QTabWidget, QGridLayout,
                             QSpinBox, QDoubleSpinBox, QMenu)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtCore import Qt, QUrl, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QCloseEvent

# 引入项目各模块
from config.settings import URBAN_COORDS, DEMAND, DATA_DIR, AMAP_WEB_KEY
from core.database import DatabaseManager
from core.algorithm import MD_CVRP_Worker
from gui.templates import HTML_TEMPLATE
from gui.bridge import MapBridge
from gui.dialogs import ChineseInputDialog

warnings.filterwarnings("ignore", category=UserWarning, message=".*Geometry is in a geographic CRS.*")


class OfflineReverseGeocodeThread(QThread):
    """【完全离线】智能融合：加入“全称优选”逻辑的空间解析线程"""
    result_ready = pyqtSignal(int, str, dict)

    def __init__(self, index, p_type, lat, lng, G, poi_gdf):
        super().__init__()
        self.index, self.p_type = index, p_type
        self.lat, self.lng = lat, lng
        self.G = G
        self.poi_gdf = poi_gdf

    def run(self):
        res_data = {'name': '', 'area': 0.0, 'pop': 0.0, 'imp': 2}
        try:
            p = Point(self.lng, self.lat)
            poi_name = ""
            poi_dist = float('inf')

            # --- 1. 查找离线空间库 (小区/建筑/绿地等) ---
            if self.poi_gdf is not None and not self.poi_gdf.empty:
                nearby_idx = list(self.poi_gdf.sindex.intersection(p.buffer(0.002).bounds))
                if nearby_idx:
                    nearby_geoms = self.poi_gdf.iloc[nearby_idx].copy()
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        nearby_geoms['dist'] = nearby_geoms.distance(p)
                    nearby_geoms = nearby_geoms.sort_values('dist')

                    # A. 如果严格落在多边形内部
                    intersects_geoms = nearby_geoms[nearby_geoms.intersects(p)].copy()
                    if not intersects_geoms.empty:
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            intersects_geoms['area'] = intersects_geoms.geometry.area
                        intersects_geoms = intersects_geoms.sort_values('area', ascending=False)

                        unique_names = []
                        for n in intersects_geoms['name']:
                            n_str = str(n).strip()
                            if n_str and n_str not in unique_names: unique_names.append(n_str)
                        # 面积倒序拼接，如：美丽家园 + 8 -> 美丽家园8
                        poi_name = "".join(unique_names)
                        poi_dist = 0.0

                        # 自动推演属性：面积 (转换为真实平方米)
                        geom = intersects_geoms.iloc[0].geometry
                        area_sqm = 0.0
                        try:
                            # 借助 geopandas 自带的投影转换，多线程下更安全、不易崩溃
                            import geopandas as gpd
                            series = gpd.GeoSeries([geom], crs="EPSG:4326")
                            area_sqm = series.to_crs("EPSG:3857").iloc[0].area
                        except Exception as e:
                            print(f"投影面积计算失败: {e}")

                        imp = 2
                        row_dict = intersects_geoms.iloc[0].to_dict()
                        amenity = str(row_dict.get('amenity', ''))
                        building = str(row_dict.get('building', ''))
                        landuse = str(row_dict.get('landuse', ''))
                        natural = str(row_dict.get('natural', ''))
                        shop = str(row_dict.get('shop', ''))
                        name_str = str(row_dict.get('name', ''))

                        # 1. 优先使用底层 OSM 空间标签判断
                        if amenity in ['school', 'hospital', 'college', 'university', 'kindergarten']:
                            imp = 4
                        elif building in ['government'] or amenity in ['police', 'fire_station']:
                            imp = 5
                        elif landuse in ['commercial', 'industrial', 'retail'] or shop != 'nan' or building in [
                            'commercial', 'office']:
                            imp = 3
                        elif natural != 'nan' or landuse in ['forest', 'grass', 'meadow']:
                            imp = 1
                        elif landuse in ['residential'] or building in ['residential', 'apartments']:
                            imp = 2

                        # 2. 中文语义增强匹配引擎（由于部分地点只有名字没有打标签，使用名字强制兜底矫正）
                        if any(k in name_str for k in ['村', '小区', '苑', '公寓', '别墅', '新村', '花园', '家园', '宿舍', '社区']):
                            imp = 2
                        elif any(k in name_str for k in ['学校', '医院', '卫生', '大学', '小学', '中学', '学院', '校区']):
                            imp = 4
                        elif any(k in name_str for k in ['公司', '厂', '市场', '商场', '广场', '中心', '店', '商业', '科技', '大厦']):
                            if not any(k in name_str for k in ['社区', '卫生']):  # 排除社区服务中心
                                imp = 3
                        elif any(k in name_str for k in ['政府', '公安', '派出所', '消防', '委', '局', '交警']):
                            imp = 5
                        elif any(k in name_str for k in ['山', '公园', '林', '岛', '风景', '农场']):
                            imp = 1

                        res_data['area'] = round(area_sqm, 1)
                        res_data['imp'] = imp

                    else:
                        # B. 不在内部，只获取最近的建筑距离备用
                        poi_dist = nearby_geoms.iloc[0]['dist']
                        poi_name = str(nearby_geoms.iloc[0]['name']).strip()

            # --- 2. 查找离线路网库 (道路) ---
            road_name = ""
            road_dist = float('inf')
            if self.G is not None:
                try:
                    u, v, key = ox_dist.nearest_edges(self.G, self.lng, self.lat)
                    edge_data = self.G.get_edge_data(u, v, key)
                    if edge_data:
                        line = edge_data.get('geometry', LineString([(self.G.nodes[u]['x'], self.G.nodes[u]['y']),
                                                                     (self.G.nodes[v]['x'], self.G.nodes[v]['y'])]))
                        r_name = edge_data.get('name', '')
                        if isinstance(r_name, list): r_name = r_name[0]
                        if r_name:
                            road_name = str(r_name).strip()
                            road_dist = line.distance(p)
                except:
                    pass

            # --- 3. 智能决策 (取消附近逻辑，谁近取谁) ---
            if poi_dist == 0.0:
                res_data['name'] = poi_name
            elif poi_name and road_name:
                res_data['name'] = road_name if road_dist < poi_dist else poi_name
            else:
                res_data['name'] = poi_name or road_name

            self.result_ready.emit(self.index, self.p_type, res_data)
        except Exception:
            self.result_ready.emit(self.index, self.p_type, res_data)


class TrafficDataFetchThread(QThread):
    """【联网 API】获取高德实时交通态势矢量数据（单网格分片）"""
    # 🌟 核心修复1：全部降维成字符串传递，彻底避开 PyQt 的底层类型转换拦截
    result_ready = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, api_key, bounds):
        super().__init__()
        self.api_key = api_key
        self.bounds = bounds

    def run(self):
        try:
            url = f"https://restapi.amap.com/v3/traffic/status/rectangle?key={self.api_key}&level=6&rectangle={self.bounds}&extensions=all"

            # 🌟 核心修复2：强制禁用系统代理。防止请求被本地梯子/代理死锁挂起
            proxies = {"http": None, "https": None}
            response = requests.get(url, timeout=5, proxies=proxies)
            data = response.json()

            if data.get('status') == '1':
                roads = data.get('trafficinfo', {}).get('roads', [])
                # 🌟 把列表字典序列化为 JSON 纯文本发给主线程
                import json
                self.result_ready.emit(json.dumps(roads))
            else:
                err_msg = f"[{data.get('infocode', '未知')}] {data.get('info', '请求失败')}"
                self.error_occurred.emit(err_msg)
        except Exception as e:
            # 万一崩溃，确保一定会抛出异常文本
            self.error_occurred.emit(f"网络异常: {str(e)}")


class PathGenerationThread(QThread):
    """【异步解耦】将耗时的几何与图论路径生成算法放到后台线程，彻底解决主界面卡顿问题"""
    # 【修复1】将 mode 设为 object，防止传参时因为 int/str 类型不匹配导致信号中断
    result_ready = pyqtSignal(list, str, object)
    error_occurred = pyqtSignal(str)

    def __init__(self, coords, mode, G):
        super().__init__()
        self.coords = coords
        self.mode = mode
        self.G = G

    def run(self):
        try:
            import networkx as nx
            import osmnx.distance as ox_dist
            from shapely.geometry import Point, LineString
            from shapely.ops import substring

            extracted_road_names = []

            exact_points = []
            for lat, lng in self.coords:
                lat, lng = float(lat), float(lng)
                u, v, key = ox_dist.nearest_edges(self.G, lng, lat)
                edge_data = self.G.get_edge_data(u, v, key)

                r_name = edge_data.get('name', '')
                if r_name:
                    if isinstance(r_name, list):
                        extracted_road_names.extend([str(n) for n in r_name if n])
                    else:
                        extracted_road_names.append(str(r_name))

                if 'geometry' in edge_data:
                    line = edge_data['geometry']
                else:
                    line = LineString([(float(self.G.nodes[u]['x']), float(self.G.nodes[u]['y'])),
                                       (float(self.G.nodes[v]['x']), float(self.G.nodes[v]['y']))])

                p = Point(lng, lat)
                proj_dist = line.project(p)
                proj_p = line.interpolate(proj_dist)

                start_pt = Point(line.coords[0])
                if start_pt.distance(
                        Point(float(self.G.nodes[u]['x']), float(self.G.nodes[u]['y']))) < start_pt.distance(
                        Point(float(self.G.nodes[v]['x']), float(self.G.nodes[v]['y']))):
                    n_u, n_v = u, v
                else:
                    n_u, n_v = v, u

                exact_points.append({
                    'u': n_u, 'v': n_v, 'line': line,
                    'proj_dist': proj_dist, 'length': line.length,
                    'lat': float(proj_p.y), 'lng': float(proj_p.x)
                })

            full_path_geom = []
            extracted_road_names = []
            G_undir = nx.Graph(self.G.to_undirected())
            for i in range(len(exact_points) - 1):
                pt1 = exact_points[i]
                pt2 = exact_points[i + 1]

                if (pt1['u'] == pt2['u'] and pt1['v'] == pt2['v']) or (pt1['u'] == pt2['v'] and pt1['v'] == pt2['u']):
                    d1, d2 = pt1['proj_dist'], pt2['proj_dist']
                    if d1 > d2: d1, d2 = d2, d1
                    sub_line = substring(pt1['line'], d1, d2)
                    # 【修复2】深度清洗 numpy 格式，强制转为原生 float
                    coords_geom = [[float(lat), float(lon)] for lon, lat in sub_line.coords]
                    if pt1['proj_dist'] > pt2['proj_dist']: coords_geom.reverse()

                    if full_path_geom and abs(full_path_geom[-1][0] - coords_geom[0][0]) < 1e-6 and abs(
                            full_path_geom[-1][1] - coords_geom[0][1]) < 1e-6:
                        full_path_geom.extend(coords_geom[1:])
                    else:
                        full_path_geom.extend(coords_geom)
                    continue

                d_uA = ox_dist.great_circle(pt1['lat'], pt1['lng'], float(self.G.nodes[pt1['u']]['y']),
                                            float(self.G.nodes[pt1['u']]['x']))
                d_vA = ox_dist.great_circle(pt1['lat'], pt1['lng'], float(self.G.nodes[pt1['v']]['y']),
                                            float(self.G.nodes[pt1['v']]['x']))
                d_uB = ox_dist.great_circle(pt2['lat'], pt2['lng'], float(self.G.nodes[pt2['u']]['y']),
                                            float(self.G.nodes[pt2['u']]['x']))
                d_vB = ox_dist.great_circle(pt2['lat'], pt2['lng'], float(self.G.nodes[pt2['v']]['y']),
                                            float(self.G.nodes[pt2['v']]['x']))

                best_cost = float('inf')
                best_route = None

                for start_node, d_start in [(pt1['u'], d_uA), (pt1['v'], d_vA)]:
                    for end_node, d_end in [(pt2['u'], d_uB), (pt2['v'], d_vB)]:
                        try:
                            path_len = nx.shortest_path_length(G_undir, start_node, end_node, weight='length')
                            total_cost = d_start + path_len + d_end
                            if total_cost < best_cost:
                                best_cost = total_cost
                                best_route = (start_node, end_node)
                        except nx.NetworkXNoPath:
                            continue

                if not best_route: continue
                best_start, best_end = best_route

                if best_start == pt1['u']:
                    sub_line = substring(pt1['line'], 0.0, pt1['proj_dist'])
                    coords_geom = [[float(lat), float(lon)] for lon, lat in sub_line.coords]
                    coords_geom.reverse()
                else:
                    sub_line = substring(pt1['line'], pt1['proj_dist'], pt1['line'].length)
                    coords_geom = [[float(lat), float(lon)] for lon, lat in sub_line.coords]

                if full_path_geom and abs(full_path_geom[-1][0] - coords_geom[0][0]) < 1e-6 and abs(
                        full_path_geom[-1][1] - coords_geom[0][1]) < 1e-6:
                    full_path_geom.extend(coords_geom[1:])
                else:
                    full_path_geom.extend(coords_geom)

                best_path = nx.shortest_path(G_undir, best_start, best_end, weight='length')
                for idx in range(len(best_path) - 1):
                    n_a, n_b = best_path[idx], best_path[idx + 1]

                    edge_data_dict = self.G.get_edge_data(n_a, n_b)
                    if not edge_data_dict: edge_data_dict = self.G.get_edge_data(n_b, n_a)
                    if not edge_data_dict: continue

                    edge_data = None
                    for data in edge_data_dict.values():
                        if 'geometry' in data:
                            edge_data = data
                            break
                    if not edge_data: edge_data = list(edge_data_dict.values())[0]

                    r_name = edge_data.get('name', '')
                    if r_name:
                        if isinstance(r_name, list):
                            extracted_road_names.extend([str(n) for n in r_name if n])
                        else:
                            extracted_road_names.append(str(r_name))

                    if 'geometry' in edge_data:
                        geom_coords = list(edge_data['geometry'].coords)
                        n_a_lon, n_a_lat = float(self.G.nodes[n_a]['x']), float(self.G.nodes[n_a]['y'])

                        dist_to_start = (float(geom_coords[0][0]) - n_a_lon) ** 2 + (
                                    float(geom_coords[0][1]) - n_a_lat) ** 2
                        dist_to_end = (float(geom_coords[-1][0]) - n_a_lon) ** 2 + (
                                    float(geom_coords[-1][1]) - n_a_lat) ** 2

                        if dist_to_end < dist_to_start: geom_coords.reverse()
                        coords_geom = [[float(lat), float(lon)] for lon, lat in geom_coords]

                        if full_path_geom and abs(full_path_geom[-1][0] - coords_geom[0][0]) < 1e-6 and abs(
                                full_path_geom[-1][1] - coords_geom[0][1]) < 1e-6:
                            full_path_geom.extend(coords_geom[1:])
                        else:
                            full_path_geom.extend(coords_geom)
                    else:
                        pt_a = [float(self.G.nodes[n_a]['y']), float(self.G.nodes[n_a]['x'])]
                        pt_b = [float(self.G.nodes[n_b]['y']), float(self.G.nodes[n_b]['x'])]
                        if full_path_geom and abs(full_path_geom[-1][0] - pt_a[0]) < 1e-6 and abs(
                                full_path_geom[-1][1] - pt_a[1]) < 1e-6:
                            full_path_geom.append(pt_b)
                        else:
                            full_path_geom.extend([pt_a, pt_b])

                if best_end == pt2['u']:
                    sub_line = substring(pt2['line'], 0.0, pt2['proj_dist'])
                    coords_geom = [[float(lat), float(lon)] for lon, lat in sub_line.coords]
                else:
                    sub_line = substring(pt2['line'], pt2['proj_dist'], pt2['line'].length)
                    coords_geom = [[float(lat), float(lon)] for lon, lat in sub_line.coords]
                    coords_geom.reverse()

                if full_path_geom and abs(full_path_geom[-1][0] - coords_geom[0][0]) < 1e-6 and abs(
                        full_path_geom[-1][1] - coords_geom[0][1]) < 1e-6:
                    full_path_geom.extend(coords_geom[1:])
                else:
                    full_path_geom.extend(coords_geom)

            final_road_name = '未知道路'
            if full_path_geom:
                if extracted_road_names:
                    from collections import Counter
                    valid_names = [n for n in extracted_road_names if '未命名' not in n]
                    if valid_names:
                        final_road_name = Counter(valid_names).most_common(1)[0][0]

            self.result_ready.emit(full_path_geom, final_road_name, self.mode)
        except Exception as e:
            self.error_occurred.emit(str(e))


class OfflineGeocodeValidationThread(QThread):
    """【完全离线】校验用户输入地名是否在路网地图中存在"""
    result_ready = pyqtSignal(int, str, str, bool, str, str)

    def __init__(self, row_idx, p_type, query, original_name, G, poi_gdf):
        super().__init__()
        self.row_idx, self.p_type = row_idx, p_type
        self.query, self.original_name = query, original_name
        self.G = G
        self.poi_gdf = poi_gdf

    def run(self):
        try:
            match_found = False
            official_name = ""

            # 优先在建筑地名库里搜索
            if self.poi_gdf is not None and not self.poi_gdf.empty:
                # 模糊匹配名称列
                matches = self.poi_gdf[self.poi_gdf['name'].str.contains(self.query, na=False)]
                if not matches.empty:
                    official_name = str(matches.iloc[0]['name'])
                    match_found = True

            # 如果没找到，再去路网边缘搜索
            if not match_found and self.G is not None:
                for u, v, key, data in self.G.edges(keys=True, data=True):
                    names = data.get('name', [])
                    if isinstance(names, str): names = [names]
                    if any(self.query in str(n) for n in names):
                        official_name = names[0]
                        match_found = True
                        break

            if match_found:
                self.result_ready.emit(self.row_idx, self.p_type, self.original_name, True, official_name, self.query)
            else:
                self.result_ready.emit(self.row_idx, self.p_type, self.original_name, False, "", self.query)
        except Exception:
            self.result_ready.emit(self.row_idx, self.p_type, self.original_name, False, "", self.query)


class TextSearchThread(QThread):
    """【极速版异步检索】采用截断匹配与字典降维遍历，实现毫秒级响应"""
    result_ready = pyqtSignal(str)

    def __init__(self, query, poi_gdf, G):
        super().__init__()
        self.query = query
        self.poi_gdf = poi_gdf
        self.G = G

    def run(self):
        import json
        import pandas as pd

        results = []
        query = self.query.strip()
        query_lower = query.lower()

        if self.poi_gdf is not None and not self.poi_gdf.empty and query:
            # 1. 极速过滤：优先只搜名字，这通常能在 10ms 内完成
            mask = self.poi_gdf['name'].str.contains(query, na=False, case=False)

            # 智能兜底：如果名字匹配太少（少于20个），才启用耗时的底层属性模糊匹配
            if mask.sum() < 20:
                fuzzy_cols = ['amenity', 'building', 'landuse', 'shop', 'natural', 'street', 'addr:street']
                for col in fuzzy_cols:
                    if col in self.poi_gdf.columns:
                        mask = mask | self.poi_gdf[col].astype(str).str.contains(query, case=False, na=False)

            matches = self.poi_gdf[mask]

            if not matches.empty:
                # 🚀 核心优化 1：截断匹配。
                # 不对几万条数据打分，利用向量化操作优先挑出“首字匹配”的数据和少量“包含匹配”的数据
                starts_mask = matches['name'].str.startswith(query, na=False)

                # 最多只取 300 条进入复杂打分逻辑（足够覆盖 Top 8 的推荐了）
                fast_matches = pd.concat([
                    matches[starts_mask].head(150),
                    matches[~starts_mask].head(150)
                ])

                # 🚀 核心优化 2：字典降维。
                # 抛弃极度缓慢的 Pandas DataFrame 逐行处理，转换为纯 Python 字典列表进行极速遍历
                records = fast_matches.to_dict('records')

                scored_results = []
                unique_names = set()

                for row in records:
                    name = str(row.get('name', '')).strip()
                    if not name or name in unique_names:
                        continue

                    name_lower = name.lower()

                    # 获取基础属性
                    city_val = str(row.get('city', row.get('addr:city', ''))).strip()
                    dist_val = str(row.get('district', row.get('addr:district', ''))).strip()

                    # 极速判断是否在珠海 (依靠文本和简单的坐标界限，不调用耗时的 geometry.contains 库)
                    in_zhuhai = ('珠海' in city_val) or ('珠海' in dist_val) or ('珠海' in name)

                    geom = row.get('geometry')
                    centroid = geom.centroid if geom else None

                    if not in_zhuhai and centroid:
                        if 113.0 <= centroid.x <= 114.0 and 21.8 <= centroid.y <= 22.5:
                            in_zhuhai = True

                    # 极速执行 7 级打分算法
                    score = 0
                    if name_lower == query_lower:
                        score = 7000
                    elif name_lower.startswith(query_lower) and in_zhuhai:
                        score = 6000
                    elif query_lower in name_lower and in_zhuhai:
                        score = 5000
                    elif name_lower.startswith(query_lower) and not in_zhuhai:
                        score = 4000
                    elif query_lower not in name_lower and in_zhuhai:
                        score = 3000
                    elif query_lower in name_lower and not in_zhuhai:
                        score = 2000
                    else:
                        score = 1000

                    score -= len(name)  # 长度惩罚：越简短越靠前

                    scored_results.append({
                        'name': name,
                        'score': score,
                        'centroid': centroid,
                        'row_dict': row
                    })
                    unique_names.add(name)

                # 按分数排序，截取 Top 11
                scored_results.sort(key=lambda x: x['score'], reverse=True)
                top_11 = scored_results[:11]

                # 🚀 核心优化 3：仅对最终展示的这 11 条数据进行格式补全，且【剔除了全城路网推演】
                for item in top_11:
                    name = item['name']
                    centroid = item['centroid']
                    row_dict = item['row_dict']

                    street = str(row_dict.get('street', row_dict.get('addr:street', ''))).strip()
                    district = str(row_dict.get('district', row_dict.get('addr:district', ''))).strip()
                    city = str(row_dict.get('city', row_dict.get('addr:city', ''))).strip()

                    if street.lower() in ['nan', 'none', 'null', '']: street = ""
                    if district.lower() in ['nan', 'none', 'null', '']: district = ""
                    if city.lower() in ['nan', 'none', 'null', '']: city = ""

                    # 坐标范围推演区市依然保留，因为只需要极少的 if-else 判断，耗时接近 0
                    if not city and centroid:
                        if 113.0 <= centroid.x <= 114.0 and 21.8 <= centroid.y <= 22.5:
                            city = "珠海市"
                            if not district:
                                if centroid.x >= 113.45:
                                    district = "香洲区"
                                elif centroid.x <= 113.30:
                                    district = "斗门区"
                                else:
                                    district = "金湾区"
                        elif 113.1 <= centroid.x <= 113.6 and 22.3 <= centroid.y <= 22.8:
                            city = "中山市"

                    parts = [p for p in [street, district, city] if p]
                    subtitle = ", ".join(parts)
                    display_str = f"{name} ({subtitle})" if subtitle else name

                    results.append({
                        'name': name,
                        'display_name': display_str,
                        'lat': centroid.y if centroid else 0,
                        'lon': centroid.x if centroid else 0
                    })

        js_code = f"if(typeof showTextSearchResults === 'function') showTextSearchResults({json.dumps(results)});"
        self.result_ready.emit(js_code)


class LogisticApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("珠海应急资源调度系统")
        self.setGeometry(100, 100, 1600, 950)

        # 用于后续寻优算法的全局路况缓存
        self.realtime_traffic_data = []

        self.db = DatabaseManager()
        self.undo_stack = []
        self.redo_stack = []
        self.all_routes_geometry = []
        self.road_conditions = []
        self.current_network_type = 'drive_service'

        self.load_data_from_db()
        self.G = self.init_graph(self.current_network_type)
        self.poi_gdf = self.init_offline_pois()  # 预加载建筑空间库
        self.init_ui()
        self.setup_bridge()
        self.traffic_timer = QTimer(self)
        self.traffic_timer.timeout.connect(self.request_traffic_bounds)

        # 程序启动后立刻检测：是否有名称列为空，触发检索
        self.fetch_missing_names()

    def init_offline_pois(self):
        """离线加载多层地理库，保留核心属性标签用于智能推算，加入 v2 极速缓存"""
        pkl_path = os.path.join(DATA_DIR, "zhuhai_all_features_v2.pkl")

        if os.path.exists(pkl_path):
            try:
                with open(pkl_path, 'rb') as f:
                    return pickle.load(f)
            except Exception:
                pass

        files_to_load = ["zhuhai_pois.geojson", "zhuhai_landuse.geojson", "zhuhai_natural.geojson"]
        gdfs = []
        print("⏳ 正在聚合地理属性引擎 (首次升级需要几秒钟)...")
        QApplication.processEvents()

        # 【修复1：扩展保留字段】新增保留城市、行政区、街道等地理层级标签（兼容标准OSM命名法）
        keep_attrs = ['name', 'geometry', 'amenity', 'building', 'landuse', 'shop', 'natural',
                      'city', 'district', 'street', 'addr:city', 'addr:district', 'addr:street']

        for filename in files_to_load:
            file_path = os.path.join(DATA_DIR, filename)
            if os.path.exists(file_path):
                try:
                    gdf = gpd.read_file(file_path)
                    if not gdf.empty and 'name' in gdf.columns:
                        existing_cols = [c for c in keep_attrs if c in gdf.columns]
                        clean_gdf = gdf[existing_cols].copy()
                        clean_gdf = clean_gdf[clean_gdf['name'].notna()]
                        gdfs.append(clean_gdf)
                except Exception:
                    pass

        if gdfs:
            combined_gdf = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True))
            _ = combined_gdf.sindex
            with open(pkl_path, 'wb') as f:
                pickle.dump(combined_gdf, f)
            print("✅ 地理属性引擎升级完成！")
            return combined_gdf

        return None

    def fetch_missing_names(self):
        """启动时：收集需要补全的受灾点/配送中心，放入单线处理队列，防止多线程引发内存崩溃"""
        self.geocode_queue = []  # 初始化任务队列
        invalid_names = ['', '未命名', '未命名位置', '正在获取...', '客户点']

        # 扫描配送中心
        for i, d in enumerate(self.depots):
            name = str(d.get('name', '')).strip()
            if name in invalid_names or (not name.startswith('配送中心') and '中心' not in name and len(name) < 2):
                d['name'] = '排队获取中...'
                self.geocode_queue.append((i, 'depot', d['pos'][0], d['pos'][1]))

        # 扫描受灾点
        for i, c in enumerate(self.customers):
            name = str(c.get('name', '')).strip()
            # 只有全默认且未锁定的才自动推算
            is_default = (c.get('area', 0) == 0 and c.get('imp', 2) == 2 and not c.get('manual_edit', False))

            if name in invalid_names or (
                    not name.startswith('受灾点') and '受灾' not in name and len(name) < 2) or is_default:
                if name in invalid_names: c['name'] = '排队获取中...'
                self.geocode_queue.append((i, 'customer', c['pos'][0], c['pos'][1]))

        if self.geocode_queue:
            self.refresh_table()
            self.log_area.append(f"⏳ 发现 {len(self.geocode_queue)} 个点需要智能识别，正在后台排队处理，请稍候...")
            self.process_next_geocode()  # 开启单线队列处理

    def process_next_geocode(self):
        """核心修复：单线处理队列中的下一个地点识别任务，避免高并发摧毁内存"""
        if not self.geocode_queue:
            self.refresh_table()
            self.sync_data_to_map()
            self.log_area.append("✅ 所有受灾点及配送中心的地理信息已智能解析完毕！")
            return

        task = self.geocode_queue.pop(0)
        idx, p_type, lat, lng = task

        # ⚠️ 致命崩溃修复：不能用单变量，存入列表防止被垃圾回收器强杀
        thread = OfflineReverseGeocodeThread(idx, p_type, lat, lng, self.G, self.poi_gdf)
        thread.result_ready.connect(self.on_queue_geocode_done)

        if not hasattr(self, 'geocode_threads'): self.geocode_threads = []
        self.geocode_threads.append(thread)

        # 【护盾】交由 Qt 底层安全销毁
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda t=thread: self.cleanup_thread(t))

        thread.start()

    def cleanup_thread(self, thread):
        """安全移除 Python 端的线程引用（防止内存泄漏与强杀崩溃）"""
        if hasattr(self, 'geocode_threads') and thread in self.geocode_threads:
            self.geocode_threads.remove(thread)

    def on_queue_geocode_done(self, index, p_type, res_data):
        """批量处理回调：只静默更新底层数据字典，不刷新地图，处理完立刻拉取下一个任务"""
        name = res_data.get('name', '').strip()
        final_name = name if name else (f"配送中心{index + 1}" if p_type == 'depot' else f"受灾点{index + 1}")

        if p_type == 'depot' and index < len(self.depots):
            self.depots[index]['name'] = final_name
        elif p_type == 'customer' and index < len(self.customers):
            c = self.customers[index]
            old_name = c.get('name', '')

            # 智能解锁逻辑
            if old_name not in ['', '排队获取中...', '正在获取...'] and old_name != final_name:
                c['manual_edit'] = False

            # 名称覆盖
            if old_name in ['', '排队获取中...', '正在获取...'] or old_name != final_name:
                c['name'] = final_name

            # 属性推演覆盖
            if not c.get('manual_edit', False):
                if res_data.get('area', 0) > 0 or c.get('area', 0) == 0: c['area'] = res_data.get('area', 0)
                if res_data.get('imp', 2) != 2 or c.get('imp', 2) == 2: c['imp'] = res_data.get('imp', 2)

        # 当前任务处理完毕，立刻递归调用处理下一个，直到队列清空
        self.process_next_geocode()

    def on_table_double_clicked(self, row, col):
        """表格双击时，提取坐标并触发绘制"""
        num_depots = len(self.depots)
        if row < num_depots:
            pos = self.depots[row]['pos']
        else:
            pos = self.customers[row - num_depots]['pos']
        self.highlight_location(pos[0], pos[1])

    def highlight_location(self, lat, lng):
        """提取该坐标最大的包裹多边形，推送到前端绘制虚线框"""
        import json
        from shapely.geometry import mapping
        js_data = "null"

        if self.poi_gdf is not None and not self.poi_gdf.empty:
            p = Point(lng, lat)
            idx = list(self.poi_gdf.sindex.intersection(p.bounds))
            if idx:
                matches = self.poi_gdf.iloc[idx].copy()
                intersects = matches[matches.intersects(p)].copy()
                if not intersects.empty:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        intersects['area'] = intersects.geometry.area
                    intersects = intersects.sort_values('area', ascending=False)
                    # 选取面积最大的多边形（如整个学校、整个小区的范围）
                    geom = intersects.iloc[0].geometry
                    js_data = json.dumps(mapping(geom))

        # 触发 JS 执行绘制
        self.browser.page().runJavaScript(
            f"if(typeof drawOutline !== 'undefined') drawOutline({js_data}, {lat}, {lng});")

    def create_centered_checkbox(self, is_checked, row_idx):
        widget = QWidget()
        checkbox = QCheckBox()
        checkbox.setChecked(bool(is_checked))
        checkbox.setStyleSheet("margin-left: 5px;")
        layout = QHBoxLayout(widget)
        layout.addWidget(checkbox)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setContentsMargins(0, 0, 0, 0)
        widget.setLayout(layout)
        checkbox.stateChanged.connect(lambda state: self.sync_checkbox_to_data(state, row_idx))
        return widget

    def create_importance_combo(self, current_val, row_idx):
        combo = QComboBox()
        opts = [("1-山区", 1), ("2-居民区", 2), ("3-商业/工厂", 3), ("4-学校/医院", 4), ("5-政府区", 5)]
        for text, val in opts:
            combo.addItem(text, val)
        idx = next((i for i, v in enumerate(opts) if v[1] == current_val), 1)
        combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(lambda: self.sync_combo_to_data(combo, row_idx))
        return combo

    def sync_checkbox_to_data(self, state, row_idx):
        is_active = 1 if state == Qt.CheckState.Checked.value else 0
        num_depots = len(self.depots)
        if row_idx < num_depots:
            self.depots[row_idx]['active'] = is_active
        else:
            self.customers[row_idx - num_depots]['active'] = is_active
        self.sync_data_to_map()

    def sync_combo_to_data(self, combo, row_idx):
        val = combo.currentData()
        num_depots = len(self.depots)
        if row_idx >= num_depots:
            self.customers[row_idx - num_depots]['imp'] = val
            self.customers[row_idx - num_depots]['manual_edit'] = True # 打锁

    def init_graph(self, network_level):
        file_name = f"zhuhai_network_{network_level}.graphml"
        pkl_name = f"zhuhai_network_{network_level}.pkl"
        file_path = os.path.join(DATA_DIR, file_name)
        pkl_path = os.path.join(DATA_DIR, pkl_name)

        if os.path.exists(pkl_path):
            try:
                with open(pkl_path, 'rb') as f:
                    g_cache = pickle.load(f)
                    if hasattr(g_cache, 'is_directed') and g_cache.is_directed():
                        g_cache = g_cache.to_undirected()
                    return g_cache
            except Exception as e:
                print(f"Pickle 缓存加载失败: {e}，将回退到 GraphML 慢速加载。")

        if os.path.exists(file_path):
            G = ox.io.load_graphml(file_path).to_undirected()
            with open(pkl_path, 'wb') as f:
                pickle.dump(G, f)
            return G

        self.show_chinese_info("路网生成提示", f"首次启用该级别的路网需要分析地理数据，大约耗时1-2分钟，请点击确认并耐心等待。")
        QApplication.processEvents()
        bbox = (112.85, 21.60, 114.52, 22.65)
        if network_level == 'main':
            custom_filter = '["highway"~"primary|secondary|tertiary|trunk|motorway"]'
            G = ox.graph_from_bbox(bbox=bbox, custom_filter=custom_filter)
        else:
            G = ox.graph_from_bbox(bbox=bbox, network_type='drive_service')

        os.makedirs(DATA_DIR, exist_ok=True)
        ox.io.save_graphml(G, file_path)

        G_undirected = G.to_undirected()
        with open(pkl_path, 'wb') as f:
            pickle.dump(G_undirected, f)
        return G_undirected

    def load_data_from_db(self):
        self.depots, self.customers, self.road_conditions, db_params = self.db.load_data()
        self.algo_params = {
            'birdNum': db_params.get('birdNum', 50),
            'iterMax': db_params.get('iterMax', 120),
            'w_initial': db_params.get('w_initial', 0.9),
            'w_final': db_params.get('w_final', 0.5),
            'c1': db_params.get('c1', 1.5),
            'c2': db_params.get('c2', 1.5),
            'mutation_rate': db_params.get('mutation_rate', 0.12),
            'local_search_prob': db_params.get('local_search_prob', 0.3)
        }
        self.geocode_threads = []

    def build_algo_params_ui(self):
        self.tab_params = QWidget()
        vbox = QVBoxLayout(self.tab_params)
        self.param_spinboxes = {}

        def make_spinbox(key, is_int, v_min, v_max, step, label_text, hint_text):
            row = QHBoxLayout()
            row.addWidget(QLabel(label_text))
            if is_int:
                sp = QSpinBox()
                sp.setRange(int(v_min), int(v_max))
                sp.setValue(int(self.algo_params[key]))
                sp.editingFinished.connect(lambda: self.update_algo_param(key, sp.value()))
            else:
                sp = QDoubleSpinBox()
                sp.setRange(v_min, v_max)
                sp.setSingleStep(step)
                sp.setValue(float(self.algo_params[key]))
                sp.editingFinished.connect(lambda: self.update_algo_param(key, sp.value()))
            self.param_spinboxes[key] = sp
            row.addWidget(sp)
            hint = QLabel(hint_text)
            hint.setStyleSheet("color: gray;")
            row.addWidget(hint)
            return row

        g1 = QGroupBox("粒子群算法 (PSO)")
        l1 = QVBoxLayout(g1)
        l1.addLayout(make_spinbox('birdNum', True, 10, 500, 1, "粒子数量 (birdNum):", "(10~500, 推荐 50)"))
        l1.addLayout(make_spinbox('iterMax', True, 10, 1000, 10, "迭代次数 (iterMax):", "(10~1000, 推荐 120)"))
        l1.addLayout(make_spinbox('w_initial', False, 0.01, 1.0, 0.1, "初始惯性因子 (w_initial):", "(0.01~1.0, 推荐 0.9)"))
        l1.addLayout(make_spinbox('w_final', False, 0.01, 1.0, 0.1, "最终惯性因子 (w_final):", "(0.01~1.0, 推荐 0.5)"))
        l1.addLayout(make_spinbox('c1', False, 0.1, 4.0, 0.1, "自我认知因子 (c1):", "(0.1~4.0, 推荐 1.5)"))
        l1.addLayout(make_spinbox('c2', False, 0.1, 4.0, 0.1, "社会认知因子 (c2):", "(0.1~4.0, 推荐 1.5)"))
        vbox.addWidget(g1)

        g2 = QGroupBox("遗传变异")
        l2 = QVBoxLayout(g2)
        l2.addLayout(
            make_spinbox('mutation_rate', False, 0.0, 1.0, 0.05, "变异概率 (mutation_rate):", "(0.0~1.0, 推荐 0.12)"))
        vbox.addWidget(g2)

        g3 = QGroupBox("2-opt 局部搜索")
        l3 = QVBoxLayout(g3)
        l3.addLayout(
            make_spinbox('local_search_prob', False, 0.0, 1.0, 0.05, "触发概率 (local_search_prob):", "(0.0~1.0, 推荐 0.3)"))
        vbox.addWidget(g3)
        vbox.addStretch()
        return self.tab_params

    def update_algo_param(self, key, value):
        if self.algo_params[key] != value:
            self.push_state()
            self.algo_params[key] = value

    def init_ui(self):
        main_widget = QWidget()
        layout = QHBoxLayout(main_widget)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.browser = QWebEngineView()
        base_url = QUrl.fromLocalFile(
            os.path.abspath(os.path.dirname(__file__) if '__file__' in locals() else '.') + "/")
        self.browser.setHtml(HTML_TEMPLATE, base_url)

        panel = QFrame()
        panel.setFixedWidth(520)
        vbox = QVBoxLayout(panel)

        top_box = QHBoxLayout()
        self.btn_undo = QPushButton("↩️ 撤销")
        self.btn_redo = QPushButton("🔁 取消撤销")
        self.btn_save = QPushButton("💾 同步数据库")

        self.btn_undo.clicked.connect(self.undo)
        self.btn_redo.clicked.connect(self.redo)
        self.btn_save.clicked.connect(self.manual_save)

        top_box.addWidget(self.btn_undo)
        top_box.addWidget(self.btn_redo)
        top_box.addWidget(self.btn_save)
        vbox.addLayout(top_box)

        level_box = QHBoxLayout()
        level_label = QLabel("路网级别:")
        level_label.setStyleSheet("font-weight: bold;")
        self.combo_level = QComboBox()
        self.combo_level.addItems(["🚗 街道级 ", "🛣️ 主干道级 "])
        self.combo_level.currentIndexChanged.connect(self.change_routing_level)
        level_box.addWidget(level_label)
        level_box.addWidget(self.combo_level)
        vbox.addLayout(level_box)

        self.tabs = QTabWidget()
        self.tab_locations = QWidget()
        self.tab_roads = QWidget()

        loc_layout = QVBoxLayout(self.tab_locations)
        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(["启用", "编号", "名称", "坐标", "需求(T)", "人口(人)", "面积(m²)", "重要等级"])
        self.table.verticalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.verticalHeader().setFixedWidth(30)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 75)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 150)
        self.table.setColumnWidth(4, 55)
        self.table.setColumnWidth(5, 55)
        self.table.setColumnWidth(6, 55)
        self.table.setColumnWidth(7, 90)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)  # 允许选中单元格
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)  # 允许 Ctrl/Shift 多选
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)  # 开启自定义右键菜单
        self.table.customContextMenuRequested.connect(lambda pos: self.show_table_context_menu(pos, self.table))
        self.table.itemChanged.connect(self.handle_table_edit)
        self.table.cellDoubleClicked.connect(self.on_table_double_clicked)
        loc_layout.addWidget(self.table)

        btn_grid = QHBoxLayout()
        self.btn_add_depot = QPushButton("➕ 添加配送中心")
        self.btn_add_cust = QPushButton("➕ 添加受灾点")
        self.btn_del_point = QPushButton("🗑️ 删除选中点")
        self.btn_add_depot.setCheckable(True)
        self.btn_add_cust.setCheckable(True)
        self.btn_add_depot.clicked.connect(lambda: self.change_mode("depot"))
        self.btn_add_cust.clicked.connect(lambda: self.change_mode("customer"))
        self.btn_del_point.clicked.connect(self.delete_selected_rows)
        btn_grid.addWidget(self.btn_add_depot)
        btn_grid.addWidget(self.btn_add_cust)
        btn_grid.addWidget(self.btn_del_point)
        loc_layout.addLayout(btn_grid)

        road_layout = QVBoxLayout(self.tab_roads)
        self.road_table = QTableWidget()
        self.road_table.setColumnCount(4)
        self.road_table.setHorizontalHeaderLabels(["破损类型", "道路名称", "坐标起点/终点", "操作"])
        self.road_table.setColumnWidth(0, 80)
        self.road_table.setColumnWidth(1, 90)
        self.road_table.setColumnWidth(2, 180)
        self.road_table.setColumnWidth(3, 100)
        self.road_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.road_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.road_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.road_table.customContextMenuRequested.connect(
            lambda pos: self.show_table_context_menu(pos, self.road_table))
        road_layout.addWidget(self.road_table)

        road_btn_grid = QGridLayout()
        self.btn_road_block = QPushButton("❌ 无法通行")
        self.btn_road_jam = QPushButton("🔴 交通堵塞")
        self.btn_road_obs = QPushButton("🟡 通行缓慢")
        self.btn_road_ctrl = QPushButton("⚪ 临时管控")
        self.btn_road_spec = QPushButton("🟣 特殊路段")
        self.btn_end_path = QPushButton("生成路径")

        for btn in [self.btn_road_block, self.btn_road_jam, self.btn_road_obs, self.btn_road_ctrl, self.btn_road_spec]:
            btn.setCheckable(True)

        self.btn_road_block.clicked.connect(lambda: self.change_mode("road_block"))
        self.btn_road_jam.clicked.connect(lambda: self.change_mode("road_jam"))
        self.btn_road_obs.clicked.connect(lambda: self.change_mode("road_slow"))
        self.btn_road_ctrl.clicked.connect(lambda: self.change_mode("road_control"))
        self.btn_road_spec.clicked.connect(lambda: self.change_mode("road_special"))
        self.btn_end_path.clicked.connect(self.force_end_path)

        self.btn_traffic_on = QPushButton("🟢 打开实时交通")
        self.btn_traffic_off = QPushButton("⚪ 关闭实时交通")
        self.btn_clear_roads = QPushButton("🗑️ 清空表格")  # 新增：清空按钮

        self.btn_traffic_on.clicked.connect(self.request_traffic_bounds)
        self.btn_traffic_off.clicked.connect(self.hide_realtime_traffic)
        self.btn_clear_roads.clicked.connect(self.clear_all_roads)  # 新增：绑定清空事件

        road_btn_grid.addWidget(self.btn_road_block, 0, 0)
        road_btn_grid.addWidget(self.btn_road_jam, 0, 1)
        road_btn_grid.addWidget(self.btn_road_obs, 0, 2)
        road_btn_grid.addWidget(self.btn_road_ctrl, 1, 0)
        road_btn_grid.addWidget(self.btn_road_spec, 1, 1)
        road_btn_grid.addWidget(self.btn_end_path, 1, 2)

        # 优化排版：让第三排的三个按钮各占一列，完美对齐
        road_btn_grid.addWidget(self.btn_traffic_on, 2, 0)
        road_btn_grid.addWidget(self.btn_traffic_off, 2, 1)
        road_btn_grid.addWidget(self.btn_clear_roads, 2, 2)
        road_layout.addLayout(road_btn_grid)

        self.tabs.addTab(self.tab_locations, "🏘️ 受灾点信息系统")
        self.tabs.addTab(self.tab_roads, "🚧 道路信息系统")
        self.tabs.addTab(self.build_algo_params_ui(), "⚙️ 算法参数配置")
        vbox.addWidget(self.tabs)
        self.tabs.currentChanged.connect(self.on_tab_changed)

        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setStyleSheet("background-color: #1e1e1e; color: #00ff00; font-family: Consolas;")
        self.log_area.setFixedHeight(140)

        self.btn_run = QPushButton("🚀 开始路径寻优")
        self.btn_run.setFixedHeight(45)
        self.btn_run.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; font-size: 18px;")
        self.btn_run.clicked.connect(self.run_optimization)

        self.filter_group = QGroupBox("路径轨迹控制")
        self.filter_layout = QVBoxLayout()
        self.filter_group.setLayout(self.filter_layout)
        self.filter_group.setVisible(False)
        vbox.addWidget(self.filter_group)

        vbox.addWidget(self.log_area)
        vbox.addWidget(self.btn_run)

        splitter.addWidget(self.browser)
        splitter.addWidget(panel)
        layout.addWidget(splitter)
        self.setCentralWidget(main_widget)
        self.refresh_table()
        self.refresh_road_table()

    def show_chinese_warning(self, title, text):
        """统一的纯中文警告弹窗"""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setText(text)
        msg_box.setIcon(QMessageBox.Icon.Warning)
        msg_box.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
        msg_box.exec()

    def show_chinese_info(self, title, text):
        """统一的纯中文提示弹窗"""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setText(text)
        msg_box.setIcon(QMessageBox.Icon.Information)
        msg_box.addButton("确定", QMessageBox.ButtonRole.AcceptRole)
        msg_box.exec()

    def change_routing_level(self, index):
        new_level = 'drive' if index == 0 else 'main'
        if new_level != self.current_network_type:
            level_name = "街道级" if index == 0 else "主干道级"
            self.log_area.append(f"\n🔄 正在切换后台路由引擎至【{level_name}】...")
            self.current_network_type = new_level
            self.G = self.init_graph(self.current_network_type)
            self.log_area.append("✅ 路网引擎切换成功！下一次寻优将采用新规划策略。")

    def on_tab_changed(self, index):
        self.change_mode("none")
        self.log_area.append("🔄 界面已切换，地图操作模式已重置。")

    def setup_bridge(self):
        self.channel = QWebChannel()
        self.bridge = MapBridge()
        self.channel.registerObject("bridge", self.bridge)
        self.browser.page().setWebChannel(self.channel)

        self.bridge.point_added.connect(self.handle_map_add)
        self.bridge.point_moved.connect(self.handle_map_move)
        self.bridge.search_converted.connect(self.handle_search_convert)
        self.bridge.road_block_added.connect(self.handle_road_block)
        self.bridge.road_block_moved.connect(self.handle_road_block_move)
        self.bridge.generate_path_requested.connect(self.handle_complex_road_segment)
        self.bridge.point_deleted_from_map.connect(self.handle_map_delete)
        self.bridge.point_double_clicked.connect(self.highlight_location)
        self.bridge.traffic_requested.connect(self.handle_traffic_bounds)
        self.bridge.coord_search_requested.connect(self.handle_coord_search)
        self.bridge.warning_requested.connect(self.show_chinese_warning)
        self.bridge.text_search_requested.connect(self.handle_text_search)
        self.browser.loadFinished.connect(self.sync_data_to_map)

    def handle_coord_search(self, lat, lng):
        import json
        if not self.is_valid_coordinate(lat, lng):
            # 取消弹出警告框，改为静默清空推荐，避免用户在退格删除时疯狂弹窗干扰
            self.browser.page().runJavaScript(
                "if(typeof showCoordSearchResults === 'function') showCoordSearchResults([]);")
            return

        results = []
        p = Point(lng, lat)

        # 1. 在本地离线空间库中搜索周边实体
        if self.poi_gdf is not None and not self.poi_gdf.empty:
            nearby_idx = list(self.poi_gdf.sindex.intersection(p.buffer(0.01).bounds))
            if nearby_idx:
                nearby_geoms = self.poi_gdf.iloc[nearby_idx].copy()
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    nearby_geoms['dist'] = nearby_geoms.distance(p)
                nearby_geoms = nearby_geoms.sort_values('dist')

                unique_names = set()
                for idx, row in nearby_geoms.iterrows():
                    n = str(row['name']).strip()
                    if n and n not in unique_names and n != 'nan':
                        unique_names.add(n)

                        # 核心修改点：取该建筑/小区的真实质心坐标！
                        centroid = row.geometry.centroid

                        # 核心修改点：不再写死珠海市，尝试从属性表中读取city或district，若无则留空
                        row_dict = row.to_dict()
                        city_str = row_dict.get('city', '')
                        if not city_str or str(city_str) == 'nan':
                            city_str = row_dict.get('district', '')
                        if str(city_str) == 'nan': city_str = ''

                        location_suffix = f"    {city_str}" if city_str else ""

                        # .4f 会自动把类似 113.5 格式化为 113.5000，完美实现您要求的动态补零
                        display_name = f"{centroid.y:.4f},{centroid.x:.4f}    {n}{location_suffix}"

                        results.append({
                            'lat': centroid.y,
                            'lon': centroid.x,
                            'name': n,
                            'display_name': display_name
                        })
                    if len(results) >= 10:  # 1首选 + 10周边
                        break

        # 2. 兜底方案：如果没搜索到任何内容，或者坐标落在空地上，对当前输入的坐标进行补零推荐
        if not results:
            results.append({
                'lat': lat,
                'lon': lng,
                'name': '精准定位处',
                'display_name': f"{lat:.4f},{lng:.4f}    精准定位处"
            })

        self.browser.page().runJavaScript(
            f"if(typeof showCoordSearchResults === 'function') showCoordSearchResults({json.dumps(results)});")

    def handle_text_search(self, query):
        """主线程快速响应：将搜索任务丢给独立 QThread，防止 UI 假死"""
        if not hasattr(self, 'search_threads'):
            self.search_threads = []

        thread = TextSearchThread(query, self.poi_gdf, self.G)
        thread.result_ready.connect(self.on_text_search_done)

        self.search_threads.append(thread)

        # 安全销毁机制，防止内存泄漏
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda t=thread: self.cleanup_search_thread(t))

        thread.start()

    def cleanup_search_thread(self, thread):
        """安全清理已完成的搜索线程"""
        if hasattr(self, 'search_threads') and thread in self.search_threads:
            self.search_threads.remove(thread)

    def on_text_search_done(self, js_code):
        """接收子线程传回的数据并在浏览器中渲染"""
        self.browser.page().runJavaScript(js_code)

    def force_end_path(self):
        self.browser.page().runJavaScript("generatePath();")
        self.log_area.append("🛤️ 正在解析节点，生成沿途路段...")

    def handle_search_convert(self, lat, lng, name):
        items = ["配送中心", "受灾点"]
        dialog = ChineseInputDialog("添加搜索点", f"请问将【{name}】设为：", items, self)
        if dialog.exec():
            item = dialog.get_selected_item()
            if "配送中心" in item:
                self.add_point_to_data("depot", (lat, lng), {'demand': 0, 'pop': 0, 'area': 0, 'imp': 0})
            else:
                self.add_point_to_data("customer", (lat, lng), {'demand': 1.0, 'pop': 500, 'area': 1000, 'imp': 2})
            self.highlight_location(lat, lng)

    def refresh_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.depots) + len(self.customers))

        for i, d in enumerate(self.depots):
            self.table.setCellWidget(i, 0, self.create_centered_checkbox(d['active'], i))
            id_item = QTableWidgetItem(f"配送中心{i + 1}")
            id_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)  # 开启多选
            name_item = QTableWidgetItem(d.get('name', '未命名'))
            pos_item = QTableWidgetItem(f"({d['pos'][0]:.4f}, {d['pos'][1]:.4f})")
            pos_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)  # 开启多选

            for col, it in enumerate([id_item, name_item, pos_item]):
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(i, col + 1, it)

            for col in range(4, 8):
                it = QTableWidgetItem("-")
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)  # 让空白单元格也能被框选
                self.table.setItem(i, col, it)

        offset = len(self.depots)
        for i, c in enumerate(self.customers):
            row_idx = offset + i
            self.table.setCellWidget(row_idx, 0, self.create_centered_checkbox(c['active'], row_idx))
            id_item = QTableWidgetItem(f"受灾点{i + 1}")
            id_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            pos_item = QTableWidgetItem(f"({c['pos'][0]:.4f}, {c['pos'][1]:.4f})")
            pos_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

            name_item = QTableWidgetItem(c.get('name', '未命名'))
            dem = QTableWidgetItem(str(c.get('demand', 0)))

            # 支持人口字段为空的渲染
            pop_val = c.get('pop', 0)
            pop = QTableWidgetItem(str(pop_val) if pop_val != "" else "")

            area = QTableWidgetItem(str(c.get('area', 0)))

            for col, it in enumerate([id_item, name_item, pos_item, dem, pop, area]):
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row_idx, col + 1, it)

            self.table.setCellWidget(row_idx, 7, self.create_importance_combo(c.get('imp', 2), row_idx))

        self.table.blockSignals(False)

    def request_traffic_bounds(self):
        """点击按钮：指令前端去拿当前屏幕的边框坐标"""
        self.log_area.append("🟢 正在计算当前地图视野范围...")
        self.browser.page().runJavaScript("fetchRealtimeTraffic();")
        # 开启 5分钟 (300000ms) 的循环定时器
        if not self.traffic_timer.isActive():
            self.traffic_timer.start(300000)

    def handle_traffic_bounds(self, sw_lat, sw_lng, ne_lat, ne_lng):
        """核心算法：如果超过 10km，进行智能网格化分割，最多支持并发 5 个请求"""
        lat_span = ne_lat - sw_lat
        lng_span = ne_lng - sw_lng

        # 寻找最优的切分网格 (M x N <= 5)，保证切分后每个小格子的对角线 < 9.5km
        valid_grids = []
        for r in range(1, 7):
            for c in range(1, 7):
                if r * c <= 8:
                    d_lat = lat_span / r
                    d_lng = lng_span / c
                    # 计算小网格的对角线长度 (预留0.5km安全边界)
                    diag = ox_dist.great_circle(sw_lat, sw_lng, sw_lat + d_lat, sw_lng + d_lng)
                    if diag < 9500:
                        valid_grids.append((r * c, r, c))

        if not valid_grids:
            self.show_chinese_warning("界面范围过大", "当前地图视野过大，不支持显示全部路况。\n请【放大地图】缩小视野后再试！")
            self.log_area.append("❌ 视野超限，为保护并发配额已终止请求。")
            return

        self.road_conditions = [rc for rc in self.road_conditions if not rc.get('is_auto_traffic', False)]
        self.refresh_road_table()

        # 按请求次数最少排序，取出最佳方案
        valid_grids.sort()
        chunks, best_r, best_c = valid_grids[0]
        self.log_area.append(f"🟢 视野验证通过！正在将界面切分为 {best_r}x{best_c} (共{chunks}个网格) 并发请求...")

        self.realtime_traffic_data = []  # 清空总缓存
        self.traffic_threads = []  # 收集线程引用防回收
        self.browser.page().runJavaScript(
            "if(window.trafficLines) { window.trafficLines.forEach(l => map.removeLayer(l)); } window.trafficLines = [];")

        d_lat = lat_span / best_r
        d_lng = lng_span / best_c

        # 循环创建网格并发起请求
        for r in range(best_r):
            for c in range(best_c):
                box_sw_lat = sw_lat + r * d_lat
                box_sw_lng = sw_lng + c * d_lng
                box_ne_lat = box_sw_lat + d_lat
                box_ne_lng = box_sw_lng + d_lng

                # 拼接高德规定的 rectangle 字符串
                bounds_str = f"{box_sw_lng:.6f},{box_sw_lat:.6f};{box_ne_lng:.6f},{box_ne_lat:.6f}"

                thread = TrafficDataFetchThread(AMAP_WEB_KEY, bounds_str)
                thread.result_ready.connect(self.on_traffic_chunk_ready)
                # 🌟 修复：严禁用 lambda 更新 UI，绑定到一个主线程的正规方法上
                thread.error_occurred.connect(self.on_traffic_error)
                self.traffic_threads.append(thread)
                thread.start()

    def on_traffic_error(self, err_msg):
        """专门接收子线程报错，安全更新 UI"""
        self.log_area.append(f"❌ 局部网格获取失败: {err_msg}")

    def on_traffic_chunk_ready(self, roads_str):
        """单块网格数据返回，直接画在地图上"""
        import json
        import math

        # --- 内部函数：高德(GCJ-02) 转 WGS-84 坐标系 ---
        def gcj02_to_wgs84(lng, lat):
            def transform_lat(lng, lat):
                ret = -100.0 + 2.0 * lng + 3.0 * lat + 0.2 * lat * lat + 0.1 * lng * lat + 0.2 * math.sqrt(abs(lng))
                ret += (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
                ret += (20.0 * math.sin(lat * math.pi) + 40.0 * math.sin(lat / 3.0 * math.pi)) * 2.0 / 3.0
                ret += (160.0 * math.sin(lat / 12.0 * math.pi) + 320 * math.sin(lat * math.pi / 30.0)) * 2.0 / 3.0
                return ret

            def transform_lng(lng, lat):
                ret = 300.0 + lng + 2.0 * lat + 0.1 * lng * lng + 0.1 * lng * lat + 0.1 * math.sqrt(abs(lng))
                ret += (20.0 * math.sin(6.0 * lng * math.pi) + 20.0 * math.sin(2.0 * lng * math.pi)) * 2.0 / 3.0
                ret += (20.0 * math.sin(lng * math.pi) + 40.0 * math.sin(lng / 3.0 * math.pi)) * 2.0 / 3.0
                ret += (150.0 * math.sin(lng / 12.0 * math.pi) + 300.0 * math.sin(lng / 30.0 * math.pi)) * 2.0 / 3.0
                return ret

            def out_of_china(lng, lat):
                return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)

            if out_of_china(lng, lat): return lng, lat
            dlat = transform_lat(lng - 105.0, lat - 35.0)
            dlng = transform_lng(lng - 105.0, lat - 35.0)
            radlat = lat / 180.0 * math.pi
            magic = math.sin(radlat)
            magic = 1 - 0.00669342162296594323 * magic * magic
            sqrtmagic = math.sqrt(magic)
            dlat = (dlat * 180.0) / ((6378245.0 * (1 - 0.00669342162296594323)) / (magic * sqrtmagic) * math.pi)
            dlng = (dlng * 180.0) / (6378245.0 / sqrtmagic * math.cos(radlat) * math.pi)
            mglat = lat + dlat
            mglng = lng + dlng
            return lng * 2 - mglng, lat * 2 - mglat

        # -----------------------------------------------

        try:
            roads = json.loads(roads_str)
        except Exception:
            roads = []

        if not roads:
            self.log_area.append("⚠️ 网格请求成功，但该区域当前 API 未返回任何道路数据。")
            return

        self.realtime_traffic_data.extend(roads)

        js_code = ""
        valid_count = 0

        # 严格按照高德官方定义映射颜色
        color_map = {
            0: '#00cc00',  # 0:未知 -> 绿色
            1: '#00cc00',  # 1:畅通 -> 绿色
            2: '#f0e12b',  # 2:缓行 -> 黄色
            3: '#e60000',  # 3:拥堵 -> 红色
            4: '#990000'  # 4:严重拥堵 -> 深红色
        }

        table_needs_refresh = False
        for road in roads:
            status = int(road.get('status', 0))

            # 彻底移除 if status <= 1: continue 的过滤逻辑，强制渲染所有返回的道路
            color = color_map.get(status, '#00cc00')

            road_name = road.get('name', '未命名道路')
            road_speed = road.get('speed', '未知')

            polyline_str = road.get('polyline', '')
            if not polyline_str: continue

            segments = polyline_str.split(';')
            coords = []
            for seg in segments:
                parts = seg.split(',')
                if len(parts) == 2:
                    raw_lng, raw_lat = float(parts[0]), float(parts[1])
                    wgs_lng, wgs_lat = gcj02_to_wgs84(raw_lng, raw_lat)
                    coords.append([wgs_lat, wgs_lng])

            if coords:
                valid_count += 1
                js_code += f"var pl = L.polyline({coords}, {{color: '{color}', weight: 5, opacity: 0.85}}).bindTooltip('{road_name} (时速: {road_speed}km/h)', {{sticky: true}}).addTo(map);\n"
                js_code += f"window.trafficLines.push(pl);\n"
                if status in [2, 3, 4]:
                    r_type = 'road_slow' if status == 2 else 'road_jam'
                    self.road_conditions.append({
                        'type': r_type,
                        'name': road_name,
                        'geom': coords,
                        'lat': coords[0][0],
                        'lng': coords[0][1],
                        'lat2': coords[-1][0],
                        'lng2': coords[-1][1],
                        'is_auto_traffic': True  # 打上自动标识，以便下次刷新时清理
                    })
                    table_needs_refresh = True

        if js_code:
            self.browser.page().runJavaScript(js_code)
            self.log_area.append(f"✅ 局部切片渲染完成：绘制 {valid_count} 条路段。")

        if table_needs_refresh:
            self.refresh_road_table()

    def hide_realtime_traffic(self):
        js_code = "if(window.trafficLines) { window.trafficLines.forEach(l => map.removeLayer(l)); window.trafficLines = []; }"
        self.browser.page().runJavaScript(js_code)
        self.realtime_traffic_data = []

        # 关闭交通时，停止定时器，并清除所有自动生成的表格数据
        self.traffic_timer.stop()
        self.road_conditions = [rc for rc in self.road_conditions if not rc.get('is_auto_traffic', False)]
        self.refresh_road_table()

        self.log_area.append("⚪ 已关闭实时交通并停止自动刷新，已从表格中清除动态路况。")

    def clear_all_roads(self):
        """清空道路信息系统所有数据，支持撤销"""
        if not self.road_conditions:
            self.log_area.append("⚠️ 道路表格已经是空的。")
            return

        # 弹出中文确认提示
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("清空确认")
        msg_box.setText("确认要清空道路信息系统中的所有数据吗？\n（包含打点与自动获取的路况，清空后可使用撤销功能恢复）")
        msg_box.setIcon(QMessageBox.Icon.Warning)
        btn_yes = msg_box.addButton("确认清空", QMessageBox.ButtonRole.YesRole)
        msg_box.addButton("取消", QMessageBox.ButtonRole.NoRole)
        msg_box.exec()

        if msg_box.clickedButton() == btn_yes:
            # 核心：将当前完整状态压入底层撤销栈
            self.push_state()

            # 若开启了实时路况，同步关闭前端的定时器和高德专用渲染图层
            self.browser.page().runJavaScript(
                "if(window.trafficLines) { window.trafficLines.forEach(l => map.removeLayer(l)); window.trafficLines = []; }")
            self.traffic_timer.stop()

            # 清空底层数据矩阵并刷新双端视图
            self.road_conditions.clear()
            self.refresh_road_table()
            self.sync_data_to_map()
            self.log_area.append("🗑️ 道路信息系统的所有内容已全部清空。")

    def handle_table_edit(self, item):
        row, col = item.row(), item.column()
        self.table.blockSignals(True)

        num_depots = len(self.depots)
        is_depot = row < num_depots
        idx = row if is_depot else row - num_depots
        target_dict = self.depots[idx] if is_depot else self.customers[idx]
        new_text = item.text().strip()

        try:
            if col == 2:  # 名称列逻辑判断
                old_name = target_dict.get('name', '')

                # 功能2：如果在表格里清空了名字（输入为空），自动触发重新获取！
                if new_text == '':
                    item.setText("正在获取...")
                    target_dict['name'] = '正在获取...'
                    thread = OfflineReverseGeocodeThread(idx, 'depot' if is_depot else 'customer',
                                                         target_dict['pos'][0], target_dict['pos'][1], self.G,
                                                         self.poi_gdf)
                    thread.result_ready.connect(self.on_reverse_geocode_done)

                    if not hasattr(self, 'geocode_threads'): self.geocode_threads = []
                    self.geocode_threads.append(thread)

                    thread.finished.connect(thread.deleteLater)
                    thread.finished.connect(lambda t=thread: self.cleanup_thread(t))

                    thread.start()
                elif new_text != old_name and new_text != '正在获取...':
                    item.setText("校验中...")
                    thread = OfflineGeocodeValidationThread(row, 'depot' if is_depot else 'customer', new_text,
                                                            old_name, self.G, self.poi_gdf)
                    thread.result_ready.connect(self.on_geocode_validation_done)
                    self.geocode_threads.append(thread)
                    thread.start()


            elif col in [4, 5, 6] and not is_depot:
                if new_text == "":
                    # 人口(5)允许为空字符串，需求(4)和面积(6)强制给0.0
                    val = "" if col == 5 else 0.0
                else:
                    val = float(new_text)
                    if val < 0: raise ValueError
                key = {4: 'demand', 5: 'pop', 6: 'area'}[col]
                if target_dict.get(key, 0) != val:
                    # 如果正在批量修改(如粘贴/清除)，不逐个格子触发撤销保存
                    if not getattr(self, '_is_batch_editing', False):
                        self.push_state()
                    target_dict[key] = val
                    target_dict['manual_edit'] = True  # 打锁
                # 如果用户触发清空，且不是人口列，强制将 UI 矫正为 0.0
                if new_text == "" and col != 5:
                    self.table.blockSignals(True)
                    item.setText("0.0")
                    self.table.blockSignals(False)
        except ValueError:
            self.show_chinese_warning("输入无效", "❌ 请输入有效的大于等于0的数字！")
            key = {4: 'demand', 5: 'pop', 6: 'area'}[col]
            item.setText(str(target_dict.get(key, 0)) if target_dict.get(key, 0) != "" else "")
        self.table.blockSignals(False)

    def on_geocode_validation_done(self, row_idx, p_type, original_name, is_valid, official_name, attempted_name):
        self.table.blockSignals(True)
        item = self.table.item(row_idx, 2)
        if is_valid:
            self.push_state()
            new_name = official_name
            if p_type == 'depot':
                self.depots[row_idx]['name'] = new_name
            else:
                self.customers[row_idx - len(self.depots)]['name'] = new_name
            item.setText(new_name)
        else:
            # 弹出中文询问框：是否强制保存？
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("地名未找到")
            msg_box.setText(f"本地地图库未找到地名【{attempted_name}】。\n是否强制保存该名称？")
            msg_box.setIcon(QMessageBox.Icon.Warning)
            btn_yes = msg_box.addButton("强制保存", QMessageBox.ButtonRole.YesRole)
            btn_no = msg_box.addButton("退回原名", QMessageBox.ButtonRole.NoRole)
            msg_box.exec()

            if msg_box.clickedButton() == btn_yes:
                self.push_state()
                if p_type == 'depot':
                    self.depots[row_idx]['name'] = attempted_name
                else:
                    self.customers[row_idx - len(self.depots)]['name'] = attempted_name
                item.setText(attempted_name)
            else:
                item.setText(original_name)

        self.table.blockSignals(False)
        self.sync_data_to_map()  # 强制保存后也需要刷新地图弹窗上的名字

    def delete_selected_rows(self):
        selected_rows = sorted([index.row() for index in self.table.selectionModel().selectedRows()], reverse=True)
        if not selected_rows: return
        self.push_state()
        for row in selected_rows:
            num_depots = len(self.depots)
            if row < num_depots:
                self.depots.pop(row)
            else:
                self.customers.pop(row - num_depots)
        self.refresh_table()
        self.sync_data_to_map()

    def refresh_road_table(self):
        self.road_table.setRowCount(len(self.road_conditions))
        type_map = {'road_block': '❌ 无法通行', 'road_jam': '🔴 交通堵塞',
                    'road_slow': '🟡 通行缓慢', 'road_control': '⚪ 临时管控', 'road_special': '🟣 特殊路段'}

        for i, rc in enumerate(self.road_conditions):
            # 1. 确保所有文本内容绝对居中
            item_type = QTableWidgetItem(type_map.get(rc['type'], '未知'))
            item_type.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.road_table.setItem(i, 0, item_type)

            item_name = QTableWidgetItem(rc.get('name', '未知路段'))
            item_name.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.road_table.setItem(i, 1, item_name)

            if rc['type'] == 'road_block':
                coord_str = f"中断点:\n({rc['lat']:.5f}, {rc['lng']:.5f})"
            else:
                coord_str = f"起: ({rc['lat']:.5f}, {rc['lng']:.5f})\n终: ({rc.get('lat2', rc['lat']):.5f}, {rc.get('lng2', rc['lng']):.5f})"

            item_coord = QTableWidgetItem(coord_str)
            item_coord.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.road_table.setItem(i, 2, item_coord)

            # 2. 构建居中的双按钮布局
            btn_widget = QWidget()
            btn_layout = QHBoxLayout(btn_widget)
            btn_layout.setContentsMargins(2, 2, 2, 2)
            btn_layout.setSpacing(6)
            btn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)  # 保证按钮居中

            # 🌟 优化点1：加入 Qt.FocusPolicy.NoFocus，彻底消除点击时表格重绘导致的UI粘滞感
            btn_jump = QPushButton("跳转")
            btn_jump.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_jump.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn_jump.clicked.connect(lambda _, row=i: self.jump_to_road(row))

            btn_del = QPushButton("删除")
            btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_del.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn_del.clicked.connect(lambda _, row=i: self.delete_road_condition(row))

            btn_layout.addWidget(btn_jump)
            btn_layout.addWidget(btn_del)
            self.road_table.setCellWidget(i, 3, btn_widget)

        self.road_table.resizeRowsToContents()

    def jump_to_road(self, row):
        if row < len(self.road_conditions):
            rc = self.road_conditions[row]
            lat = rc.get('lat')
            lng = rc.get('lng')
            if lat and lng:
                # 🌟 终极修复：废除多余的 QTimer 避免多重单线程阻塞死锁。
                # 加入 setTimeout + invalidateSize(true) 强制浏览器引擎重算 WebGL 图层矩阵，彻底杜绝地图加载白屏。
                js_code = f"""
                if (typeof map !== 'undefined') {{
                    map.setView([{lat}, {lng}], 16, {{animate: false}});
                    setTimeout(function() {{
                        map.invalidateSize(true);
                    }}, 200);
                }}
                """
                self.browser.page().runJavaScript(js_code)

    def delete_road_condition(self, row):
        self.push_state()
        self.road_conditions.pop(row)
        self.refresh_road_table()
        self.sync_data_to_map()

    def push_state(self, action_type=None, action_data=None):
        self.redo_stack.clear()
        state = {
            'depots': copy.deepcopy(self.depots),
            'customers': copy.deepcopy(self.customers),
            'road_conditions': copy.deepcopy(self.road_conditions),
            'algo_params': copy.deepcopy(self.algo_params),
            'action_type': action_type,
            'action_data': copy.deepcopy(action_data)
        }
        self.undo_stack.append(state)
        if len(self.undo_stack) > 30: self.undo_stack.pop(0)

    def is_valid_coordinate(self, lat, lng):
        return 21.60 <= lat <= 22.65 and 112.85 <= lng <= 114.52

    def undo(self):
        self.browser.page().runJavaScript("tryUndoTempMarker();", self.process_undo)

    def process_undo(self, js_result):
        if js_result:
            self.log_area.append("↩️ 已撤销最后一个临时坐标打点。")
        else:
            if not self.undo_stack:
                self.log_area.append("⚠️ 已经没有可以撤销的操作了。")
                return

            # 🔁 撤销前，将状态压入 redo 栈。特殊标记：如果我们正在撤销“生成路径”，通知 redo 栈未来恢复时清空打点。
            is_reverting_path = (self.undo_stack[-1].get('action_type') == 'generate_path')
            current_state = {
                'depots': copy.deepcopy(self.depots),
                'customers': copy.deepcopy(self.customers),
                'road_conditions': copy.deepcopy(self.road_conditions),
                'algo_params': copy.deepcopy(self.algo_params),
                'action_type': 'revert_generate_path' if is_reverting_path else None
            }
            self.redo_stack.append(current_state)

            state = self.undo_stack.pop()

            self.depots = state['depots']
            self.customers = state['customers']
            self.road_conditions = state['road_conditions']

            if 'algo_params' in state:
                self.algo_params = state['algo_params']
                for key, sp in self.param_spinboxes.items():
                    sp.blockSignals(True)
                    if isinstance(sp, QSpinBox): sp.setValue(int(self.algo_params[key]))
                    else: sp.setValue(float(self.algo_params[key]))
                    sp.blockSignals(False)

            self.refresh_table()
            self.refresh_road_table()
            self.sync_data_to_map()

            # 🌟 核心修复：确保在地图完全清理 (sync_data_to_map) 后，再通过 setTimeout 延迟重绘消失的坐标点！
            if state.get('action_type') == 'generate_path':
                coords = state.get('action_data', {}).get('coords', [])
                mode = state.get('action_data', {}).get('mode', 'none')

                js_code = f"restoreTempMarkers({json.dumps(coords)}, '{mode}');"
                self.browser.page().runJavaScript(js_code)
                self.log_area.append("↩️ 已撤销生成的路径，并恢复原始坐标打点状态。")
            else:
                self.log_area.append("↩️ 已撤销上一步操作，状态已还原。")

    def redo(self):
        if not self.redo_stack:
            self.log_area.append("⚠️ 已经没有可以取消撤销的操作了。")
            return

        is_redoing_path = (self.redo_stack[-1].get('action_type') == 'revert_generate_path')
        current_state = {
            'depots': copy.deepcopy(self.depots),
            'customers': copy.deepcopy(self.customers),
            'road_conditions': copy.deepcopy(self.road_conditions),
            'algo_params': copy.deepcopy(self.algo_params),
            'action_type': 'generate_path' if is_redoing_path else None
        }
        self.undo_stack.append(current_state)

        state = self.redo_stack.pop()

        self.depots = state['depots']
        self.customers = state['customers']
        self.road_conditions = state['road_conditions']

        if 'algo_params' in state:
            self.algo_params = state['algo_params']
            for key, sp in self.param_spinboxes.items():
                sp.blockSignals(True)
                if isinstance(sp, QSpinBox): sp.setValue(int(self.algo_params[key]))
                else: sp.setValue(float(self.algo_params[key]))
                sp.blockSignals(False)

        self.refresh_table()
        self.refresh_road_table()
        self.sync_data_to_map()

        # 🌟 核心修复：如果我们“取消撤销”回到了生成路段的状态，确保把界面上残留的打点清理掉
        if state.get('action_type') == 'revert_generate_path':
            self.browser.page().runJavaScript("clearTempRoadMarkers();")
            self.log_area.append("🔁 已取消撤销，路径重新生成，清理坐标打点。")
        else:
            self.log_area.append("🔁 已取消撤销，恢复至下一步状态。")

    def add_point_to_data(self, p_type, pos, data):
        self.push_state()
        if p_type == "depot":
            self.depots.append({'pos': pos, 'active': 1})
        else:
            self.customers.append({'pos': pos, 'demand': data.get('demand', 1.0), 'active': 1,
                                   'pop': data.get('pop', 0), 'area': data.get('area', 0), 'imp': data.get('imp', 2)})
        self.refresh_table()
        self.sync_data_to_map()

    def handle_map_delete(self, index, p_type):
        self.push_state()
        if p_type == "depot":
            self.depots.pop(index)
        else:
            self.customers.pop(index)
        self.log_area.append(f"🗑️ 已通过地图删除 {'配送中心' if p_type == 'depot' else '受灾点'}{index + 1}")
        self.refresh_table()
        self.sync_data_to_map()

    def update_point_position(self, p_type, index, new_pos):
        lat, lng = new_pos
        if not self.is_valid_coordinate(lat, lng):
            self.show_chinese_warning("越界警告", "❌ 移动的坐标超出系统支持的地理范围，已自动弹回原位！")
            self.sync_data_to_map()
            return
        self.push_state()
        if p_type == "depot":
            self.depots[index]['pos'] = new_pos
        else:
            self.customers[index]['pos'] = new_pos
        self.refresh_table()
        self.sync_data_to_map()

    def handle_map_add(self, lat, lng, mode):
        if not self.is_valid_coordinate(lat, lng):
            self.show_chinese_warning("越界警告", "❌ 坐标超出系统支持的地理范围，打点无效！")
            return
        if mode in ["depot", "customer"]:
            data = {'demand': round(random.uniform(0.5, 2.0), 1), 'pop': random.randint(100, 2000),
                    'area': random.randint(500, 5000), 'imp': random.randint(1, 5)} if mode == "customer" else {}

            self.push_state()
            new_data = {'pos': (lat, lng), 'active': 1, 'name': '正在获取...'}
            new_data.update(data)

            idx = len(self.depots) if mode == "depot" else len(self.customers)
            if mode == "depot":
                self.depots.append(new_data)
            else:
                self.customers.append(new_data)

            self.refresh_table()
            self.sync_data_to_map()

            # 功能3：新增点时，自动补全名称
            thread = OfflineReverseGeocodeThread(idx, mode, lat, lng, self.G, self.poi_gdf)
            thread.result_ready.connect(self.on_reverse_geocode_done)

            if not hasattr(self, 'geocode_threads'): self.geocode_threads = []
            self.geocode_threads.append(thread)

            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(lambda t=thread: self.cleanup_thread(t))

            thread.start()

    def on_reverse_geocode_done(self, index, p_type, res_data):
        name = res_data.get('name', '').strip()
        final_name = name if name else (f"配送中心{index + 1}" if p_type == 'depot' else f"受灾点{index + 1}")

        if p_type == 'depot' and index < len(self.depots):
            self.depots[index]['name'] = final_name
        elif p_type == 'customer' and index < len(self.customers):
            c = self.customers[index]
            old_name = c.get('name', '')

            # 【智能解锁】：如果解析出了新地名，且与旧地名不同，说明地点变了，直接解除手动锁！
            if old_name not in ['', '正在获取...'] and old_name != final_name:
                c['manual_edit'] = False

            # 名字覆盖
            if old_name in ['', '正在获取...'] or old_name != final_name:
                c['name'] = final_name

            # 如果没锁（或者是刚被解锁），就用新的推演数据覆盖面积、人口、等级
            if not c.get('manual_edit', False):
                if res_data.get('area', 0) > 0 or c.get('area', 0) == 0:
                    c['area'] = res_data.get('area', 0)
                if res_data.get('imp', 2) != 2 or c.get('imp', 2) == 2:
                    c['imp'] = res_data.get('imp', 2)

        self.refresh_table()
        self.sync_data_to_map()

    def handle_road_block(self, lat, lng):
        try:
            u, v, key = ox_dist.nearest_edges(self.G, lng, lat)
            edge_data = self.G.get_edge_data(u, v, key)
            if 'geometry' in edge_data:
                line = edge_data['geometry']
            else:
                line = LineString([(self.G.nodes[u]['x'], self.G.nodes[u]['y']),
                                   (self.G.nodes[v]['x'], self.G.nodes[v]['y'])])

            p = Point(lng, lat)
            proj_dist = line.project(p)
            proj_p = line.interpolate(proj_dist)
            rc_lat, rc_lng = proj_p.y, proj_p.x

            dist = ox_dist.great_circle(lat, lng, rc_lat, rc_lng)
            if dist > 100:
                self.show_chinese_warning("警告", "选取的标点距离道路过远，请选择在道路内！")
                return

            self.push_state()
            self.road_conditions.append({
                'type': 'road_block', 'lat': rc_lat, 'lng': rc_lng,
                'name': '道路中断', 'geom': []
            })
            self.log_area.append(f"🚧 成功打点：无法通行 (准确坐标: {rc_lat:.4f}, {rc_lng:.4f})")
            self.refresh_road_table()
            self.sync_data_to_map()
        except Exception:
            self.log_area.append(f"❌ 阻断打点失败，请稍微放大地图重试。")

    def handle_road_block_move(self, index, lat, lng):
        try:
            u, v, key = ox_dist.nearest_edges(self.G, lng, lat)
            edge_data = self.G.get_edge_data(u, v, key)

            if 'geometry' in edge_data:
                line = edge_data['geometry']
            else:
                line = LineString([(self.G.nodes[u]['x'], self.G.nodes[u]['y']),
                                   (self.G.nodes[v]['x'], self.G.nodes[v]['y'])])

            p = Point(lng, lat)
            proj_dist = line.project(p)
            proj_p = line.interpolate(proj_dist)
            rc_lat, rc_lng = proj_p.y, proj_p.x

            dist = ox_dist.great_circle(lat, lng, rc_lat, rc_lng)
            if dist > 100:
                self.show_chinese_warning("提示", "请确保至少勾选了一个配送中心和一个受灾点！")
                self.sync_data_to_map()
                return

            self.push_state()
            self.road_conditions[index]['lat'] = rc_lat
            self.road_conditions[index]['lng'] = rc_lng

            self.refresh_road_table()
            self.sync_data_to_map()
            self.log_area.append(f"🚧 阻断点已移动并自动吸附至: ({rc_lat:.4f}, {rc_lng:.4f})")
        except Exception:
            self.log_area.append(f"❌ 移动阻断点失败，已重置。")
            self.sync_data_to_map()

    def handle_complex_road_segment(self, coords_json, mode):
        """主线程调度：将路径生成的脏活累活甩给后台线程，自身保持不卡顿"""
        try:
            coords = json.loads(coords_json)
            if len(coords) < 2: return
            self.push_state(action_type="generate_path", action_data={"coords": coords, "mode": mode})

            self.log_area.append("🛤️ 正在后台为您计算连续路段，请稍候...")

            # 开启后台计算线程
            self.path_thread = PathGenerationThread(coords, mode, self.G)
            self.path_thread.result_ready.connect(self.on_path_generated)
            self.path_thread.error_occurred.connect(lambda e: self.log_area.append(f"❌ 路径生成异常：{e}"))
            self.path_thread.start()
        except Exception as e:
            self.log_area.append(f"❌ 路径生成启动异常：{str(e)}")

    def on_path_generated(self, full_path_geom, final_road_name, mode):
        """后台线程跑完路径后，安全地通知主线程进行 UI 渲染"""
        if full_path_geom:
            self.road_conditions.append({
                'type': mode,
                'name': final_road_name,
                'geom': full_path_geom,
                'lat': full_path_geom[0][0],
                'lng': full_path_geom[0][1],
                'lat2': full_path_geom[-1][0],
                'lng2': full_path_geom[-1][1]
            })
            self.log_area.append("✅ 连续路段已生成 (无视单向限制，0偏差吸附)！")
            self.refresh_road_table()
            self.sync_data_to_map()
        else:
            self.log_area.append("❌ 未能识别出有效的连续路段。")

    def handle_map_move(self, index, lat, lng, p_type):
        if not self.is_valid_coordinate(lat, lng):
            self.show_chinese_warning("越界警告", "❌ 移动的坐标超出系统支持的地理范围，已自动弹回原位！")
            self.sync_data_to_map()
            return

        self.push_state()
        if p_type == "depot":
            self.depots[index]['pos'] = (lat, lng)
            self.depots[index]['name'] = '正在获取...'
        else:
            self.customers[index]['pos'] = (lat, lng)
            self.customers[index]['name'] = '正在获取...'

        self.refresh_table()
        self.sync_data_to_map()

        # 触发名称重新获取
        thread = OfflineReverseGeocodeThread(index, p_type, lat, lng, self.G, self.poi_gdf)
        thread.result_ready.connect(self.on_reverse_geocode_done)

        if not hasattr(self, 'geocode_threads'): self.geocode_threads = []
        self.geocode_threads.append(thread)

        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda t=thread: self.cleanup_thread(t))

        thread.start()

        name = f"配送中心{index + 1}" if p_type == "depot" else f"受灾点{index + 1}"
        self.log_area.append(f"📍 {name} 已移动至新坐标: ({lat:.6f}, {lng:.6f})，正在解析新地名...")

    def change_mode(self, mode):
        buttons = [self.btn_add_depot, self.btn_add_cust, self.btn_road_block, self.btn_road_jam, self.btn_road_obs,
                   self.btn_road_ctrl, self.btn_road_spec]
        target_btn = None
        if mode == "depot":
            target_btn = self.btn_add_depot
        elif mode == "customer":
            target_btn = self.btn_add_cust
        elif mode == "road_block":
            target_btn = self.btn_road_block
        elif mode == "road_jam":
            target_btn = self.btn_road_jam
        elif mode == "road_slow":
            target_btn = self.btn_road_obs
        elif mode == "road_control":
            target_btn = self.btn_road_ctrl
        elif mode == "road_special":
            target_btn = self.btn_road_spec

        for btn in buttons:
            if btn != target_btn: btn.setChecked(False)

        final_mode = mode if (target_btn and target_btn.isChecked()) else "none"
        self.browser.page().runJavaScript(f"setMode('{final_mode}')")

    def sync_data_to_map(self):
        self.browser.page().runJavaScript("clearMarkers(); clearRoadConditions();")

        for i, d in enumerate(self.depots):
            id_str = f"配送中心{i + 1}"
            name_str = d.get('name', '未命名')
            self.browser.page().runJavaScript(
                f"addMarker({d['pos'][0]}, {d['pos'][1]}, '{id_str}', '{name_str}', 'red', {i}, 'depot', {d['active']}, 0, 0, 0, 0)")

        for i, c in enumerate(self.customers):
            id_str = f"受灾点{i + 1}"
            name_str = c.get('name', '未命名')
            self.browser.page().runJavaScript(
                f"addMarker({c['pos'][0]}, {c['pos'][1]}, '{id_str}', '{name_str}', 'blue', {i}, 'customer', {c['active']}, {c.get('demand', 0)}, {c.get('pop', 0)}, {c.get('area', 0)}, {c.get('imp', 2)})")

        for i, rc in enumerate(self.road_conditions):
            if rc['type'] == 'road_block':
                data_str = json.dumps([rc['lat'], rc['lng']])
            else:
                data_str = json.dumps(rc.get('geom', []))
            self.browser.page().runJavaScript(f"drawRoadCondition('{rc['type']}', '{data_str}', {i});")

    def run_optimization(self):
        active_depots = [d for d in self.depots if d['active']]
        active_custs = [c for c in self.customers if c['active']]

        if not active_depots or not active_custs:
            QMessageBox.warning(self, "提示", "请确保至少勾选了一个配送中心和一个受灾点！")
            return

        self.btn_run.setEnabled(False)
        self.log_area.append(f"⏳ 寻优开始：{len(active_depots)}中心, {len(active_custs)}受灾点...")

        params = {
            'CAPACITY': 8, 'DISTANCE': 30000, 'C0': 100, 'C1': 1,
            **self.algo_params
        }
        self.worker = MD_CVRP_Worker(self.G, active_depots, active_custs, params)
        self.worker.progress_output.connect(lambda m: self.log_area.append(m))
        self.worker.calculation_done.connect(self.on_calc_finished)
        self.worker.start()

    def on_calc_finished(self, result):
        self.btn_run.setEnabled(True)
        self.log_area.append(f"✨ 寻优成功！最优成本: {result['gBest']:.1f}")
        self.draw_result_routes(result['gLine_car'], result['path_matrix'])
        self.update_filter_ui()
        self.refresh_map_display()

    def draw_result_routes(self, routes, path_matrix):
        self.all_routes_geometry = []
        for car_idx, route in enumerate(routes):
            full_car_geometry = []
            for k in range(len(route) - 1):
                u_idx = route[k]
                v_idx = route[k + 1]

                path_nodes = path_matrix.get(f"{u_idx}_{v_idx}", [])
                if not path_nodes: continue

                for n1, n2 in zip(path_nodes[:-1], path_nodes[1:]):
                    edge_data_dict = self.G.get_edge_data(n1, n2)
                    if edge_data_dict:
                        edge_data = None
                        for data in edge_data_dict.values():
                            if 'geometry' in data:
                                edge_data = data
                                break
                        if not edge_data: edge_data = list(edge_data_dict.values())[0]

                        if 'geometry' in edge_data:
                            coords = list(edge_data['geometry'].coords)
                            n1_lon, n1_lat = self.G.nodes[n1]['x'], self.G.nodes[n1]['y']
                            dist_to_start = (coords[0][0] - n1_lon) ** 2 + (coords[0][1] - n1_lat) ** 2
                            dist_to_end = (coords[-1][0] - n1_lon) ** 2 + (coords[-1][1] - n1_lat) ** 2
                            if dist_to_end < dist_to_start: coords.reverse()
                            full_car_geometry.extend([[lat, lon] for lon, lat in coords])
                        else:
                            full_car_geometry.append([self.G.nodes[n1]['y'], self.G.nodes[n1]['x']])
                            full_car_geometry.append([self.G.nodes[n2]['y'], self.G.nodes[n2]['x']])
            self.all_routes_geometry.append(full_car_geometry)

    def update_filter_ui(self):
        self.filter_group.setVisible(True)
        for i in reversed(range(self.filter_layout.count())):
            self.filter_layout.itemAt(i).widget().setParent(None)

        for i in range(len(self.all_routes_geometry)):
            cb = QCheckBox(f"车辆 {i + 1} 路径轨迹")
            cb.setChecked(True)
            cb.stateChanged.connect(self.refresh_map_display)
            self.filter_layout.addWidget(cb)

    def refresh_map_display(self):
        colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2']
        js_clear = "if(window.lines) { window.lines.forEach(l => map.removeLayer(l)); } window.lines = [];"
        self.browser.page().runJavaScript(js_clear)

        for i in range(self.filter_layout.count()):
            cb = self.filter_layout.itemAt(i).widget()
            if cb.isChecked() and i < len(self.all_routes_geometry):
                path_data = json.dumps(self.all_routes_geometry[i])
                color = colors[i % len(colors)]
                js_draw = f"var poly = L.polyline({path_data}, {{color: '{color}', weight: 6, opacity: 0.85, dashArray: '10, 8', lineJoin: 'round'}}).addTo(map); window.lines.push(poly);"
                self.browser.page().runJavaScript(js_draw)

    def manual_save(self):
        self.db.save_data(self.depots, self.customers, self.road_conditions, self.algo_params)
        self.log_area.append("💾 数据状态及算法配置已同步至本地数据库。")

    def check_data_changed(self):
        db_depots, db_customers, db_roads, db_params = self.db.load_data()

        if self.algo_params != db_params: return True

        if len(self.depots) != len(db_depots): return True
        for curr, db_d in zip(self.depots, db_depots):
            if round(curr['pos'][0], 5) != round(db_d['pos'][0], 5): return True
            if round(curr['pos'][1], 5) != round(db_d['pos'][1], 5): return True
            if curr.get('active', 1) != db_d.get('active', 1): return True
            if curr.get('name', '') != db_d.get('name', ''): return True

        if len(self.customers) != len(db_customers): return True
        for curr, db_c in zip(self.customers, db_customers):
            if round(curr['pos'][0], 5) != round(db_c['pos'][0], 5): return True
            if round(curr['pos'][1], 5) != round(db_c['pos'][1], 5): return True
            if curr.get('active', 1) != db_c.get('active', 1): return True
            if curr.get('name', '') != db_c.get('name', ''): return True
            if curr.get('demand', 0) != db_c.get('demand', 0): return True
            if curr.get('pop', 0) != db_c.get('pop', 0): return True
            if curr.get('area', 0) != db_c.get('area', 0): return True
            if curr.get('imp', 2) != db_c.get('imp', 2): return True

        if len(self.road_conditions) != len(db_roads): return True
        for curr, db_r in zip(self.road_conditions, db_roads):
            if curr['type'] != db_r['type']: return True
            if round(curr['lat'], 5) != round(db_r['lat'], 5): return True
            if round(curr['lng'], 5) != round(db_r['lng'], 5): return True
            if curr.get('name', '') != db_r.get('name', ''): return True

        return False

    def closeEvent(self, event: QCloseEvent):
        """窗口关闭拦截：智能保存与语音提示验证"""
        if self.check_data_changed():
            try:
                from PyQt6.QtTextToSpeech import QTextToSpeech
                if not hasattr(self, 'tts_engine'):
                    self.tts_engine = QTextToSpeech(self)
                    for loc in self.tts_engine.availableLocales():
                        if loc.name().startswith('zh'):
                            self.tts_engine.setLocale(loc)
                            break
                self.tts_engine.say("当前信息与数据库有变动，是否保存？")
            except Exception as e:
                print(f"语音播报初始化失败，走静默流: {e}")

            # ======= 修改为纯中文的选项框 ======
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("保存提示")
            msg_box.setText("当前信息与数据库有变动，是否保存？")

            # 使用 addButton 强制设为中文按钮文本
            btn_yes = msg_box.addButton("是", QMessageBox.ButtonRole.YesRole)
            btn_no = msg_box.addButton("否", QMessageBox.ButtonRole.NoRole)
            btn_cancel = msg_box.addButton("取消", QMessageBox.ButtonRole.RejectRole)

            msg_box.exec()

            if msg_box.clickedButton() == btn_yes:
                self.manual_save()
                if hasattr(self, 'tts_engine'): self.tts_engine.stop()
                event.accept()
            elif msg_box.clickedButton() == btn_no:
                if hasattr(self, 'tts_engine'): self.tts_engine.stop()
                event.accept()
            else:
                if hasattr(self, 'tts_engine'): self.tts_engine.stop()
                event.ignore()
        else:
            event.accept()

    def show_table_context_menu(self, pos, table):
        """生成并显示右键菜单"""
        menu = QMenu(self)
        copy_action = menu.addAction("复制")
        paste_action = menu.addAction("粘贴")
        clear_action = menu.addAction("清除内容")

        action = menu.exec(table.viewport().mapToGlobal(pos))
        if not action:
            return

        if action == copy_action:
            self.copy_table_selection(table)
        elif action == paste_action:
            self.paste_table_selection(table)
        elif action == clear_action:
            self.clear_table_selection(table)

    def copy_table_selection(self, table):
        """表格多选复制逻辑，兼容 Excel 标准换行/制表符"""
        selection = table.selectedIndexes()
        if not selection: return

        row_data = {}
        for index in selection:
            # 取数，对空值进行防崩处理
            row_data.setdefault(index.row(), {})[index.column()] = index.data() or ""

        lines = []
        for r in sorted(row_data.keys()):
            cols = row_data[r]
            # 按列排序并用 \t 组合
            line = "\t".join(str(cols.get(c, "")) for c in sorted(cols.keys()))
            lines.append(line)

        QApplication.clipboard().setText("\n".join(lines))

    def paste_table_selection(self, table):
        """表格智能粘贴，支持批量撤销与安全写入"""
        text = QApplication.clipboard().text()
        if not text: return
        selection = table.selectedIndexes()
        if not selection: return

        self.push_state()  # 记录修改前的状态供撤销使用
        self._is_batch_editing = True  # 开启批量锁，防止底层联动疯狂弹窗与保存栈溢出

        try:
            start_row = min(index.row() for index in selection)
            start_col = min(index.column() for index in selection)

            lines = text.strip('\n').split('\n')
            for i, line in enumerate(lines):
                cells = line.split('\t')
                for j, cell_text in enumerate(cells):
                    target_row = start_row + i
                    target_col = start_col + j
                    if target_row < table.rowCount() and target_col < table.columnCount():
                        item = table.item(target_row, target_col)
                        if item:
                            item.setText(cell_text)
                        else:
                            new_item = QTableWidgetItem(cell_text)
                            new_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                            table.setItem(target_row, target_col, new_item)
        finally:
            self._is_batch_editing = False

    def clear_table_selection(self, table):
        """清空选中的单元格内容，支持批量撤销"""
        selection = table.selectedIndexes()
        if not selection: return

        self.push_state()  # 将即将进行的清空动作统一存入撤销栈
        self._is_batch_editing = True  # 开启批量锁

        try:
            for index in selection:
                item = table.item(index.row(), index.column())
                if item:
                    item.setText("")
        finally:
            self._is_batch_editing = False