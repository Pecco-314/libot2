import logging
import os
import multiprocessing
import re
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, wait

from rapidocr_onnxruntime import RapidOCR

logger = logging.getLogger("capture")
_ocr_instance = None

def _init_ocr_worker():
    global _ocr_instance
    _ocr_instance = RapidOCR()
    logger.info("RapidOCR Worker initialized in process %s", os.getpid())

def _dummy_task():
    return True

def _extract_timestamp(filepath: str) -> int:
    # 从类似 frame_20260509_025351.jpg 或 .processing 中提取时间
    filename = os.path.basename(filepath)
    match = re.search(r'frame_(\d{8}_\d{6})', filename)
    if match:
        time_str = match.group(1)
        try:
            dt = datetime.strptime(time_str, "%Y%m%d_%H%M%S")
            return int(dt.timestamp())
        except ValueError:
            pass
    import time
    return int(time.time()) # 解析失败则回退到当前时间

def _process_frame_task(image_path: str, room_id: int) -> tuple[int, str, list[str], int]:
    global _ocr_instance
    frame_timestamp = _extract_timestamp(image_path)
    
    try:
        result, _ = _ocr_instance(image_path)
        
        if not result:
            return room_id, image_path, [], frame_timestamp

        valid_items = []
        # 匹配时间格式，例如 02:53.53, 02:53, [02:53]
        time_pattern = re.compile(r'^\[?\d{2}:\d{2}(:\d{2})?(\.\d+)?\]?$')

        for line in result:
            if len(line) < 3:
                continue
                
            box = line[0]
            text = line[1].strip()
            confidence = float(line[2])
            
            # 1. 置信度过滤
            if confidence < 0.75:
                logger.info("Discarded low-confidence OCR [%s]: '%s' (score: %.4f)", room_id, text, confidence)
                continue
                
            # 2. 文本内容过滤 (丢弃歌词时间轴)
            if time_pattern.fullmatch(text):
                logger.info("Discarded time-like text [%s]: '%s'", room_id, text)
                continue

            # 计算 bounding box 的几何信息
            x_coords = [p[0] for p in box]
            y_coords = [p[1] for p in box]
            min_y, max_y = min(y_coords), max(y_coords)
            min_x = min(x_coords)
            height = max_y - min_y
            center_y = (min_y + max_y) / 2

            # 3. 高度过滤 (丢弃极小的注音或噪点)
            if height < 25:
                logger.info("Discarded small text [%s] (height %d): '%s'", room_id, height, text)
                continue

            valid_items.append({
                'text': text,
                'min_x': min_x,
                'center_y': center_y,
                'height': height
            })

        # 4. 同行合并聚类 (Line Clustering)
        lines = []
        for item in valid_items:
            matched_line = False
            for line_group in lines:
                ref = line_group[0]
                # 如果两个框的 Y 轴中心距小于它们最大高度的一半，认为在同一行
                if abs(item['center_y'] - ref['center_y']) < max(item['height'], ref['height']) * 0.5:
                    line_group.append(item)
                    matched_line = True
                    break
            if not matched_line:
                lines.append([item])

        # 5. 排序并拼接
        final_texts = []
        for line_group in lines:
            # 同一行内按 X 坐标从左到右排序
            line_group.sort(key=lambda x: x['min_x'])
            # 英文被切断通常是因为中间有空格，这里统一用空格拼接同一行的分块
            merged = " ".join([x['text'] for x in line_group]).strip()
            if merged:
                final_texts.append(merged)
        
        return room_id, image_path, final_texts, frame_timestamp
        
    except Exception as e:
        logger.error("RapidOCR failed for %s: %s", image_path, e)
        return room_id, image_path, [], frame_timestamp

class OCREnginePool:
    def __init__(self, max_workers: int = 3):
        context = multiprocessing.get_context('spawn')
        self.pool = ProcessPoolExecutor(
            max_workers=max_workers,
            initializer=_init_ocr_worker,
            mp_context=context
        )
        
        logger.info("Warming up RapidOCR engine processes, please wait...")
        futures = [self.pool.submit(_dummy_task) for _ in range(max_workers)]
        wait(futures)
        logger.info("All RapidOCR workers are warmed up and ready.")

    def submit_frame(self, image_path: str, room_id: int):
        return self.pool.submit(_process_frame_task, image_path, room_id)

    def shutdown(self):
        self.pool.shutdown(wait=True)