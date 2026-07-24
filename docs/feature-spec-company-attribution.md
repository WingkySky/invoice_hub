# 功能规格（PRD）：公司归属维度（Company Attribution）

> 关联文档：`docs/product-diagnosis-2026-07-23.md`（优先级 **P0**）
> 产品：票归集（Invoice Gather，代码模块 `invoice_hub`）
> 作者/日期：产品通 · 2026-07-23
> 状态：**已与用户对齐**（2026-07-23 确认 Q1–Q5），可进入实现

---

## 0. 背景与上下文（为什么现在做）

代码事实（已复核 `db.py` / `matching.py` / `config/rules.json`）：

- `accounts` 表记录的是**邮箱账号**（name / email / imap 配置），不是公司。
- `invoices` 表只有 `buyer` 字段 —— 它是 PDF 自动解析出的**购买方名称字符串**（`rules.json` 用正则从"购买方名称：xxx 统一社会信用代码"里抠出来的）。
- `db.get_stats()` 当前的聚合维度是 `GROUP BY buyer`；`matching.py` 模板回填时也是按 `buyer` 做双向模糊匹配。
- 全链路**没有任何"公司实体"**：没有公司清单、没有 `buyer → 公司` 的映射表、没有人工纠正归属的入口。

而你的真实场景是：**每天从多个供应商把发票收拢 → 按"所属公司"分好 → 识别金额/号 → 回填清单 → 打包给财务**。其中"分好所属公司"是你手工流程里最机械、最容易错的一步，现在系统把它交给脆弱的 `buyer` 自动识别硬扛——解析失败就归到"未知"，别名/简写就和模板失配，且**你永远无法人工建立一张规范公司清单、无法纠正归属**。

这也是为什么诊断里把"多客户隔离"从 P2 修正为 **P0**：对你而言那不是"代账多租户"，而是核心工作流本身。本功能是后面"按公司打包导出""指标（归属准确率）""agent 自动归集"三件事的**共同地基**，必须先钉死。

---

## 1. 问题陈述（Problem Statement）

**谁遇到这个问题、多频繁**：你（发票归集枢纽角色），每天 1 次归集动作，每次涉及 N 家公司的几十上百张发票。

**问题**：系统没有"公司"维度，发票只能按自动解析出的 `buyer` 字符串归类和聚合。这导致——
1. 同一家公司因发票版式不同，解析出"广州南沙友谊人才服务有限公司""南沙友谊""南沙友谊人才"等多个 `buyer` 变体，**在聚合/匹配时四分五裂**；
2. 解析失败的发票 `buyer` 为空/错，被丢进"未知"，**你无法把它们归到正确公司**；
3. **你没有任何入口建立规范公司清单、也无法人工纠正**某张发票的所属公司。

**不解决的代价**：你仍要在导出/打包前，肉眼把"未知"和"变体"重新拼回正确的公司，等于那段最机械的"分所属公司"手工活没被吃掉——与"省下 1–2 小时"的核心目标直接冲突；也无法做到"按公司一键打包给财务"。

**证据**：代码复核（`db.py` 无 company 表 / `invoices` 无 `company_id`）；诊断报告 §10 修订版优先级（P0）。

---

## 2. 目标（Goals）

> 目标是**结果（outcome）**而非产出（output）。每条都可回答"怎么算成功"。

