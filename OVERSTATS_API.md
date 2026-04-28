# Overstats API

面向 `overstats` 本地服务的 HTTP 接口说明。

## 服务地址

- 默认：`http://127.0.0.1:18080`
- 健康检查：`GET /healthz`

## 通用约定

- 除图片接口外，返回 `application/json; charset=utf-8`
- 所有 `POST` 请求体均为 JSON 对象
- 兼容字段别名：
  - `bnet_id` / `bnetId`
  - `customer_token` / `customerToken`
  - `match_id` / `matchId`
  - `index` / `idx`

通用错误格式：

```json
{
  "ok": false,
  "error": "error_code",
  "message": "错误描述",
  "hint": "可选建议",
  "details": {}
}
```

## 1. `dashen_profile`

端点：

- `POST /api/v2/dashen-profile`
- `POST /api/v2/dashen-profile/image`

请求至少提供一项：

- `bnet_id`
- `customer_token`

可选字段：

- `season`
- `include_previous_season`
- `mode`: `quick` / `competitive`

## 2. `dashen_match`

### 2.1 列表

端点：

- `POST /api/v2/dashen-match`
- `POST /api/v2/dashen-match/image`
- `POST /api/v2/dashen-match/replies`

请求至少提供一项：

- `bnet_id`
- `customer_token`

可选字段：

- `limit`
- `include_fight`
- `include_previous_season`

`/api/v2/dashen-match/replies` 返回：

```json
{
  "ok": true,
  "customer_token": "string",
  "resolved": {
    "query": "Player#12345",
    "full_id": "Player#12345",
    "bnet_id": "12345",
    "has_customer_token": true
  },
  "replies": [
    {
      "type": "meta",
      "meta_type": "ds_match_list",
      "data": {
        "full_id": "Player#12345",
        "resolved": {},
        "match_entries": []
      }
    },
    {
      "type": "image",
      "media_type": "image/png",
      "base64": "..."
    }
  ]
}
```

### 2.2 详情

端点：

- `POST /api/v2/dashen-match/detail`
- `POST /api/v2/dashen-match/detail/image`
- `POST /api/v2/dashen-match/detail/replies`

详情请求方式二选一：

1. `bnet_id|customer_token + index`
2. `customer_token + match_id`

`/api/v2/dashen-match/detail/replies` 额外字段：

- `show_all_heroes: bool`
- `analyze: bool`

统一返回：

```json
{
  "ok": true,
  "customer_token": "string",
  "resolved": {},
  "match_id": "string",
  "match_kind": "normal",
  "replies": [
    {
      "type": "meta",
      "meta_type": "ds_match_detail_players",
      "data": {
        "player_ids": [],
        "competitive": true
      }
    },
    {
      "type": "image",
      "media_type": "image/png",
      "base64": "..."
    }
  ]
}
```

详情输出规则：

- 默认：主战绩图 + 查询者英雄详细图
- `show_all_heroes=true`：主战绩图 + 全员瀑布图
- `show_all_heroes=true` 且 `analyze=true`：主战绩图 + 全员瀑布图 + AI锐评图
- `match_kind == "fight"`：只返回角斗主图；若请求全员详细 / AI锐评，会追加文本说明

## 3. `dashen_summary`

端点：

- `POST /api/v2/dashen-summary/today`
- `POST /api/v2/dashen-summary/today/image`
- `POST /api/v2/dashen-summary/yesterday`
- `POST /api/v2/dashen-summary/yesterday/image`
- `POST /api/v2/dashen-summary/week`
- `POST /api/v2/dashen-summary/week/image`

请求至少提供一项：

- `bnet_id`
- `full_id`
- `customer_token`

## 4. `dashen_rank_history`

端点：

- `POST /api/v2/dashen-rank-history`
- `POST /api/v2/dashen-rank-history/image`

请求至少提供一项：

- `bnet_id`
- `customer_token`

可选字段：

- `start_season`
- `end_season`

## 5. 其他公共接口

- `POST /api/v2/query`
- `GET /healthz`
