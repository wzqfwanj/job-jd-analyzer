#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""岗位 JD 分析系统后端（Python 3.12 标准库，零第三方依赖）。

流水线：上传 JD / 公司详情截图 -> DeepSeek 视觉提取结构化信息 -> Bing RSS 联网搜索公司
-> DeepSeek 综合生成「公司在做什么」「公司需要什么样的人才」。

数据：data/jobs.json、data/settings.json（API Key，不提交仓库）、data/uploads/ 原图。
接口契约见 API.md。
"""

from __future__ import annotations

import argparse
import base64
import binascii
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
JOBS_FILE = DATA_DIR / "jobs.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

APP_NAME = "岗位 JD 分析系统"
APP_VERSION = "1.0.0"
DEFAULT_PORT = 8600
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = (os.environ.get("DEEPSEEK_MODEL") or "deepseek-flash").strip() or "deepseek-flash"
API_TIMEOUT = 300
SEARCH_TIMEOUT = 25
MAX_BODY_BYTES = 45 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_JOBS = 500

JOB_TYPES = [
    "技术负责人", "算法", "后端", "前端", "全栈", "客户端", "测试", "运维",
    "数据", "安全", "产品", "设计", "运营", "市场", "销售", "人力", "财务", "其他",
]

IMAGE_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/bmp": "bmp",
}
EXT_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp",
}

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 招聘聚合站：内容多是岗位列表，对「公司在做什么」价值低
JOB_SITES = ("zhipin.com", "zhaopin.com", "51job.com", "liepin.com", "lagou.com",
             "kanzhun.com", "maimai.cn", "nowcoder.com", "yingjiesheng.com")
# 泛泛解释站：价值低但保留
LOW_VALUE_SITES = ("baike.baidu.com/item/", "zhihu.com/question", "jingyan.baidu.com",
                   "zhidao.baidu.com", "zdic.net", "hgcha.com")
SEARCH_BUDGET = 8.0  # 联网搜索总预算（秒）

DATA_URL_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.S)
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clone(obj):
    """深拷贝（对象都是 JSON 兼容结构）。"""
    return json.loads(json.dumps(obj, ensure_ascii=False))


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class ApiError(Exception):
    """可预期的业务错误，返回给前端。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# 设置（API Key 只保存在本机）
# ---------------------------------------------------------------------------

_settings_lock = threading.Lock()


def load_settings() -> dict:
    data = {"apiKey": "", "baseUrl": ""}
    if SETTINGS_FILE.exists():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data["apiKey"] = str(raw.get("apiKey") or "").strip()
                data["baseUrl"] = str(raw.get("baseUrl") or "").strip()
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[警告] 读取 data/settings.json 失败：{exc}", file=sys.stderr)
    return data


def save_settings(settings: dict) -> None:
    with _settings_lock:
        atomic_write_json(SETTINGS_FILE, settings)


def current_settings() -> dict:
    return load_settings()


def api_key() -> str:
    env_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env_key:
        return env_key
    return current_settings().get("apiKey", "")


def base_url() -> str:
    env_base = (os.environ.get("DEEPSEEK_BASE_URL") or "").strip()
    return (env_base or current_settings().get("baseUrl") or DEFAULT_BASE_URL).rstrip("/")


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return "已保存"
    return f"{key[:7]}…{key[-2:]}"


# ---------------------------------------------------------------------------
# 数据存储：内存 dict + data/jobs.json 原子写入
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}
_jobs_lock = threading.RLock()


def default_job_skeleton() -> dict:
    return {
        "status": "processing",
        "stage": "extract",
        "error": "",
        "images": {"jd": "", "company": ""},
        "job": {
            "title": "", "type": "其他", "company": "", "salary": "", "city": "",
            "experience": "", "education": "", "tags": [], "summary": "",
            "responsibilities": [], "requirements": [], "bonusPoints": [],
        },
        "company": {
            "name": "", "industry": "", "size": "", "stage": "", "description": "",
            "highlights": [],
        },
        "analysis": {
            "doing": "", "talent": "", "sources": [], "query": "", "generatedAt": "",
        },
    }


def normalize_job(job: dict) -> dict:
    """补齐缺失字段，兼容旧数据。"""
    if not isinstance(job, dict):
        return job
    skeleton = default_job_skeleton()
    for key, value in skeleton.items():
        if key not in job or job[key] is None:
            job[key] = clone(value)
        elif isinstance(value, dict) and isinstance(job[key], dict):
            for sub_key, sub_value in value.items():
                if sub_key not in job[key] or job[key][sub_key] is None:
                    job[key][sub_key] = clone(sub_value)
    job.setdefault("id", uuid.uuid4().hex)
    job.setdefault("createdAt", now_text())
    job.setdefault("updatedAt", job["createdAt"])
    if job.get("job", {}).get("type") not in JOB_TYPES:
        job["job"]["type"] = "其他"
    return job


def load_jobs() -> None:
    global _jobs
    loaded: dict[str, dict] = {}
    if JOBS_FILE.exists():
        try:
            raw = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else []
            for item in items:
                if isinstance(item, dict) and item.get("id"):
                    loaded[str(item["id"])] = normalize_job(item)
        except (OSError, json.JSONDecodeError) as exc:
            backup = JOBS_FILE.with_name(f"jobs.corrupt-{int(time.time())}.json")
            try:
                JOBS_FILE.replace(backup)
            except OSError:
                pass
            print(f"[警告] data/jobs.json 读取失败（{exc}），已备份为 {backup.name}", file=sys.stderr)
    # 上次程序退出时还在处理的任务，重启后标记为失败，允许用户重新分析
    for job in loaded.values():
        if job.get("status") == "processing":
            job["status"] = "error"
            job["stage"] = "error"
            job["error"] = job.get("error") or "程序重启导致分析中断，请点击「重新分析」。"
    with _jobs_lock:
        _jobs = loaded


