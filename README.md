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

## 交换表

- `flow_rules.csv`。正常及改约情况下各环节依赖和最短业务耗时。依赖列表用 | 分隔。 主键由 `flow`、`stage` 确定。
- `journeys.csv`。报到业务窗、是否有替补及预约能否变动。锁定预约 fixed=1。 主键由 `journey` 确定。
- `time_proofs.csv`。各方已经核验的时刻区间，可以没有某一环节。 主键由 `journey`、`stage` 确定。
- `family_changes.csv`。家属认可的暂停时段。 主键由 `journey` 确定。
- `joint_variants.csv`。待协商可移动窗口。delta 是相对原计划的分钟变化，两列可同时适用。 主键由 `variant` 确定。
- `workday_schedule.csv`。按日可讨论的车辆、陪诊员和原锁定业务。turnaround 为同一班员间隔余量。 主键由 `day`、`journey` 确定。
- `window_acceptance.csv`。后续双方认可的窗口安排和生效日。未列出的方案未核准。 主键由 `variant` 确定。
