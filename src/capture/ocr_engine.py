import logging
import os
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, wait

# 引入 RapidOCR
from rapidocr_onnxruntime import RapidOCR

logger = logging.getLogger("capture")
_ocr_instance = None

def _init_ocr_worker():
    global _ocr_instance
    _ocr_instance = RapidOCR()
    logger.info("RapidOCR Worker initialized in process %s", os.getpid())

def _dummy_task():
    # 依然保留用于预热模型权重的空任务
    return True

def _process_frame_task(image_path: str, room_id: int) -> tuple[int, str, str]:
    global _ocr_instance
    try:
        # RapidOCR 的调用直接返回 (result, elapse)
        # 如果没有识别到任何文字，result 为 None
        result, _ = _ocr_instance(image_path)
        
        if not result:
            return room_id, image_path, ""

        # RapidOCR 的 result 结构为: [ [[框点], "文字内容", 置信度], ... ]
        # 所以 line[1] 就是纯文本
        texts = [line[1] for line in result if len(line) >= 2]
        merged_text = " ".join(texts).strip()
        
        return room_id, image_path, merged_text
    except Exception as e:
        logger.error("RapidOCR failed for %s: %s", image_path, e)
        return room_id, image_path, ""

class OCREnginePool:
    def __init__(self, max_workers: int = 3):
        context = multiprocessing.get_context('spawn')
        self.pool = ProcessPoolExecutor(
            max_workers=max_workers,
            initializer=_init_ocr_worker,
            mp_context=context
        )
        
        logger.info("Warming up RapidOCR engine processes, please wait...")
        # 强制预热
        futures = [self.pool.submit(_dummy_task) for _ in range(max_workers)]
        wait(futures)
        logger.info("All RapidOCR workers are warmed up and ready.")

    def submit_frame(self, image_path: str, room_id: int):
        return self.pool.submit(_process_frame_task, image_path, room_id)

    def shutdown(self):
        self.pool.shutdown(wait=True)