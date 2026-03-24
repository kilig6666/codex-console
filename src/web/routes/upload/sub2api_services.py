"""
Sub2API 服务管理 API 路由
"""

from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ....database import crud
from ....database.session import get_db
from ....core.upload.sub2api_upload import (
    batch_upload_to_sub2api,
    fetch_remote_sub2api_proxies,
    test_sub2api_connection,
)

router = APIRouter()


# ============== Pydantic Models ==============

class Sub2ApiServiceCreate(BaseModel):
    name: str
    api_url: str
    api_key: str
    enabled: bool = True
    priority: int = 0


class Sub2ApiServiceUpdate(BaseModel):
    name: Optional[str] = None
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


class Sub2ApiServiceResponse(BaseModel):
    id: int
    name: str
    api_url: str
    has_key: bool
    enabled: bool
    priority: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class Sub2ApiTestRequest(BaseModel):
    api_url: Optional[str] = None
    api_key: Optional[str] = None


class Sub2ApiUploadRequest(BaseModel):
    account_ids: List[int]
    service_id: Optional[int] = None
    concurrency: int = 3
    priority: int = 50
    proxy_id: Optional[int] = None


class Sub2ApiRemoteProxyResponse(BaseModel):
    id: int
    name: str
    protocol: str
    host: str
    port: int
    username: str = ""
    status: str


class Sub2ApiRemoteProxyListResponse(BaseModel):
    service: Sub2ApiServiceResponse
    proxies: List[Sub2ApiRemoteProxyResponse]


def _to_response(svc) -> Sub2ApiServiceResponse:
    return Sub2ApiServiceResponse(
        id=svc.id,
        name=svc.name,
        api_url=svc.api_url,
        has_key=bool(svc.api_key),
        enabled=svc.enabled,
        priority=svc.priority,
        created_at=svc.created_at.isoformat() if svc.created_at else None,
        updated_at=svc.updated_at.isoformat() if svc.updated_at else None,
    )


def _resolve_sub2api_service(service_id: Optional[int] = None):
    with get_db() as db:
        if service_id:
            svc = crud.get_sub2api_service_by_id(db, service_id)
        else:
            services = crud.get_sub2api_services(db, enabled=True)
            svc = services[0] if services else None

    if not svc:
        raise HTTPException(status_code=400, detail="未找到可用的 Sub2API 服务")
    return svc


def _to_remote_proxy_response(proxy: dict) -> Sub2ApiRemoteProxyResponse:
    proxy_id = proxy.get("id")
    if proxy_id is None:
        raise ValueError("远端 Sub2API 代理缺少 id")

    return Sub2ApiRemoteProxyResponse(
        id=int(proxy_id),
        name=str(proxy.get("name") or "").strip() or f"Proxy {proxy_id}",
        protocol=str(proxy.get("protocol") or "").strip(),
        host=str(proxy.get("host") or "").strip(),
        port=int(proxy.get("port") or 0),
        username=str(proxy.get("username") or "").strip(),
        status=str(proxy.get("status") or "inactive").strip() or "inactive",
    )


# ============== API Endpoints ==============

@router.get("", response_model=List[Sub2ApiServiceResponse])
async def list_sub2api_services(enabled: Optional[bool] = None):
    """获取 Sub2API 服务列表"""
    with get_db() as db:
        services = crud.get_sub2api_services(db, enabled=enabled)
        return [_to_response(s) for s in services]


@router.post("", response_model=Sub2ApiServiceResponse)
async def create_sub2api_service(request: Sub2ApiServiceCreate):
    """新增 Sub2API 服务"""
    with get_db() as db:
        svc = crud.create_sub2api_service(
            db,
            name=request.name,
            api_url=request.api_url,
            api_key=request.api_key,
            enabled=request.enabled,
            priority=request.priority,
        )
        return _to_response(svc)