def save_jobs_locked() -> None:
    """调用方必须持有 _jobs_lock。"""
    items = sorted(_jobs.values(), key=lambda j: str(j.get("createdAt", "")), reverse=True)
    atomic_write_json(JOBS_FILE, items)


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return clone(job) if job else None


def update_job(job_id: str, mutator) -> dict | None:
    """在锁内修改任务并落盘；mutator 接收 job dict，返回值忽略。"""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        mutator(job)
        job["updatedAt"] = now_text()
        normalize_job(job)
        save_jobs_locked()
        return clone(job)


def job_public(job: dict) -> dict:
    out = clone(job)
    images = out.get("images") or {}
    out["images"] = {
        "jd": f"/api/images/{images['jd']}" if images.get("jd") else None,
        "company": f"/api/images/{images['company']}" if images.get("company") else None,
    }
    # 读取时再做一次轻量归一化，保证历史数据也按最新规则展示（分段/去重/条数/摘句）
    job_info = out.get("job")
    if isinstance(job_info, dict) and job_info.get("summary"):
        job_info["summary"] = _truncate_text(str(job_info["summary"]), 120)
    analysis = out.get("analysis")
    if isinstance(analysis, dict) and analysis.get("doing"):
        analysis["doing"] = _ensure_paragraphs(str(analysis.get("doing") or ""))
        analysis["talent"] = _truncate_bullets(str(analysis.get("talent") or ""), 6)
    return out


# ---------------------------------------------------------------------------
# DeepSeek 调用
# ---------------------------------------------------------------------------


def deepseek_chat(messages: list, max_tokens: int = 6000, temperature: float = 0.2,
                  timeout: int = API_TIMEOUT) -> str:
    key = api_key()
    if not key:
        raise ApiError("还没有配置 DeepSeek API Key，请点击右上角「设置」填写。", 400)
    body = {
        "model": DEFAULT_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    req = urllib.request.Request(
        base_url() + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        message = f"DeepSeek 接口返回 {exc.code}"
        if exc.code == 401:
            message += "（API Key 无效，请到设置里检查）"
        elif exc.code == 402:
            message += "（账户余额不足，请去 DeepSeek 平台充值）"
        elif exc.code == 429:
            message += "（请求太频繁，请稍后再试）"
        if detail:
            message += f"。{detail}"
        raise ApiError(message, 502) from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"无法连接 DeepSeek 接口：{exc.reason}", 502) from exc
    except (TimeoutError, OSError) as exc:
        raise ApiError(f"调用 DeepSeek 超时或网络异常：{exc}", 502) from exc
    except json.JSONDecodeError as exc:
        raise ApiError("DeepSeek 返回了无法解析的内容", 502) from exc

    choices = payload.get("choices") or []
    if not choices:
        raise ApiError("DeepSeek 没有返回结果，请稍后重试", 502)
    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if not content:
        reason = choices[0].get("finish_reason") or ""
        if reason == "length":
            raise ApiError("模型输出被截断（截图内容可能过长），请重试或换一张更清晰的截图", 502)
        raise ApiError("模型没有返回有效内容，请稍后重试", 502)
    return content


def _strip_code_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _balanced_json_slice(text: str) -> str:
    """从第一个 { 或 [ 开始做括号配平截取；未闭合时补全括号。"""
    start = -1
    for index, char in enumerate(text):
        if char in "{[":
            start = index
            break
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth <= 0:
                return text[start:index + 1]
    # 被截断：去掉尾部不完整片段，按未闭合的括号数量补全
    tail = text[start:]
    if in_string:
        tail += '"'
    tail = re.sub(r",\s*$", "", tail)
    stack = []
    in_string = False
    escaped = False
    for char in tail:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]" and stack:
            stack.pop()
    closers = "".join("}" if opener == "{" else "]" for opener in reversed(stack))
    return tail + closers


def extract_json(text: str) -> dict:
    """从模型输出里稳妥地抠出 JSON 对象（兼容代码块、前后缀、轻微截断）。"""
    text = _strip_code_fence(text)
    candidates = [text, _balanced_json_slice(text)]
    for candidate in candidates:
        if not candidate:
            continue
        for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                data = json.loads(attempt)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    raise ValueError("没有找到合法的 JSON 对象")

# ---------------------------------------------------------------------------
# 截图提取
# ---------------------------------------------------------------------------

