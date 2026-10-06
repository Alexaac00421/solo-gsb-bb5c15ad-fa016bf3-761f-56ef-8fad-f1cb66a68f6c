# 实验室样品流转系统

基于 **Python + FastAPI + PostgreSQL** 的实验室样品流转管理 Web 应用，支持样品登记、交接流转、温度监控、自动隔离与完整审计导出。

## 功能

- **样品录入**：样品编号、允许温区（下限/上限）、当前位置。
- **交接流转**：提交含交出人、接收人、目的地的交接单；**接收人确认前样品位置不变**，确认后位置才更新为目的地。
- **温度监控**：记录温度读数；**越界读数使样品立即隔离，且该样品待确认的交接自动失效**。
- **隔离管控**：隔离期间禁止发起转移；仅能通过**注明理由的复核结论**解除隔离。
- **设备故障登记**：冷库设备故障时登记故障编号、库位、起止时间、原因与操作者；系统按样品登记与已确认交接形成的位置时间段找出与故障时段有交集的样品，一次性固定影响清单及各自重叠时段，并将其中未隔离样品隔离、使其待确认交接失效。
- **读数更正**：更正保留原值、理由与操作者（形成更正链），**不会自动解除隔离**。
- **并发控制**：样品行级锁 + 条件更新（状态/版本号），并发确认或解除隔离时只有基于最新状态的操作生效，冲突返回明确提示。
- **页面呈现**：样品当前状态、流转时间线、温度与更正记录、异常（隔离）记录、审计时间线。
- **审计导出**：单个样品或全部审计记录可导出 CSV（UTF-8，含全部事件明细）。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后访问：

| 入口 | 地址 |
| --- | --- |
| Web 页面 | http://localhost:8000 |
| 健康检查 | http://localhost:8000/health |
| 全部审计导出 | http://localhost:8000/export.csv |

Compose 包含两个服务：`db`（PostgreSQL 16，含健康检查）与 `web`（FastAPI）。`web` 等待 `db` 就绪后启动，**建表等初始化步骤在应用启动时自动完成**；`SEED_DEMO=true` 时额外写入两条演示样品数据。

停止并清空数据：

```bash
docker compose down -v
```

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://lab:labpass@db:5432/labdb`（compose 内） | SQLAlchemy 数据库连接串 |
| `SEED_DEMO` | `true`（compose 内） | 启动时若无数据则写入演示样品 |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `lab` / `labpass` / `labdb` | 数据库账号（compose 的 db 服务） |

端口映射：`8000`（Web）、`5432`（PostgreSQL，便于本地调试，可在 compose 中删除）。

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 启动一个本地 PostgreSQL，或用 SQLite 快速体验：
export DATABASE_URL="sqlite:///./lab.db"
uvicorn app.main:app --reload --port 8000
```

## 设备故障登记（本轮新增）

管理人员在 **设备故障** 页面（导航栏「设备故障」→「+ 登记故障」，或 `GET /faults/new`）登记冷库设备故障：

| 字段 | 说明 |
| --- | --- |
| 故障编号 | 唯一；相同编号及登记字段重复提交返回原结果，编号相同而字段不同提示冲突 |
| 库位 | 发生故障的库位，与样品位置精确匹配 |
| 起止时间 | **左闭右开**；开始必须早于结束，且**结束时间不得晚于提交时刻** |
| 原因 / 操作者 | 必填 |

提交后系统在一个事务内原子完成：

1. **影响计算**：按样品登记（初始位置）与已确认交接（目的地变更）重建每个样品的位置时间段（左闭右开，最后一段延续至今），找出与故障时段有交集的样品，逐段固定重叠时段写入影响清单（`fault_impacts`）；没有受影响样品也保存空清单。
2. **状态变更**：清单中**当前未隔离**的样品立即隔离（生成隔离记录，原因注明故障编号与库位）、其待确认交接失效；**已隔离**样品仅关联该故障（`FaultImpact.quarantine_id` 指向现有有效隔离），不重复生成有效隔离。
3. **并发与幂等**：登记时按 id 升序锁定全部样品行，与交接确认、录温串行化，基于最新状态原子完成；任何失败整体回滚，不留部分清单或状态变更。

页面入口：

| 入口 | 地址 |
| --- | --- |
| 故障列表 | http://localhost:8000/faults |
| 登记故障 | http://localhost:8000/faults/new |
| 故障详情（影响清单及重叠时段/隔离依据） | http://localhost:8000/faults/{id} |

样品详情页新增「关联设备故障」卡片；解除隔离仍走样品详情页**注明理由的复核流程**。

业务规则验证（SQLite 内存库，无需启动服务）：

```bash
python tests/test_faults.py
```

## 业务规则与并发设计

- 所有状态变更在单事务内完成；样品状态变更前执行 `SELECT ... FOR UPDATE` 行锁，串行化互斥操作（如「记录越界温度」与「确认交接」）。
- 交接确认使用条件更新 `UPDATE ... WHERE status='PENDING'`；解除隔离使用 `WHERE status='QUARANTINED' AND version=:expected_version`（版本号随页面下发）。并发请求中只有基于最新状态的一个生效，其余收到 **409 冲突提示**（页面以红色消息条展示）。
- 温度读数原始值永不修改；更正写入独立记录（原值、新值、理由、操作者），页面同时展示原始值与当前有效值。
- 每次状态变更写入审计日志（事件类型、操作人、JSON 明细、时间），CSV 导出即完整审计流水。

## 项目结构

```
app/
  main.py        # FastAPI 路由、页面渲染、CSV 导出
  services.py    # 业务逻辑（事务、行锁、条件更新）
  models.py      # SQLAlchemy 模型
  database.py    # 引擎与会话
  templates/     # Jinja2 页面
  static/        # 样式
tests/
  test_faults.py # 设备故障登记业务规则验证
Dockerfile
docker-compose.yml
requirements.txt
```
