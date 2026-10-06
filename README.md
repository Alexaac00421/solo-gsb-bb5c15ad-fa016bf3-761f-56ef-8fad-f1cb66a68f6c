# 实验室样品流转系统

基于 **Python + FastAPI + PostgreSQL** 的实验室样品流转管理 Web 应用，支持样品登记、交接流转、温度监控、自动隔离与完整审计导出。

## 功能

- **样品录入**：样品编号、允许温区（下限/上限）、当前位置。
- **交接流转**：提交含交出人、接收人、目的地的交接单；**接收人确认前样品位置不变**，确认后位置才更新为目的地。
- **温度监控**：记录温度读数；**越界读数使样品立即隔离，且该样品待确认的交接自动失效**。
- **隔离管控**：隔离期间禁止发起转移；仅能通过**注明理由的复核结论**解除隔离。
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
Dockerfile
docker-compose.yml
requirements.txt
```
