# 东港水产拍卖结算

给水产市场结算员处理双次称重、扣杂计价、结算更正与应付复核。

七个模块：称重采信、货品换算、毛皮配对、扣杂竞价、更正裁决、应付对账、结算归档。每个导航页均可重跑同一批数据，隔离行保留编号、原值、行号和原因码。

```sh
docker compose up --build -d
docker compose exec -T app pytest -q
```

浏览器访问 http://localhost:18240 。默认载入现场脏批，包含53条逻辑输入、重复表头、缺列、空值、重复冲突、缺皮重、毛重小于皮重等问题。切换“核验清批”或“旧编码批”后重跑。进入“更正裁决”核对票00001：原应付1168.50CNY，更正后1285.35CNY，已付1168.50CNY，仅待付116.85CNY。

API：各模块 `POST /api/<导航英文名>`；完整运行 `POST /api/runs`，JSON参数可用 `dataset` 或 `csv_text`。查询历史 `GET /api/runs`，复核 `POST /api/runs/{run_id}/review`，传入当前 `revision`。封存下载只有 `GET /api/export/{run_id}` 一个入口。

同输入和规则摘要生成相同批号，不新增重复记录；复核版本改变后旧版本提交返回409。SQLite保存在命名卷。可使用新卷名称重建演示数据，原卷保留。

## 晚场补传核对

手持终端断网恢复后各自补传成交，财务按已同步票先付款，不必等全部终端补齐。访问 `/session`（导航“晚场 · 补传核对”）核对：各终端同步水位与缺口批次、待补票（跨终端票号冲突或缺称重依据，挂起不并入）、当前可付款结果、每次发布的新增与撤回金额。

- `POST /api/session/uploads` 补传一批：`{session_id, terminal_id, seq, transactions:[{ticket,fish,gross_kg,tare_kg,price,rate,paid}]}`。同终端同批次号重传内容一致时原样确认、不新增成交；内容不一致返回409。同票同内容跨终端回放只计一笔。
- `POST /api/session/publish` 发布可付款结果：`{session_id, expect_version}`。单事务提交，失败回滚保留旧版；内容未变不增版；`expect_version` 过期返回409。
- `GET /api/session/state`、`GET /api/session/evidence` 查询场次状态与原始证据；`POST /api/session/demo` 载入演示场次（断网→补传→冲突→补证→发布全流程）。

原始成交与称重证据只增不改，挂起与重复记录同样保留；可付款集由原始证据派生，重放结果一致。

定义见 `docs/DATA-DICTIONARY.md`，模块与测试映射见 `docs/DEPTH.md`。源码在 `app/`，业务样例在 `sample_data/`，测试在 `tests/`。不连接实际秤台和支付渠道。

采信页支持上传或粘贴CSV。完整源字节与参数随批次持久化，跨页通过run_id读取同一结果。`POST /api/runs/{run_id}/rerun` 读取该批不可变输入快照重新计算，规则未变时批号不变。切换另一示例则创建或复用另一批次。