VISION_SYSTEM = """你是「岗位 JD 截图信息抽取器」。用户会给你 1-2 张 Boss 直聘截图，其中至少一张是岗位 JD 截图，另一张可能是同一岗位 JD 的后续部分，也可能是「公司详情」页截图。

你的唯一任务是：把截图里**明确可见**的信息抽取成严格 JSON。必须遵守：
1. 只提取截图中真实出现的信息。看不到就留空：字符串用 ""，数组用 []。禁止推测、补全、编造；禁止填写「未提及」「无」这类占位词。
2. 不输出任何解释、前后缀或 Markdown 代码块，直接输出一个 JSON 对象；即使信息很少也要输出完整结构（所有键都在）。
3. 多张截图时先判断关系：若属于同一个岗位（同一岗位 JD 的上下页），把内容合并去重（截图重叠部分只保留一次，不要把同一段职责/要求重复写两遍）；若一张是公司详情页，则用它的信息补全 company，company.name 以公司详情页为准。
4. 忽略页面噪音：收藏/举报/立即沟通/微信扫码分享、按钮、页脚、联系方式占位符（如 *****@***）、平台广告等。
5. 保留原文措辞，可轻度精简与合并同类项，但不得改写事实。

JSON 结构必须完全如下（键名、层级、类型都不能变）：
{"job":{"title":"","type":"","company":"","salary":"","city":"","experience":"","education":"","tags":[],"summary":"","responsibilities":[],"requirements":[],"bonusPoints":[]},"company":{"name":"","industry":"","size":"","stage":"","description":"","highlights":[]}}

字段规则：
- job.title：岗位名称原文（如 "Harness应用开发工程师"）。
- job.type：必须从以下枚举中选**且只选一个**：技术负责人 / 算法 / 后端 / 前端 / 全栈 / 客户端 / 测试 / 运维 / 数据 / 安全 / 产品 / 设计 / 运营 / 市场 / 销售 / 人力 / 财务 / 其他。判定看岗位核心职责，不看公司行业；标题含架构师、技术总监、技术负责人、研发负责人、CTO、技术专家（带团队/定架构）选「技术负责人」；「高级/资深 XX 工程师」只是级别，不算技术负责人。客户端=Android/iOS/Flutter/桌面端；算法=模型训练/微调/算法研究/CV/NLP/推荐/强化学习；Agent、LLM 应用开发、RAG、大模型平台工程等以工程落地为主的岗位算「后端」，不算「算法」；数据=数据分析/数据工程/数仓/BI；测试=测试开发/QA；运维=SRE/DevOps/实施运维；实在无法归类才用「其他」；整张图不是岗位 JD 时 type 用 ""。
- job.company：公司名。只允许来自两处：①公司详情截图；②JD 页「公司简介」段落里作为**招聘方**明确写出的公司名（含英文名）。JD 正文里作为合作方、客户、竞品、技术/模型名出现的公司不算（例如「与 DeepSeek 的 Harness 团队合作」，DeepSeek 是合作方）。两处都没有就留 ""，不要从岗位名或正文猜。只有英文名时保留英文原名。
- job.salary：照抄，如 "25-45K·15薪"。
- job.city / experience / education：照抄标签，如 "杭州"、"3-5年"、"本科"。
- job.tags：JD 页技能标签（如 ["Golang","Harness","Agent"]），最多 8 个。
- job.summary：1-2 句话概括这个岗位做什么、招什么层次的人；只能基于截图内容归纳。
- job.responsibilities：岗位职责/工作内容，逐条一个数组元素；原文有编号或分段就按条拆。
- job.requirements：任职要求/岗位要求，逐条一个数组元素（含学历、年限、技术栈等）；只取「任职要求/岗位要求/技能要求」段落；不要混入岗位名称、"职位：全职"、薪资福利、公司介绍等元信息；去掉条目末尾多余的「；。」。
- job.bonusPoints：加分项/优先条件，逐条一个数组元素；没有就 []。
- company.name：公司名称（可与 job.company 相同）。同样只来自公司详情截图或「公司简介」段落。
- company.industry / size / stage：公司详情页里的行业、规模（如 "100-499人"）、融资阶段（如 "B轮"）；没有就 ""。融资阶段必须是原文出现的词，只有描述性语句（如「已完成千万美元种子轮融资」）时可填「种子轮」，不确定就 ""。
- company.description：公司简介原文（可合并多段），没有就 ""。
- company.highlights：从公司简介中提炼的客观亮点（融资、团队背景、赛事成绩、合作方、产品线），每条一个数组元素，最多 6 条；只能来自原文，没有就 []。

若截图里没有岗位标题、也没有职责/要求（不是岗位 JD，或只是无法定位归属的片段），把所有字段留空（数组为 []，type 为 ""），不要凭猜测填写；后端会提示用户重新上传。"""

VISION_USER_1 = """第 1 张截图是「岗位 JD 截图」。请按约定抽取信息，直接输出 JSON。"""

VISION_USER_2 = """第 1 张是「岗位 JD 截图」，第 2 张可能是该岗位 JD 的后续部分，也可能是「公司详情」页截图。请合并抽取信息（同一段内容只保留一次）；若第 2 张是公司详情，用它的信息补全公司字段。直接输出 JSON。"""

def _image_path(filename: str) -> Path | None:
    if not filename or not SAFE_NAME_RE.match(filename):
        return None
    path = UPLOADS_DIR / filename
    return path if path.is_file() else None


def _image_data_url(filename: str) -> str | None:
    path = _image_path(filename)
    if path is None:
        return None
    ext = path.suffix.lower().lstrip(".")
    mime = EXT_MIME.get(ext, "image/png")
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _as_list(value, limit: int = 20) -> list[str]:
    if isinstance(value, str):
        parts = [p.strip(" 　•-·") for p in re.split(r"[\n；;]+", value)]
        return [p for p in parts if p][:limit]
    if isinstance(value, list):
        result = []
        for item in value:
            text = _as_text(item)
            if text:
                result.append(text)
        return result[:limit]
    return []


