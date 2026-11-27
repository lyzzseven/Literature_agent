# 文献与数据智能体

输入研究主题、检索词和要抽取的字段，程序检索文献、尝试下载开放 PDF，并读取可以取得的全文。它把数据、表格和原图预览整理成独立的任务目录。每条输出都保留 PDF 页码，数据额外保留原始值、单位和证据文字。

## 先运行现有例子

在项目目录打开终端。这个例子使用工作区已有的 AtomAgents 论文，无需联网、下载模型或设置密钥。

```sh
cd "/Users/liuyang/Desktop/试验项目/literature_agent"
./run.sh examples/atomagents-offline.json --offline
```

输出目录显示在终端，默认位于 `runs/run-…/`。示例将读取前 10 页，并抽取第 7 页中原文报告的一个模拟尺寸，另外导出图 1 至图 7 的图片和第 4 页表 1。示例的字段用于验证工具，报告的是论文中的计算设置。

如果换到一台新电脑，使用 Python 3.10 或更新版本创建环境。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run.sh --help
```

启动器也接受环境变量 `PYTHON_BIN` 指定的解释器。

## 运行自己的任务

1. 复制 `examples/peo.json`。填写 `topic` 作为任务描述，并在 `queries` 写入中英文检索词。检索按这些词执行。命令行 `--topic` 会同时覆盖主题和检索词。
2. 按需修改 `fields` 和 `outputs`。配置和本地 PDF 之间的相对路径从配置所在目录计算。也可用多个 `--pdf` 追加本地文件。
3. 根据需要在 `model.mode` 选择 `rules` 或 `ollama`。规则模式使用字段中的 `pattern`。本机 Ollama 模式还能尝试抽取没有正则的字段，并以原文校验模型结果。
4. 执行任务并查看 `manifest.json` 的覆盖和告警。

```sh
# 在线检索，按配置尝试获取开放 PDF
./run.sh examples/peo.json

# 处理自己已有的全文，保留配置中的数据和图表筛选
./run.sh examples/peo.json --offline --pdf "/绝对路径/论文.pdf" --out runs
```

### 使用 OpenAlex 联网检索

要按新的主题实时查询 OpenAlex，只导出题录，可以运行：

```sh
./run.sh examples/peo.json --search-only --provider openalex \
  --topic "AZ31 magnesium alloy plasma electrolytic oxidation frequency"
```

`--search-only` 跳过全文下载、本地 PDF 读取和模型抽取。去掉该选项即可沿用任务配置里的全文获取和抽取流程。`--provider` 可以重复，默认来源由任务配置决定。题录按 DOI 和规范化题名去重，各来源的查询时间、命中数和失败原因会写入 `manifest.json`。

OpenAlex 优先使用环境变量 `OPENALEX_API_KEY`，也可读取项目内已被 Git 忽略的 `.env` 中的同名项。使用本地文件时将权限设为 `0600`。开放全文链接来自 OpenAlex，也可通过 `UNPAYWALL_EMAIL` 环境变量启用 Unpaywall DOI 查询。未找到可读取全文的条目保留题录和来源链接，可用 `--pdf` 导入已有全文。

## 使用 Ollama 抽取

启动 Ollama，运行 `ollama list` 确认已安装模型。示例默认使用 `qwen3:4b`。可以用命令行临时启用或更换模型，例如用本地 PEO 论文抽取数据。

```sh
./run.sh examples/peo.json --offline --pdf "/绝对路径/PEO论文.pdf" \
  --extractor ollama --ollama-model qwen3:4b
```

`--extractor` 覆盖配置中的 `model.mode`，`--ollama-model` 覆盖 `model.name`。`--offline` 跳过外部文献检索和下载，同时允许请求本机 Ollama。服务默认在 `http://127.0.0.1:11434`，`LITERATURE_OLLAMA_URL` 可指定其他本机端口。

模型从可读取的 PDF 文本和解析后的表格中抽取配置指定的字段。数值、单位、样品、条件及所附证据经过字面校验后才写入 `data.csv` 或 `data.json`，记录标记为 `ollama_literal_checked`，仍需复核字段归属和表格版面。模型失败时，规则结果和图表仍可导出。`manifest.json` 的 `ollama` 项记录实际导出的模型记录数，以及每篇文献的候选、已处理和失败情况。图像仍按图注筛选和导出。

### 脉冲频率论文示例

`examples/peo-frequency-1808.json` 已包含工作区的 `1808.06587v1.pdf` 路径。在项目目录直接运行即可。

```sh
./run.sh examples/peo-frequency-1808.json --offline
```

该配置使用核对过的正文页、表格单元格边界和图件页码。图件保留实际图像所在的整页，索引同时记录图注所在页。两张 EDS 表保留括号外的质量口径和括号内的原子口径。W/Al 原子比由表格规则提取，记录可回溯到具体表行。涉及多个频率的正文数据可以保留论文原报的序列。仅以图像给出的曲线和柱状图导出为原页图件。