| # | 目标 | 类型 | 怎么算成功 |
|---|------|------|-----------|
| G1 | 用户能维护一张**规范的公司清单**（含别名），作为归属的真相源；清单可由归集发票**自动识别预填**、再人工编辑 | 用户目标 | 用户可增删改公司及其别名；支持从 `buyer` 去重一键导入候选 |
| G2 | 新抓取的发票能**自动归属**到公司（基于 `buyer` + 别名），无需每张手工分 | 用户目标 | 自动归属率 ≥ 90%（见 §6 指标） |
| G3 | 用户对"归错/未归"的发票能**一键纠正**（单张 + 批量） | 用户目标 | 人工纠正动作可在 ≤2 次点击内完成；批量按 id 列表完成 |
| G4 | 发票列表与汇总能**按公司分组/筛选**，为"按公司打包"铺路 | 用户目标 | 列表与统计支持 `company_id` 维度；导出携带 `company_id` |
| G5 | 现有历史发票可**一键回填**公司归属（迁移幂等、可重跑） | 业务目标 | 存量发票回填后，"未归类"占比 < 5% |
| G6 | 未归类/歧义发票**用户可感知、且能看到"为什么没归上"**；开发者可结构化排查原因 | 用户+工程目标 | 列表有醒目标识 + 结构化 `attribution_reason`；无需翻日志 |
| G7 | 为 agent 自动归集提供**结构化接口**（列出未归类 + 按 id 归属） | 业务目标 | 提供 `--json` 可读、可写归属的 API（不只为人类 UI） |

> 注：本功能**不直接**产出"省下手工时长"这个北极星指标——它是通过 G2/G3/G4 把"分所属公司"机械活自动化来贡献北极星。北极星本身的度量见独立的指标工作流（P2）。

---

## 3. 非目标（Non-Goals）

明确**这一版不做**，防范围蔓延：

1. **不做多用户 / 角色 / 权限**：本产品是单人本地工具（你本人），不做租户隔离、不做登录体系。→ 原因：场景是单归集人，做权限是过度工程；Web 鉴权是独立合规项（P1），不在此 PRD。
2. **不做下游对接（报销/税务/财务系统）**：本版只把"公司"在本地归好，不推送到任何外部系统。→ 原因：诊断已定为 Non-goal；且与"按公司打包导出"是不同工作流。
3. **不做 ML/语义自动分类模型**：归属靠"别名规则 + 模糊子串 + 人工纠正"，不训练分类器。→ 原因：数据量小、alias 规则已够用、ML 投入产出比低且不可解释。
4. **不改动 PDF 解析引擎**：`buyer` 仍由 `rules.json` 正则提取，本功能**消费** `buyer`，不重新解析。→ 原因：解析质量是另一个 P0 独立项，避免耦合。
5. **不做"按公司打包导出"的完整实现**：本 PRD 只交付"按公司分组/筛选 + 导出携带 `company_id` 的钩子"，真正的 PDF 包+清单生成归到 P1「按公司打包」。→ 原因：打包涉及文件组织/命名，是独立可交付单元。
6. **不做"自动定稿多家独立公司"**：遇到未知 `buyer` 时，系统可**自动生成候选公司**（以 `buyer` 为初始名，见 R1c），但**不会自动合并/定稿**——候选公司需用户确认、改名、合并别名。→ 原因：避免脏数据；归属权最终交还用户，但首屏不再空白（用户已确认 Q1）。

---

## 4. 用户故事（User Stories）

按优先级排序。角色统一为 **发票归集人（你）**；最后两条是 **agent 调用方 / 开发者**。

1. **作为发票归集人，我要能新建/编辑/删除一张「公司」记录（名称 + 别名 + 可选统一社会信用代码），以便建立规范的公司清单作为归属真相源。**
2. **作为发票归集人，我希望系统能从我已归集的发票里自动识别出候选公司（按 `buyer` 去重预填），以便我不用从零手工建清单、只补别名和改名。**
3. **作为发票归集人，我希望新抓取的每张发票自动按 `buyer` 匹配公司别名并打上归属，以便大部分发票无需我手工分。**
4. **作为发票归集人，当某张发票归属错误或为空（"未知"）时，我要能手动把它分到正确公司（单张或批量），以便纠正自动归属的偏差。**
5. **作为发票归集人，我要能给公司配多个别名（全称/简称/常见错写），以便同一家公司不同叫法都能命中归属。**
6. **作为发票归集人，我要能在发票列表和汇总里按公司筛选/分组，以便一眼看清每家公司归集情况、为打包做准备。**
7. **作为发票归集人，我要有一个"未归类 / 歧义"清单视图，以便集中处理自动没归上的发票（而不是在全部发票里翻）。**
8. **作为发票归集人，我希望未归类/歧义发票在列表有醒目标识，并直接显示"为什么没归上"（buyer 为空 / 无匹配别名 / 命中多公司 / 公司清单为空），以便一眼判断该补别名还是改归属。**
9. **作为发票归集人，我要能对历史发票一键回填公司归属，以便老数据也能按公司管理。**
10. **作为 agent 调用方，我要能用 `--json` 拿到"未归类发票列表"（含原因）并用公司 id 批量归属，以便让 agent 自动完成归集归类。**
11. **作为开发者，我希望每张发票记录归属状态与未识别原因（结构化字段 `attribution_status` + `attribution_reason`），以便排查解析/匹配问题、不靠翻滚动日志。**

