# 多邮箱发票中枢（数据驱动版）

一个**数据驱动**的本地发票管理中枢：把多个邮箱账号、抓取到的邮件、解析后的发票记录
全部放进一个 **SQLite 数据库**（唯一真相源），代码只负责"操作数据"，查看界面是
一个**从数据库读数据的通用网页**，而不是每次手搓一份写死的报告。

## 为什么是"数据驱动"
- **账号 = 数据**：邮箱账号存在 `accounts` 表里，可在网页里直接增删/启停，不必改代码。
- **解析规则 = 数据**：发票字段（买方/金额/发票号/日期/销售方/城市）的提取正则写在
  `config/rules.json` 里，换一种发票版式只需加 pattern。
- **发票记录 = 数据**：所有结果进 `invoices` 表，网页按任意条件筛选/排序/合计，无硬编码内容。

## 核心功能

### 📥 多邮箱发票抓取
- 支持 IMAP 协议：163 / QQ / Gmail / Outlook 等主流邮箱
- 支持腾讯企业邮箱官方 API（企业微信邮箱 / 经典企业邮箱专业版），可读取 IMAP 不可见的归档邮件
- 可按账号设置各自的抓取起始日期偏好
- 自动抓取 + 手动抓取 + 定时自动抓取（可设间隔分钟数）

### 🧾 发票解析
- 标准增值税电子发票 / 数电票高命中率
- PDF 正文链接式发票自动抓取下载链接（51发票等平台支持解密）
- 字段识别规则在 `config/rules.json`，数据驱动，无需改代码

### 🔍 模板匹配回填（新）
- 上传 Excel 模板（含金额列），系统自动从库内发票按金额匹配回填发票号
- 第一轮：一对一金额精确匹配（精确到分），相同金额时用备注模糊校验打分
- 第二轮：多对一凑票（模板多行金额相加 = 一张发票金额）
- 支持列映射自动识别 + 手动调整
- 可配置日期范围、是否覆盖已有发票号、最大凑票行数等参数
- 匹配结果实时预览，一键导出回填后的 Excel

### 📊 数据查看与导出
- 顶部卡片：张数 / 合计金额 / 买方 TOP
- 表格筛选：关键字 / 邮箱 / 城市 / 买方 / 日期
- 点列头排序，点「查看」预览 PDF
- 勾选导出：PDF 打包 + 清单（CSV / MD / XLSX），下载为一个 zip
- 离线命令行导出 Excel / HTML

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

## 三步使用

### 1. 各邮箱开启 IMAP 并生成授权码
- **163**：mail.163.com → 设置 → POP3/SMTP/IMAP → 开启 IMAP → 生成授权码
- **QQ**：mail.qq.com → 设置 → 账户 → 开启 IMAP/SMTP → 生成授权码
- **Gmail**：两步验证后生成"应用专用密码"
- **Outlook**：outlook.com → 设置 → 同步 → 开启 IMAP
- **腾讯企业邮箱**：见下方「腾讯企业邮箱」章节

### 2. 建库 + 加账号
```bash
cd invoice_hub

python hub.py init          # 建 SQLite 库 + 播种 config/accounts.json
# 在网页里「添加邮箱」填入授权码，或命令行：
python hub.py accounts add --email gzfesco_waifu@163.com \
        --name waifu-163 --host imap.163.com --port 993 --password <授权码>
python hub.py accounts      # 查看已管理账号
```

### 3. 抓取 + 查看
```bash
python hub.py fetch --since 2026-07-09   # 拉取全部启用账号的发票邮件 → 入库
python hub.py serve --port 8000           # 起 Web 控制台
```
浏览器打开 http://127.0.0.1:8000 。

## Web 控制台功能

### 邮箱账号管理
- 增 / 编辑（含 IMAP 主机·端口·授权码）/ 删 / 启停
- 每行「测试」按钮验证连接是否通
- 各账号独立设置抓取起始日期偏好

### 抓取可视化
- 「立即抓取全部」或单账号「抓取」→ 后台拉取
- 「抓取进度」面板实时滚动日志
- 可设「每 N 分钟自动抓取」

### 上传本地 PDF
- 点「上传本地PDF」选多个文件 → 直接解析入库
- 自动建 `upload@local` 账号，不必走命令行

### 发票浏览
- 顶部卡片显示张数 / 合计 / 买方 TOP
- 表格按关键字/邮箱/城市/买方/日期筛选
- 点列头排序、点「查看」看 PDF
- 勾选 / 全选 → 导出选中：PDF 打包 + 清单（CSV / MD / XLSX），下载为 zip

### 模板匹配（新）
- 上传 Excel 模板 → 自动识别列映射
- 下载标准模板作为参考格式
- 可手动调整金额列 / 发票号列 / 日期列 / 买方列 / 销售方列 的映射
- 配置匹配参数：日期范围天数、是否覆盖已有发票号、最大凑票行数
- 后台执行匹配，实时进度条
- 结果预览：展示匹配状态（一对一/凑票/未匹配）、匹配的发票信息
- 一键导出回填结果为 Excel

## 腾讯企业邮箱
如使用腾讯企业邮箱（企业微信邮箱 / 经典企业邮箱专业版），可通过官方 API 抓取，
支持读取 IMAP 不可见的邮件归档。

配置方法：
1. 在 `config/` 下新建 `tencent.json`（该文件已在 `.gitignore`，不会上传）
2. 填入凭证：
```json
{
  "variant": "wecom",
  "corpid": "你的企业ID",
  "corpsecret": "你的应用Secret",
  "email": "yourname@yourcompany.com"
}
```
- `variant`：`wecom` 为企业微信邮箱，`exmail` 为经典腾讯企业邮箱（专业版）
- 经典版需管理员在后台开启邮件 API 权限

## 没有邮箱凭据也能用：导入本地 PDF
```bash
python hub.py seed ../invoices_20260714/ --account 本地导入
```
把任意本地 PDF 文件夹按同样的规则解析入库，立刻能在网页查看——适合补录、演示、
或先把历史发票归档。

## 离线导出
```bash
python hub.py report --xlsx data/发票汇总.xlsx --html data/发票汇总.html
```

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

## 安装依赖
```bash
pip install -r requirements.txt
```

## 说明
- 正文链接式发票千差万别：引擎用启发式抓"下载 PDF"链接；个别站点需登录态/验证码时
  抓不到，会在 `note` 字段标记，可手动补。
- PDF 字段识别对标准增值税电子发票/数电票命中率高；版式特殊的识别为空时，去
  `config/rules.json` 加对应 pattern 即可，无需改代码。
- `data/`、`config/accounts.json`、`config/tencent.json` 含发票与授权码，
  已在 `.gitignore` 忽略，切勿上传。
- `sample/` 为示例数据目录，已排除，不会上传到仓库。
