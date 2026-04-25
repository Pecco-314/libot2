import asyncio
import httpx
from pathlib import Path

# 使用 JsDelivr 镜像加速：https://fastly.jsdelivr.net/gh/用户/仓库@分支/文件路径
FONTS_TO_DOWNLOAD = {
    "NotoSansCJKsc-Regular.otf": "https://fastly.jsdelivr.net/gh/googlefonts/noto-cjk@main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
    "NotoColorEmoji.ttf": "https://fastly.jsdelivr.net/gh/googlefonts/noto-emoji@main/fonts/NotoColorEmoji.ttf",
    "NotoSansThai-Regular.ttf": "https://fastly.jsdelivr.net/gh/googlefonts/noto-fonts@main/hinted/ttf/NotoSansThai/NotoSansThai-Regular.ttf",
    "NotoSansKannada-Regular.ttf": "https://fastly.jsdelivr.net/gh/googlefonts/noto-fonts@main/hinted/ttf/NotoSansKannada/NotoSansKannada-Regular.ttf",
    "NotoSans-Regular.ttf": "https://fastly.jsdelivr.net/gh/googlefonts/noto-fonts@main/hinted/ttf/NotoSans/NotoSans-Regular.ttf",
    "NotoSansSymbols-Regular.ttf": "https://fastly.jsdelivr.net/gh/googlefonts/noto-fonts@main/hinted/ttf/NotoSansSymbols/NotoSansSymbols-Regular.ttf",
    "NotoSansSymbols2-Regular.ttf": "https://fastly.jsdelivr.net/gh/googlefonts/noto-fonts@main/hinted/ttf/NotoSansSymbols2/NotoSansSymbols2-Regular.ttf"
}

async def download_font(name: str, url: str, folder: Path, client: httpx.AsyncClient):
    target_path = folder / name
    if target_path.exists() and target_path.stat().st_size > 1000: # 简单校验文件大小
        print(f"跳过 {name}，文件已存在。")
        return

    print(f"正在通过镜像下载 {name}...")
    try:
        # 增加 headers 模拟浏览器访问，防止某些 CDN 拦截
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        response = await client.get(url, headers=headers, timeout=300.0)
        response.raise_for_status()
        target_path.write_bytes(response.content)
        print(f"成功下载: {name}")
    except Exception as e:
        print(f"下载 {name} 失败 (镜像): {e}")

async def main():
    # 确保目录存在
    save_dir = Path("fonts")
    save_dir.mkdir(parents=True, exist_ok=True)

    print("开始从国内加速镜像构建字体包...")
    
    async with httpx.AsyncClient(follow_redirects=True) as client:
        tasks = [download_font(name, url, save_dir, client) for name, url in FONTS_TO_DOWNLOAD.items()]
        await asyncio.gather(*tasks)

    print("\n--- 下载尝试结束 ---")
    print(f"请检查目录: {save_dir.absolute()}")

if __name__ == "__main__":
    asyncio.run(main())