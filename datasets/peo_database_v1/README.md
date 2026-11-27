# 镁合金 PEO 工艺、结构与性能数据库 v1

## 数据范围

本库在膜厚训练数据的基础上加入工艺、微观结构、测试条件、观测结果以及可追溯的原文图表。数据库内有 113 篇题录、7 篇带核定观测的研究、60 个试样组和 251 条复核观测。结构观测 74 条，其中 7 条为 SEM 形貌描述。性能观测 177 条。另有 23 条智能体抽取原始记录用于复核。

- 工艺记录含基体牌号、电解液及组分浓度、处理时间、供电模式、频率、占空比和可确认口径的电流密度。
- 结构记录含膜厚、表面孔隙面积分数、截面孔隙面积分数、Ra、Rz 和显微形貌描述。图件包括表面及截面 SEM、EDS 原表、加工波形和放电图。孔隙率的测量部位及方法可沿检测协议查询。[孔隙率与粗糙度原表](https://www.mdpi.com/2079-6412/10/10/937#table_body_display_coatings-10-00937-t002)
- 性能记录含极化电阻 Rp、腐蚀电流密度、电位及磨损体积。保留 EIS 介质和浸泡时点，区分 SBF 与 Hanks 极化协议，并保存磨损载荷与滑动参数。制备时长与测试时长分别存储。[EIS 表 8–11](https://www.mdpi.com/2075-4701/10/11/1521) · [SBF 极化数据](https://www.mdpi.com/2079-6412/9/12/826) · [Hanks 极化数据](https://www.mdpi.com/2079-6412/12/9/1286)
- 24 幅图件和 7 个表格文件内嵌在 SQLite 中。图表索引附原文来源、许可标记及 SHA-256，91 条试样关联保留可以确认的面板标识。期刊开放图件按原始文件保存。本地 PDF 图件按原页保存，请按对应论文许可使用。5 个表格文件为明确标注的原表选列转录，另外 2 个为智能体提取的 EDS 原表。

## 表与视图

| 对象 | 用途 |
| --- | --- |
| `papers`、`samples` | DOI 题录、研究分组和样品键 |
| `processes`、`electrolyte_components` | 样品制备参数、组分质量浓度及原文定位 |
| `protocols`、`observations` | 方法、介质、测试时长、数值或定性描述、原报单位和出处 |
| `assets`、`sample_assets` | 原始图表字节、索引与试样/面板关系 |
| `auto_candidates` | 待复核候选，保留任务标识、原始数据和证据 |
| `model_candidates` | 数值、制备参数、检测协议和研究分组的联表，共 244 行 |
| `sample_figure_index` | 已确认试样对应关系的图件索引 |

## 预处理与模型选择

膜厚和粗糙度统一为 µm，电流密度为 A/cm²，制备时间为 s，测试时间为 h，磨损体积为 mm³。百分数保留测量部位，缺失项保存为 NULL，原报数值与单位另存。图件完整保留标尺、坐标轴与图例，曲线作为图像证据。

入模时指定 `metric` 和兼容的 `protocol_id`，并按 `study_group` 划分训练、验证和测试。避免将 DOI、样品编号、原文定位以及与目标同次测得的结果作为输入。Rp 的 144 行是同一研究的 16 个样品组各 9 个时点。磨损条目也来自单项研究。它们可用于研究内对照及流程测试，独立研究的泛化评价需要扩充数据。同一多面板图及其关联样品随论文分组，以防图像进入不同数据折。

示例查询返回指定 NaCl 和时点下的 Rp：

```sql
SELECT study_group, sample_key, duration_s, current_setpoint_a_cm2, value, unit
FROM model_candidates
WHERE metric = 'polarization_resistance' AND protocol_id = 'hadzima:eis' AND exposure_h = 24;
```

## 复建与导出

在 `literature_agent` 目录运行。数据库可单独复制使用。复建依赖输入配置、核定记录、同目录 `sources` 及所引用的原智能体运行目录。

```bash
./run.sh build-database datasets/peo_database_v1/database_inputs.json \
  --out datasets/peo_database_v1/peo.sqlite --export datasets/peo_database_v1/export
```

`export` 提供数值、组织描述、测试协议、工艺、电解液组分和图件索引的 CSV，以及可直接打开的图件和表格文件。`reviewed_additions.json` 保存此次逐源记录的核定观测与图件面板关联。后续扩充可把新的导出目录加入 `database_inputs.json` 的 `runs`，并在原文复核后增补核定记录及试样关联。
