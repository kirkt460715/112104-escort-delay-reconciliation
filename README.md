# 陪诊服务跨机构延误归因与窗口调整

接送队、服务站和院方为陪诊协调会提供的行程、劳务及窗口凭证。 所有人员、服务机构和业务凭证均为合成资料。

各时刻为 UTC+08:00。为便于上报，minute 按行程业务日 00:00 起计，超过 1440 的时刻属于次日。kind=canceled 为院方取消，no_replacement 用于确认没有替补；没有报告不能代替取消。

## 资料查阅

需要 Python 3.11.2，查看资料与运行资料检查只用标准库。CSV 使用 UTF-8，首行为字段名。空字段表示该项未填写或不适用，具体含义按各表约定。

```sh
python3 catalog.py
python3 -m unittest discover -s tests -v
```

例如，`python3 catalog.py --table flow_rules` 可读取该表原始记录。资料检查核对结构、唯一键及部分业务引用。

## 复盘与窗口试算

`review.py` 把行程复盘和窗口试算做成可重复使用的能力，供每次联合排期会使用。只读交换表，不改变台账。

```sh
python3 review.py journey E005 E016     # 单次行程复盘
python3 review.py report --flow weekday # 按流程与时段汇总
python3 review.py whatif J-A            # 单方案试算（缺省为全部方案）
python3 review.py whatif J-A J-B --combine  # 组合试算，检查可否一起承诺
python3 review.py acceptance            # 核准复核
```

各子命令加 `--json` 输出机器可读结果。

- **journey**。由已核验区间重建行程：每段给出可确定与可能的接续延误及归因；就绪区间重叠时保留不同归因成立的范围且同一段分钟只计一次；家属认可暂停覆盖的分钟不记服务方超时；等替补窗的分钟不全摊入原窗口责任方；取消与到院先后无法辨认时如实标注。对错过原窗口的行程逐项测试已登记方案的单一杠杆能否赶回。
- **report**。按同类流程和时段报告等待分布、脱落与归因支撑。原窗口、替补、取消分别给出可核对人次（附行程号），取消保留在母体，替补不重复计为完成。合并影响按可合计口径：同一行程上可同时成立的归因才合计，竞争分钟只计一次。
- **whatif**。用同一批行程证据比较移动接人窗、改报到截止或两边一起改；锁定预约（fixed=1）不动；带入车辆跨程到点与连班陪诊员周转约束（基线已不足的如实呈现）；取消人次保持不变。同占相同窗口的方案不可一起承诺；无排班表或凭证不足的排法带着证据缺口留作试用。
- **acceptance**。双方均确认的方案自生效日起适用，按那段时期的真实窗口重判是否改善；未生效方案只留作试用。

## 交换表

- `flow_rules.csv`。正常及改约情况下各环节依赖和最短业务耗时。依赖列表用 | 分隔。 主键由 `flow`、`stage` 确定。
- `journeys.csv`。报到业务窗、是否有替补及预约能否变动。锁定预约 fixed=1。 主键由 `journey` 确定。
- `time_proofs.csv`。各方已经核验的时刻区间，可以没有某一环节。 主键由 `journey`、`stage` 确定。
- `family_changes.csv`。家属认可的暂停时段。 主键由 `journey` 确定。
- `joint_variants.csv`。待协商可移动窗口。delta 是相对原计划的分钟变化，两列可同时适用。 主键由 `variant` 确定。
- `workday_schedule.csv`。按日可讨论的车辆、陪诊员和原锁定业务。turnaround 为同一班员间隔余量。 主键由 `day`、`journey` 确定。
- `window_acceptance.csv`。后续双方认可的窗口安排和生效日。未列出的方案未核准。 主键由 `variant` 确定。
