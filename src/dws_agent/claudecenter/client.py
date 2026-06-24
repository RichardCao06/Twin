"""ClaudeCenter Console 的最小 HTTP 客户端（只用标准库 urllib + cookiejar）。

对接的 REST 端点（apps/console/app/api）：

  POST  /api/auth/login   {username,password}            → 200 {user}; Set-Cookie 会话
  GET   /api/projects                                     → 200 {projects:[{id,name,...}]}
  POST  /api/tasks        {projectId,title,description,…} → 201 {task}（status=draft）
  PATCH /api/tasks/{id}   {action:"publish"}              → 200 {task}（draft→pending）

会话 cookie 由 CookieJar 自动随后续请求带上。配置经环境变量：

  CLAUDE_CENTER_URL       Console 地址，如 http://127.0.0.1:3000
  CLAUDE_CENTER_USER      登录用户名（**建议专用 publisher 账号，别用 admin**）
  CLAUDE_CENTER_PASSWORD  密码

只读 / 只建草稿的方法不触发任何执行；唯一会让 Worker 跑起来的是 publish_task，
调用方（CLI）把它放在你显式确认之后。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Optional


class ClaudeCenterError(RuntimeError):
    """对接 ClaudeCenter 时的可读错误（连不上 / 鉴权失败 / 业务报错）。"""


class ClaudeCenterClient:
    """登录拿会话 cookie，然后列项目 / 建 draft 任务 / 发布任务。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        timeout: int = 30,
    ) -> None:
        self.base_url = (base_url or os.environ.get("CLAUDE_CENTER_URL") or "").rstrip("/")
        self.user = user or os.environ.get("CLAUDE_CENTER_USER")
        self.password = password or os.environ.get("CLAUDE_CENTER_PASSWORD")
        self.timeout = timeout
        if not self.base_url:
            raise ClaudeCenterError(
                "缺 CLAUDE_CENTER_URL（如 http://127.0.0.1:3000）—— 在环境变量里配置")
        self._jar = CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar))
        self._logged_in = False

    # -- low level ---------------------------------------------------------
    def _request(self, method: str, path: str, body: Any = None):
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"error": raw[:300]}
            return exc.code, payload
        except urllib.error.URLError as exc:
            raise ClaudeCenterError(
                "连不上 ClaudeCenter（%s）：%s —— Console 起了吗？"
                % (self.base_url, exc.reason))

    @staticmethod
    def _err(payload: Any) -> str:
        if isinstance(payload, dict):
            return str(payload.get("error") or payload)
        return str(payload)

    # -- auth --------------------------------------------------------------
    def login(self) -> dict:
        if not self.user or not self.password:
            raise ClaudeCenterError(
                "缺 CLAUDE_CENTER_USER / CLAUDE_CENTER_PASSWORD")
        code, payload = self._request(
            "POST", "/api/auth/login",
            {"username": self.user, "password": self.password})
        if code != 200:
            raise ClaudeCenterError("登录失败（HTTP %s）：%s" % (code, self._err(payload)))
        self._logged_in = True
        return payload.get("user") or {}

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    # -- projects ----------------------------------------------------------
    def list_projects(self) -> list:
        self._ensure_login()
        code, payload = self._request("GET", "/api/projects")
        if code != 200:
            raise ClaudeCenterError("拉取项目失败（HTTP %s）：%s" % (code, self._err(payload)))
        return payload.get("projects") or []

    def resolve_project(self, name_or_id: str) -> dict:
        """按 id 精确，否则按 name（大小写不敏感）唯一匹配，返回项目对象。"""
        projects = self.list_projects()
        for p in projects:
            if p.get("id") == name_or_id:
                return p
        low = name_or_id.lower()
        matches = [p for p in projects if (p.get("name") or "").lower() == low]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            names = ", ".join(p.get("name", "?") for p in projects) or "(无项目)"
            raise ClaudeCenterError("找不到项目 '%s'。现有：%s" % (name_or_id, names))
        raise ClaudeCenterError("项目名 '%s' 不唯一，请改用项目 id" % name_or_id)

    # -- tasks -------------------------------------------------------------
    def create_task(
        self,
        project_id: str,
        title: str,
        description: str,
        *,
        base_branch: Optional[str] = None,
        target_branch: Optional[str] = None,
        submit_mode: str = "pr",
        auto_reply: bool = False,
        model: Optional[str] = None,
    ) -> dict:
        """建任务（落 **draft**，不会被 Worker 认领，需 publish 才进队列）。"""
        self._ensure_login()
        body: dict = {
            "projectId": project_id,
            "title": title,
            "description": description,
            "submitMode": "push" if submit_mode == "push" else "pr",
        }
        if base_branch:
            body["baseBranch"] = base_branch
        if target_branch:
            body["targetBranch"] = target_branch
        if auto_reply:
            body["autoReply"] = True
        if model and model != "default":
            body["model"] = model
        code, payload = self._request("POST", "/api/tasks", body)
        if code != 201:
            raise ClaudeCenterError("建任务失败（HTTP %s）：%s" % (code, self._err(payload)))
        return payload.get("task") or {}

    def publish_task(self, task_id: str) -> dict:
        """发布 draft（draft→pending，Worker 随后认领执行）。危险动作，由 CLI 在你确认后调用。"""
        self._ensure_login()
        code, payload = self._request(
            "PATCH", "/api/tasks/%s" % task_id, {"action": "publish"})
        if code != 200:
            raise ClaudeCenterError("发布失败（HTTP %s）：%s" % (code, self._err(payload)))
        return payload.get("task") or {}
