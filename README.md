# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/rules.py` 负责评估、定位、授权和状态机，`src/repository.py` 管理 SQLite、版本和审计链，`src/service.py` 编排权限，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。接口为 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`、`GET /api/items/<id>/audit`、`POST /api/offline/batches`、`GET /api/offline/batches` 和 `GET /api/offline/measurements`。测试覆盖完整调查流程、测量更正、重复事件、跨区越权、定位置信度、版本冲突和离线回收。

## 离线回收（offline recovery）

监测车进山离线测量，回网合并时事件常已改过，先交的记录当旧版本丢掉。

- **多车批量回传**：`POST /api/offline/batches` 接收 `measurements` 数组，一次处理多车测量。
- **重复上传只入库一次**：批次号去重 + 测量内容指纹去重，重复提交不重复入库。
- **测量更新后服务端重算**：合并时由服务端 `assess()` 重算评估，不采用车里离线结果。
- **结案停用后迟到测量照旧留档**：状态不回退，改挂 `pending_conflict` 待核对冲突。
- **有冲突时定位停用先拒绝**：`locate`/`suspend` 动作在 `pending_conflict` 未清除前返回 409。
- **监测车只交本区测量**：跨区守角色边界，`monitor` 角色只能提交 `X-Region` 本区测量，`regulator` 可跨区。
- **旧数据无批次号兼容并入**：`batch_id` 可选，缺失时按内容指纹去重并入。
- **并发提交确定**：`BEGIN IMMEDIATE` 事务内完成去重与冲突判定，结果不随先后改变。

批次请求体示例：

```json
{
  "batch_id": "batch-001",
  "vehicle_id": "vehicle-1",
  "measurements": [
    {
      "station_id": "ST-01",
      "region": "north",
      "frequency_mhz": 2400.0,
      "bandwidth_mhz": 20.0,
      "strength_dbm": -45,
      "detected_at": "2026-10-01T10:00:00+00:00",
      "offline_assessment": { "score": 99, "level": "critical" }
    }
  ]
}
```

每条测量的 `disposition` 为 `merged`（合并）、`duplicate`（重复）、`conflict`（冲突待核对）、`archived`（结案留档）或 `discarded`（旧版本丢弃）。核对后可通过 `clear_conflict` 动作清除冲突标记。
