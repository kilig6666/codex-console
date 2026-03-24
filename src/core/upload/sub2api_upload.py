"""
Sub2API 账号上传功能
将账号以 sub2api-data 格式批量导入到 Sub2API 平台
"""

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from curl_cffi import requests as cffi_requests

from ...database.models import Account
from ...database.session import get_db

logger = logging.getLogger(__name__)


def _extract_sub2api_data(response) -> Any:
    try:
        payload = response.json()
    except Exception:
        payload = None

    if response.status_code >= 400:
        if isinstance(payload, dict) and payload.get("message"):
            raise ValueError(str(payload["message"]))
        response_text = (response.text or "").strip()
        raise ValueError(
            f"HTTP {response.status_code}" + (f" - {response_text[:200]}" if response_text else "")
        )

    if isinstance(payload, dict):
        if payload.get("code") not in (None, 0):
            raise ValueError(str(payload.get("message") or "Sub2API 返回错误"))
        return payload.get("data", payload)

    if payload is None:
        raise ValueError("Sub2API 返回了无法解析的响应")

    return payload


def _normalize_remote_sub2api_proxy(proxy: dict) -> dict:
    protocol = str(proxy.get("protocol") or "").strip()
    host = str(proxy.get("host") or "").strip()
    try:
        port = int(proxy.get("port") or 0)
    except (TypeError, ValueError):
        port = 0

    normalized = {
        "id": proxy.get("id"),
        "name": str(proxy.get("name") or "").strip(),
        "protocol": protocol,
        "host": host,
        "port": port,
        "username": str(proxy.get("username") or "").strip(),
        "password": str(proxy.get("password") or "").strip(),
        "status": str(proxy.get("status") or "inactive").strip() or "inactive",
    }
    if not normalized["name"]:
        if normalized["id"] is not None:
            normalized["name"] = f"Proxy {normalized['id']}"
        else:
            normalized["name"] = f"{protocol}://{host}:{port}"
    return normalized


def _build_sub2api_proxy_key(proxy: dict) -> str:
    normalized = _normalize_remote_sub2api_proxy(proxy)
    return (
        f"{normalized['protocol']}|{normalized['host']}|{normalized['port']}|"
        f"{normalized['username']}|{normalized['password']}"
    )


def _build_sub2api_proxy_payload(proxy: dict) -> dict:
    normalized = _normalize_remote_sub2api_proxy(proxy)
    if not normalized["protocol"] or not normalized["host"] or normalized["port"] <= 0:
        raise ValueError(f"远端 Sub2API 代理 {normalized['id']} 配置不完整，无法生成上传数据")

    return {
        "proxy_key": _build_sub2api_proxy_key(normalized),
        "name": normalized["name"],
        "protocol": normalized["protocol"],
        "host": normalized["host"],
        "port": normalized["port"],
        "username": normalized["username"],
        "password": normalized["password"],
        "status": normalized["status"],
    }


def fetch_remote_sub2api_proxies(api_url: str, api_key: str) -> List[dict]:
    if not api_url:
        raise ValueError("Sub2API URL 未配置")
    if not api_key:
        raise ValueError("Sub2API API Key 未配置")

    url = api_url.rstrip("/") + "/api/v1/admin/proxies/all"

    try:
        response = cffi_requests.get(
            url,
            headers={"x-api-key": api_key},
            proxies=None,
            timeout=15,
            impersonate="chrome110",
        )
        data = _extract_sub2api_data(response)
        if not isinstance(data, list):
            raise ValueError("远端 Sub2API 代理列表格式异常")
        return [_normalize_remote_sub2api_proxy(item) for item in data if isinstance(item, dict)]
    except ValueError:
        raise
    except cffi_requests.exceptions.ConnectionError as e:
        raise ValueError(f"无法连接到远端 Sub2API 服务: {e}") from e
    except cffi_requests.exceptions.Timeout as e:
        raise ValueError("拉取远端 Sub2API 代理列表超时") from e
    except Exception as e:
        logger.error(f"拉取远端 Sub2API 代理列表异常: {e}")
        raise ValueError(f"拉取远端 Sub2API 代理列表失败: {e}") from e


