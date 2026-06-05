"""
文件可寻性增强任务 (D15-D20).

目标 (v6 用户原话): "让模型更好地找到文件, 当前找文件只能依赖于时间戳跟文件名"

工作: 用 OCR/vision/extract 提取文件内容, UPDATE cold_nodes 的 content + file_metadata,
      让 search_files 能按内容找.

- D15 图片 (event-driven, file_uploaded 事件)
- D16 PDF 深度提取  
- D17 Office (docx/xlsx/pptx)
- D18 音视频转写
- D19 索引复审 (cooldown)
- D20 失效引用清理 (cooldown)
"""

from app.core.dream.tasks.file_searchability import d15_image_index  # noqa: F401
from app.core.dream.tasks.file_searchability import d16_pdf_deep  # noqa: F401
from app.core.dream.tasks.file_searchability import d17_office_index  # noqa: F401
from app.core.dream.tasks.file_searchability import d18_media_metadata  # noqa: F401
from app.core.dream.tasks.file_searchability import d19_d20_review_cleanup  # noqa: F401
