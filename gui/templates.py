HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <link rel="stylesheet" href="leaflet/leaflet.css" />
    <script src="leaflet/leaflet.js"></script>
    <script src="qrc:///qtwebchannel/qwebchannel.js"></script>
    <style>
        #map { height: 100vh; width: 100%; margin: 0; }
        .compact-label { 
            background: rgba(255,255,255,0.9); 
            border: 1px solid #999; border-radius: 3px;
            padding: 1px 4px; font-size: 11px; color: #333;
            box-shadow: 0 1px 3px rgba(0,0,0,0.2); margin-top: -30px; 
        }
        .search-container {
            position: absolute; top: 15px; left: 50px; z-index: 1000;
            background: white; border-radius: 4px; box-shadow: 0 2px 5px rgba(0,0,0,0.3);
            display: flex; width: 550px; /* 增加宽度 */
        }
        .search-input { flex-grow: 1; padding: 10px; border: none; outline: none; border-radius: 4px 0 0 4px; font-size: 14px; }
        .search-btn { background: #ffffff; border: none; padding: 0 12px; cursor: pointer; border-radius: 0 4px 4px 0; border-left: 1px solid #eee; transition: background 0.2s; }
        .search-btn:hover { background: #f0f0f0; }
        .search-results {
            position: absolute; top: 100%; left: 0; width: 100%;
            max-height: 300px; overflow-y: auto; display: none;
            background: white; border-top: 1px solid #eee; border-radius: 0 0 4px 4px; box-shadow: 0 2px 5px rgba(0,0,0,0.3);
        }
        .search-item { padding: 10px; cursor: pointer; border-bottom: 1px solid #f5f5f5; font-size: 13px; color: #555; }
        .search-item:hover { background: #f0f8ff; color: #000; }
        .map-legend {
            position: absolute; bottom: 20px; right: 20px; z-index: 1000;
            background: rgba(255, 255, 255, 0.95); padding: 12px; border-radius: 5px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.3); font-size: 12px; line-height: 1.8; color: #333;
        }
        .legend-item { display: flex; align-items: center; margin-bottom: 4px; }
        .legend-color { width: 25px; height: 4px; display: inline-block; margin-right: 8px; border-radius: 2px;}
        .road-drawing-mode { cursor: crosshair !important; }
    </style>
</head>
<body>
    <div id="map"></div>
    <div class="search-container" style="top: 15px; left: 50px; width: 350px;">
        <input type="text" id="search-input" class="search-input" placeholder="输入地点名称...">
        <button id="search-btn" class="search-btn">🔍</button>
        <div id="search-results" class="search-results"></div>
    </div>

    <div class="search-container" style="top: 15px; left: 430px; width: 350px;">
        <input type="text" id="coord-input" class="search-input" placeholder="请输入坐标…(示例:22.2589,113.5455)">
        <button id="coord-btn" class="search-btn">🌐</button>
        <div id="coord-search-results" class="search-results"></div>
    </div>
    <div class="map-legend">
        <div style="font-weight: bold; margin-bottom: 5px; border-bottom: 1px solid #ddd; padding-bottom: 3px;">图例说明</div>
        <div class="legend-item"><span>📍</span><span style="color:red; margin-right:8px; font-weight:bold;">红</span> 配送中心</div>
        <div class="legend-item"><span>📍</span><span style="color:blue; margin-right:8px; font-weight:bold;">蓝</span> 受灾点</div>
        <div class="legend-item"><span style="font-size:14px; font-weight:bold; margin-right:8px; width:20px; text-align:center;">❌</span> 无法通行</div>
        <div class="legend-item"><div class="legend-color" style="background: red;"></div> 交通堵塞</div>
        <div class="legend-item"><div class="legend-color" style="background: yellow;"></div> 通行缓慢</div>
        <div class="legend-item"><div class="legend-color" style="background: gray;"></div> 临时管控</div>
        <div class="legend-item"><div class="legend-color" style="background: purple;"></div> 特殊路段</div>
    </div>

    <script>
        var bounds = L.latLngBounds([21.60, 112.85], [22.65, 114.52]);
        var map = L.map('map', { zoomControl: false, maxBounds: bounds, maxBoundsViscosity: 1.0, minZoom: 10, doubleClickZoom: false }).setView([22.25, 113.55], 13);
        L.tileLayer('http://127.0.0.1:9999/tiles/{z}/{x}/{y}.png').addTo(map);

        var bridge;
        new QWebChannel(qt.webChannelTransport, function(channel) { bridge = channel.objects.bridge; });

        var markers = [];
        var roadFeatures = [];
        var mode = "none";
        
        var currentSearchMarker = null; // 新增：独立追踪搜索产生的坐标点

        // 新增：清除所有搜索产生的视觉元素（打点和虚线框）
        function clearSearchVisuals() {
            if (currentSearchMarker) {
                map.removeLayer(currentSearchMarker);
                currentSearchMarker = null;
            }
            if (outlineLayer) {
                map.removeLayer(outlineLayer);
                outlineLayer = null;
            }
        }

        // 临时路况节点管理
        var tempRoadMarkers = []; 

        map.on('click', function(e) {
            if (mode === "depot" || mode === "customer") bridge.add_point(e.latlng.lat, e.latlng.lng, mode);
        });

        map.on('dblclick', function(e) {
            if (mode.startsWith("road_")) {
                if (mode === "road_block") {
                    bridge.add_road_block(e.latlng.lat, e.latlng.lng);
                } else {
                    addTempRoadMarker(e.latlng.lat, e.latlng.lng);
                }
            }
        });
        
        var outlineLayer = null;
        function drawOutline(geoJsonData, lat, lng) {
            if (outlineLayer) {
                map.removeLayer(outlineLayer);
                outlineLayer = null;
            }
            if (geoJsonData) {
                // 画虚线框：橘色，3像素粗，5像素虚线段，轻微半透明填充
                outlineLayer = L.geoJSON(geoJsonData, {
                    style: {color: '#ff7800', weight: 3, opacity: 0.8, dashArray: '5, 5', fillOpacity: 0.1}
                }).addTo(map);
                map.fitBounds(outlineLayer.getBounds(), {padding: [50, 50]});
            } else {
                // 如果该地点没有轮廓，只简单居中地图
                map.setView([lat, lng], 17);
            }
        }

        function requestDeletePoint(index, type) {
            bridge.delete_point(index, type);
            map.closePopup();
        }

        function addMarker(lat, lng, idLabel, nameLabel, color, index, type, isActive, demand, pop, area, imp) {
            if (!isActive) return;
            var iconUrl = (type === 'depot') ? 'leaflet/images/marker-icon-2x-red.png' : 'leaflet/images/marker-icon-2x-blue.png';
            var icon = new L.Icon({ iconUrl: iconUrl, shadowUrl: 'leaflet/images/marker-shadow.png', iconSize: [18, 30], iconAnchor: [9, 30], popupAnchor: [1, -28], shadowSize: [30, 30] });
            var m = L.marker([lat, lng], {draggable: true, icon: icon}).addTo(map);
            
            // 鼠标悬停只显示最基础的紧凑提示
            m.bindTooltip(idLabel, {permanent: false, direction: 'top', className: 'compact-label'});

            // 鼠标右键点击弹出的详细信息框（对齐表格所有字段）
            let infoHtml = `<div style="min-width: 160px; font-size: 13px;">
                <b style="font-size: 14px; color: #333;">${idLabel}</b><br>
                <b>名称:</b> ${nameLabel}<br>
                <b>坐标:</b> ${lat.toFixed(5)}, ${lng.toFixed(5)}<br>`;
            
            if(type === 'customer') {
                infoHtml += `<b>需求:</b> ${demand} T<br>
                             <b>人口:</b> ${pop} 人<br>
                             <b>面积:</b> ${area} m²<br>
                             <b>等级:</b> ${imp}<br>`;
            }
            infoHtml += `<div style="text-align:center; margin-top:10px;">
                            <button style="background:#e53935; color:white; border:none; border-radius:3px; padding:5px 12px; cursor:pointer; width:100%;" 
                                    onclick="requestDeletePoint(${index}, '${type}')">🗑️ 删除该点</button>
                         </div></div>`;
            m.bindPopup(infoHtml);

            m.on('contextmenu', function(e) { this.openPopup(); });
            m.on('dragend', function(e) {
                var pos = e.target.getLatLng();
                bridge.move_point(index, pos.lat, pos.lng, type);
            });
            markers.push(m);
            m.on('dblclick', function(e) {
            if (window.bridge) {
                window.bridge.on_point_double_clicked(e.latlng.lat, e.latlng.lng);
            }
        });
        }

        // --- 临时路网节点功能 ---
        function addTempRoadMarker(lat, lng) {
            let icon = L.divIcon({
                className: 'custom-search-marker',
                html: '<div style="font-size:26px; text-shadow: 0 2px 4px rgba(0,0,0,0.5);">📍</div>',
                iconSize: [26, 26], iconAnchor: [13, 26], popupAnchor: [0, -26]
            });
            let m = L.marker([lat, lng], {draggable: true, icon: icon}).addTo(map);
            let uid = tempRoadMarkers.length;

            m.bindTooltip("路况定点 (拖拽移动, 右键删除)", {permanent: false, direction: 'top'});
            m.on('contextmenu', function(e) {
                map.removeLayer(m);
                tempRoadMarkers = tempRoadMarkers.filter(marker => marker !== m);
            });
            tempRoadMarkers.push(m);
        }

        // 调用后台根据所有临时打点生成真实路网路径
        function generatePath() {
            if(tempRoadMarkers.length < 2) return;
            let coords = tempRoadMarkers.map(m => {
                let pos = m.getLatLng();
                return [pos.lat, pos.lng];
            });
            // 序列化后发送给后台处理
            bridge.generate_complex_road_segment(JSON.stringify(coords), mode);
            
            // 1. 将当前的红点转移到“待消失”队列，把它们留在地图上供用户观看
            if (!window.fadingMarkers) window.fadingMarkers = [];
            window.fadingMarkers = window.fadingMarkers.concat(tempRoadMarkers);
            
            // 2. 彻底清空逻辑打点数组。这样撤销状态机恢复正常，且下一条路径绝对是独立的
            tempRoadMarkers = []; 
        }
        
        // 获取当前地图视野，并发送给 Python 后台请求交通数据
        function fetchRealtimeTraffic() {
            if (typeof bridge !== 'undefined' && bridge) {
                var bounds = map.getBounds();
                // 传回: 南(lat), 西(lng), 北(lat), 东(lng)
                bridge.request_traffic(bounds.getSouth(), bounds.getWest(), bounds.getNorth(), bounds.getEast());
            }
        }

        function clearTempRoadMarkers() {
            tempRoadMarkers.forEach(m => map.removeLayer(m));
            tempRoadMarkers = [];
        }
        
        function tryUndoTempMarker() {
            if (tempRoadMarkers && tempRoadMarkers.length > 0) {
                let m = tempRoadMarkers.pop();
                map.removeLayer(m);
                return true;
            }
            return false;
        }

        function restoreTempMarkers(coords, restoredMode) {
            setMode(restoredMode);
            coords.forEach(c => {
                addTempRoadMarker(c[0], c[1]);
            });
        }

        function clearMarkers() { markers.forEach(m => map.removeLayer(m)); markers = []; }

        function drawRoadCondition(type, dataStr, index) {
            let data = JSON.parse(dataStr);
            if (type === 'road_block') {
                let m = L.marker([data[0], data[1]], {
                    draggable: true, 
                    icon: L.divIcon({className: '', html: '<div style="color:black; font-size:20px; font-weight:bold; text-shadow: 0 0 3px white;">❌</div>', iconSize:[20,20], iconAnchor:[10,10]})
                }).addTo(map);
                m.on('dragend', function(e) {
                    var pos = e.target.getLatLng();
                    bridge.move_road_block(index, pos.lat, pos.lng);
                });
                
                roadFeatures.push(m);
            } else {
                let colorMap = {'road_jam': '#ff0000', 'road_slow': '#ffea00', 'road_control': '#666666', 'road_special': '#aa00ff'};
                let poly = L.polyline(data, {color: colorMap[type], weight: 9, opacity: 1.0, lineJoin: 'round'}).addTo(map);
                roadFeatures.push(poly);
            }
        }
        function clearRoadConditions() { roadFeatures.forEach(f => map.removeLayer(f)); roadFeatures = []; 
        if (window.fadingMarkers) {
                window.fadingMarkers.forEach(m => map.removeLayer(m));
                window.fadingMarkers = [];
            }
        }

        function setMode(m) { 
            mode = m; 
            clearTempRoadMarkers(); 
            let mapDiv = document.getElementById('map');
            if (mode.startsWith("road_")) mapDiv.classList.add('road-drawing-mode');
            else mapDiv.classList.remove('road-drawing-mode');
        }

        let currentSearchResults = [];
        function selectSearchResult(item) {
            let lat = parseFloat(item.lat), lon = parseFloat(item.lon);
            
            addSearchResultMarker(lat, lon, item.name || item.display_name.split(',')[0]);
            
            document.getElementById('search-results').style.display = 'none';
            document.getElementById('search-input').value = item.name || item.display_name.split(',')[0];
            
            // 【新增】将坐标同步显示在坐标搜索框中
            document.getElementById('coord-input').value = `${lat.toFixed(4)}, ${lon.toFixed(4)}`;
            
            if (typeof bridge !== 'undefined' && bridge) {
                bridge.on_point_double_clicked(lat, lon);
            } else {
                map.setView([lat, lon], 17);
            }
        }

        document.getElementById('search-btn').addEventListener('click', function() {
            let q = document.getElementById('search-input').value.trim();
            if (currentSearchResults.length > 0) selectSearchResult(currentSearchResults[0]);
            else if(q.length > 0) triggerSearch(q);
        });

        let searchTimeout = null;
        document.getElementById('search-input').addEventListener('input', function(e) {
            let q = e.target.value.trim();
            
            // 联动清空坐标框及其下拉
            document.getElementById('coord-input').value = '';
            document.getElementById('coord-search-results').style.display = 'none';
            clearSearchVisuals();

            let resDiv = document.getElementById('search-results');
            if(q.length === 0) { 
                resDiv.style.display = 'none'; 
                currentSearchResults = []; 
                if (searchTimeout) clearTimeout(searchTimeout);
                return; 
            }
            
            if (searchTimeout) clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => {
                triggerSearch(q);
            }, 150);
        });

        // 🌟 彻底重写：不再向国外发网络请求，直接通过 bridge 召唤本地 Python 引擎
        function triggerSearch(q) {
            if (typeof bridge !== 'undefined' && bridge) {
                bridge.search_text(q);
            }
        }

        // 🌟 新增：接收 Python 传回的本地搜索结果并渲染
        function showTextSearchResults(data) {
            currentSearchResults = data;
            let resDiv = document.getElementById('search-results');
            resDiv.innerHTML = '';
            
            if(data && data.length > 0) {
                resDiv.style.display = 'block';
                data.forEach(item => {
                    let div = document.createElement('div'); 
                    div.className = 'search-item';
                    div.innerText = item.display_name;
                    div.onclick = () => { selectSearchResult(item); };
                    resDiv.appendChild(div);
                });
            } else {
                // 【新增】搜索为空时的提示
                resDiv.style.display = 'block';
                let div = document.createElement('div'); 
                div.className = 'search-item';
                div.style.color = 'red';
                div.style.cursor = 'default';
                div.style.textAlign = 'center';
                div.innerText = '⚠️ 未查询到相关地点';
                resDiv.appendChild(div);
            }
        }

        function addSearchResultMarker(lat, lng, name) {
            clearSearchVisuals(); // 每次添加前，先清理旧的搜索残余
            var icon = L.divIcon({ className: 'custom-search-marker', html: '<div style="font-size:26px; text-shadow: 0 2px 4px rgba(0,0,0,0.5);">📍</div>', iconSize: [26, 26], iconAnchor: [13, 26], popupAnchor: [0, -26] });
            currentSearchMarker = L.marker([lat, lng], {icon: icon}).addTo(map);
            currentSearchMarker.bindTooltip("🔎 搜索结果: " + name + "<br><span style='color:blue;'>[ 右键点击 ]</span> 可设为调度点", {permanent: true, direction: 'right'});
            currentSearchMarker.on('contextmenu', function(e) { bridge.convert_search(lat, lng, name); clearSearchVisuals(); });
        }
        
        function showCoordSearchResults(results) {
            let resDiv = document.getElementById('coord-search-results');
            resDiv.innerHTML = '';
            if(results && results.length > 0) {
                resDiv.style.display = 'block';
                results.forEach(item => {
                    let div = document.createElement('div'); 
                    div.className = 'search-item';
                    // 按照要求保留空格格式
                    div.innerHTML = `<pre style="margin:0; font-family:inherit;">${item.display_name}</pre>`;
                    div.onclick = () => { 
                        // 点击推荐项后，复用原有的地点聚焦和划线逻辑
                        selectSearchResult(item); 
                        document.getElementById('coord-input').value = `${item.lat.toFixed(4)},${item.lon.toFixed(4)}`;
                    };
                    resDiv.appendChild(div);
                });
            } else {
                resDiv.style.display = 'none';
            }
        }

        // 监听坐标输入框的动态输入 (满足4位小数即触发推荐)
        let coordTimeout = null;
        document.getElementById('coord-input').addEventListener('input', function(e) {
            let val = e.target.value.trim();
            
            // 联动清空地名框及其下拉
            document.getElementById('search-input').value = '';
            document.getElementById('search-results').style.display = 'none';
            clearSearchVisuals();

            let resDiv = document.getElementById('coord-search-results');
            
            if(val.length === 0 || !val.includes('.')) { 
                resDiv.style.display = 'none'; 
                if (coordTimeout) clearTimeout(coordTimeout);
                return; 
            }

            if (coordTimeout) clearTimeout(coordTimeout);
            coordTimeout = setTimeout(() => {
                // 【新增】将中文逗号转为英文，并正则匹配“空格或逗号”作为分隔符
                let cleanVal = val.replace(/，/g, ',').trim();
                let match = cleanVal.match(/^(\d+\.\d+)[\s,]+(\d+\.\d+)$/);
                
                if(match) {
                    let lat = parseFloat(match[1]);
                    let lng = parseFloat(match[2]);
                    
                    // 【新增】地图越界拦截检测
                    if (lat >= 21.60 && lat <= 22.65 && lng >= 112.85 && lng <= 114.52) {
                        if (typeof bridge !== 'undefined') {
                            bridge.search_coordinate(lat, lng);
                        }
                    } else {
                        resDiv.style.display = 'block';
                        resDiv.innerHTML = '<div class="search-item" style="color:red; cursor:default; text-align:center;">⚠️ 搜索坐标不在地图范围内</div>';
                    }
                } else {
                    resDiv.style.display = 'none';
                }
            }, 150);
        });

        // 监听坐标搜索按钮点击
        document.getElementById('coord-btn').addEventListener('click', function() {
            let val = document.getElementById('coord-input').value.trim();
            let cleanVal = val.replace(/，/g, ',').trim();
            let match = cleanVal.match(/^(\d+\.\d+)[\s,]+(\d+\.\d+)$/);
            
            if(match) {
                let lat = parseFloat(match[1]);
                let lng = parseFloat(match[2]);
                
                if (lat >= 21.60 && lat <= 22.65 && lng >= 112.85 && lng <= 114.52) {
                    if (typeof bridge !== 'undefined') bridge.search_coordinate(lat, lng);
                } else {
                    // 【新增】点击按钮时，越界则弹窗强制提示
                    if (typeof bridge !== 'undefined') {
                        bridge.show_warning("越界警告", "搜索坐标不在地图范围内！\\n\\n支持的纬度: 21.60 ~ 22.65\\n支持的经度: 112.85 ~ 114.52");
                    }
                }
            } else {
                if (typeof bridge !== 'undefined') {
                    bridge.show_warning("格式错误", "坐标格式不正确！\\n请输入正确的经纬度，可用逗号或空格分隔。\\n示例: 22.2589, 113.5455 或 22.2589 113.5455");
                }
            }
        });
        
    </script>
</body>
</html>
"""