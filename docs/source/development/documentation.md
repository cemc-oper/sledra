# 文档维护

中文说明位于 `docs/source/`，采用与 cemc-oper 项目一致的 Sphinx、PyData Sphinx Theme 和 MyST 写作方式；此处启用 MyST-NB 处理 Markdown 与 Notebook。组件、示例、测试及试用包的 README 也使用中文。命令、字段名、错误代码和原始夹具不翻译。

## 构建与预览

仓库根目录的公共入口：

```bash
make setup
make docs
python3 -m http.server --directory build/docs/html 8000
```

打开 `http://localhost:8000`。`make docs` 先检查仓库 Markdown 导航，再调用 Sphinx HTML 构建，启用 `-n -W --keep-going -E -a`：交叉引用严格检查，警告导致非零退出，每次完整读取并写出页面。HTML 位于 `build/docs/html/`，日志与摘要保存在新建 `test-results/docs-.../`。CI 调用相同入口。

仅安装文档依赖并构建站点时可使用：

```bash
uv sync --only-group docs --locked --no-python-downloads --python 3.12
uv run --only-group docs --locked --offline --no-python-downloads --python 3.12 \
  python -m sphinx -n -W --keep-going -E -a -b html docs/source build/docs/html
```

示例显式选择已安装的 Python 3.12，与当前实测工具链一致；使用其他版本时须准备对应缓存并补验。首次同步需联网，后续构建使用准备好的缓存。只装 docs 组不提供完整开发检查所需的 Ruff；恢复完整开发环境请执行 `make setup`。文档工具不打入记录器试用包，也不增加记录运行依赖。

## Read the Docs 托管

仓库根目录的 `.readthedocs.yaml` 使用 Read the Docs v2 配置：Ubuntu 24.04、Python 3.12 和 uv 0.11.2。安装阶段执行 `uv sync --only-group docs --locked`，将根 `uv.lock` 中的文档依赖安装到 `READTHEDOCS_VIRTUALENV_PATH` 指定的环境；锁文件与项目配置不一致时构建失败。构建前运行 `scripts/check_docs.py` 检查仓库 Markdown 导航，再使用 `docs/source/conf.py` 构建 HTML，并将警告视为错误。

配置只安装文档依赖，不调用需要 Go 工具链的 `make setup`；Notebook 仍不执行。配置字段和构建阶段见 [Read the Docs 配置参考](https://docs.readthedocs.com/platform/stable/config-file/v2.html)与[构建定制说明](https://docs.readthedocs.com/platform/stable/build-customization.html)。

提交并推送配置后，在 Read the Docs 导入 `cemc-oper/sledra` 仓库，使用根目录的 `.readthedocs.yaml`，选择 `main` 分支并触发首次构建。站点地址以导入后分配的项目域名为准；在控制台确认构建成功后，再启用需要的版本并将实际地址加入项目 README。仓库配置和本地构建验证不代表托管项目已创建或站点已上线。

## 写作约定

`conf.py` 设置 `language = "zh_CN"`、`html_theme = "pydata_sphinx_theme"` 和 `extensions = ["myst_nb"]`。MyST-NB 已自动加载 Markdown 解析器，不重复启用 myst_parser。版本和传递依赖由根 uv.lock 锁定。

### 目录与导航

站点首页只收录“开始使用、指南、开发、参考”四个顶层章节。按读者任务放置页面，加入所属目录 `index.md` 的 toctree，不在首页平铺单篇文档。测试与验证、兼容性与迁移分别作为开发和参考的子章节：

```text
docs/source/
├── index.md
├── getting-started/          # 开始使用：安装、源码快速开始
├── guides/                   # 指南：记录配置、本地与 HPC 验证工具
├── development/              # 开发：流程、文档维护、工程交接
│   └── testing/              # 测试与验证：本地回归、CI
└── reference/                # 参考：架构、CLI、契约来源
    └── compatibility/        # 兼容性与迁移：支持、版本、路径迁移
```

每个章节目录都有 `index.md`，简要说明适用任务并导航到子页面。章节用 toctree 组织页面，设置 `:titlesonly:` 仅列出页面标题；单篇文档的二级、三级标题由页内目录导航，无需为每个小节创建页面。

### 链接与内容

页面之间使用相对 Markdown 链接；标题和可见说明使用中文，保留文件路径与技术标识。仓库内源码导航使用真实相对路径；站点指向未收录的源码资源时，使用明确的 GitHub 文件链接，避免生成不可访问的本地链接。移动页面时同步更新 README、组件说明和打包脚本中的路径，并运行 `make docs`。

MyST 支持 fenced directive，例如 toctree、note 和代码块。Notebook 扩展已启用，但 `nb_execution_mode = "off"`，构建只渲染已有内容，不自动执行代码或需要业务环境/内核；当前没有计算 Notebook 示例。新增执行型文档时再明确其运行环境与验收条件。

构建不抓取远端 intersphinx inventory，不依赖相邻规划目录。检查和 HTML 构建须无警告；本地构建通过不表示托管站点已发布。

配置用法可查阅 [Sphinx 构建说明](https://www.sphinx-doc.org/en/master/man/sphinx-build.html)、[PyData 主题安装说明](https://pydata-sphinx-theme.readthedocs.io/en/stable/user_guide/install.html)和 [MyST-NB 入门](https://myst-nb.readthedocs.io/en/latest/quickstart.html)。