**边界/异常故事**：
- 空状态：用户尚未建/导入任何公司时，列表照常显示，但所有发票均"公司清单为空"未归类，并提示"先从发票导入候选公司"。
- 歧义：某 `buyer` 同时命中多家公司别名时，**不自动选**，标记 `ambiguous` 并展示原因，由用户指定。
- 别名冲突：新建别名若与已有公司别名重叠，系统提示冲突，阻止保存或要求选择。

---

## 5. 需求（Requirements）

### 5.1 Must-Have（P0）—— 缺失则功能不成立

**R1 公司实体与 CRUD**
- 新增 `companies` 表（见 §7 数据模型）：`id`、`name`（规范名，唯一）、`aliases`（别名集合）、`tax_id`（可选，=统一社会信用代码）、`created_at`。
- 提供增/删/改/查接口（DB 函数 + API 端点 + Web UI 入口）。
- 验收：Given 用户提交公司名"广州南沙友谊人才服务有限公司"；When 保存；Then 库内出现一条 `companies` 记录且 `name` 唯一；重名保存被拒。

**R1c 从发票自动识别候选公司**（用户已确认 Q1）
- 端点 `POST /api/companies/import-from-invoices`：对 `invoices.buyer` 去重（非空），为每个 distinct buyer 生成一条**候选公司**（`name=buyer`、空别名、状态=候选）。
- 幂等：已存在同名规范公司则跳过；可重复执行，不产生重复。
- 验收：Given 库内有 buyer="南沙友谊""南沙友谊人才"两条；When 导入；Then 生成 2 条候选公司；再次执行不产生第 3 条。

**R2 发票挂接 `company_id`**
- `invoices` 表新增 `company_id INTEGER`（外键可空，指向 `companies.id`），加索引 `idx_inv_company`。
- 迁移幂等：仿 `db._migrate_invoices` 的 ALTER 补列模式，老库自动加列，不影响现有数据。
- 验收：现有 `invoices` 行在迁移后 `company_id` 为 NULL（待回填），应用正常启动、查询不报错。

**R3 自动归属（写入时）**
- 发票插入（`db.insert_invoice`）时调用 `resolve_company`（见 §7）：基于 `buyer` 对公司别名匹配，写入 `company_id` + `attribution_status` + `attribution_reason`（见 R3b）。
- 匹配规则：精确匹配 → 子串双向包含（与 `matching._buyer_match` 一致）→ 命中唯一公司写 `company_id`；命中多家写 NULL + `ambiguous`；无命中写 NULL + `unclassified`；`buyer` 为空写 NULL + `unclassified('buyer 为空(解析失败)')`；公司清单为空写 NULL + `unclassified('公司清单为空')`。
- 验收：Given 已建公司 A 别名含"南沙友谊"；When `buyer="广州南沙友谊人才服务有限公司"` 入库；Then `company_id=A.id`；Given 别名同时匹配 A、B；Then `company_id` 为 NULL 且 `attribution_status='ambiguous'`。

