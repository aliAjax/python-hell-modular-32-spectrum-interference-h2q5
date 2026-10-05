# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/rules.py` 负责评估、定位、授权和状态机，`src/repository.py` 管理 SQLite、版本和审计链，`src/service.py` 编排权限，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。接口为 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`、`POST /api/offline-batches` 和 `GET /api/items/<id>/audit`。测试覆盖完整调查流程、测量更正、重复事件、跨区越权、定位置信度、版本冲突和离线回收。协议接入、真实无线电传播模型和执法权限仍需由外部系统实现。

## 离线回收（监测车回网合并）

监测车离线测量后通过 `POST /api/offline-batches` 批量回传，报文为 `{"batch_id": 可选, "measurements": [{"item_id", "measurement_id": 可选, "strength_dbm", "observed_at", ...}]}`，车辆身份取 `X-User-Id`。

- 多车批量回传：单批最多 200 条，整批单事务入库，校验失败整体回滚，修正后重传即可。
- 重复上传只入库一次：按 `车辆|批次|测量号` 生成去重键并加唯一约束；旧数据无批次号走 `legacy` 命名空间，连测量号也没有时按内容哈希兼容并入。
- 测量更新后服务端重算：规范值取 `observed_at` 最新的测量（并列按车辆/测量号字典序），评估由服务端重算，车里上报的 score/level 一律不采用。
- 结案停用后迟到测量照旧留档，状态不回退，规范值不改写，改挂待核对冲突（`conflicts[].status=pending`）。
- 有冲突时定位/停用先拒绝（409 `pending_conflict`），用 `review_conflicts` 动作（analyst/coordinator）核对后放行。
- 监测车只交本区测量：按 `X-Region` 校验测量区域与目标事件区域，跨区 403。
- 去重和冲突判定不随回传先后改变：冲突按全集（已归档测量 + 事件原始强度）极差超过 6 dB 判定，涉及引用按全集重算。
