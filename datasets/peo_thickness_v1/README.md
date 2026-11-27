# 镁合金 PEO 膜厚训练数据 v1

## 语料与标签

以镁合金的等离子体电解氧化（PEO/MAO）涂层为范围。OpenAlex 与 Crossref 的四组检索词合并得到 235 条候选，标题筛选后保留 113 篇。题录表保留全名单、DOI、数据库、索引中的开放 PDF 链接和原文核验状态。部分候选只完成题名级筛选。两次 Crossref 请求失败，细节见检索记录。

训练目标为原文直接报告的未后处理 PEO 膜厚。基体牌号、处理时长、电解液类型以及样品或工艺组合须能从原文对齐。7 个研究分组共登记 56 条观测，WE43 的 No. 2 在 §3.1.2 与 §3.3 使用同一配方与处理时长，并报告同一膜厚和离散量。两处记录合并后为 55 条。原始值和单位见 `reviewed_observations.json`。Tu 等的 2 条膜厚先由本机 PDF 抽取，再核对原文第 5、10 页。其余数据从在线原文表格和结果段转录、核查。当前语料覆盖 AZ31、AZ31B、WE43 和 LA141。

| 数据来源 | 纳入试样组 | 膜厚定位 |
| --- | ---: | --- |
| [Hadzima 等](https://www.mdpi.com/2075-4701/10/11/1521) | 16 | 表 3–6 |
| [Wenxia Zhang 等](https://www.mdpi.com/2079-6412/12/9/1286) | 12 | §3.1.2、§3.3 |
| [Yuanyuan Zhu 等](https://doi.org/10.3390/coatings9030197) | 9 | 表 3 |
| [Niedźwiedź 等](https://www.mdpi.com/2079-6412/15/8/961) | 9 | 表 3 |
| [AZ31 含氟涂层研究](https://www.mdpi.com/2079-6412/9/12/826) | 4 | 表 2 |
| [Toulabifard 等](https://www.mdpi.com/2079-6412/10/10/937) | 3 | 表 2 的涡流测量列 |
| [Tu 等的 arXiv v1](https://arxiv.org/abs/1808.06587) | 2 | PDF 第 10 页 §3.2 正文 |

## 模型输入

- `training.csv` 每行代表一个工艺试样组。`thickness_um` 是以 µm 表示的目标。离散量和测量方法随记录保留，统计意义不明的 ± 标记为 `reported_plus_minus_unspecified`。
- `cleaning_report.json` 的 `features` 是输入列允许清单。`study_group` 是划分组键，DOI、样品号、定位、测量方法和标签离散量用于审核。电流密度分为设定值、峰值和未明口径，单位为 A/cm²。
- `electrolyte_g_l:` 开头的列为原报化学组分的质量浓度。水合盐、无水盐和液态水玻璃配方保留各自身份。空白表示该值未进入结构化字段。只有原文明确为零时写入 0，包括 C-0 的 KF。多阶段或正负脉冲不同频率记入来源说明，单值频率留空。
- 该版目标限于膜厚。`training.csv` 中的标签均为正文或表格值。条件不足的测量、图上估读、复合后处理层及冲突记录应保留在核查流程。
- 每次将整篇原始研究留出。可能复用同批数据的出版版本应填写同一 `study_group`。拟合及缺失值处理须在训练折内进行。示例已实跑 7 折，训练中位数与处理时间对数线性模型的折间平均 MAE 分别约为 12.30 和 12.69 µm。数据适合基线训练与验证方法开发，跨独立研究的泛化仍需更多数据。

## 复现

在 `literature_agent` 目录使用以下命令。实时检索结果会随数据库索引更新。

```sh
./run.sh datasets/peo_thickness_v1/search_config.json --search-only
./run.sh prepare-training datasets/peo_thickness_v1/reviewed_observations.json \
  --out datasets/peo_thickness_v1
```

新增研究可复制 `reviewed_observations.json` 中的一个 `studies` 项。每条 `samples` 记录保留唯一样品编号、过程及膜厚定位、原报单位与审查状态。已有抽取结果先核对样品归属和文献位置，复核后再登记为 `reviewed: true`。同一研究显示为预印本与出版版本时，用相同研究组键连接。
