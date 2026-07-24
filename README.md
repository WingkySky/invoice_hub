# 票归集（Invoice Gather）

> 多供应商发票**自动归集与整理中枢**——把散落在多个邮箱、多个供应商的发票，自动抓取、识别、按公司归类、回填清单、打包交给财务，**每天省下 1–2 小时机械活**。

---

## 它解决什么问题

你每天要面对多个供应商的发票：逐个点开链接 → 分好所属公司 → 用软件识别金额和发票号 → 回填到一份清单 → 打包归集给财务做账。这套动作机械、重复、占用 1–2 小时。

**票归集** 把上面每一步都尽量自动化：

| 手工动作 | 票归集怎么做 |
|---|---|
| 一个个点开发票链接 | 多邮箱自动抓取（IMAP / 腾讯企业邮 API / 本地 PDF 导入） |
| 识别金额 + 发票号 | 数据驱动的字段提取（正则 + 布局 + OCR 兜底），入库即带金额/发票号/买方/销售方 |
| 分好所属公司 | 按「购买方」自动分组查看（**显式公司管理见路线图**） |
| 回填清单 | Excel 模板匹配回填（一对一 + 多对一凑票） |
| 打包给财务 | 勾选导出：PDF 打包 + 清单（CSV / MD / XLSX）下载为 zip |

最终目标：连"点一下"都省掉——**做成一个 Skill，让 agent 一句话帮你归集整理**。

---

## 核心能力

### 📥 多来源发票抓取
- **IMAP**：163 / QQ / Gmail / Outlook 等主流邮箱
- **腾讯企业邮箱官方 API**：可读取 IMAP 不可见的邮件归档（企业微信邮箱 / 经典企业邮箱专业版）
- 增量水位线，断点续拉；自动 / 手动 / 定时（每 N 分钟）抓取
- 本地 PDF 导入：`seed` 一个文件夹即可解析入库（补录 / 演示 / 历史归档）

### 🧾 发票解析（数据驱动）
- 标准增值税电子发票 / 数电票高命中率
- 正文链接式发票自动抓下载链接（51发票等平台支持国密解密）
- 字段（买方 / 金额 / 发票号 / 日期 / 销售方 / 城市）提取规则写在 `config/rules.json`，**换一种发票版式只改配置、不改代码**

### 🔁 模板匹配回填
- 上传含金额列的 Excel 模板，系统从库内发票按金额匹配回填发票号
- 第一轮：一对一精确匹配（到分），同金额用备注模糊打分；第二轮：多对一凑票（多行相加 = 一张发票）
- 列映射自动识别 + 手动调整，结果实时预览，一键导出回填后的 Excel

### 📊 查看、筛选与导出
- 顶部卡片：张数 / 合计 / 买方 TOP；表格按关键字 / 邮箱 / 城市 / 买方 / 日期筛选
- 勾选 / 全选 → 导出 PDF 打包 + 清单（CSV / MD / XLSX）为 zip
- 离线命令行导出 Excel / HTML

### 🤖 面向 agent / 程序调用
- 任意命令加 `--json` 即以 JSON 输出，便于 agent 解析与编排
- 这是「让 agent 自动归集」的接口地基（见路线图）

---

## 30 秒上手

### 1. 各邮箱开启 IMAP 并生成授权码
- **163**：设置 → POP3/SMTP/IMAP → 开启 IMAP → 生成授权码
- **QQ**：设置 → 账户 → 开启 IMAP/SMTP → 生成授权码
- **Gmail**：两步验证后生成「应用专用密码」
- **Outlook**：设置 → 同步 → 开启 IMAP
- **腾讯企业邮箱**：见文末「腾讯企业邮箱」章节

### 2. 建库 + 加账号
```bash
cd invoice_hub
python hub.py init          # 建 SQLite 库 + 播种 config/accounts.json
# 网页里「添加邮箱」填授权码，或命令行：
python hub.py accounts add --email gzfesco_waifu@163.com \
        --name waifu-163 --host imap.163.com --port 993 --password <授权码>
python hub.py accounts      # 查看已管理账号
```

### 3. 抓取 + 查看
```bash
python hub.py fetch --since 2026-07-09   # 拉取全部启用账号的发票邮件 → 入库
python hub.py serve --port 8000          # 起 Web 控制台
```
浏览器打开 http://127.0.0.1:8000 。

---

## 为什么"数据驱动"

代码只负责"操作数据"，所有管理对象都是数据，不在代码里写死：

- **账号 = 数据**：邮箱账号在 `accounts` 表，网页里增删 / 启停
- **解析规则 = 数据**：提取正则在 `config/rules.json`，换版式只改配置
- **发票记录 = 数据**：结果进 `invoices` 表，网页按任意条件筛选 / 排序 / 合计

查看界面是一个**从数据库读数据的通用网页**，而不是每次手搓一份写死的报告。

---