## 自定义提取与输出

| 设置 | 用途 |
| --- | --- |
| `fields[].id`、`label`、`category` | 定义字段编号、名称和类别。类别可以自定义，例如成分、工艺、结构、性能 |
| `fields[].description`、`aliases` | 描述正确提取口径，为模型筛选有相关词的文本区域。留空别名可考虑全部区域 |
| `fields[].pattern` | 可选 Python 正则，必须提供命名捕获组 `value`。可另捕获 `unit`、`sample`、`conditions` |
| `fields[].table_pattern`、`use_model` | 可为单个表格行设置正则规则。令 `use_model` 为 `false` 可仅用显式规则处理该字段 |
| `fields[].units`、`value_pattern` | 可限制 Ollama 输出的单位和值格式。用 `[""]` 要求单位留空 |
| `pdf_layout` | 可选的单篇版式配置。按 SHA-256 对应指定 PDF，并指定正文抽取页、表格行列范围及图件与图注页码 |
| `outputs.data`、`outputs.categories` | 开关数据表，选择要提取的字段类别。空列表代表所有类别 |
| `outputs.formats` | 选择 CSV、JSON 或两者 |
| `outputs.tables`、`outputs.table_keywords` | 开关并筛选原表导出。Ollama 抽取仍可使用解析出的相关表格 |
| `outputs.figure_types` | 多选 `microstructure`、`performance`、`other` 或自定义图类。`all` 保留所有检测到的图。空列表关闭图输出 |
| 顶层 `figure_keywords`、`outputs.figure_keywords` | 顶层按类型配置识别词。输出选项再按具体关键词过滤。每个列表内任一词命中即可 |
| `max_pages`、`max_downloads`、`model.max_regions_per_paper` | 限制每篇扫描页数、下载尝试次数和模型读取区域数 |
| `model.timeout_seconds` | 单次 Ollama 请求的超时秒数，默认 180 |

例如，只保留性能字段、显微图及腐蚀相关表格时，可以把配置的输出部分换成：

```json
{
  "data": true,
  "formats": ["csv"],
  "categories": ["性能"],
  "tables": true,
  "table_keywords": ["corrosion", "腐蚀"],
  "figure_types": ["microstructure"],
  "figure_keywords": []
}
```

## 整理膜厚训练数据

[镁合金 PEO 膜厚数据集](datasets/peo_thickness_v1/README.md)包含筛选后的文献清单、原文核定记录、清洗版训练表和按研究留出验证结果。使用 `prepare-training` 可以重建训练表并检查新增记录。

```sh
./run.sh prepare-training datasets/peo_thickness_v1/reviewed_observations.json \
  --out datasets/peo_thickness_v1
```

先复核抽取值对应的试样、单位、制备条件和原文位置，再登记到核定记录。整理程序统一膜厚、时间和组分浓度的单位，隔离冲突或缺证据的记录，并保留缺失字段和研究分组。输入特征使用清洗报告中列出的 `features`，划分使用 `study_group`。

## 构建工艺、结构和性能数据库

`build-database` 将核定的试样与测量记录、智能体运行导出、表格原文件及图像写入 SQLite。完整流程和指标口径见 [PEO 数据库说明](datasets/peo_database_v1/README.md)。

```sh
./run.sh build-database datasets/peo_database_v1/database_inputs.json \
  --out datasets/peo_database_v1/peo.sqlite --export datasets/peo_database_v1/export
```

## 导出文件

| 文件或目录 | 内容 |
| --- | --- |
| `papers.csv/json` | 题录、来源、DOI、开放全文信息、本地 PDF、读取状态和实际扫描页数 |
| `data.csv/json` | 按字段存储的一条一记录。包括样品、条件、原始数值和单位、页码、来源区域及证据 |
| `tables/`、`table_index.csv/json` | 逐表单元格、PDF 页码、边界和表名。表格中的空单元格保留为空 |
| `figures/`、`figure_index.csv/json` | 图像预览、类别、图号、PDF 页码、图注、裁剪范围和待复核状态 |
| `manifest.json` | 任务配置、计数、全文缺失及失败记录 |

原始数值保留作者写法，字段抽取需核对样品与测试条件。文本层不足的扫描 PDF 需要先做 OCR。自动图分类依据图注和附近可读取文字，边栏图注或图边界不确定时输出整页预览供定位。表格行列和图片边界均需抽查，尤其是跨页、复杂合并单元格和多面板图。PNG 导出保留坐标轴与曲线的视觉内容，图中曲线的数值化需另行读取坐标和图例。

[Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api) · [OpenAlex API](https://help.openalex.org/api/authentication/) · [Unpaywall API](https://unpaywall.org/api) · [Ollama 结构化输出](https://docs.ollama.com/capabilities/structured-outputs)
