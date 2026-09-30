# 清算铁路委托运输收入协同基础服务

本项目提供综合交通运输业务共享的服务端基础能力：底层负责运营机构、交通节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计；其上构建**委托运输收入清算域**，把委托协议、运价版本、运行区段、席位/货运服务、渠道扣费与税费口径关联到确定的运输事实，先产出各方都能复算的结算草案，再按职责完成确认、争议、支付与关账。

## 清算域设计要点

- **规则版本化、不可变**：委托协议、运价版本（税率、渠道费率、区段里程单价，生效区间不重叠）、运行区段（归属唯一线路管理方）、席位/货运服务及顺序跨线径路；登记后只追加新版本，不回改。
- **运输事实只追加、幂等**：售票/联程、改签、部分退运、停运赔付、跨月回执按外部事件号去重；运价按**业务发生日**固化，回执跨月不改历史口径。
- **确定性可复算**：金额一律整数「分」，纯函数引擎按里程×单价权重用最大余数法分摊，运营方作为剩余项吸收舍入，成分分录代数和严格等于有符号票款（退运/赔付为负）。`shared_basis` 暴露规则、权重、票款与守恒汇总，任一参与方不依赖系统内部状态即可独立复算每条分录。
- **金额守恒、不重复计费**：任一事实在整个期间生命周期内只进入一个版本（`fact_inclusions` 唯一）；同一业务事件重放返回原回执。版本守恒为 `分录合计 = 本版本新事实票款 + 转入争议副本`。
- **确认/争议/支付按职责**：责任方确认己方分录；争议只把相关分录冻结为 `disputed`，无争议收入照常 `payable → paid`；关账要求无未决 `projected`。
- **晚到与关账后更正**：期间或版本关账后，晚到票务事件与未决争议一律生成新版本（`regular → adjustment → correction`）**追加分录**，已关账版本永不改写；旧争议分录置 `carried` 并结转副本，余额只计一次。
- **最小可见性**：参与方经 API 只能读取与己方相关的分录，外加共享复算依据；监管 `auditor` 可还原任一期间的规则快照、全量分录、争议与最终余额。

## 目录

- `src/transport_coordination/`：基础登记域、清算引擎（`clearing.py`）、清算服务（`clearing_service.py`）、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `tests/`：基础规则、确定性引擎守恒、清算工作流、可见性复算、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m transport_coordination.acceptance
PYTHONPATH=src python3 -m transport_coordination.clearing_acceptance
```

验收命令使用临时 SQLite 数据库。清算验收会回放联程销售、改签、部分退运、停运赔付与跨月晚到回执，核对金额守恒、幂等重放、争议只冻结相关项、无争议继续支付、关账后更正进入新版本、参与方可见性隔离与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，所有写操作需带唯一 `request_id` 实现幂等。清算域主要接口：

- 规则：`POST /agreements`、`/agreements/activate`、`/parties`、`/segments`、`/services`、`/tariff-versions`
- 事实：`POST /transport-facts`
- 期间与版本：`POST /periods`、`/periods/close`、`/settlements/generate`、`/settlements/close`、`/settlements/pay-confirmed`
- 职责操作：`POST /entries/confirm`、`/entries/pay`、`/disputes`、`/disputes/withdraw`
- 查询与审计：`GET /settlements/report?version_id=`、`GET /periods/balances?period_id=`、`GET /periods/audit?period_id=`（仅 auditor）

服务重启后 SQLite 中的规则、运输事实、结算版本、分录与审计历史继续保留。