@router.get("/remote-proxies", response_model=Sub2ApiRemoteProxyListResponse)
async def list_remote_sub2api_proxies(service_id: Optional[int] = None):
    """拉取目标 Sub2API 服务中的远端代理列表"""
    svc = _resolve_sub2api_service(service_id)

    try:
        proxies = fetch_remote_sub2api_proxies(svc.api_url, svc.api_key)
        proxy_items = [_to_remote_proxy_response(proxy) for proxy in proxies]
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return Sub2ApiRemoteProxyListResponse(
        service=_to_response(svc),
        proxies=proxy_items,
    )


@router.get("/{service_id}", response_model=Sub2ApiServiceResponse)
async def get_sub2api_service(service_id: int):
    """获取单个 Sub2API 服务详情"""
    with get_db() as db:
        svc = crud.get_sub2api_service_by_id(db, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Sub2API 服务不存在")
        return _to_response(svc)


@router.get("/{service_id}/full")
async def get_sub2api_service_full(service_id: int):
    """获取 Sub2API 服务完整配置（含 API Key）"""
    with get_db() as db:
        svc = crud.get_sub2api_service_by_id(db, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Sub2API 服务不存在")
        return {
            "id": svc.id,
            "name": svc.name,
            "api_url": svc.api_url,
            "api_key": svc.api_key,
            "enabled": svc.enabled,
            "priority": svc.priority,
        }


@router.patch("/{service_id}", response_model=Sub2ApiServiceResponse)
async def update_sub2api_service(service_id: int, request: Sub2ApiServiceUpdate):
    """更新 Sub2API 服务配置"""
    with get_db() as db:
        svc = crud.get_sub2api_service_by_id(db, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Sub2API 服务不存在")

        update_data = {}
        if request.name is not None:
            update_data["name"] = request.name
        if request.api_url is not None:
            update_data["api_url"] = request.api_url
        # api_key 留空则保持原值
        if request.api_key:
            update_data["api_key"] = request.api_key
        if request.enabled is not None:
            update_data["enabled"] = request.enabled
        if request.priority is not None:
            update_data["priority"] = request.priority

        svc = crud.update_sub2api_service(db, service_id, **update_data)
        return _to_response(svc)


@router.delete("/{service_id}")
async def delete_sub2api_service(service_id: int):
    """删除 Sub2API 服务"""
    with get_db() as db:
        svc = crud.get_sub2api_service_by_id(db, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Sub2API 服务不存在")
        crud.delete_sub2api_service(db, service_id)
        return {"success": True, "message": f"Sub2API 服务 {svc.name} 已删除"}


@router.post("/{service_id}/test")
async def test_sub2api_service(service_id: int):
    """测试 Sub2API 服务连接"""
    with get_db() as db:
        svc = crud.get_sub2api_service_by_id(db, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Sub2API 服务不存在")
        success, message = test_sub2api_connection(svc.api_url, svc.api_key)
        return {"success": success, "message": message}


@router.post("/test-connection")
async def test_sub2api_connection_direct(request: Sub2ApiTestRequest):
    """直接测试 Sub2API 连接（用于添加前验证）"""
    if not request.api_url or not request.api_key:
        raise HTTPException(status_code=400, detail="api_url 和 api_key 不能为空")
    success, message = test_sub2api_connection(request.api_url, request.api_key)
    return {"success": success, "message": message}


@router.post("/upload")
async def upload_accounts_to_sub2api(request: Sub2ApiUploadRequest):
    """批量上传账号到 Sub2API 平台"""
    if not request.account_ids:
        raise HTTPException(status_code=400, detail="账号 ID 列表不能为空")

    with get_db() as db:
        if request.service_id:
            svc = crud.get_sub2api_service_by_id(db, request.service_id)
        else:
            svcs = crud.get_sub2api_services(db, enabled=True)
            svc = svcs[0] if svcs else None

        if not svc:
            raise HTTPException(status_code=400, detail="未找到可用的 Sub2API 服务")

        api_url = svc.api_url
        api_key = svc.api_key

    results = batch_upload_to_sub2api(
        request.account_ids,
        api_url,
        api_key,
        concurrency=request.concurrency,
        priority=request.priority,
        proxy_id=request.proxy_id,
    )
    return results