def _truncate_text(text: str, limit: int = 120) -> str:
    """超长时尽量在句末截断，避免出现半句话。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("；"))
    if boundary >= 40:
        return cut[:boundary + 1]
    return cut.rstrip("，,；;、 ") + "…"


def classify_job_type(title: str, extra: list[str] | None = None) -> str:
    text = (title or "").lower()
    tag_text = " ".join(extra or []).lower()
    rules = [
        ("技术负责人", ["技术负责人", "技术总监", "架构师", "研发经理", "技术经理",
                    "研发负责人", "技术 lead", "tech lead", "cto", "负责人"]),
        ("算法", ["算法", "机器学习", "深度学习", "大模型", "llm", "nlp", "cv", "推荐",
                 "搜索", "广告算法", "强化学习"]),
        ("后端", ["后端", "服务端", "java", "golang", "go 开发", "python 开发",
                 "c++", "node", "django", "spring"]),
        ("前端", ["前端", "web 开发", "react", "vue", "javascript", "typescript"]),
        ("全栈", ["全栈"]),
        ("客户端", ["客户端", "android", "ios", "flutter", "鸿蒙"]),
        ("测试", ["测试", "qa"]),
        ("运维", ["运维", "sre", "devops", "kubernetes", "k8s"]),
        ("数据", ["数据", "数仓", "bi", "数据分析", "数据开发", "etl"]),
        ("安全", ["安全", "渗透", "风控"]),
        ("产品", ["产品经理", "产品"]),
        ("设计", ["设计", "ui", "ux", "交互"]),
        ("运营", ["运营"]),
        ("市场", ["市场", "营销", "品牌", "增长"]),
        ("销售", ["销售", "商务", "bd"]),
        ("人力", ["人力", "hr", "招聘"]),
        ("财务", ["财务", "会计", "审计"]),
    ]
    for job_type, keywords in rules:
        for keyword in keywords:
            if keyword in text or keyword in tag_text:
                return job_type
    return "其他"


def _dedupe_texts(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        key = re.sub(r"[\W_]+", "", item).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def normalize_extraction(data: dict) -> dict:
    job_part = data.get("job")
    company_part = data.get("company")
    if not isinstance(job_part, dict):
        job_part = {}
    if not isinstance(company_part, dict):
        company_part = {}

    job = {
        "title": _as_text(job_part.get("title")),
        "type": _as_text(job_part.get("type")),
        "company": _as_text(job_part.get("company")),
        "salary": _as_text(job_part.get("salary")),
        "city": _as_text(job_part.get("city")),
        "experience": _as_text(job_part.get("experience")),
        "education": _as_text(job_part.get("education")),
        "tags": _dedupe_texts(_as_list(job_part.get("tags"), 8)),
        "summary": _truncate_text(_as_text(job_part.get("summary")), 120),
        "responsibilities": _dedupe_texts(_as_list(job_part.get("responsibilities"), 14)),
        "requirements": _dedupe_texts(_as_list(job_part.get("requirements"), 14)),
        "bonusPoints": _dedupe_texts(_as_list(job_part.get("bonusPoints"), 10)),
    }
    # 模型给出的分类（提示词里有明确判定规则）优先；不合法时用关键词规则兜底。
    if job["type"] not in JOB_TYPES:
        job["type"] = classify_job_type(
            job["title"], job["tags"] + job["requirements"] + [job["summary"]]
        )

    company = {
        "name": _as_text(company_part.get("name")) or job["company"],
        "industry": _as_text(company_part.get("industry")),
        "size": _as_text(company_part.get("size")),
        "stage": _as_text(company_part.get("stage")),
        "description": _as_text(company_part.get("description")),
        "highlights": _dedupe_texts(_as_list(company_part.get("highlights"), 6)),
    }
    if company["name"] and not job["company"]:
        job["company"] = company["name"]
    return {"job": job, "company": company}


def extract_from_images(job: dict) -> dict:
    images = job.get("images") or {}
    jd_url = _image_data_url(images.get("jd") or "")
    if not jd_url:
        raise ApiError("找不到岗位 JD 截图文件，请重新上传。", 400)
    data_urls = [jd_url]
    company_url = _image_data_url(images.get("company") or "") if images.get("company") else None
    if company_url:
        data_urls.append(company_url)

    user_text = VISION_USER_1 if len(data_urls) == 1 else VISION_USER_2
    content = [{"type": "text", "text": user_text}]
    for data_url in data_urls:
        content.append({"type": "image_url", "image_url": {"url": data_url}})
    messages = [
        {"role": "system", "content": VISION_SYSTEM},
        {"role": "user", "content": content},
    ]
    max_tokens = 6000 if len(data_urls) == 1 else 8000
    last_error = None
    for attempt in range(2):
        raw = deepseek_chat(messages, max_tokens=max_tokens, temperature=0.1)
        try:
            return normalize_extraction(extract_json(raw))
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": raw[:2000]},
                    {"role": "user", "content": "上面的输出不是合法 JSON。请严格只输出要求的 JSON 对象，不要任何解释、不要 Markdown 代码块。"},
                ]
                max_tokens = 8000
    raise ApiError(f"没能解析模型的提取结果（{last_error}），请重试或换一张更清晰的截图", 502)

# ---------------------------------------------------------------------------
# 联网搜索（Bing RSS，无需 API Key）
# ---------------------------------------------------------------------------


def _clean_html_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def bing_rss(query: str, limit: int = 10, timeout: int = 6) -> list[dict]:
    url = "https://cn.bing.com/search?format=rss&q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    results: list[dict] = []
    try:
        root = ET.fromstring(raw)
        items = root.findall(".//item")
        for item in items:
            title = _clean_html_text(item.findtext("title") or "")
            link = (item.findtext("link") or "").strip()
            desc = _clean_html_text(item.findtext("description") or "")
            if title and link.startswith("http"):
                results.append({"title": title, "url": link, "snippet": desc[:260]})
            if len(results) >= limit:
                break
    except ET.ParseError:
        for block in re.findall(r"<item>(.*?)</item>", raw, re.S)[:limit]:
            title_match = re.search(r"<title>(.*?)</title>", block, re.S)
            link_match = re.search(r"<link>(.*?)</link>", block, re.S)
            desc_match = re.search(r"<description>(.*?)</description>", block, re.S)
            title = _clean_html_text(title_match.group(1) if title_match else "")
            link = link_match.group(1).strip() if link_match else ""
            if title and link.startswith("http"):
                results.append({
                    "title": title,
                    "url": link,
                    "snippet": _clean_html_text(desc_match.group(1) if desc_match else "")[:260],
                })
    return results


def _source_of(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def _relevance(item: dict, company: str) -> int:
    """0-3 分：公司名在标题/摘要/链接中的命中程度（含招聘站/泛解释站扣分）。"""
    name = (company or "").lower().strip()
    if not name:
        return 0
    text = (item["title"] + " " + item["snippet"]).lower()
    link = item["url"].lower()
    score = 0
    if name in text:
        score += 3
    elif name in link:
        score += 2
    else:
        zh = re.sub(r"[^\u4e00-\u9fa5]", "", name)
        if len(zh) >= 4 and any(zh[i:i + 3] in text for i in range(len(zh) - 2)):
            score += 1
        words = re.findall(r"[a-z]{3,}", name)
        if words and words[0] in text:
            score += 1
    if any(site in link for site in JOB_SITES):
        score -= 2
    if any(site in link for site in LOW_VALUE_SITES):
        score -= 1
    return score


def search_company(company_name: str) -> list[dict]:
    """Bing RSS 搜索公司。实测中文 query 加词会崩，所以先用纯公司名，不足再降级。"""
    name = (company_name or "").strip()
    if not name:
        return []
    ascii_only = bool(re.fullmatch(r"[\x00-\x7f]+", name))
    queries = [name]
    if ascii_only or len(name) < 4:
        queries.append(name + " 公司介绍")
    queries.append(name + " 融资")

    deadline = time.monotonic() + SEARCH_BUDGET
    seen_links: set[str] = set()
    seen_titles: set[str] = set()
    results: list[dict] = []
    for query in queries:
        if time.monotonic() > deadline:
            break
        try:
            items = bing_rss(query, limit=10)
        except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError) as exc:
            print(f"[搜索] 「{query}」失败：{exc}", file=sys.stderr)
            continue
        scored = []
        for item in items:
            if item["url"].split("?")[0] in seen_links:
                continue
            score = _relevance(item, name)
            if score <= 0:
                continue
            scored.append((score, item))
        scored.sort(key=lambda pair: -pair[0])
        for score, item in scored:
            link_key = item["url"].split("?")[0]
            title_key = re.sub(r"[\W_]+", "", item["title"])[:30]
            if link_key in seen_links or title_key in seen_titles:
                continue
            seen_links.add(link_key)
            seen_titles.add(title_key)
            results.append(item)
            if len(results) >= 8:
                break
        if len(results) >= 3:
            break
    return results[:8]

# ---------------------------------------------------------------------------
# 综合分析：公司在做什么 / 需要什么样的人才
# ---------------------------------------------------------------------------

ANALYSIS_SYSTEM = """你是资深行业与招聘分析师，擅长从公开资料判断一家公司在做什么、以及它在为什么样的人付钱。你会拿到三部分材料：
1) 从岗位 JD 截图抽取的结构化信息（JSON）；
2) 联网搜索到的公开资料（若干条：标题、来源、摘要、链接，可能含噪音）；
3) 可选：用户额外填写的公司名或备注。

