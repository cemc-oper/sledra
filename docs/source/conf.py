"""Sledra 中文文档的 Sphinx 配置。"""

project = "Sledra"
author = "cemc-oper"
copyright = "2026, cemc-oper"

extensions = ["myst_nb"]
language = "zh_CN"
nitpicky = True
exclude_patterns = []
myst_enable_extensions = ["colon_fence", "fieldlist"]
myst_heading_anchors = 4
# 文档构建只渲染已有内容，避免执行示例、提交作业或要求 Notebook 内核。
nb_execution_mode = "off"

html_theme = "pydata_sphinx_theme"
html_title = "Sledra 中文文档"
html_theme_options = {
    "github_url": "https://github.com/cemc-oper/sledra",
    "show_toc_level": 2,
    "navigation_with_keys": False,
}
