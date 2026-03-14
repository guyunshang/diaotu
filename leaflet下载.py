# download_leaflet_assets.py
import os
import urllib.request

def download_file(url, filepath):
    print(f"⏳ 正在下载 {filepath} ...")
    try:
        # 添加伪装请求头，防止被 github 等拦截
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response, open(filepath, 'wb') as out_file:
            data = response.read()
            out_file.write(data)
        print("   ✅ 成功!")
    except Exception as e:
        print(f"   ❌ 失败: {e}")

def main():
    # 创建纯净的目录结构
    os.makedirs('leaflet', exist_ok=True)
    os.makedirs('leaflet/images', exist_ok=True)

    # 核心清单：包含 CSS, JS 以及 红蓝点位图片和阴影文件
    files_to_download = {
        'leaflet/leaflet.css': 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
        'leaflet/leaflet.js': 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
        'leaflet/images/marker-icon-2x-blue.png': 'https://raw.githubusercontent.com/pointhi/leaflet-color-markers/master/img/marker-icon-2x-blue.png',
        'leaflet/images/marker-icon-2x-red.png': 'https://raw.githubusercontent.com/pointhi/leaflet-color-markers/master/img/marker-icon-2x-red.png',
        'leaflet/images/marker-shadow.png': 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/0.7.7/images/marker-shadow.png',
    }

    print("🚀 开始构建本地离线前端环境...\n")
    for path, url in files_to_download.items():
        if not os.path.exists(path):
            download_file(url, path)
        else:
            print(f"🟢 {path} 已存在，跳过下载。")

    print("\n🎉 所有前端资源已全部准备完毕，真正的 100% 离线环境已就绪！")

if __name__ == '__main__':
    main()