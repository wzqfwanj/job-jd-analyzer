# 接口契约

本地服务默认地址：`http://127.0.0.1:8600`。所有接口返回 JSON（图片接口除外），
错误统一为 `{"ok": false, "error": "..."}`，HTTP 状态码 400 / 404 / 413 / 500 / 502。

## 设置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查：`{"ok":true,"hasKey":bool,"version":"1.0.0"}` |
| GET | `/api/settings` | 读取设置：`{"ok":true,"hasKey":bool,"keyHint":"sk-e0b9…","baseUrl":""}`（Key 只返回掩码） |
| POST | `/api/settings` | 保存设置：`{"apiKey":"sk-...","baseUrl":""}`，返回同 GET |
| POST | `/api/settings/test` | 测试连接：`{"apiKey"?: "sk-..."}` → `{"ok":true,"models":["deepseek-flash"]}` / `{"ok":false,"error":"..."}` |

API Key 保存于本机 `data/settings.json`，已被 `.gitignore` 排除，不会上传 GitHub。
也支持环境变量 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`（优先于设置文件）。

## 岗位

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/jobs` | 岗位列表（按创建时间倒序）+ 类型枚举 + 各类型数量 |
| POST | `/api/jobs` | 上传截图创建岗位，立即返回 `status=processing`，后台分析 |
| GET | `/api/jobs/{id}` | 单个岗位（轮询用） |
| PATCH | `/api/jobs/{id}` | 修改 `title` / `company` / `type` |
| POST | `/api/jobs/{id}/reanalyze` | 用已有截图重新联网搜索 + 分析 |
| DELETE | `/api/jobs/{id}` | 删除岗位及其截图 |
| GET | `/api/images/{filename}` | 原图 |

`POST /api/jobs` 请求体：

```json
{ "jdImage": "data:image/png;base64,...", "companyImage": "data:image/png;base64,..." }
```

`companyImage` 可为 `null`，用于上传第二张截图（公司详情页，或同一岗位 JD 的后续一页）。
岗位 JD 截图必填；单张图片解码后不超过 20MB。没有配置 API Key 时返回 400。

`GET /api/jobs` 响应：

```json
{
  "ok": true,
  "jobs": [Job, ...],
  "types": ["技术负责人", "算法", "后端", "前端", "全栈", "客户端", "测试", "运维", "数据", "安全", "产品", "设计", "运营", "市场", "销售", "人力", "财务", "其他"],
  "counts": { "技术负责人": 1, "算法": 2 }
}
```

## Job 结构

```json
{
  "id": "20261104-a1b2c3",
  "createdAt": "2026-11-04 10:30:00",
  "updatedAt": "2026-11-04 10:31:00",
  "status": "processing | done | error",
  "stage": "extract | search | analyze | done | error",
  "error": "",
  "images": {
    "jd": "/api/images/jd-20261104-a1b2c3.png",
    "company": "/api/images/company-20261104-a1b2c3.png"
  },
  "job": {
    "title": "Harness 应用开发工程师",
    "type": "技术负责人",
    "company": "Pyromind Dynamics",
    "salary": "30-40K·15薪",
    "city": "杭州",
    "experience": "1-3年",
    "education": "硕士",
    "tags": ["大模型算法", "Python"],
    "summary": "负责 Agent Harness 产品架构与落地",
    "responsibilities": ["..."],
    "requirements": ["..."],
    "bonusPoints": ["..."]
  },
  "company": {
    "name": "Pyromind Dynamics",
    "industry": "人工智能",
    "size": "",
    "stage": "种子轮",
    "description": "RLaaS 创业公司……",
    "highlights": ["..."]
  },
  "analysis": {
    "doing": "第一段……\n\n第二段……",
    "talent": "- 结论一\n- 结论二",
    "sources": [{ "title": "", "url": "https://...", "snippet": "" }],
    "query": "Pyromind Dynamics",
    "generatedAt": "2026-11-04 10:31:00"
  }
}
```

## 阶段说明

| stage | 前端文案 |
| --- | --- |
| `extract` | 正在读取截图… |
| `search` | 正在联网搜索公司… |
| `analyze` | 正在生成分析… |
| `done` | 完成 |
| `error` | 失败（`error` 字段为原因） |