**R3b 归属状态与未识别原因（用户可感知 + 开发者可排查）**
- `invoices` 新增 `attribution_status`（枚举：`classified` / `unclassified` / `ambiguous`）与 `attribution_reason`（文本，如 `buyer 为空(解析失败)` / `无匹配别名` / `命中多公司(歧义)` / `公司清单为空`）。
- Web 列表对 `unclassified`/`ambiguous` 显示醒目标识（角标/颜色），并在行内/详情展示 `attribution_reason`，让用户**感知并理解**为何没归上。
- `--json` 输出与 DB 查询均携带这两字段，开发者无需翻日志即可排查。
- 验收：Given 一张 `buyer` 为空的发票；When 入库；Then `attribution_status='unclassified'` 且 `attribution_reason='buyer 为空(解析失败)'`；Given 命中多家；Then `status='ambiguous'`、`reason='命中多公司(歧义)'`。

**R4 人工纠正归属（单张 + 批量）**
- API：`POST /api/invoices/assign`，body `{invoice_ids:[...], company_id:N}`（company_id 可为 null 表示"置为未归类"）。
- Web UI：列表行内下拉/多选批量改公司。
- 验收：Given 选 3 张发票并指定公司 B；When 提交；Then 这 3 张 `company_id` 更新为 B、`attribution_status='classified'`、`attribution_reason='人工指定'`；空选提交被拒。

**R5 按公司筛选/分组**
- `db.get_invoices` 的 `filters` 支持 `company_id`；`db.get_stats` 增加"按公司聚合"路径（或新增 `get_stats_by_company`）。
- 验收：Given 筛选 `company_id=A`；When 查询；Then 仅返回 A 的发票；统计按公司拆分金额/张数正确。

**R6 未归类/歧义队列视图**
- 列表支持过滤 `attribution_status IN ('unclassified','ambiguous')`；视图内展示 `attribution_reason` 与 `buyer`，便于用户判断该补别名还是改归属。
- 验收：Given 存在 5 张未归类发票；When 打开该视图；Then 仅显示这 5 张并各自带原因。

**R7 历史回填**
- 端点 `POST /api/companies/backfill`（或命令 `python hub.py backfill-company`）：对 `company_id IS NULL` 的历史发票重跑 R3 逻辑；幂等可重跑。
- 验收：Given 存量 100 张未归类且已建覆盖别名；When 执行回填；Then 自动归属 ≥90 张，剩余进入未归类队列；重复执行不产生重复/错误。

### 5.2 Should-Have（P1）—— 核心价值已成立，作为快跟

**R8 未知 `buyer` 的"建议创建公司"提示**：当同一未知 `buyer` 出现多次（如 ≥3 张），在未归类视图给出"据此新建公司/别名"的一键建议，但仍需用户确认。
**R9 歧义标记可视化**：`ambiguous` 发票在列表有更高优先级醒目标识（已部分由 R3b 覆盖，此处强化排序/角标）。
**R10 导出钩子**：`get_invoices_by_ids` / 导出接口在返回中携带 `company_id` 与 `company_name`，供 P1「按公司打包」直接消费（本版不实现打包本身）。

### 5.3 Could-Have（P2）—— 架构保险，本版不建但设计要兼容

**R11 模糊匹配阈值/策略可配置**（精确 / 子串 / 编辑距离），存入配置而非写死。
**R12 公司与模板的默认绑定**：为 `matching.py` 提供"按公司选默认模板"的扩展点（当前 matching 按 buyer，不改逻辑，仅留接口）。

### 5.4 Won't-Have（this time）
- 多用户/权限（见 §3-1）
- 下游系统对接（见 §3-2）
- ML 分类（见 §3-3）
- 改解析引擎（见 §3-4）
- 完整按公司打包导出（见 §3-5，归 P1）
- 自动定稿多家独立公司（见 §3-6）

---

## 6. 成功指标（Success Metrics）

> 基线说明：当前**无埋点**（诊断 P0-2），故绝对值基线待指标工作流（P2）上线后获取。下表给**目标假设**，上线 2 周后校准。