## 目录结构
```
invoice_hub/
  data/
    invoice_hub.db     # SQLite —— 唯一真相源（账号/邮件/发票）
    pdfs/              # 下载的发票 PDF 原件，按邮箱分文件夹
  config/
    accounts.json      # 账号种子（init 时播种；实际授权码用 accounts add 填）
    rules.json         # 发票字段提取规则（数据驱动，不写死在代码）
    tencent.json       # 腾讯企业邮箱 API 凭证（可选，不上传）
  web/
    templates/
      standard_template.xlsx   # 模板匹配标准模板
    app.py            # 标准库 HTTP 服务，把 DB 读成 JSON API
    index.html        # 通用数据驱动查看器（管理邮箱 + 筛选/合计发票 + 模板匹配）
  db.py               # 数据层：建表 / 账号CRUD / 发票查询
  engine.py           # 引擎：IMAP 拉取 + 按 rules.json 解析 + 入库
  matching.py         # 模板匹配回填引擎
  tencent_mail.py     # 腾讯企业邮箱官方 API 适配
  api.py              # 统一 API 封装（CLI / Web 共用）
  hub.py              # CLI 调度入口
```

---

## 命令行速查
```bash
python hub.py init                  # 建库 + 播种账号
python hub.py accounts list         # 列出邮箱账号
python hub.py accounts add ...      # 添加/更新邮箱账号
python hub.py accounts test <id>    # 测试账号连接
python hub.py fetch --since 2026-07-01   # 抓取全部启用账号
python hub.py seed <pdf_dir>        # 本地 PDF 导入
python hub.py report --xlsx out.xlsx     # 离线导出
python hub.py serve --port 8000     # 启动 Web 控制台
```
任意命令加 `--json` 即以 JSON 格式输出，便于 agent / 程序解析。

---

## Web 控制台

- **邮箱账号管理**：增 / 编辑（含 IMAP 主机·端口·授权码）/ 删 / 启停；每行「测试」验证连接；各账号独立设置抓取起始日期
- **抓取可视化**：「立即抓取全部」或单账号「抓取」；「抓取进度」实时滚动日志；可设「每 N 分钟自动抓取」
- **上传本地 PDF**：选多个文件直接解析入库，自动建 `upload@local` 账号
- **发票浏览**：顶部卡片（张数 / 合计 / 买方 TOP）；关键字 / 邮箱 / 城市 / 买方 / 日期筛选；点列头排序、点「查看」看 PDF；勾选导出 PDF 打包 + 清单 zip
- **模板匹配**：上传 Excel → 自动识别列映射；可手动调整列；配置日期范围 / 覆盖 / 最大凑票行数；结果预览（一对一 / 凑票 / 未匹配）；一键导出回填 Excel

---

## 腾讯企业邮箱
如使用腾讯企业邮箱（企业微信邮箱 / 经典企业邮箱专业版），可通过官方 API 抓取，支持读取 IMAP 不可见的邮件归档。

```json
// config/tencent.json（已在 .gitignore，不会上传）
{
  "variant": "wecom",            // wecom=企业微信邮箱；exmail=经典腾讯企业邮箱（专业版）
  "corpid": "你的企业ID",
  "corpsecret": "你的应用Secret",
  "email": "yourname@yourcompany.com"
}
```
经典版需管理员在后台开启邮件 API 权限。

---

## 没有邮箱凭据也能用：导入本地 PDF
```bash
python hub.py seed ../invoices_20260714/ --account 本地导入
```
把任意本地 PDF 文件夹按同样的规则解析入库，立刻能在网页查看——适合补录、演示、或先把历史发票归档。

---

## 离线导出
```bash
python hub.py report --xlsx data/发票汇总.xlsx --html data/发票汇总.html
```

---

## 已知边界 & 路线图

**诚实说明当前能力：**
- **按公司「显式」归集尚未做**：现在按发票「购买方」字段自动分组查看，但还没有独立的「公司清单 + 人工归属 / 纠正」机制（这正是下一步重点）。
- **解析质量不可观测**：暂无量级置信度 / 质量报告，个别版式识别为空时需手动去 `config/rules.json` 加 pattern。
- **合规待加固**：授权码当前按原样存储、Web 控制台无鉴权，仅限本机使用，请勿在共享机器上跑。

**下一步（按优先级）：**
1. **公司归属维度**——公司清单 + 自动建议归属 + 人工纠正 + 按公司分组（让「分所属公司」可控可纠）
2. **解析质量可观测**——字段级置信度 + 质量报告 + 待核对清单
3. **按公司一键打包给财务**——每家公司：PDF 包 + 清单
4. **抓取失败 / 未归属队列 + 一键重试**
5. **Skill / Agent 化接口**——封装「抓取 → 归属 → 按公司出包」，让 agent 一句话触发

> 完整产品诊断与优先级见 `docs/product-diagnosis-2026-07-23.md`。

---

## 隐私与合规
- `data/`、`config/accounts.json`、`config/tencent.json` 含发票与授权码，已在 `.gitignore` 忽略，**切勿上传**
- 发票含买方税号、金额等敏感个人信息；当前为本地单用户工具，请勿在多用户 / 共享环境部署
- Web 控制台绑定 `127.0.0.1`，但仍建议仅在可信本机使用

---

## 安装依赖
```bash
pip install -r requirements.txt
```

## 说明
- 正文链接式发票千差万别：引擎用启发式抓「下载 PDF」链接；个别站点需登录态 / 验证码时抓不到，会在 `note` 字段标记，可手动补。
- PDF 字段识别对标准增值税电子发票 / 数电票命中率高；版式特殊的识别为空时，去 `config/rules.json` 加对应 pattern 即可，无需改代码。
- `sample/` 为示例数据目录，已排除，不会上传到仓库。