def fetch_remote_sub2api_proxy(api_url: str, api_key: str, proxy_id: int) -> dict:
    if proxy_id is None:
        raise ValueError("远端 Sub2API 代理 ID 不能为空")
    if not api_url:
        raise ValueError("Sub2API URL 未配置")
    if not api_key:
        raise ValueError("Sub2API API Key 未配置")

    url = api_url.rstrip("/") + f"/api/v1/admin/proxies/{proxy_id}"

    try:
        response = cffi_requests.get(
            url,
            headers={"x-api-key": api_key},
            proxies=None,
            timeout=15,
            impersonate="chrome110",
        )
        data = _extract_sub2api_data(response)
        if not isinstance(data, dict):
            raise ValueError("远端 Sub2API 代理详情格式异常")
        return _normalize_remote_sub2api_proxy(data)
    except ValueError:
        raise
    except cffi_requests.exceptions.ConnectionError as e:
        raise ValueError(f"无法连接到远端 Sub2API 服务: {e}") from e
    except cffi_requests.exceptions.Timeout as e:
        raise ValueError("拉取远端 Sub2API 代理详情超时") from e
    except Exception as e:
        logger.error(f"拉取远端 Sub2API 代理详情异常: {e}")
        raise ValueError(f"拉取远端 Sub2API 代理详情失败: {e}") from e


