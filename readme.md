# Overstats

`Overstats` 是一个本地 HTTP 服务，封装网易大神相关接口，用于查询守望先锋玩家资料、近期对局、单场详情、历史段位和周期总结。

## 当前能力

- 玩家资料卡：`dashen_profile`
- 大神对局列表与详情：`dashen_match`
- 大神对局富回复链路：列表、多图详情、`*` 全员详细、`**` AI锐评
- 今日 / 昨日 / 本周总结：`dashen_summary`
- 历史段位：`dashen_rank_history`
- BattleTag 解析到 `customer_token`
- 图片渲染、本地资源缓存、请求排队与请求统计

## 大神对局 replies 链路

本次版本新增：

- `POST /api/v2/dashen-match/replies`
- `POST /api/v2/dashen-match/detail/replies`

返回形态统一为：

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
      "meta_type": "ds_match_list",
      "data": {}
    },
    {
      "type": "image",
      "media_type": "image/png",
      "base64": "..."
    }
  ]
}
```

详情接口支持：

- 普通详情：主战绩图 + 查询者英雄详细
- `show_all_heroes=true`：主战绩图 + 全员瀑布图
- `show_all_heroes=true` 且 `analyze=true`：主战绩图 + 全员瀑布图 + AI锐评图
- `match_kind == "fight"`：仅返回角斗主图；若请求 `*` / `**`，追加不支持说明文本

## 运行要求

- 推荐 Python 3.11+
- 支持 Windows / Linux
- 需要可访问网易大神相关接口

安装依赖：

```bash
pip install -r requirements.txt
```

## 配置

主要配置文件：`overstats/Overstats/config/config.py`

至少需要配置：

- `DASHEN_ACCOUNTS`
- `DASHEN_DTS`
- `DASHEN_SERVER`

可选新增配置：

- `ANALYSIS_BASE_URL`
- `ANALYSIS_API_KEY`
- Match stats reference data defaults to `src/db/match_stats.sqlite3`

`src/db/match_stats.sqlite3` 未准备好时：

- 周期总结仍可工作
- 大神对局全员详细仍可渲染
- 只是部分均值 / 分位高亮会退化

## 启动

```bash
python -m overstats.run
```

或：

```bash
cd overstats
python run.py
```

默认地址：

```text
http://127.0.0.1:18080
```

健康检查：

```bash
curl http://127.0.0.1:18080/healthz
```

## 常用接口

- `POST /api/v2/dashen-profile`
- `POST /api/v2/dashen-profile/image`
- `POST /api/v2/dashen-match`
- `POST /api/v2/dashen-match/image`
- `POST /api/v2/dashen-match/replies`
- `POST /api/v2/dashen-match/detail`
- `POST /api/v2/dashen-match/detail/image`
- `POST /api/v2/dashen-match/detail/replies`
- `POST /api/v2/dashen-rank-history`
- `POST /api/v2/dashen-rank-history/image`
- `POST /api/v2/dashen-summary/today`
- `POST /api/v2/dashen-summary/today/image`
- `POST /api/v2/dashen-summary/yesterday`
- `POST /api/v2/dashen-summary/yesterday/image`
- `POST /api/v2/dashen-summary/week`
- `POST /api/v2/dashen-summary/week/image`

更细的字段说明见 [OVERSTATS_API.md](./OVERSTATS_API.md)。
