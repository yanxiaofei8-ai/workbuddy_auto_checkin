#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 签到助手 · 签到执行模块

本模块只负责在**已获得有效接口令牌**的前提下执行：
  - 查询签到状态、执行每日签到
  - 查询 / 领取 / 派出 Buddy 旅行
  - 可选的结果消息推送

令牌由调用方（rt_auth.py）在用户显式配置长效令牌后换取并传入；
本模块仅使用调用方传入的令牌。

仅请求官方接口，不改动本机任何配置文件。
"""

import sys

# Python 版本守卫：太旧时给出友好提示，而不是抛出一堆堆栈
if sys.version_info < (3, 6):
    sys.stderr.write(
        "WorkBuddy签到助手：需要 Python 3.6+，当前为 %s。\n"
        "请安装 Python 3.8+（https://www.python.org/downloads/，勾选 Add to PATH），\n"
        "或直接使用 WorkBuddy 自带的托管 Python（~/.workbuddy/binaries/python）。\n"
        % sys.version.split()[0])
    sys.exit(2)

import json
import os
import random
import subprocess
import sys
import time
import urllib.request
import urllib.error

# 多渠道消息推送模块（接口/配置项对齐 totorosir-push-message 技能，自包含、零依赖）
try:
    import push_message  # 与本脚本同目录
except Exception:  # noqa: BLE001 - 模块缺失时回落到静默，不影响签到
    push_message = None  # type: ignore

# ---- 配置 ----
STATUS_PATH = "/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/billing/meter/daily-checkin"
# 账户可用积分余额接口（套餐额度）；与签到活动累计积分是两个不同口径
RESOURCE_BASE = "https://www.workbuddy.cn"
RESOURCE_SUMMARY_PATH = "/billing/meter/get-user-resource-summary"
HTTP_TIMEOUT = 10
MAX_RETRY = 1
VERSION = "3.1.8"

# 推送渠道覆盖（由命令行 --push-channels 设定，None=用配置里全部已就绪渠道）
_PUSH_CHANNELS = None
_PUSH_CONFIRM_PAID = False

# ---- 派猫猫旅行配置 ----
# 关键：旅行接口与签到接口不是同一个域名，且路径不带 /v2 前缀，写错一律 404。
TRAVEL_BASE = "https://www.workbuddy.cn"
TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"   # GET  旅行状态
TRAVEL_CONFIG_PATH = "/activity/growth/buddy/travel/config"   # GET  可选地点
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"     # POST 领取旅行积分
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"   # POST 派出 Buddy
TRAVEL_STATE_TEXT = {
    "idle": "空闲（可派遣）",
    "traveling": "旅行中",
    "arrived": "已到达，待领取",
}
# 失败推送配置（含密钥，仅本地，不入库）
NOTIFY_CONFIG = os.path.join(os.path.expanduser("~"),
                             ".workbuddy", "scripts", "notify_config.json")


def api_call(base, path, token, payload=None, method="POST"):
    url = base + path
    # GET 且未显式传 payload 时不发请求体；其余情况按 JSON 编码发送
    if method == "GET" and payload is None:
        data = None
    else:
        data = json.dumps(payload if payload is not None else {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "WorkBuddy-Checkin-Script/1.1")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, {"raw": body}
    except urllib.error.HTTPError as e:
        # 非 2xx 也读取响应体（如已签到返回的 HTTP 400 / code=10001）
        try:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except json.JSONDecodeError:
                return e.code, {"raw": body}
        except Exception:
            return e.code, {"raw": ""}


def mask_token(t):
    if not t:
        return "<empty>"
    return t[:6] + "..." + t[-4:]


def _extract_checkin_total(*bodies):
    """提取「本期签到活动累计发放积分」（data.total_credits）。

    口径说明：签到状态接口返回的 total_credits 是本期活动累计发放的签到积分
    （约为签到天数 × 每日额度），**不是账户可用积分余额**。账户可用余额需另查
    get-user-resource-summary，见 query_credit_balance()。历史版本把该字段当成
    「积分余额」推送，导致通知数值与客户端实际余额不符。
    """
    for body in bodies:
        if not isinstance(body, dict):
            continue
        for node in (body, body.get("data"), body.get("result")):
            if not isinstance(node, dict):
                continue
            v = node.get("total_credits")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return v
    return None


def _fmt_credit(value):
    """额度字段规整为可读字符串：「1930.19」「500」；无效返回 None。"""
    try:
        f = float(str(value).strip())
        if f != f or f < 0:  # NaN / 负值
            return None
        if f == int(f):
            return str(int(f))
        return ("%.2f" % f).rstrip("0").rstrip(".")
    except Exception:
        return None


def query_credit_balance(token):
    """只读查询账户真实可用积分余额（套餐剩余额度）。

    口径：POST /billing/meter/get-user-resource-summary → data.TotalCredits；
    字段缺失时回退为各套餐 CycleRemainCapacity 之和。
    任何失败一律静默降级返回 ok=False，不影响签到主流程与退出码。
    """
    out = {"available": None, "main_remain": None,
           "packages": [], "ok": False}
    try:
        st, body = api_call(RESOURCE_BASE, RESOURCE_SUMMARY_PATH, token)
        if st != 200 or not isinstance(body, dict) or body.get("code") != 0:
            return out
        data = body.get("data")
        if not isinstance(data, dict):
            return out
        pkgs = data.get("Packages")
        if isinstance(pkgs, list):
            for p in pkgs:
                if isinstance(p, dict):
                    out["packages"].append(
                        _fmt_credit(p.get("CycleRemainCapacity")) or "-")
        if out["packages"]:
            out["main_remain"] = out["packages"][0]
        total = _fmt_credit(data.get("TotalCredits"))
        if total is None and out["packages"]:
            try:
                total = _fmt_credit(sum(
                    float(x) for x in out["packages"] if x not in (None, "-")))
            except Exception:
                total = None
        out["available"] = total
        out["ok"] = total is not None
    except Exception:
        pass
    return out


# ---------------- 消息推送（多渠道，见 push_message.py） ----------------

def load_notify_config():
    """读取本地推送配置（原始结构，可能含新 channels 或旧扁平字段）。

    仅用于判定 enabled / success_notify 等顶层开关；实际渠道解析交给 push_message。
    """
    if not os.path.isfile(NOTIFY_CONFIG):
        return None
    try:
        with open(NOTIFY_CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return None
        return cfg
    except Exception:
        return None


def _push_failure(res):
    """签到失败时，按本地配置推送提醒；配置缺失或推送模块不可用时静默跳过。

    通过 push_message.send_message 调度多渠道（钉钉/飞书/企业微信/PushPlus/Bark/邮件/…），
    单渠道失败不影响其他渠道，也不影响签到结果与退出码。
    """
    if push_message is None:
        return
    now = time.strftime("%Y-%m-%d %H:%M:%S")  # 本机时区（北京时间）
    msg = res.get("msg", "未知原因")
    title = "⚠️ WorkBuddy签到助手 · 签到失败"
    content = (
        "### ⚠️ WorkBuddy签到助手 · 签到失败\n\n"
        "> **时间**：%s\n\n"
        "> **原因**：%s\n\n"
        "> **处理建议**：请检查网络连接与令牌是否有效；"
        "令牌失效时重新录入后可再跑一次脚本。\n"
    ) % (now, msg)
    try:
        result = push_message.send_message(
            title, content, channels=_PUSH_CHANNELS,
            content_type="markdown", confirm_paid=_PUSH_CONFIRM_PAID)
        res["detail"]["notify"] = result.get("results", [])
    except Exception:
        pass  # 推送失败绝不影响签到


def _push_success(res):
    """签到成功（新签到 / 今日已签跳过）时，按本地配置推送播报。

    仅当 notify_config.json 中 success_notify=true 时才推送；否则静默。
    通过 push_message.send_message 调度多渠道；失败时静默，不影响签到。
    """
    if push_message is None:
        return
    cfg = load_notify_config()
    if not cfg or cfg.get("enabled") is False:
        return
    if not cfg.get("success_notify"):
        return

    now = time.strftime("%Y-%m-%d %H:%M:%S")  # 本机时区（北京时间）
    action = res.get("action")
    # 仅对明确的成功态推送（新签到 / 今日已签跳过）；其他态（失败 / 纯查询）不推
    if action not in ("clicked", "skip_already_signed"):
        return
    msg = res.get("msg", "")
    points = res.get("points")
    streak = res.get("detail", {}).get("streak_days")
    balance = res.get("balance")

    if action == "skip_already_signed":
        title = "✅ WorkBuddy签到助手 · 今日已签"
        content = (
            "### ✅ WorkBuddy签到助手 · 今日已签\n\n"
            "> **时间**：%s\n\n"
            "> **状态**：今日已签到，无需重复领取（幂等保护）\n\n"
            "> **说明**：系统定时任务 / 技能已正常执行，无需处理。\n"
        ) % now
    else:
        title = "✅ WorkBuddy签到助手 · 签到成功"
        lines = (
            "### ✅ WorkBuddy签到助手 · 签到成功\n\n"
            "> **时间**：%s\n\n"
            "> **状态**：%s\n"
        ) % (now, msg)
        if points:
            lines += "> **积分**：+%s\n\n" % points
        if streak:
            lines += "> **连续天数**：第 %s 天\n\n" % streak
        if balance is not None:
            lines += "> **可用积分余额**：%s\n\n" % balance
        lines += "> **说明**：系统定时任务 / 技能已正常执行，无需处理。\n"
        content = lines

    try:
        result = push_message.send_message(
            title, content, channels=_PUSH_CHANNELS,
            content_type="markdown", confirm_paid=_PUSH_CONFIRM_PAID)
        res["detail"]["notify_success"] = result.get("results", [])
    except Exception:
        pass  # 推送失败绝不影响签到


def notify_system(res):
    """签到完成后弹出操作系统级桌面通知（toast / 气球提示），展示结果 + 积分余额。

    跨平台兼容 Windows / macOS / Linux；best-effort、非阻塞、失败静默，
    绝不影响签到结果与退出码。受 --no-notify 一并抑制（与消息推送调试开关一致）。
    """
    try:
        status = res.get("status")
        title = "WorkBuddy签到助手"
        if status == "error":
            body = "签到失败：" + str(res.get("msg", ""))
        else:
            body = str(res.get("msg", "签到完成"))
            bal = res.get("balance")
            if bal is not None:
                body += " ｜ 可用积分余额：" + str(bal)
        _system_toast(title, body)
    except Exception:
        pass  # 通知失败绝不影响签到


def _system_toast(title, body):
    """按操作系统分发到原生命令；任何异常一律忽略。"""
    plat = sys.platform
    try:
        if plat == "darwin":
            msg = body.replace('"', "'")
            subprocess.run(
                ["osascript", "-e",
                 'display notification "%s" with title "%s"' % (msg, title)],
                timeout=5, check=False,
            )
        elif plat.startswith("linux"):
            subprocess.run(["notify-send", title, body], timeout=5, check=False)
        elif plat == "win32":
            _win_toast(title, body)
    except Exception:
        pass


def _win_toast(title, body):
    """Windows：用 .NET 气球提示（非阻塞、约 5s）。PowerShell 不可用或被策略拦截时静默跳过。"""
    safe_title = title.replace("'", "''")
    safe_body = body.replace("'", "''")
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "Add-Type -AssemblyName System.Drawing;"
          "$n=New-Object System.Windows.Forms.NotifyIcon;"
          "$n.Icon=[System.Drawing.SystemIcons]::Information;"
          "$n.Visible=$true;"
          "$n.ShowBalloonTip(5000,'%s','%s','Info');"
          "Start-Sleep -Milliseconds 150;"
          "$n.Dispose()") % (safe_title, safe_body)
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        timeout=10, check=False,
    )


# ---------------- 主流程 ----------------

def _do_checkin(base, token, check_only):
    """执行签到主流程（查询状态 → 必要时领取）。返回结构化结果片段。"""
    result = {"status": "unknown", "action": None, "points": None,
              "balance": None, "checkin_total_credits": None,
              "msg": "", "detail": {}}
    attempt = 0
    last_err = None
    # 余额与累计积分均通过结构化字段输出（balance / checkin_total_credits），
    # 由汇总层统一排版，避免 msg 中出现两套口径或重复数值。
    while attempt <= MAX_RETRY:
        attempt += 1
        try:
            # 1) 查询今日状态
            st_code, st_body = api_call(base, STATUS_PATH, token)
            result["detail"]["status_http"] = st_code
            result["detail"]["status_resp"] = st_body
            # 服务端 5xx 时 code 可能也是 10001（与「今日已签到」同码），
            # 一律不得据此判定已签到，否则会漏签；先重试，仍失败则如实报错。
            if st_code >= 500:
                last_err = "状态接口 HTTP %s（%s）" % (
                    st_code, str((st_body or {}).get("msg") or "")[:60])
                if attempt <= MAX_RETRY:
                    time.sleep(2)
                    continue
                result.update(status="error", action="failed",
                              msg="签到状态查询失败: " + last_err)
                return result
            # 签到活动累计积分：仅作「本期累计」展示，不是账户余额
            ctotal = _extract_checkin_total(st_body)
            if ctotal is not None:
                result["checkin_total_credits"] = ctotal
                result["detail"]["checkin_total_credits"] = ctotal
            # 账户真实可用积分余额另查资源汇总接口（只读，失败静默降级）
            bal = query_credit_balance(token)
            result["balance"] = bal["available"]
            result["detail"]["balance"] = bal["available"]
            result["detail"]["balance_detail"] = bal

            # 判断是否已签到：兼容多种返回形态
            today_signed = False
            if isinstance(st_body, dict):
                if st_body.get("today_checked_in") is True:
                    today_signed = True
                elif st_body.get("data", {}).get("today_checked_in") is True:
                    today_signed = True
                elif str(st_body.get("code")) == "10001":
                    today_signed = True

            if check_only:
                result.update(
                    status="ok",
                    action="skip_check_only",
                    msg="状态查询成功（未执行领取）",
                )
                result["detail"]["today_signed"] = today_signed
                return result

            if today_signed:
                result.update(status="ok", action="skip_already_signed",
                              msg="今日已签到，无需重复领取")
                return result

            # 2) 领取签到
            ck_code, ck_body = api_call(base, CHECKIN_PATH, token)
            result["detail"]["checkin_http"] = ck_code
            result["detail"]["checkin_resp"] = ck_body

            if isinstance(ck_body, dict):
                code = str(ck_body.get("code", ""))
                msg = ck_body.get("msg") or ck_body.get("message") or ""
                data = ck_body.get("data") if isinstance(ck_body.get("data"), dict) else {}
                # 已签/重复提示（幂等保护）
                if code == "10001" or "已签到" in msg or "今天已签到" in msg:
                    result.update(status="ok", action="skip_already_signed",
                                  msg="今日已签到（接口返回 code=10001）")
                    return result
                # 领取成功判定：HTTP 2xx 且业务码为成功（兼容无 code / code=0 / code=200）
                success_code = code in ("", "0", "200")
                if 200 <= ck_code < 300 and success_code:
                    credit = (ck_body.get("credit") or data.get("credit")
                              or data.get("daily_credit") or data.get("today_credit"))
                    streak = ck_body.get("streak_days") or data.get("streak_days")
                    # 领取响应若带回累计积分则更新「本期累计」，不触碰账户余额
                    ctotal = _extract_checkin_total(ck_body)
                    if ctotal is not None:
                        result["checkin_total_credits"] = ctotal
                        result["detail"]["checkin_total_credits"] = ctotal
                    result.update(status="ok", action="clicked",
                                  points=credit,
                                  msg="领取成功" + (("，+%s 积分" % credit) if credit else "") +
                                      (("，连续第 %s 天" % streak) if streak else ""))
                    result["detail"]["streak_days"] = streak
                    return result
                # 其余视为失败（含 2xx 但业务码异常、或非 2xx）
                result.update(status="error", action="failed",
                              msg=msg or ("HTTP %s（业务码 %s）" % (ck_code, code)))
                return result
            else:
                result.update(status="error", action="failed",
                              msg="领取接口返回非 JSON: %s" % ck_body.get("raw", "")[:200])
                return result

        except urllib.error.HTTPError as e:
            last_err = "HTTP %s: %s" % (e.code, e.reason)
        except urllib.error.URLError as e:
            last_err = "网络错误: %s" % e.reason
        except Exception as e:
            last_err = "异常: %s" % e

        # 重试前稍作等待
        if attempt <= MAX_RETRY:
            time.sleep(2)

    result.update(status="error", msg="重试 %d 次后仍失败: %s" % (MAX_RETRY, last_err))
    return result


# ---------------- 派猫猫旅行 ----------------

def _travel_get(token, path):
    """旅行只读 GET；失败 / 非 200 / 非 0 码均返回 None（静默降级，不影响签到主结果）。"""
    try:
        st, body = api_call(TRAVEL_BASE, path, token, method="GET")
        if st != 200 or not isinstance(body, dict) or body.get("code") != 0:
            return None
        return body.get("data")
    except Exception:
        return None


def _fmt_duration(seconds):
    """把秒数格式化为「X 小时 Y 分」。"""
    if seconds is None:
        return None
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours > 0:
        return "%d 小时 %d 分" % (hours, minutes)
    if minutes > 0:
        return "%d 分钟" % minutes
    return "%d 秒" % seconds


def parse_travel(status, config=None):
    """规整派猫猫旅行状态；字段缺失填 None，绝不编造。"""
    if not isinstance(status, dict):
        return {"available": False}
    locations = []
    if isinstance(config, dict) and isinstance(config.get("locations"), list):
        for loc in config["locations"]:
            if isinstance(loc, dict):
                locations.append({"id": loc.get("id"),
                                  "name": loc.get("name") or "未命名地点"})
    state = status.get("state")
    remaining = None
    if state == "traveling":
        arrive, now = status.get("arrive_at"), status.get("server_now")
        if isinstance(arrive, int) and isinstance(now, int):
            remaining = max(0, arrive - now)
    loc = status.get("location")
    return {
        "available": True,
        "state": state,
        "state_text": TRAVEL_STATE_TEXT.get(state, state),
        "location_name": loc.get("name") if isinstance(loc, dict) else None,
        "reward_credit": status.get("reward_credit"),
        "remaining_seconds": remaining,
        "remaining_text": _fmt_duration(remaining) if remaining is not None else None,
        "daily_limit_reached": status.get("daily_limit_reached"),
        "locations": locations,
    }


def travel_claim(token):
    """领取已到达旅行的积分（写接口）。无可领取时服务端返回非 0 码，如实上报。"""
    try:
        st, body = api_call(TRAVEL_BASE, TRAVEL_CLAIM_PATH, token, payload={})
    except Exception as e:
        return {"action": "failed", "success": False, "message": str(e)}
    if st == 200 and isinstance(body, dict) and body.get("code") == 0:
        data = body.get("data") or {}
        credit = data.get("reward_credit")
        return {"action": "claimed", "success": True, "reward_credit": credit,
                "message": "已领取旅行奖励 +%s 积分" % (credit if credit is not None else "?")}
    return {"action": "failed", "success": False,
            "message": (body.get("msg") if isinstance(body, dict) else None) or ("HTTP %s" % st),
            "code": body.get("code") if isinstance(body, dict) else None}


def travel_depart(token, location_id=None, locations=None):
    """派出 Buddy 旅行（写接口）。location_id 为空时从可选地点中随机。

    仅调用方确认「未达每日上限」后才应调用本函数；本函数不再重复查询状态。
    """
    chosen = location_id
    if chosen is None:
        ids = [loc.get("id") for loc in (locations or []) if loc.get("id") is not None]
        if not ids:
            return {"action": "failed", "success": False,
                    "message": "未能获取可选地点列表，已跳过派遣"}
        chosen = random.choice(ids)
    try:
        st, body = api_call(TRAVEL_BASE, TRAVEL_DEPART_PATH, token,
                            payload={"location_id": chosen})
    except Exception as e:
        return {"action": "failed", "success": False,
                "location_id": chosen, "message": str(e)}
    if st == 200 and isinstance(body, dict) and body.get("code") == 0:
        data = body.get("data") or {}
        loc = data.get("location") or {}
        return {"action": "departed", "success": True, "location_id": chosen,
                "location_name": loc.get("name"), "arrive_at": data.get("arrive_at"),
                "message": "已派出 Buddy 前往【%s】" % (loc.get("name") or ("地点 %s" % chosen))}
    return {"action": "failed", "success": False, "location_id": chosen,
            "message": (body.get("msg") if isinstance(body, dict) else None) or ("HTTP %s" % st),
            "code": body.get("code") if isinstance(body, dict) else None}


def travel_auto(token, location_id=None):
    """派猫猫旅行全自动闭环（写操作）：先领取、后派遣。

    安全约束：派遣前必须先确认 daily_limit_reached 为假，达上限绝不发写请求。
    已到达(arrived) 状态会一直保留、不会丢积分，下次运行自动补领。
    """
    log = []
    status = _travel_get(token, TRAVEL_STATUS_PATH)
    if status is None:
        return {"available": False, "success": False,
                "log": ["查询旅行状态失败，已跳过全部写操作"], "status": None}

    if status.get("state") == "arrived":
        result = travel_claim(token)
        log.append(result.get("message") or result.get("action"))
        if result.get("action") == "claimed":
            fresh = _travel_get(token, TRAVEL_STATUS_PATH)
            if fresh is not None:
                status = fresh

    if status.get("daily_limit_reached"):
        log.append("今日派遣次数已用完，跳过派遣")
    elif status.get("state") in (None, "", "idle"):
        config = _travel_get(token, TRAVEL_CONFIG_PATH) or {}
        result = travel_depart(token, location_id, config.get("locations") or [])
        log.append(result.get("message") or result.get("action"))
    else:
        log.append("Buddy 正在旅行中，无需派遣")

    final = _travel_get(token, TRAVEL_STATUS_PATH) or status
    return {"available": True, "success": True, "log": log, "status": final}


def _run_travel(token, auto=True, location_id=None):
    """收集派猫猫旅行数据；auto 为真时执行全自动闭环（先领后派）。"""
    status = _travel_get(token, TRAVEL_STATUS_PATH)
    config = _travel_get(token, TRAVEL_CONFIG_PATH)
    auto_log = None
    if auto:
        res = travel_auto(token, location_id)
        auto_log = res.get("log") or []
        if res.get("status"):
            status = res["status"]
    parsed = parse_travel(status, config)
    if auto_log is not None:
        parsed["auto_log"] = auto_log
    return parsed


def _append_travel_msg(msg, travel):
    """把旅行状态拼进主消息，便于桌面通知与日志一眼看全。"""
    parts = []
    if travel.get("state_text"):
        parts.append(travel["state_text"])
    if travel.get("location_name"):
        parts.append(travel["location_name"])
    if travel.get("state") == "traveling" and travel.get("remaining_text"):
        parts.append("还需 %s" % travel["remaining_text"])
    if travel.get("state") == "arrived" and travel.get("reward_credit") is not None:
        parts.append("可领 %s 积分" % travel["reward_credit"])
    auto_log = travel.get("auto_log") or []
    if auto_log:
        parts.append("；".join(str(x) for x in auto_log))
    if not parts:
        return msg
    tail = "派猫猫旅行：" + "，".join(parts)
    return (msg + " ｜ " + tail) if msg else tail


# ---------------- 环境自检与配置模板 ----------------