def upload_to_sub2api(
    accounts: List[Account],
    api_url: str,
    api_key: str,
    concurrency: int = 3,
    priority: int = 50,
    proxy_id: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    上传账号列表到 Sub2API 平台（不走代理）

    Args:
        accounts: 账号模型实例列表
        api_url: Sub2API 地址，如 http://host
        api_key: Admin API Key（x-api-key header）
        concurrency: 账号并发数，默认 3
        priority: 账号优先级，默认 50
        proxy_id: 远端 Sub2API 代理 ID，用于生成 Sub2API 识别的 proxy_key（可选）

    Returns:
        (成功标志, 消息)
    """
    if not accounts:
        return False, "无可上传的账号"

    if not api_url:
        return False, "Sub2API URL 未配置"

    if not api_key:
        return False, "Sub2API API Key 未配置"

    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    proxy_payload = None
    if proxy_id is not None:
        try:
            remote_proxy = fetch_remote_sub2api_proxy(api_url, api_key, proxy_id)
            proxy_payload = _build_sub2api_proxy_payload(remote_proxy)
        except ValueError as e:
            return False, str(e)

    account_items = []
    for acc in accounts:
        if not acc.access_token:
            continue
        expires_at = int(acc.expires_at.timestamp()) if acc.expires_at else 0
        account_item = {
            "name": acc.email,
            "platform": "openai",
            "type": "oauth",
            "credentials": {
                "access_token": acc.access_token,
                "chatgpt_account_id": acc.account_id or "",
                "chatgpt_user_id": "",
                "client_id": acc.client_id or "",
                "expires_at": expires_at,
                "expires_in": 863999,
                "model_mapping": {
                    "gpt-5.1": "gpt-5.1",
                    "gpt-5.1-codex": "gpt-5.1-codex",
                    "gpt-5.1-codex-max": "gpt-5.1-codex-max",
                    "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
                    "gpt-5.2": "gpt-5.2",
                    "gpt-5.2-codex": "gpt-5.2-codex",
                    "gpt-5.3": "gpt-5.3",
                    "gpt-5.3-codex": "gpt-5.3-codex",
                    "gpt-5.4": "gpt-5.4"
                },
                "organization_id": acc.workspace_id or "",
                "refresh_token": acc.refresh_token or "",
            },
            "extra": {},
            "concurrency": concurrency,
            "priority": priority,
            "rate_multiplier": 1,
            "auto_pause_on_expired": True,
        }
        if proxy_payload is not None:
            account_item["proxy_key"] = proxy_payload["proxy_key"]
        account_items.append(account_item)

    if not account_items:
        return False, "所有账号均缺少 access_token，无法上传"

    payload = {
        "data": {
            "type": "sub2api-data",
            "version": 1,
            "exported_at": exported_at,
            "proxies": [proxy_payload] if proxy_payload is not None else [],
            "accounts": account_items,
        },
        "skip_default_group_bind": True,
    }

    url = api_url.rstrip("/") + "/api/v1/admin/accounts/data"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "Idempotency-Key": f"import-{exported_at}",
    }

    try:
        response = cffi_requests.post(
            url,
            json=payload,
            headers=headers,
            proxies=None,
            timeout=30,
            impersonate="chrome110",
        )

        if response.status_code in (200, 201):
            return True, f"成功上传 {len(account_items)} 个账号"

        error_msg = f"上传失败: HTTP {response.status_code}"
        try:
            detail = response.json()
            if isinstance(detail, dict):
                error_msg = detail.get("message", error_msg)
        except Exception:
            error_msg = f"{error_msg} - {response.text[:200]}"
        return False, error_msg

    except Exception as e:
        logger.error(f"Sub2API 上传异常: {e}")
        return False, f"上传异常: {str(e)}"


def batch_upload_to_sub2api(
    account_ids: List[int],
    api_url: str,
    api_key: str,
    concurrency: int = 3,
    priority: int = 50,
    proxy_id: Optional[int] = None,
) -> dict:
    """
    批量上传指定 ID 的账号到 Sub2API 平台

    Returns:
        包含成功/失败/跳过统计和详情的字典
    """
    results = {
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "details": []
    }

    with get_db() as db:
        accounts = []
        for account_id in account_ids:
            acc = db.query(Account).filter(Account.id == account_id).first()
            if not acc:
                results["failed_count"] += 1
                results["details"].append({"id": account_id, "email": None, "success": False, "error": "账号不存在"})
                continue
            if not acc.access_token:
                results["skipped_count"] += 1
                results["details"].append({"id": account_id, "email": acc.email, "success": False, "error": "缺少 access_token"})
                continue
            accounts.append(acc)

        if not accounts:
            return results

        success, message = upload_to_sub2api(
            accounts,
            api_url,
            api_key,
            concurrency,
            priority,
            proxy_id=proxy_id,
        )

        if success:
            for acc in accounts:
                results["success_count"] += 1
                results["details"].append({"id": acc.id, "email": acc.email, "success": True, "message": message})
        else:
            for acc in accounts:
                results["failed_count"] += 1
                results["details"].append({"id": acc.id, "email": acc.email, "success": False, "error": message})

    return results


def test_sub2api_connection(api_url: str, api_key: str) -> Tuple[bool, str]:
    """
    测试 Sub2API 连接（GET /api/v1/admin/accounts/data 探活）

    Returns:
        (成功标志, 消息)
    """
    if not api_url:
        return False, "API URL 不能为空"
    if not api_key:
        return False, "API Key 不能为空"

    url = api_url.rstrip("/") + "/api/v1/admin/accounts/data"
    headers = {"x-api-key": api_key}

    try:
        response = cffi_requests.get(
            url,
            headers=headers,
            proxies=None,
            timeout=10,
            impersonate="chrome110",
        )

        if response.status_code in (200, 201, 204, 405):
            return True, "Sub2API 连接测试成功"
        if response.status_code == 401:
            return False, "连接成功，但 API Key 无效"
        if response.status_code == 403:
            return False, "连接成功，但权限不足"

        return False, f"服务器返回异常状态码: {response.status_code}"

    except cffi_requests.exceptions.ConnectionError as e:
        return False, f"无法连接到服务器: {str(e)}"
    except cffi_requests.exceptions.Timeout:
        return False, "连接超时，请检查网络配置"
    except Exception as e:
        return False, f"连接测试失败: {str(e)}"
