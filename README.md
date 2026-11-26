# 外包转写受理不明时的额度和客户结果

这里保存一套可脱机使用的合成运维资料。两个事件文件各自从相同清册开始，模拟两次独立运行。

## 清册和事件

`samples/inventory.json` 记录演练开始时已知的资源、对象与约定。额度和小计均为最小货币单位。周期范围左闭右开。每部门普通与应急余额分开，宁可保留敞口。两个出口属于同一供应商，报价是一次受理的最大供应商费用。

`samples/shift-log.jsonl` 和 `samples/definitive-void.jsonl` 每行是一个 JSON 对象。通用字段 `at` 是资料到达值班侧的演练秒，`kind` 标明来源记录的类型。同一秒内保持给定文件顺序。事实发生时间由记录自身字段提供。lookup=absent_pending 表示暂未可查。final_void 表示供应商保证无扣款且无执行。legacy 出口不保证重复请求去重，tracked 出口对相同 attempt 幂等并可查证。receipt 的 provider_operation 用于账单归属，bill 的 revision 是同一 charge 的新净额版本，不能叠加。账单可以有非本部门条目。intent 来自经过本地确认的客户，关键资料是 tenant/number/digest/language。

文件可包括申请、已发指令、状态观测和外部回执。出现于记录中的申请或意图可能未获准，外部回执可能迟到或与此前观测冲突。

## 浏览资料

在仓库根目录用 Python 3.11 运行。只需标准库。

```sh
python3 sample_reader.py samples/shift-log.jsonl
python3 -m unittest discover -s tests -v
```

原始资料可按 `--kind` 筛选。程序按接收顺序显示记录。单元测试核对资料能够读取，以及事件夹具的基本结构。
