# 多维分析实验平台

面向实验设计、指标计算、样本分析、效果评估与结论发布的数据分析实验平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实验设计 | `experiment` | 实验方案 | 方案编号、实验名称、对照组说明 |
| 样本分群 | `sample_group` | 样本分群 | 分群编号、分群名称、筛选条件 |
| 指标定义 | `metric_def` | 指标定义 | 指标编号、指标名称、计算公式 |
| AB结果分析 | `ab_result` | 分析报告 | 报告编号、实验编号、分析指标 |
| 分群对比 | `segment_compare` | 对比任务 | 任务编号、分群A、分群B |
| 多维下钻 | `drill_down` | 下钻维度 | 维度编号、维度名称、下钻层级 |
| 数据筛选 | `data_filter` | 筛选规则 | 规则编号、数据来源、筛选字段 |
| 归因分析 | `attr_analysis` | 归因任务 | 任务编号、目标指标、候选因子 |
| 异常检测 | `anomaly_detect` | 检测规则 | 规则编号、监控指标、检测算法 |
| 预测模型 | `forecast_model` | 模型配置 | 模型编号、预测对象、历史窗口 |
| 数据导出 | `data_export` | 导出任务 | 任务编号、数据来源、导出格式 |
| 同期群追踪 | `cohort_track` | 同期群定义 | 群组编号、群组名称、首次行为 |
| 可视化配置 | `chart_config` | 图表配置 | 图表编号、图表类型、数据来源 |
| 洞察报告 | `insight_report` | 洞察报告 | 报告编号、分析主题、数据来源 |
| 数据新鲜度 | `data_freshness` | 新鲜度监控 | 监控编号、数据表名、最新分区 |
| 口径注册 | `metric_alias` | 口径版本 | 版本编号、指标编号、变更说明 |
| 实验因子 | `expr_factor` | 因子配置 | 因子编号、因子名称、因子层级 |
| 评估看板 | `evaluate_board` | 看板配置 | 看板编号、看板名称、关联实验 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## V016 数据导出：导出文件清单

数据导出原先只有任务状态，V016 新增「导出文件清单」域，读取导出任务
（`data_export`）与洞察结论（`insight_report`）两张数据表，按字段清单整理出
可交接的导出文件，文件内容可回查报告版本与导出批次。

新增数据表（`app/seed.py`，内存仓库，事务见 `app/store.py` 的 `transaction()`）：

| 表 | 用途 |
| --- | --- |
| `export_manifest` | 导出文件清单头：任务编号、导出批次、报告编号、报告版本、版本捕获方式、后续新版本、留档批次号 |
| `export_file` | 清单内文件记录：字段清单、内容指纹、下载路径、书签登记、结论快照 |
| `export_checkpoint` | 片段检查点：中断后只续写缺失片段，已写片段幂等不重写 |
| `export_residual` | 残留记录：出现残留时按文件清单核对并在事务中整体回滚 |
| `export_audit` | V016 留档核对记录（`V016-ARC-xxxx`） |
| `export_handoff` | 交接记录：V016 核对无误后转交接收方的导出文件清单 |

关键约束（实现于 `app/services/export_manifest.py`）：

- 文件内容与导出批次在同一事务提交；任何一步失败整体回滚，不会出现没有版本说明的旧包。
- 文件按固定规则渲染并记录 SHA-256 指纹，重复导出不会生成内容不同的同名文件；
  新批次用批次号隔离文件名，上一份完整文件始终可下载。
- 版本冲突两种策略：最新版本（导出中可刷新为当前最新报告版本与生成时间）、
  冻结版本（保留任务冻结时捕获的版本，只列出后续新版本，文件结论快照不可变）。
- 报告发布新版本走 `POST /api/insight_report/{id}/versions`，与导出批次在同一事务联动；
  洞察报告侧用 `GET /api/insight_report/v016/cross-check?report_no=` 按报告编号
  核对各批次文件里的结论版本（数据导出侧等价入口
  `GET /api/data_export/reports/{report_no}/cross-check`）。
- 前端导航页表格与详情抽屉共用 `views/data_export/useExportManifests.ts`
  的同一份业务数据；洞察报告页提供版本发布与跨模块核对面板。

领域：数据工程
子方向：数据分析与数据应用
