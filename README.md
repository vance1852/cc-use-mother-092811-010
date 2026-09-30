# 清算铁路委托运输收入协同基础服务

本项目提供综合交通运输业务共享的服务端基础能力，负责运营机构、交通节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域服务可以在这些稳定边界上扩展自己的状态、规则和接口。

在基础能力之上，`transport_coordination.clearing` 子系统实现了一套**委托运输收入清算系统**：把委托协议、生效日期版本化的运价、运行区段、席位/货运服务、渠道扣费与税费口径关联到确定的运输事实，先生成参与方都能复算的结算草案，再按职责确认与关账。

## 清算模型

- **规则层**：委托协议绑定线路管理方、客货运营方与代售渠道；运价按生效日期版本化，记录各区段里程、里程单价、线路/运营分成基点、渠道扣费率、税口径（价内/价外）与停运赔付分担。规则带稳定指纹，供各参与方独立复算。
- **事实层（只追加）**：售票/运单、改签、部分退运、停运赔付、跨月回执、关账后追加分录。`request_id` 与 `event_id` 双重幂等，同一业务事件重放绝不重复计费。
- **分录层（金额守恒）**：纯函数引擎（`clearing/engine.py`）把规则+事实清算为一组和恒为 0 的整数辅币分录；联程按区段里程权重用最大余数法精确拆票，改签对原票逐行精确取负再记新票，部分退运按当前有效收入比例冲回，`@customer`/`@tax` 外部账户保证全口径守恒。
- **关账层（只追加新版本）**：分录一旦物化即不可变，期间版本是事件集合快照。晚到票务事件不推翻已确认/已关账分账，只进入新版本；open 草案可吸纳新事件重建，确认或关账后冻结。关账后更正只能以零和追加分录进入新版本（可跨月）。
- **争议隔离**：争议只挂在单条分录上（open/upheld 计入冻结），同账户、同事件的无争议收入照常支付；裁决放行则解冻。
- **权限与审计**：参与方经 API 只能读取本组织明细与共享规则依据，operator 不能做审计还原；auditor/admin 可经 `/clearing/audit` 还原任一期间的规则指纹、事件集合、分录、争议、确认记录与最终余额。全部动作进入同一条哈希串联审计链。

## 目录

- `src/transport_coordination/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/transport_coordination/clearing/`：清算引擎（纯函数）、清算服务（事务/幂等/版本/争议/关账）、HTTP 路由与离线验收；
- `tests/`：基础规则、事务边界、接口路由、清算引擎、清算服务与端到端验收测试。

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
PYTHONPATH=src python3 -m transport_coordination.clearing.acceptance
```

验收命令会在临时 SQLite 数据库中登记运营机构、操作者、交通节点和参考资料，核对幂等回执与审计链；清算验收则走完联程售票、改签精确冲回、部分退运、停运赔付、跨月回执、争议冻结、按职责确认关账、晚到事件进入新版本、关账后跨月追加分录更正与监管审计还原。成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## 清算 HTTP 接口

所有写接口均需 `X-Actor-Id` 与请求体内的 `request_id`（幂等键）。

| 方法 & 路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /clearing/agreements` | admin | 登记委托协议（多方组织） |
| `POST /clearing/fare-versions` | admin | 登记生效日期版本化运价 |
| `POST /clearing/events` | admin/operator | 接收运输事实（sale/change/refund/cancel_payout/receipt/adjustment） |
| `POST /clearing/drafts/build` | admin/operator | 生成/重建期间结算草案 |
| `POST /clearing/drafts/confirm` | admin/operator | 参与方按职责确认版本 |
| `POST /clearing/periods/close` | admin/operator | 关账并固化余额 |
| `POST /clearing/disputes`、`/clearing/disputes/resolve` | admin/operator | 对单条分录提争议/裁决 |
| `GET /clearing/lines` | 各方 | 参与方只读自己的分录；auditor/admin 读全量 |
| `GET /clearing/basis?agreement_id=` | 参与方/审计 | 读取规则版本与指纹用于复算 |
| `GET /clearing/drafts?period=&version_no=` | 各方 | 读取草案/关账版本 |
| `GET /clearing/balances?period=` | 各方 | 读取关账后最终余额 |
| `GET /clearing/audit?period=` | auditor/admin | 还原任一期间规则、分录、争议与余额 |

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。