**领先指标（Leading，上线数日内可见）**
| 指标 | 定义 | 基线（待采） | 目标 | 测量方式 |
|------|------|------|------|---------|
| 自动归属率 | 新发票中 `attribution_status='classified'` 占比 | 0%（无此功能） | ≥ 90% | 入库时统计 |
| 人工纠正率 | 被手动改过归属的发票占比 | — | < 10% | assign 接口计数 |
| 未归类占比 | `attribution_status='unclassified'` 占比（存量回填后） | 100% | < 5% | 周期性统计 |
| 歧义率 | `attribution_status='ambiguous'` 占比 | — | < 3% | 写入时统计 |
| 原因可解释率 | 未归类/歧义发票中带结构化 `attribution_reason` 的占比 | 0% | 100% | 字段统计 |
| 单次归集归属耗时 | 一批发票从抓取到全部归类完的人工操作时长 | 手工≈数十分钟 | < 5 分钟 | 用户计时/埋点 |

**滞后指标（Lagging，数周）**
- 北极星贡献：**每天省下的归集手工时长**（1–2h → ≈0）——本功能通过 G2/G3/G4 贡献，整体度量见指标工作流。
- 打包完整率（P1 上线后）：交给财务的"按公司包"是否 100% 覆盖当月发票。

**评估节奏**：上线后 1 周看领先指标初值，2 周校准目标，1 个月复盘对北极星的贡献。

---

## 7. 数据模型建议（技术附录 · 供工程评审）

> 仅描述**增量变更**，不改动现有 `accounts` / `emails` / `invoices` 其它字段。沿用现有迁移风格（`db.py` 的 `_migrate_*` 幂等 ALTER）。

**新增 `companies` 表**
```sql
CREATE TABLE IF NOT EXISTS companies(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,        -- 规范公司名（候选公司初始即 buyer）
  tax_id     TEXT,                        -- 可选：统一社会信用代码(企业信用代码)，用于校验/匹配；
                                          --   注意 ≠ 发票"税收分类编码"(税目编号，属商品行项目，不在本功能范围)
  aliases    TEXT,                        -- JSON 数组，如 ["南沙友谊","南沙友谊人才"]
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

**`invoices` 增量**
```sql
ALTER TABLE invoices ADD COLUMN company_id         INTEGER;  -- 指向 companies.id，可空
ALTER TABLE invoices ADD COLUMN attribution_status TEXT DEFAULT 'unclassified'; -- classified/unclassified/ambiguous
ALTER TABLE invoices ADD COLUMN attribution_reason TEXT;    -- 未识别的结构化原因（用户+开发者可见）
CREATE INDEX IF NOT EXISTS idx_inv_company ON invoices(company_id);
CREATE INDEX IF NOT EXISTS idx_inv_attr    ON invoices(attribution_status);
```

**自动归属伪逻辑（在 `insert_invoice` 内或触发器式调用，写入三字段）**
```
def resolve_company(buyer, has_any_company):
    if not has_any_company:
        return (None, 'unclassified', '公司清单为空')
    if not buyer:
        return (None, 'unclassified', 'buyer 为空(解析失败)')
    hits = [c for c in companies if buyer==c.name or any(buyer in a or a in buyer for a in c.aliases)]
    if len(hits) == 1: return (hits[0].id, 'classified',  '自动匹配别名')
    if len(hits) > 1:  return (None,        'ambiguous',  '命中多公司(歧义)')
    return (None, 'unclassified', '无匹配别名')
