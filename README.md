# 果品等级换约下的分段结算

`资料/演练.json` 保存本地合成的业务记录，不含个人信息及实际企业数据。各资料集的字段、单位和有效范围见[资料格式](资料格式.md)。需要 Python 3.11，仅使用标准库，从项目根目录运行。

## 能力概览

引擎接收四类资料并识别段身份 `(shipment, segment)`：

- **逐箱读数** `boxes`：段内细化，不另加数量；`checks` 的闭区间替代单点读数，
  跨边界时保留全部可行档，绝不假设读数居中。
- **分选线交叉表** `grids`：同段按到件时间与递增 `revision` 替换，旧版留痕；
  上报形态区分为 `duplicate`（重复上报）、`partial`（逐箱补齐）、`standalone`（独立段）。
- **计量复核** `segment_limits` / `orders`：段与整单两级称重范围，作为硬约束。
- **交货及款项** `payments` / `pools`：双额度池（确定款 `advance`、争议准备额 `hold`）。

关键口径：

1. 逐箱覆盖部分按交货单生效合同直接落档，形成**确定收入**；
2. 仅有交叉表的匿名箱，在箱数、单箱重量、联合分档几何相交约束下，由运输型
   线性规划（`settle/lp.py`，无第三方依赖）求整单应付的**可行区间**——上下限
   各自由同一个完整（分档+重量）分配支撑，不是各档最多值的拼凑；
3. 换约价差通过同一批箱在两套合同下的联合可行格配对，给出价差与另一合同金额区间；
4. 总量与分项不能同时成立时，指出具体不能结算的段并隔离，其余已可核货照常出账；
5. 额度池先保护已有签发，新增按 `policy.priority`（交货日、编号）排序；
   已付款项只能另挂原凭证做补付/追回，撤回只释放未付款的 `signed`。

## 命令

```sh
# 资料包记录规模
python3 inspect_packet.py

# 整单应付区间、确定款、隔离段、换约价差（机器可读）
python3 -m settle.cli assess 资料/演练.json --alt old --alt new

# 果农口径：确定/估计/复核可解开金额/剩余证据差
python3 -m settle.cli farmer 资料/演练.json D1 --alt new

# 额度池分段结算安排（机器可读）
python3 -m settle.cli plan 资料/演练.json

# 财务口径：双额度占用、可签/待资/证据差、支付台账
python3 -m settle.cli finance 资料/演练.json
```

## 测试

```sh
python3 -m compileall -q settle tests inspect_packet.py
python3 -m unittest discover -s tests -v
```

测试覆盖分档几何与跨档复核、LP 端点手工核算、revision 替换、确定款、
隔离段、换约价差、额度优先与重复占额防护、补交证据后的额度重排，以及
确认/撤回/补付/追回台账。
