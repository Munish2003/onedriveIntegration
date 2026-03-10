"""
Microsoft OAuth Backend — FastAPI
Sign in with Microsoft using MSAL (Authorization Code Flow).
Client logs in, picks a folder, and access is restricted to that folder using cross-drive paths.
"""

import os
import uuid
import datetime
import msal
from pydantic import BaseModel
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import URLSafeSerializer
import aiohttp

load_dotenv()

# ── Config ──────────────────────────────────────────────
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
TENANT_ID = os.getenv("TENANT_ID", "common")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:8000/auth/callback")
SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key")
PUBLIC_URL = os.getenv("PUBLIC_URL")

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES = ["User.Read"]  # Microsoft Graph basic profile

ONEDRIVE_SCOPES = ["Files.Read", "User.Read"]  # Read-only access to client files
ONEDRIVE_REDIRECT_URI = os.getenv("ONEDRIVE_REDIRECT_URI", "http://localhost:8000/onedrive/callback")

# ── App ─────────────────────────────────────────────────
app = FastAPI(title="SalezX OneDrive Integration")
app.mount("/static", StaticFiles(directory="static"), name="static")

sessions: dict[str, dict] = {}
serializer = URLSafeSerializer(SECRET_KEY)
auth_flows: dict[str, dict] = {}


# ── Helpers ─────────────────────────────────────────────
def _build_msal_app():
    return msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
    )

def _get_session_id(request: Request) -> str | None:
    token = request.cookies.get("session")
    if not token:
        return None
    try:
        return serializer.loads(token)
    except Exception:
        return None

def _get_session_user(request: Request) -> dict | None:
    session_id = _get_session_id(request)
    if not session_id:
        return None
    return sessions.get(session_id)

async def _get_valid_access_token(user: dict, session_id: str) -> str | None:
    """Returns a valid access token, refreshing it if necessary."""
    client = _build_msal_app()
    
    # Try using the refresh token first if we have one
    refresh_token = user.get("onedrive_refresh_token")
    if refresh_token:
        result = client.acquire_token_by_refresh_token(refresh_token, scopes=ONEDRIVE_SCOPES)
        if "access_token" in result:
            # Update refresh token if a new one was issued
            if "refresh_token" in result:
                user["onedrive_refresh_token"] = result["refresh_token"]
                sessions[session_id] = user
            return result["access_token"]

    # Fallback to the stored access token (might be expired, but we try)
    return user.get("onedrive_access_token")