```

**API 端点建议（新增，复用现有 `api.py` 风格）**
- `GET  /api/companies` —— 公司清单
- `POST /api/companies` —— 新建（含 aliases / tax_id）
- `POST /api/companies/import-from-invoices` —— 从去重 buyer 导入候选公司（R1c）
- `PUT  /api/companies/<id>` / `DELETE /api/companies/<id>`
- `POST /api/invoices/assign` —— 批量归属 `{invoice_ids, company_id}`
- `POST /api/companies/backfill` —— 历史回填
- 列表/统计接口增加 `company_id` / `attribution_status` 过滤参数（兼容 `--json` 输出，供 agent）

**与现有逻辑的关系**
- `matching.py` 的 buyer 匹配**不改**，它继续按 `buyer` 工作；公司归属是更上层维度。后续 R12 可让 matching 优先按 `company_id` 选模板，但本版不动。
- `get_stats` 现有 `by_buyer` 聚合保留（向后兼容），新增按公司维度。

---

## 8. 开放问题（已与用户对齐 · 2026-07-23）

| # | 问题 | 结论 |
|---|------|------|
| Q1 | 公司清单初始来源？ | **自动识别 + 可编辑**：系统从 `invoices.buyer` 去重自动生成候选公司（R1c），用户再改名/合并/补别名。已落地为 R1c。 |
| Q2 | 别名匹配策略？ | 本版用"子串双向包含"（与 `matching._buyer_match` 一致）；阈值/编辑距离作为 P2 可配置（R11），不阻塞。 |
| Q3 | 歧义发票未处理能否进打包？ | 打包（P1）时再定；本版仅标记 `ambiguous` 并让用户感知（R3b）。 |
| Q4 | `tax_id` 是什么 / 是否采集？ | `tax_id` = **统一社会信用代码（企业信用代码，18 位，标识"哪家公司"）**，用于校验/匹配；**≠ 发票"税收分类编码"（税目编号，标的是商品/服务类别，属另一维度，本期不做）**。字段可空预留，是否采集待合规评估，不阻塞。 |
| Q5 | 未识别如何让用户/开发者感知？ | 新增 `attribution_status` + `attribution_reason` 结构化字段（R3b）+ Web 醒目标识：用户可见原因、开发者可排查，无需翻日志。 |

---

## 9. 时间与排期考量（Timeline）

- **无硬截止**：属内部效率工具，无对外合同/合规死线（合规加固在 P1 单独排）。
- **建议分期**：R1–R7（P0）作为单个可交付单元，做完即能"导入候选 + 自动归属 + 纠正 + 按公司看 + 感知原因"；R8–R10（P1）紧随；打包（P1 另一 PRD）与指标（P2）并行不阻塞。
- **依赖**：本 PRD 不依赖其它功能；但它**被**「按公司打包导出」「指标（归属准确率）」「agent 自动归集」依赖，应优先排。

---

## 10. 实现评估（代码改动点 · 回应"是否进行代码优化"）

> 结论先行：**值得做，且应现在做**（它是打包/指标/agent 化的地基）。本功能以**新增**为主，不是对既有逻辑的大改；但有几个点会顺带优化既有实现。

**A. 新增（主要工作量）**
- `db.py`：新增 `companies` 表 + `invoices.company_id/attribution_status/attribution_reason`；在 `_migrate_invoices` 旁加 `_migrate_company` 幂等补列；新增 CRUD、`resolve_company`、`backfill`、`import_from_invoices` 函数。
- `engine.py`（入库处）：调用 `resolve_company` 写入三字段（用事务包住"解析 + 归属"，避免部分写入）。
- `api.py`：新增 §7 列出的端点（含 `import-from-invoices` / `assign` / `backfill`）。
- `web/app.py`：公司管理页 + 列表归属列/筛选/未归类·歧义角标 + 原因展示。

**B. 既有代码的可优化点（顺带，尊重既有行为）**
- `get_stats` 当前 `GROUP BY buyer`；新增按公司聚合路径，建议抽公共聚合函数，避免两份逻辑漂移。
- `matching.py` 的 `_buyer_match` 与归属匹配逻辑重复，可抽取为共享 `buyer_match()` 工具，归属与模板回填共用，减少漂移（**不改匹配结果**）。
- `insert_invoice` 用事务包住"解析 + 归属写入"，避免部分写入导致状态不一致。

**C. 工作量与风险**
- 预估：中等（约 1–2 天，含 Web）。风险低：纯增量、迁移幂等、不碰解析引擎与匹配核心结果。
- 主要风险：别名歧义导致误归属（用 `ambiguous` + 人工确认兜底）；回填大表时的写锁（沿用现有分批提交模式）。

---

*本 PRD 基于代码事实与用户自述场景撰写（N=1，即用户本人）。已与用户对齐 Q1–Q5，可进入实现；建议下一步落地 R1–R7。*
