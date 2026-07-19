# 多邮箱发票中枢（数据驱动版）

一个**数据驱动**的本地发票管理中枢：把多个邮箱账号、抓取到的邮件、解析后的发票记录
全部放进一个 **SQLite 数据库**（唯一真相源），代码只负责"操作数据"，查看界面是
一个**从数据库读数据的通用网页**，而不是每次手搓一份写死的报告。

## 为什么是"数据驱动"
- **账号 = 数据**：邮箱账号存在 `accounts` 表里，可在网页里直接增删/启停，不必改代码。
- **解析规则 = 数据**：发票字段（买方/金额/发票号/日期/销售方/城市）的提取正则写在
  `config/rules.json` 里，换一种发票版式只需加 pattern。
- **发票记录 = 数据**：所有结果进 `invoices` 表，网页按任意条件筛选/排序/合计，无硬编码内容。

## 目录结构
```
invoice_hub/
  data/
    invoice_hub.db     # SQLite —— 唯一真相源（账号/邮件/发票）
    pdfs/              # 下载的发票 PDF 原件，按邮箱分文件夹
  config/
    accounts.json      # 账号种子（init 时播种；实际授权码用 accounts add 填）
    rules.json         # 发票字段提取规则（数据驱动，不写死在代码）
  db.py               # 数据层：建表 / 账号CRUD / 发票查询
  engine.py           # 引擎：IMAP 拉取 + 按 rules.json 解析 + 入库
  hub.py              # CLI 调度入口
  web/
    app.py            # 标准库 HTTP 服务，把 DB 读成 JSON API
    index.html        # 通用数据驱动查看器（管理邮箱 + 筛选/合计发票）
```

## 三步使用

### 1. 各邮箱开启 IMAP 并生成授权码
- **163**：mail.163.com → 设置 → POP3/SMTP/IMAP → 开启 IMAP → 生成授权码
- **QQ**：mail.qq.com → 设置 → 账户 → 开启 IMAP/SMTP → 生成授权码
- **Gmail**：两步验证后生成"应用专用密码"
- **Outlook**：outlook.com → 设置 → 同步 → 开启 IMAP

### 2. 建库 + 加账号
```bash
PYENV="C:/Users/Ifesco/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
cd invoice_hub

"$PYENV" hub.py init          # 建 SQLite 库 + 播种 config/accounts.json
# 在网页里「添加邮箱」填入授权码，或命令行：
"$PYENV" hub.py accounts add --email gzfesco_waifu@163.com \
        --name waifu-163 --host imap.163.com --port 993 --password <授权码>
"$PYENV" hub.py accounts      # 查看已管理账号
```

### 3. 抓取 + 查看
```bash
"$PYENV" hub.py fetch --since 2026-07-09   # 拉取全部启用账号的发票邮件 → 入库
"$PYENV" hub.py serve --port 8000           # 起 Web 控制台
```
浏览器打开 http://127.0.0.1:8000 ：
- 顶部卡片显示张数 / 合计 / 买方 TOP
- 「邮箱账号管理」：增 / **编辑（含 IMAP 主机·端口·授权码）** / 删 / 启停；每行有「**测试**」按钮验证连接是否通
- **抓取可视化**：点「立即抓取全部」或单账号「抓取」→ 后台拉取，下方「抓取进度」面板实时滚动日志；还能设「每 N 分钟自动抓取」
- **网页上传本地 PDF**：点「上传本地PDF」选多个文件 → 直接解析入库（自动建 `upload@local` 账号），不必走命令行
- 表格可按关键字/邮箱/城市/买方/日期筛选、点列头排序、点「查看」看 PDF
- **勾选 / 全选 → 导出选中**：一键把所有勾选发票的 PDF 打包进 `pdfs/` 文件夹，并附 **清单.csv / .md / .xlsx**，下载为一个 zip

## 没有邮箱凭据也能用：导入本地 PDF
```bash
"$PYENV" hub.py seed ../invoices_20260714/ --account 本地导入
```
把任意本地 PDF 文件夹按同样的规则解析入库，立刻能在网页查看——适合补录、演示、
或先把历史发票归档。

## 离线导出
```bash
"$PYENV" hub.py report --xlsx data/发票汇总.xlsx --html data/发票汇总.html
```

## 说明
- 正文链接式发票千差万别：引擎用启发式抓"下载 PDF"链接；个别站点需登录态/验证码时
  抓不到，会在 `note` 字段标记，可手动补。
- PDF 字段识别对标准增值税电子发票/数电票命中率高；版式特殊的识别为空时，去
  `config/rules.json` 加对应 pattern 即可，无需改代码。
- `data/` 和 `config/accounts.json` 含发票与授权码，已在 `.gitignore` 忽略，切勿上传。