请输出两个分析，写入严格 JSON：
- "doing"：这家公司在做什么。**严格 2-3 段（最多 3 段）**，段落之间用空行分隔。讲清：主营业务/产品与商业模式、技术方向与技术栈、所处阶段（融资/规模/团队背景）、近期动向，最后落到「这个岗位在其中的位置」。每段 2-4 句。「公开资料有限」这类说明并入最后一段，不要单独占一段。
- "talent"：公司需要什么样的人才。**严格 4-6 条**，每条以 "- " 开头，一行一条，条与条之间用 \n 分隔。要结合公司业务方向 + JD 要求，写出能力画像（技术深度、工程素养、业务理解、软素质、成长潜力），不要照抄 JD 原句。

硬性规则：
1. 只使用给定材料。不得编造具体融资金额、客户名、产品名、营收、人数；材料没写就不写。
2. 搜索资料只是参考，可能有错、过期或张冠李戴（同名公司、招聘聚合页、百科泛泛介绍）。与 JD/公司矛盾时以公司详情与 JD 为准；无法确认时用保守措辞：「公开信息较少」「从 JD 看」「据报道（未经官方确认）」。
3. 如果搜索结果与该公司明显无关（例如搜到的是同名不同公司），不要采用，说明「公开资料有限，以下主要基于 JD 推断」。
4. 公司名是英文或中英混排时，保留原名，不要硬翻译。
5. 语言：简体中文，专业、克制，不用营销腔；不要出现「作为 AI」「以下是分析」等元话语。
6. 只输出 JSON 对象，不要 Markdown 代码块，不要解释。sources 由后端根据搜索结果拼装，你不要生成。
JSON 结构：
{"doing":"第一段…\n\n第二段…","talent":"- 条目1\n- 条目2"}"""

ANALYSIS_USER_TEMPLATE = """【岗位与公司信息（从 JD 截图抽取）】
{job_json}

【联网搜索资料（Bing，{n} 条，可能含噪音）】
{search_text}