# ── Client Auth Routes ──────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = _get_session_user(request)
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    with open("static/login.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/auth/login")
async def auth_login():
    client = _build_msal_app()
    flow = client.initiate_auth_code_flow(
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    state = flow["state"]
    auth_flows[state] = flow
    return RedirectResponse(url=flow["auth_uri"])

@app.get("/auth/callback")
async def auth_callback(request: Request):
    params = dict(request.query_params)
    state = params.get("state", "")

    flow = auth_flows.pop(state, None)
    if not flow:
        return HTMLResponse("<h2>❌ Session expired. <a href='/'>Try again</a></h2>", status_code=400)

    client = _build_msal_app()
    result = client.acquire_token_by_auth_code_flow(flow, params)

    if "error" in result:
        return HTMLResponse(f"<h2>❌ Login failed: {result.get('error_description')}</h2>", status_code=400)

    id_token_claims = result.get("id_token_claims", {})
    user_info = {
        "ms_id": id_token_claims.get("oid", id_token_claims.get("sub", "")),
        "email": id_token_claims.get("preferred_username", ""),
        "name": id_token_claims.get("name", ""),
        "tenant_id": id_token_claims.get("tid", ""),
        "access_token": result.get("access_token", ""),
        "onedrive_connected": False,
    }

    session_id = str(uuid.uuid4())
    sessions[session_id] = user_info
    signed_token = serializer.dumps(session_id)

    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(key="session", value=signed_token, httponly=True, max_age=86400 * 7, samesite="lax")
    return response

@app.get("/auth/logout")
async def auth_logout(request: Request):
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("session")
    return response

@app.get("/dashboard")
async def dashboard(request: Request):
    user = _get_session_user(request)
    if not user:
        return RedirectResponse(url="/", status_code=302)
    with open("static/dashboard.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/api/me")
async def api_me(request: Request):
    user = _get_session_user(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    return {
        "ms_id": user["ms_id"],
        "email": user["email"],
        "name": user["name"],
        "tenant_id": user["tenant_id"],
        "onedrive_connected": user["onedrive_connected"],
        "connected_folders": user.get("onedrive_connected_folders", []),
    }


# ── OneDrive Integration ────────────────────────────────
@app.post("/integrations/microsoft/connect")
async def integrations_microsoft_connect(request: Request):
    """
    Public API — returns a connect_url for the client.
    """
    client = _build_msal_app()
    flow = client.initiate_auth_code_flow(
        scopes=ONEDRIVE_SCOPES,
        redirect_uri=ONEDRIVE_REDIRECT_URI,
    )
    state = flow["state"]
    auth_flows[state] = flow
    return {"connect_url": flow["auth_uri"]}

@app.get("/onedrive/connect")
async def onedrive_connect(request: Request):
    client = _build_msal_app()
    flow = client.initiate_auth_code_flow(
        scopes=ONEDRIVE_SCOPES,
        redirect_uri=ONEDRIVE_REDIRECT_URI,
    )
    state = flow["state"]
    auth_flows[state] = flow
    return RedirectResponse(url=flow["auth_uri"])

@app.get("/onedrive/callback")
async def onedrive_callback(request: Request):
    user = _get_session_user(request)
    session_id = _get_session_id(request)
    if not user or not session_id:
        return HTMLResponse("<h2>Unauthorized. Please log in first.</h2>", status_code=401)

    params = dict(request.query_params)
    state = params.get("state", "")
    flow = auth_flows.pop(state, None)
    if not flow:
        return HTMLResponse("<h2>Invalid state or timeout.</h2>", status_code=400)

    client = _build_msal_app()
    result = client.acquire_token_by_auth_code_flow(flow, params)

    if "error" in result:
        return HTMLResponse(f"<h2>OneDrive Connect Failed: {result.get('error_description')}</h2>", status_code=400)

    user["onedrive_connected"] = True
    user["onedrive_access_token"] = result.get("access_token")
    user["onedrive_refresh_token"] = result.get("refresh_token")
    sessions[session_id] = user

    return RedirectResponse(url="/dashboard", status_code=302)

@app.get("/api/onedrive/shared-folders")
async def shared_folders(request: Request):
    user = _get_session_user(request)
    session_id = _get_session_id(request)
    if not user or not user.get("onedrive_connected"):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    access_token = await _get_valid_access_token(user, session_id)
    if not access_token:
        return JSONResponse({"error": "Failed to get access token"}, status_code=401)

    url = "https://graph.microsoft.com/v1.0/me/drive/sharedWithMe"
    headers = {"Authorization": f"Bearer {access_token}"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                err = await resp.text()
                return JSONResponse({"error": err}, status_code=resp.status)
            
            data = await resp.json()
            folders = []
            for item in data.get("value", []):
                if "folder" in item:
                    folders.append({
                        "id": item["id"],
                        "name": item.get("name"),
                        "drive_id": item["remoteItem"]["parentReference"]["driveId"] if "remoteItem" in item else item.get("parentReference", {}).get("driveId"),
                        "item_id": item["remoteItem"]["id"] if "remoteItem" in item else item["id"],
                        "child_count": item["folder"].get("childCount", 0),
                        "shared_by": item.get("createdBy", {}).get("user", {}).get("displayName", "Unknown")
                    })
            
            return {"folders": folders}

@app.get("/api/onedrive/folders")
async def onedrive_folders(request: Request, parent_id: str = None):
    user = _get_session_user(request)
    session_id = _get_session_id(request)
    if not user or not user.get("onedrive_connected"):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    access_token = await _get_valid_access_token(user, session_id)
    if not access_token:
        return JSONResponse({"error": "Failed to get token"}, status_code=401)

    url = "https://graph.microsoft.com/v1.0/me/drive/root/children"
    if parent_id:
        url = f"https://graph.microsoft.com/v1.0/me/drive/items/{parent_id}/children"

    headers = {"Authorization": f"Bearer {access_token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return JSONResponse({"error": await resp.text()}, status_code=resp.status)
            
            data = await resp.json()
            folders = [f for f in data.get("value", []) if "folder" in f]
            
            # For "My Folders", we just return their own driveId
            my_drive_id = ""
            if folders:
                my_drive_id = folders[0].get("parentReference", {}).get("driveId", "")
            
            res_folders = []
            for f in folders:
                res_folders.append({
                    "id": f["id"],
                    "name": f.get("name"),
                    "child_count": f["folder"].get("childCount", 0),
                    "drive_id": my_drive_id,
                    "item_id": f["id"]
                })
            
            return {"folders": res_folders}

class ConnectFolderRequest(BaseModel):
    folder_id: str = None  # Legacy
    drive_id: str = None
    item_id: str = None
    name: str = "Connected Folder"

@app.post("/api/onedrive/connect-folder")
async def connect_folder(req: ConnectFolderRequest, request: Request):
    user = _get_session_user(request)
    session_id = _get_session_id(request)
    if not user or not session_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # If legacy `folder_id` is passed, we need to find its drive_id
    drive_id = req.drive_id
    item_id = req.item_id
    
    # Get auth token unconditionally as we need it for Graph queries
    access_token = await _get_valid_access_token(user, session_id)
    if not access_token:
        return JSONResponse({"error": "No valid token"}, status_code=401)

    if req.folder_id and not drive_id:
        # User selected from "My Folders" but we only got folder_id
        # Let's get the drive details
        url = f"https://graph.microsoft.com/v1.0/me/drive/items/{req.folder_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    drive_id = data.get("parentReference", {}).get("driveId")
                    item_id = req.folder_id
                else:
                    return JSONResponse({"error": "Invalid folder ID"}, status_code=400)
    
    if not drive_id or not item_id:
        return JSONResponse({"error": "Missing drive_id or item_id"}, status_code=400)

    # Set up Webhook Subscription
    delta_link = None
    subscription_id = None
    subscription_expiration_date = None

    if PUBLIC_URL:
        # 1. Get initial delta token
        delta_url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/delta"
        headers = {"Authorization": f"Bearer {access_token}"}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(delta_url, headers=headers) as resp:
                if resp.status == 200:
                    delta_data = await resp.json()
                    delta_link = delta_data.get("@odata.deltaLink")
                    
        # 2. Clean up orphaned subscriptions and check for existing drive subscription
        sub_url = "https://graph.microsoft.com/v1.0/subscriptions"
        our_webhook_url = f"{PUBLIC_URL}/api/onedrive/webhook"
        
        connected_folders = user.get("onedrive_connected_folders", [])
        valid_sub_ids = [f.get("subscription_id") for f in connected_folders if f.get("subscription_id")]
        
        async with aiohttp.ClientSession() as session:
            async with session.get(sub_url, headers=headers) as resp:
                if resp.status == 200:
                    subs_data = await resp.json()
                    for sub in subs_data.get("value", []):
                        if sub.get("notificationUrl") == our_webhook_url:
                            if sub["id"] not in valid_sub_ids:
                                del_url = f"{sub_url}/{sub['id']}"
                                async with session.delete(del_url, headers=headers) as del_resp:
                                    if del_resp.status == 204:
                                        print(f"🧹 Deleted orphaned subscription: {sub['id']}")
                                        
        existing_sub = next((f for f in connected_folders if f.get("drive_id") == drive_id and f.get("subscription_id")), None)
        
        if existing_sub:
            subscription_id = existing_sub["subscription_id"]
            subscription_expiration_date = existing_sub["subscription_expiration"]
            print(f"🔄 Reusing existing webhook subscription: {subscription_id}")
        else:
            # 3. Subscribe to changes (webhook fires for whole drive, but Delta API will filter to the folder)
            now = datetime.datetime.now(datetime.UTC)
            expiration = now + datetime.timedelta(days=29)  # Max 29 days for DriveItem
            
            sub_payload = {
               "changeType": "updated",
               "notificationUrl": our_webhook_url,
               "resource": f"/drives/{drive_id}/root",
               "expirationDateTime": expiration.isoformat().replace("+00:00", "Z"),
               "clientState": session_id,
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(sub_url, headers=headers, json=sub_payload) as resp:
                    if resp.status == 201:
                        sub_data = await resp.json()
                        subscription_id = sub_data.get("id")
                        subscription_expiration_date = sub_data.get("expirationDateTime")
                        print(f"✅ Webhook subscription created: {subscription_id}")
                    else:
                        print(f"⚠️ Failed to create webhook subscription: {await resp.text()}")

    # Save to user session (append to list)
    connected_folders = user.get("onedrive_connected_folders", [])
    
    # Check if this exact folder is already connected to avoid duplicates
    existing = next((f for f in connected_folders if f["item_id"] == item_id), None)
    if not existing:
        connected_folders.append({
            "drive_id": drive_id,
            "item_id": item_id,
            "folder_name": req.name,
            "delta_link": delta_link,
            "subscription_id": subscription_id,
            "subscription_expiration": subscription_expiration_date
        })
        user["onedrive_connected_folders"] = connected_folders
        print(f"➕ [ADD FOLDER] Added '{req.name}' to tracking under subscription: {subscription_id}")
        print(f"📊 [TRACKING] Currently tracking {len(connected_folders)} folder(s) for this session.")
        sessions[session_id] = user
    
    signed_token = serializer.dumps(session_id)
    response = JSONResponse({"status": "linked", "folder_name": req.name})
    response.set_cookie(key="session", value=signed_token, httponly=True, max_age=86400 * 7, samesite="lax")
    return response

class DisconnectFolderRequest(BaseModel):
    folder_id: str

@app.post("/api/onedrive/disconnect")
async def disconnect_folder(req: DisconnectFolderRequest, request: Request):
    user = _get_session_user(request)
    session_id = _get_session_id(request)
    if not user or not session_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    connected_folders = user.get("onedrive_connected_folders", [])
    
    # Find the specific folder to disconnect
    target_folder = next((f for f in connected_folders if f["item_id"] == req.folder_id), None)
    
    if target_folder:
        subscription_id = target_folder.get("subscription_id")
        
        # Check if any OTHER folder is currently using this subscription
        other_users = [f for f in connected_folders if f.get("subscription_id") == subscription_id and f.get("item_id") != req.folder_id]
        
        if subscription_id and len(other_users) == 0:
            # No other folder needs this subscription, safe to delete from Graph
            access_token = await _get_valid_access_token(user, session_id)
            if access_token:
                sub_url = f"https://graph.microsoft.com/v1.0/subscriptions/{subscription_id}"
                headers = {"Authorization": f"Bearer {access_token}"}
                async with aiohttp.ClientSession() as session:
                    async with session.delete(sub_url, headers=headers) as resp:
                        if resp.status == 204:
                            print(f"🧹 Deleted subscription: {subscription_id}")
                        else:
                            print(f"⚠️ Failed to delete subscription {subscription_id}: {resp.status}")
        elif len(other_users) > 0:
            print(f"🔄 Kept subscription {subscription_id} active for {len(other_users)} other folder(s)")
        
        folder_name = target_folder.get("folder_name", "Unknown")
        print(f"➖ [REMOVE FOLDER] Removed '{folder_name}' from tracking under subscription: {subscription_id}")
        
        # Remove folder from session list
        connected_folders = [f for f in connected_folders if f["item_id"] != req.folder_id]
        user["onedrive_connected_folders"] = connected_folders
        print(f"📊 [TRACKING] Remaining tracked folders for this session: {len(connected_folders)}")
        sessions[session_id] = user
    
    signed_token = serializer.dumps(session_id)
    response = JSONResponse({"status": "disconnected"})
    response.set_cookie(key="session", value=signed_token, httponly=True, max_age=86400 * 7, samesite="lax")
    return response

@app.get("/api/onedrive/files")
async def onedrive_files(request: Request, folder_id: str = None, recursive: bool = False):
    user = _get_session_user(request)
    session_id = _get_session_id(request)
    if not user:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not folder_id:
        return JSONResponse({"error": "Missing folder_id"}, status_code=400)

    connected_folders = user.get("onedrive_connected_folders", [])
    target_folder = next((f for f in connected_folders if f["item_id"] == folder_id), None)

    if not target_folder:
        return JSONResponse({"error": "Folder not connected"}, status_code=400)

    drive_id = target_folder["drive_id"]
    item_id = target_folder["item_id"]

    access_token = await _get_valid_access_token(user, session_id)
    if not access_token:
        return JSONResponse({"error": "Failed to get valid token"}, status_code=401)

    headers = {"Authorization": f"Bearer {access_token}"}

    if recursive:
        # Use Delta API to get ALL files recursively in one call
        url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/delta"
        all_items = []
        
        async with aiohttp.ClientSession() as session:
            while url:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        return JSONResponse({"error": f"Graph API Error: {err}"}, status_code=resp.status)
                    
                    data = await resp.json()
                    for item in data.get("value", []):
                        # Skip deleted items and the root folder itself
                        if item.get("deleted") or "@removed" in item:
                            continue
                        if item.get("id") == item_id:
                            continue
                        all_items.append(item)
                    
                    # Follow pagination
                    url = data.get("@odata.nextLink")
        
        return {"files": all_items, "recursive": True}
    else:
        # Original: direct children only
        url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}/children"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    return JSONResponse({"error": f"Graph API Error: {err}"}, status_code=resp.status)
                
                data = await resp.json()
                return {"files": data.get("value", [])}

@app.get("/api/onedrive/download/{file_id}")
async def onedrive_download(file_id: str, request: Request):
    user = _get_session_user(request)
    session_id = _get_session_id(request)
    if not user:
        return Response("Unauthorized", status_code=401)

    drive_id = user.get("onedrive_connected_drive_id")
    if not drive_id:
        return Response("No connected folder", status_code=400)

    access_token = await _get_valid_access_token(user, session_id)
    if not access_token:
        return Response("Failed to get valid token", status_code=401)

    url = f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{file_id}/content"
    headers = {"Authorization": f"Bearer {access_token}"}

    async def stream_file():
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    yield b"Error downloading file"
                    return
                async for chunk in resp.content.iter_chunked(65536):
                    yield chunk

    return StreamingResponse(
        stream_file(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename=\"{file_id}\""}
    )

@app.get("/api/onedrive/webhook")
async def verify_webhook(validationToken: str = ""):
    """Microsoft Graph sends a validationToken on subscription creation."""
    return Response(content=validationToken, media_type="text/plain", status_code=200)

@app.post("/api/onedrive/webhook")
async def handle_webhook(request: Request):
    """Handle incoming notifications from Microsoft Graph."""
    # ── Validation handshake ──
    # Graph sends validationToken as a query param on a POST request
    validation_token = request.query_params.get("validationToken")
    if validation_token:
        print(f"✅ Webhook validation request received, responding with token")
        return Response(content=validation_token, media_type="text/plain", status_code=200)

    # ── Process change notifications ──
    try:
        data = await request.json()
    except Exception:
        return Response(status_code=202)  # Acknowledge even if bad

    print(f"🔔 Webhook received: {len(data.get('value', []))} notifications")
    
    for notification in data.get("value", []):
        client_state = notification.get("clientState") # We set this to session_id
        resource = notification.get("resource", "unknown")
        sub_id = notification.get("subscriptionId")
        
        if not client_state or client_state not in sessions:
            print(f"⚠️  Skipping notification — session not found (server was restarted?). Resource: {resource}")
            print(f"    ℹ️  Please reconnect the folder from the dashboard to re-establish the session.")
            continue
            
        user = sessions[client_state]
        connected_folders = user.get("onedrive_connected_folders", [])
        
        # Find ALL connected folders that rely on this webhook subscription
        target_folders = [f for f in connected_folders if f.get("subscription_id") == sub_id]
        
        if not target_folders:
            print(f"⚠️  Skipping notification — no folders found connected to subscription {sub_id}")
            continue
            
        # Get a fresh token
        access_token = await _get_valid_access_token(user, client_state)
        headers = {"Authorization": f"Bearer {access_token}"}
        
        # Query the Delta API for EACH connected folder mapped to this drive's subscription
        async with aiohttp.ClientSession() as session:
            # deduplicate target folders by item_id in case there are buggy session duplicates
            unique_folders = {f["item_id"]: f for f in target_folders}.values()
            
            for target_folder in unique_folders:
                current_link = target_folder.get("delta_link")
                
                if not current_link:
                    print(f"⚠️  Skipping notification — no delta link stored for folder {target_folder.get('folder_name')}.")
                    continue
                
                while current_link:
                    async with session.get(current_link, headers=headers) as resp:
                        if resp.status != 200:
                            print(f"⚠️ Delta API returned {resp.status}")
                            break
                        delta_data = await resp.json()
                        
                        # Process exactly what changed
                        changes = delta_data.get("value", [])
                        for item in changes:
                            item_id = item.get("id", "unknown")
                            item_name = item.get("name")
                            display = f"[{target_folder.get('folder_name')}] {item_name} (ID: {item_id})" if item_name else f"[{target_folder.get('folder_name')}] ID: {item_id}"
                            
                            if item.get("deleted") or "@removed" in item:
                                print(f"🗑️  [DELETED]: {display}")
                            elif "file" in item:
                                size = item.get("size", 0)
                                last_modified = item.get("lastModifiedDateTime", "")
                                print(f"📝  [FILE ADDED/MODIFIED]: {display} | Size: {size} bytes | Modified: {last_modified}")
                            elif "folder" in item:
                                print(f"📁  [FOLDER CHANGED]: {display}")
                            else:
                                print(f"🔄  [CHANGED]: {display}")
                                
                        # Handle pagination
                        if "@odata.nextLink" in delta_data:
                            current_link = delta_data["@odata.nextLink"]
                        elif "@odata.deltaLink" in delta_data:
                            target_folder["delta_link"] = delta_data["@odata.deltaLink"]
                            sessions[client_state] = user
                            current_link = None  # Done!
                        else:
                            current_link = None
                        
    # Must immediately return 202 Accepted to Microsoft Graph
    return Response(status_code=202)


# ── Start server ────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    print("🚀 Starting server at http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
