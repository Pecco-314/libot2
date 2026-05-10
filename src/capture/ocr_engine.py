import logging
import os
import multiprocessing
import re
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, wait

from rapidocr_onnxruntime import RapidOCR
from src.db.transcript import get_recent_transcripts
from src.common.utils import ROOT

logger = logging.getLogger("capture")
MODEL_PATH = ROOT / "models"
JP_PATH = MODEL_PATH / "japan_PP-OCRv4_rec_infer"
KR_PATH = MODEL_PATH / "korean_PP-OCRv4_rec_infer"

# 声明三种语言的实例
_ocr_ch = None
_ocr_jp = None
_ocr_kr = None

def _init_ocr_worker():
    global _ocr_ch, _ocr_jp, _ocr_kr
    
    # 1. 默认中文引擎（中英数）
    _ocr_ch = RapidOCR()
    
    # 2. 日韩引擎
    # det_model 可以复用中文的检测模型（框选位置的能力是通用的），只需要替换 rec_model（文字识别）
    try:
        _ocr_jp = RapidOCR(rec_model_path=str(JP_PATH / "model.onnx"), rec_keys_path=str(JP_PATH / "japan_dict.txt"))
        _ocr_kr = RapidOCR(rec_model_path=str(KR_PATH / "model.onnx"), rec_keys_path=str(KR_PATH / "korean_dict.txt"))
        pass
    except Exception as e:
        logger.warning("Failed to load JP/KR models, fallback to CH only: %s", e)

    logger.info("Multi-lang RapidOCR Workers initialized in process %s", os.getpid())

def _dummy_task():
    return True

def _extract_timestamp(filepath: str) -> int:
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
    return int(time.time())

def _detect_language_by_unicode(text: str) -> str:
    """通过 Unicode 区块极速判断语言分布"""
    if not text:
        return "ch"
    
    # 剥离标点符号和空格
    clean_text = re.sub(r'\s|[^\w]', '', text)
    if not clean_text:
        return "ch"
        
    # 统计平假名、片假名数量
    jp_count = len(re.findall(r'[\u3040-\u309F\u30A0-\u30FF]', clean_text))
    # 统计韩文谚文数量
    kr_count = len(re.findall(r'[\uAC00-\uD7A3]', clean_text))

    total_len = len(clean_text)
    
    # 只要日韩文特征字符占比超过 10%，就切路由
    if kr_count > total_len * 0.1:
        return "kr"
    if jp_count > total_len * 0.1:
        return "jp"
        
    return "ch"

def _process_frame_task(image_path: str, room_id: int) -> tuple[int, str, list[str], int]:
    global _ocr_ch, _ocr_jp, _ocr_kr
    frame_timestamp = _extract_timestamp(image_path)
    
    try:
        # ==========================================
        # 1. 跨模态上下文路由：用 ASR 记录预判 OCR 语言
        # ==========================================
        recent_asr_text = get_recent_transcripts(room_id, frame_timestamp, window_seconds=15)
        target_lang = _detect_language_by_unicode(recent_asr_text)
        
        # 路由选择引擎，如果没有加载对应模型，则平滑降级到中文
        if target_lang == "jp" and _ocr_jp:
            ocr_engine = _ocr_jp
            logger.info("Routing OCR -> [JP] based on ASR context.")
        elif target_lang == "kr" and _ocr_kr:
            ocr_engine = _ocr_kr
            logger.info("Routing OCR -> [KR] based on ASR context.")
        else:
            ocr_engine = _ocr_ch
            
        # ==========================================
        # 2. 执行识别与后处理
        # ==========================================
        result, _ = ocr_engine(image_path)
        
        if not result:
            return room_id, image_path, [], frame_timestamp

        valid_items = []
        time_pattern = re.compile(r'^\[?\d{2}:\d{2}(:\d{2})?(\.\d+)?\]?$')

        for line in result:
            if len(line) < 3:
                continue
                
            box = line[0]
            text = line[1].strip()
            confidence = float(line[2])
            
            if confidence < 0.75:
                logger.info("Discarded low-confidence OCR [%s]: '%s' (score: %.4f)", room_id, text, confidence)
                continue
                
            if time_pattern.fullmatch(text):
                continue

            x_coords = [p[0] for p in box]
            y_coords = [p[1] for p in box]
            min_y, max_y = min(y_coords), max(y_coords)
            min_x = min(x_coords)
            height = max_y - min_y
            center_y = (min_y + max_y) / 2

            if height < 30:
                continue

            valid_items.append({
                'text': text,
                'min_x': min_x,
                'center_y': center_y,
                'height': height
            })

        lines = []
        for item in valid_items:
            matched_line = False
            for line_group in lines:
                ref = line_group[0]
                if abs(item['center_y'] - ref['center_y']) < max(item['height'], ref['height']) * 0.5:
                    line_group.append(item)
                    matched_line = True
                    break
            if not matched_line:
                lines.append([item])

        final_texts = []
        for line_group in lines:
            line_group.sort(key=lambda x: x['min_x'])
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