{extra}
请输出 JSON：{{"doing":"...","talent":"- ..."}}"""


def _truncate_paragraphs(text: str, max_paragraphs: int = 3) -> str:
    raw = str(text or "").strip()
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", raw) if part.strip()]
    if len(paragraphs) <= 1:
        # 模型有时用单个换行分段，这里也认
        single_lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if len(single_lines) > 1:
            paragraphs = single_lines
    # 去掉内容重复/互相包含的段落（模型偶发重复输出）
    deduped: list[str] = []
    for paragraph in paragraphs:
        normalized = re.sub(r"[\W_]+", "", paragraph)
        replaced = False
        for index, kept in enumerate(deduped):
            kept_normalized = re.sub(r"[\W_]+", "", kept)
            if normalized and kept_normalized and (
                normalized in kept_normalized or kept_normalized in normalized
            ):
                if len(normalized) > len(kept_normalized):
                    deduped[index] = paragraph
                replaced = True
                break
        if not replaced:
            deduped.append(paragraph)
    paragraphs = deduped
    if not paragraphs:
        return ""
    if len(paragraphs) <= max_paragraphs:
        return "\n\n".join(paragraphs)
    head = paragraphs[:max_paragraphs - 1]
    merged = " ".join(paragraphs[max_paragraphs - 1:]).strip()
    return "\n\n".join(head + [merged])


def _ensure_paragraphs(text: str) -> str:
    """保证「公司在做什么」至少 2 段、最多 3 段：模型偶尔会挤成一大段。"""
    text = _truncate_paragraphs(text, 3)
    if text.count("\n\n") >= 1 or len(text) < 220:
        return text
    sentences = [s.strip() for s in re.findall(r"[^。！？!?；;]+[。！？!?；;]?", text) if s.strip()]
    if len(sentences) < 3:
        return text
    size = max(1, len(sentences) // 3)
    chunks: list[str] = []
    for index in range(0, len(sentences), size):
        chunk = "".join(sentences[index:index + size]).strip()
        if chunk:
            chunks.append(chunk)
    while len(chunks) > 3:
        chunks[-2] = chunks[-2] + " " + chunks.pop()
    return "\n\n".join(chunks[:3])



def _truncate_bullets(text: str, max_items: int = 6) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    bullets: list[str] = []
    for line in lines:
        if re.match(r"^[-•·*]\s*", line):
            bullets.append("- " + re.sub(r"^[-•·*]\s*", "", line))
        elif bullets:
            bullets[-1] += " " + line
        else:
            bullets.append("- " + line)
    return "\n".join(bullets[:max_items])


def analyze_company(job: dict, search_results: list[dict]) -> dict:
    job_info = job.get("job") or {}
    company = job.get("company") or {}
    company_name = company.get("name") or job_info.get("company") or ""
    job_json = json.dumps({"job": job_info, "company": company}, ensure_ascii=False, indent=1)
    if search_results:
        search_text = "\n\n".join(
            f"[{index}] 标题：{item['title']}\n来源：{_source_of(item['url'])}\n"
            f"摘要：{item['snippet'] or '（无摘要）'}\n链接：{item['url']}"
            for index, item in enumerate(search_results, 1)
        )
    else:
        search_text = "（无搜索结果）"

    prompt = ANALYSIS_USER_TEMPLATE.format(
        job_json=job_json,
        n=len(search_results),
        search_text=search_text,
        extra="",
    )
    messages = [
        {"role": "system", "content": ANALYSIS_SYSTEM},
        {"role": "user", "content": prompt},
    ]

    last_error = None
    for attempt in range(2):
        raw = deepseek_chat(messages, max_tokens=6000, temperature=0.3)
        try:
            data = extract_json(raw)
            doing = _ensure_paragraphs(_as_text(data.get("doing") or data.get("company_doing")))
            talent = str(
                data.get("talent") or data.get("talents") or data.get("needs") or ""
            ).strip()
            if not doing or not talent:
                raise ValueError("doing/talent 为空")
            sources = [
                {
                    "title": item["title"],
                    "url": item["url"],
                    "snippet": item["snippet"],
                    "source": _source_of(item["url"]),
                }
                for item in search_results[:8]
            ]
            return {
                "doing": doing,
                "talent": _truncate_bullets(talent, 6),
                "sources": sources,
                "query": company_name,
                "generatedAt": now_text(),
            }
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == 0:
                messages = messages + [
                    {"role": "assistant", "content": raw[:2000]},
                    {"role": "user", "content": "上面的输出不是合法 JSON。请严格只输出 JSON 对象，不要任何解释。"},
                ]
    raise ApiError(f"分析结果解析失败（{last_error}），请点击「重新分析」重试", 502)

# ---------------------------------------------------------------------------
# 分析流水线（后台线程）
# ---------------------------------------------------------------------------

_pipeline_lock = threading.Lock()
_running_jobs: set[str] = set()


def start_pipeline(job_id: str, mode: str = "full") -> bool:
    with _pipeline_lock:
        if job_id in _running_jobs:
            return False
        _running_jobs.add(job_id)
    thread = threading.Thread(target=_pipeline_entry, args=(job_id, mode), daemon=True)
    thread.start()
    return True


def _pipeline_entry(job_id: str, mode: str) -> None:
    try:
        run_pipeline(job_id, mode)
    finally:
        with _pipeline_lock:
            _running_jobs.discard(job_id)


def _set_stage(job_id: str, stage: str) -> None:
    def mutate(job: dict) -> None:
        job["status"] = "processing"
        job["stage"] = stage
        job["error"] = ""
    update_job(job_id, mutate)


def _set_error(job_id: str, message: str) -> None:
    def mutate(job: dict) -> None:
        job["status"] = "error"
        job["stage"] = "error"
        job["error"] = message
    update_job(job_id, mutate)


def run_pipeline(job_id: str, mode: str = "full") -> None:
    job = get_job(job_id)
    if not job:
        return
    try:
        if mode == "full" or not (job.get("job") or {}).get("title"):
            _set_stage(job_id, "extract")
            extraction = extract_from_images(job)
            extracted_job = extraction["job"]
            if not extracted_job["title"]:
                raise ApiError("未识别到岗位 JD，请上传岗位详情页截图（截图需包含职位名称）。", 400)
            if not extracted_job["responsibilities"] and not extracted_job["requirements"]:
                raise ApiError("截图中缺少岗位职责/要求，请上传完整的岗位 JD 截图。", 400)

            def apply_extraction(current: dict) -> None:
                current["job"] = extraction["job"]
                current["company"] = extraction["company"]
            update_job(job_id, apply_extraction)

        _set_stage(job_id, "search")
        job = get_job(job_id) or {}
        company_name = (job.get("company") or {}).get("name") or (job.get("job") or {}).get("company") or ""
        search_results = search_company(company_name)

        _set_stage(job_id, "analyze")
        job = get_job(job_id) or {}
        analysis = analyze_company(job, search_results)

        def finish(current: dict) -> None:
            current["analysis"] = analysis
            current["status"] = "done"
            current["stage"] = "done"
            current["error"] = ""
        update_job(job_id, finish)
        print(f"[完成] {company_name or '未知公司'} · {(job.get('job') or {}).get('title', '')}")
    except ApiError as exc:
        print(f"[失败] {exc.message}", file=sys.stderr)
        _set_error(job_id, exc.message)
    except Exception as exc:  # noqa: BLE001 - 后台线程兜底，避免任务卡在 processing
        print(f"[失败] 未预期错误：{exc}", file=sys.stderr)
        _set_error(job_id, f"分析失败：{exc}")


# ---------------------------------------------------------------------------
# 图片 / 任务创建
# ---------------------------------------------------------------------------


def decode_image(data_url: str, label: str) -> tuple[bytes, str]:
    if not isinstance(data_url, str) or not data_url.strip().startswith("data:"):
        raise ApiError(f"{label}格式不正确", 400)
    match = DATA_URL_RE.match(data_url.strip())
    if not match:
        raise ApiError(f"{label}格式不正确（需要 base64 data URL）", 400)
    mime = match.group(1).lower()
    ext = IMAGE_MIME_EXT.get(mime)
    if not ext:
        raise ApiError(f"{label}格式不支持：{mime}", 400)
    try:
        raw = base64.b64decode(match.group(2), validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(f"{label}数据损坏，请重新上传", 400) from exc
    if not raw:
        raise ApiError(f"{label}是空图片", 400)
    if len(raw) > MAX_IMAGE_BYTES:
        raise ApiError(f"{label}不能超过 20MB", 400)
    return raw, ext


def save_upload(filename: str, raw: bytes) -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOADS_DIR / filename).write_bytes(raw)


def create_job(payload: dict) -> dict:
    if not api_key():
        raise ApiError("还没有配置 DeepSeek API Key，请点击右上角「设置」填写。", 400)
    with _jobs_lock:
        if len(_jobs) >= MAX_JOBS:
            raise ApiError(f"最多保存 {MAX_JOBS} 个岗位，请先删除一些旧岗位。", 400)

    jd_raw, jd_ext = decode_image(payload.get("jdImage"), "岗位 JD 截图")
    company_raw = company_ext = None
    if payload.get("companyImage"):
        company_raw, company_ext = decode_image(payload.get("companyImage"), "公司详情截图")

    job_id = time.strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6]
    jd_name = f"jd-{job_id}.{jd_ext}"
    save_upload(jd_name, jd_raw)
    company_name = None
    if company_raw is not None and company_ext:
        company_name = f"company-{job_id}.{company_ext}"
        save_upload(company_name, company_raw)

    job = normalize_job(default_job_skeleton())
    job.update({
        "id": job_id,
        "createdAt": now_text(),
        "updatedAt": now_text(),
        "status": "processing",
        "stage": "extract",
        "images": {"jd": jd_name, "company": company_name or ""},
    })
    with _jobs_lock:
        _jobs[job_id] = job
        save_jobs_locked()
    start_pipeline(job_id, "full")
    return clone(job)


def delete_job(job_id: str) -> bool:
    with _jobs_lock:
        job = _jobs.pop(job_id, None)
        if job is None:
            return False
        save_jobs_locked()
    for filename in (job.get("images") or {}).values():
        path = _image_path(filename or "")
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass
    return True


def patch_job(job_id: str, payload: dict) -> dict | None:
    def mutate(job: dict) -> None:
        job_info = job.setdefault("job", {})
        company = job.setdefault("company", {})
        if "title" in payload:
            job_info["title"] = _as_text(payload.get("title"))[:80]
        if "company" in payload:
            name = _as_text(payload.get("company"))[:80]
            job_info["company"] = name
            company["name"] = name
        if "type" in payload:
            new_type = _as_text(payload.get("type"))
            if new_type not in JOB_TYPES:
                raise ApiError("岗位类型不合法", 400)
            job_info["type"] = new_type
    try:
        return update_job(job_id, mutate)
    except ApiError:
        raise


# ---------------------------------------------------------------------------
# 测试连接
# ---------------------------------------------------------------------------


def test_deepseek(key: str | None = None) -> dict:
    key = (key or api_key()).strip()
    if not key:
        return {"ok": False, "error": "还没有填写 API Key"}
    req = urllib.request.Request(
        base_url() + "/models",
        headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"ok": False, "error": "API Key 无效（401），请检查后重试"}
        return {"ok": False, "error": f"接口返回 {exc.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": f"无法连接：{exc}"}
    models = [str(item.get("id")) for item in (payload.get("data") or []) if isinstance(item, dict)]
    return {"ok": True, "models": models}


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"JD-Analyzer/{APP_VERSION}"

    def log_message(self, fmt: str, *args) -> None:  # 保持控制台干净
        pass

    # ---- 响应工具 ----
    def _send(self, status: int, body: bytes, content_type: str,
              extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError("请求头不合法", 400)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ApiError("上传内容太大（图片合计不能超过约 30MB）", 413)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("请求体不是合法 JSON", 400) from exc
        if not isinstance(data, dict):
            raise ApiError("请求体必须是 JSON 对象", 400)
        return data

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/health":
                self._json({"ok": True, "hasKey": bool(api_key()), "version": APP_VERSION})
            elif path == "/api/settings":
                settings = current_settings()
                self._json({
                    "ok": True,
                    "hasKey": bool(api_key()),
                    "keyHint": mask_key(api_key()),
                    "baseUrl": settings.get("baseUrl", ""),
                })
            elif path == "/api/jobs":
                self._list_jobs()
            elif path.startswith("/api/jobs/"):
                job_id = path[len("/api/jobs/"):].strip("/")
                job = get_job(job_id)
                if job is None:
                    self._error("岗位不存在", 404)
                else:
                    self._json({"ok": True, "job": job_public(job)})
            elif path.startswith("/api/images/"):
                filename = urllib.parse.unquote(path[len("/api/images/"):])
                self._serve_image(filename)
            elif path in STATIC_FILES:
                self._serve_static(*STATIC_FILES[path])
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._error("页面不存在", 404)
        except ApiError as exc:
            self._error(exc.message, exc.status)
        except Exception as exc:  # noqa: BLE001
            print(f"[错误] {exc}", file=sys.stderr)
            self._error(f"服务器内部错误：{exc}", 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/jobs":
                payload = self._read_json()
                job = create_job(payload)
                self._json({"ok": True, "job": job_public(job)})
            elif path == "/api/settings":
                payload = self._read_json()
                settings = current_settings()
                if "apiKey" in payload:
                    settings["apiKey"] = str(payload.get("apiKey") or "").strip()
                if "baseUrl" in payload:
                    scheme_ok = str(payload.get("baseUrl") or "").strip()
                    if scheme_ok and not re.match(r"^https?://", scheme_ok):
                        raise ApiError("Base URL 需要以 http:// 或 https:// 开头", 400)
                    settings["baseUrl"] = scheme_ok.rstrip("/")
                save_settings(settings)
                self._json({
                    "ok": True,
                    "hasKey": bool(api_key()),
                    "keyHint": mask_key(api_key()),
                    "baseUrl": settings.get("baseUrl", ""),
                })
            elif path == "/api/settings/test":
                payload = self._read_json()
                key = payload.get("apiKey") if payload.get("apiKey") else None
                self._json(test_deepseek(key))
            elif path.startswith("/api/jobs/") and path.endswith("/reanalyze"):
                job_id = path[len("/api/jobs/"):-len("/reanalyze")].strip("/")
                self._reanalyze(job_id)
            else:
                self._error("接口不存在", 404)
        except ApiError as exc:
            self._error(exc.message, exc.status)
        except Exception as exc:  # noqa: BLE001
            print(f"[错误] {exc}", file=sys.stderr)
            self._error(f"服务器内部错误：{exc}", 500)

    def do_PATCH(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path.startswith("/api/jobs/") and not path.endswith("/reanalyze"):
                job_id = path[len("/api/jobs/"):].strip("/")
                payload = self._read_json()
                job = patch_job(job_id, payload)
                if job is None:
                    self._error("岗位不存在", 404)
                else:
                    self._json({"ok": True, "job": job_public(job)})
            else:
                self._error("接口不存在", 404)
        except ApiError as exc:
            self._error(exc.message, exc.status)
        except Exception as exc:  # noqa: BLE001
            print(f"[错误] {exc}", file=sys.stderr)
            self._error(f"服务器内部错误：{exc}", 500)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            if path.startswith("/api/jobs/"):
                job_id = path[len("/api/jobs/"):].strip("/")
                if delete_job(job_id):
                    self._json({"ok": True})
                else:
                    self._error("岗位不存在", 404)
            else:
                self._error("接口不存在", 404)
        except ApiError as exc:
            self._error(exc.message, exc.status)
        except Exception as exc:  # noqa: BLE001
            print(f"[错误] {exc}", file=sys.stderr)
            self._error(f"服务器内部错误：{exc}", 500)

    # ---- 业务 ----
    def _list_jobs(self) -> None:
        with _jobs_lock:
            jobs = [job_public(clone(item)) for item in _jobs.values()]
        jobs.sort(key=lambda item: str(item.get("createdAt", "")), reverse=True)
        counts = {job_type: 0 for job_type in JOB_TYPES}
        for job in jobs:
            job_type = (job.get("job") or {}).get("type") or "其他"
            counts[job_type] = counts.get(job_type, 0) + 1
        self._json({"ok": True, "jobs": jobs, "types": JOB_TYPES, "counts": counts})

    def _reanalyze(self, job_id: str) -> None:
        job = get_job(job_id)
        if job is None:
            self._error("岗位不存在", 404)
            return
        if with_running(job_id):
            self._json({"ok": True, "job": job_public(job), "running": True})
            return

        def reset(current: dict) -> None:
            current["status"] = "processing"
            current["stage"] = "search"
            current["error"] = ""
        update_job(job_id, reset)
        start_pipeline(job_id, "analyze")
        updated = get_job(job_id) or job
        self._json({"ok": True, "job": job_public(updated)})

    def _serve_image(self, filename: str) -> None:
        path = _image_path(filename)
        if path is None:
            self._error("图片不存在", 404)
            return
        ext = path.suffix.lower().lstrip(".")
        self._send(200, path.read_bytes(), EXT_MIME.get(ext, "application/octet-stream"))

    def _serve_static(self, filename: str, content_type: str) -> None:
        path = WEB_DIR / filename
        if not path.is_file():
            self._error("前端文件缺失，请检查 web/ 目录", 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def with_running(job_id: str) -> bool:
    with _pipeline_lock:
        return job_id in _running_jobs


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} 本地服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，仅本机）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--data-dir", default="", help="数据目录（默认项目下 data/，测试时可隔离）")
    args = parser.parse_args()

    global DATA_DIR, UPLOADS_DIR, JOBS_FILE, SETTINGS_FILE
    if args.data_dir:
        DATA_DIR = Path(args.data_dir).expanduser().resolve()
        UPLOADS_DIR = DATA_DIR / "uploads"
        JOBS_FILE = DATA_DIR / "jobs.json"
        SETTINGS_FILE = DATA_DIR / "settings.json"

    for directory in (DATA_DIR, UPLOADS_DIR, WEB_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    load_jobs()

    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}"
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"[错误] 无法监听 {args.host}:{args.port}（{exc}）。可能端口被占用，换一个端口："
              f"python server.py --port 8601", file=sys.stderr)
        sys.exit(1)

    print("=" * 56)
    print(f"  {APP_NAME} v{APP_VERSION}")
    print(f"  访问地址：{url}")
    print(f"  API Key：{'已配置' if api_key() else '未配置（请到页面右上角「设置」填写）'}")
    print(f"  数据目录：{DATA_DIR}")
    print("  关闭这个窗口或按 Ctrl+C 即可停止服务")
    print("=" * 56)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
