import os, json, uuid, asyncio
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from redis.asyncio import Redis
from .config import settings
from .db import Base, engine, SessionLocal, get_db
from .models import User, Project, Module, WorkLog, Photo, Comment, Notification, AuditLog, UserProject, Role, WorkStatus
from .security import hash_password, verify_password, make_token, current_user, manager_only, foreman_or_manager
from .telegrambot import send_text, main_menu, keyboard, answer_callback

redis = Redis.from_url(settings.redis_url, decode_responses=True)
scheduler = AsyncIOScheduler(timezone=settings.timezone)
ALLOWED_UNITS = {"м", "шт", "м²", "комплект", "кг", "л"}
UPLOAD_DIR = Path(settings.upload_dir)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

class Hub:
    def __init__(self): self.connections = set()
    async def connect(self, ws):
        await ws.accept(); self.connections.add(ws)
    def disconnect(self, ws): self.connections.discard(ws)
    async def broadcast(self, payload):
        dead = []
        for ws in list(self.connections):
            try: await ws.send_json(payload)
            except Exception: dead.append(ws)
        for ws in dead: self.disconnect(ws)
hub = Hub()

async def publish(event):
    data = json.dumps(event, ensure_ascii=False, default=str)
    await redis.publish("stroykontrol:events", data)
    await hub.broadcast(event)

async def notify_managers(db, work, project, module, user):
    managers = (await db.execute(select(User).where(User.role == Role.manager, User.active == True))).scalars().all()
    text = f"🔔 Новая работа\n\n{project.name}\nМодуль {module.name if module else '—'}\n{user.full_name}\n{work.work_name}" + (f" — {work.quantity:g} {work.unit}" if work.quantity is not None else "")
    for manager in managers:
        db.add(Notification(user_id=manager.id, work_id=work.id, message=text))
        if manager.telegram_user_id:
            try: await send_text(manager.telegram_user_id, text)
            except Exception: pass
    await db.commit()

async def daily_report():
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date()
    start = datetime.combine(today, time.min, tzinfo=tz).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    async with SessionLocal() as db:
        rows = (await db.execute(select(WorkLog).where(WorkLog.created_at >= start, WorkLog.created_at < end).order_by(WorkLog.created_at))).scalars().all()
        managers = (await db.execute(select(User).where(User.role == Role.manager, User.active == True))).scalars().all()
        grouped = {}
        for w in rows:
            p = await db.get(Project, w.project_id)
            m = await db.get(Module, w.module_id) if w.module_id else None
            grouped.setdefault((m.name if m else "Без модуля", p.name), []).append(w)
        lines = ["Итоговый отчёт", today.strftime("%d.%m.%Y"), ""]
        for (mod, proj), works in grouped.items():
            lines += [f"Модуль {mod}", proj]
            for w in works:
                lines.append(f"* {w.work_name}" + (f" — {w.quantity:g} {w.unit}" if w.quantity is not None else ""))
            lines.append("")
        report = "\n".join(lines) if rows else f"Итоговый отчёт\n{today:%d.%m.%Y}\n\nРабот за день нет."
        for manager in managers:
            if manager.telegram_user_id:
                try: await send_text(manager.telegram_user_id, report)
                except Exception: pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as db:
        admin = (await db.execute(select(User).where(User.username == settings.bootstrap_admin_username))).scalar_one_or_none()
        if not admin and settings.bootstrap_admin_password:
            db.add(User(username=settings.bootstrap_admin_username, full_name="Руководитель", password_hash=hash_password(settings.bootstrap_admin_password), role=Role.manager))
            await db.commit()
    scheduler.add_job(daily_report, "cron", hour=settings.report_hour, minute=settings.report_minute, id="daily_report", replace_existing=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)
    await redis.aclose()
    await engine.dispose()

app = FastAPI(title="СтройКонтроль API", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="web"), name="static")

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return (Path("web/index.html")).read_text(encoding="utf-8")

class LoginIn(BaseModel):
    username: str
    password: str

@app.post("/api/login")
async def login(data: LoginIn, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.username == data.username))).scalar_one_or_none()
    if not user or not user.active or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "Неверный логин или пароль")
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    return {"access_token": make_token(user), "token_type": "bearer", "role": user.role.value, "name": user.full_name}

@app.get("/api/me")
async def me(user: User = Depends(current_user)):
    return {"id": user.id, "name": user.full_name, "role": user.role.value}

@app.get("/api/projects")
async def projects(db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    q = select(Project).where(Project.active == True)
    if user.role != Role.manager:
        ids = select(UserProject.project_id).where(UserProject.user_id == user.id)
        q = q.where(Project.id.in_(ids))
    items = (await db.execute(q.order_by(Project.name))).scalars().all()
    return [{"id": p.id, "name": p.name} for p in items]

@app.post("/api/projects")
async def add_project(name: str = Form(...), db: AsyncSession = Depends(get_db), user: User = Depends(manager_only)):
    p = Project(name=name.strip())
    db.add(p); await db.commit(); await db.refresh(p)
    return {"id": p.id, "name": p.name}

@app.post("/api/modules")
async def add_module(project_id: int = Form(...), name: str = Form(...), db: AsyncSession = Depends(get_db), user: User = Depends(manager_only)):
    if not await db.get(Project, project_id): raise HTTPException(404, "Объект не найден")
    m = Module(project_id=project_id, name=name.strip())
    db.add(m); await db.commit(); await db.refresh(m)
    return {"id": m.id, "name": m.name}

@app.post("/api/users")
async def add_user(username: str = Form(...), full_name: str = Form(...), role: Role = Form(...), password: str = Form(""), telegram_user_id: str = Form(""), db: AsyncSession = Depends(get_db), user: User = Depends(manager_only)):
    u = User(username=username, full_name=full_name, role=role, password_hash=hash_password(password) if password else None, telegram_user_id=telegram_user_id or None)
    db.add(u); await db.commit(); await db.refresh(u)
    return {"id": u.id, "username": u.username, "role": u.role.value}

@app.post("/api/assign")
async def assign_user(user_id: int = Form(...), project_id: int = Form(...), db: AsyncSession = Depends(get_db), actor: User = Depends(manager_only)):
    exists = (await db.execute(select(UserProject).where(UserProject.user_id == user_id, UserProject.project_id == project_id))).scalar_one_or_none()
    if not exists:
        db.add(UserProject(user_id=user_id, project_id=project_id)); await db.commit()
    return {"ok": True}

class WorkIn(BaseModel):
    project_id: int
    module_id: int | None = None
    work_name: str = Field(min_length=2, max_length=500)
    quantity: float | None = None
    unit: str | None = None
    status: WorkStatus = WorkStatus.completed
    comment: str | None = None

def work_dict(w):
    return {"id": w.id, "project_id": w.project_id, "project": w.project.name if w.project else None,
            "module_id": w.module_id, "module": w.module.name if w.module else None,
            "user_id": w.user_id, "foreman": w.user.full_name if w.user else None,
            "work_name": w.work_name, "quantity": w.quantity, "unit": w.unit,
            "status": w.status.value, "comment": w.comment,
            "created_at": w.created_at.isoformat(), "updated_at": w.updated_at.isoformat(),
            "photos": [{"id": p.id, "name": p.original_name, "url": f"/api/photos/{p.id}"} for p in w.photos]}

async def get_work(db, wid):
    return (await db.execute(select(WorkLog).where(WorkLog.id == wid))).scalar_one_or_none()

@app.post("/api/works")
async def create_work(data: WorkIn, db: AsyncSession = Depends(get_db), user: User = Depends(foreman_or_manager)):
    project = await db.get(Project, data.project_id)
    if not project: raise HTTPException(404, "Объект не найден")
    if user.role != Role.manager:
        allowed = (await db.execute(select(UserProject).where(UserProject.user_id == user.id, UserProject.project_id == data.project_id))).scalar_one_or_none()
        if not allowed: raise HTTPException(403, "Нет доступа к этому объекту")
    module = await db.get(Module, data.module_id) if data.module_id else None
    if data.module_id and (not module or module.project_id != project.id): raise HTTPException(400, "Модуль не относится к объекту")
    if data.unit and data.unit not in ALLOWED_UNITS: raise HTTPException(400, "Недопустимая единица измерения")
    w = WorkLog(project_id=project.id, module_id=data.module_id, user_id=user.id, work_name=data.work_name, quantity=data.quantity, unit=data.unit, status=data.status, comment=data.comment)
    db.add(w); await db.flush()
    db.add(AuditLog(work_id=w.id, actor_id=user.id, action="created", before_data=None, after_data={"work_name": w.work_name, "quantity": w.quantity, "unit": w.unit, "status": w.status.value}))
    await db.commit()
    w = await get_work(db, w.id)
    event = {"type": "work.created", "work": work_dict(w)}
    await publish(event)
    await notify_managers(db, w, project, module, user)
    return event["work"]

@app.get("/api/works")
async def list_works(project_id: int | None = None, module_id: int | None = None, foreman_id: int | None = None, status: WorkStatus | None = None, date_from: date | None = None, date_to: date | None = None, q: str | None = None, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    stmt = select(WorkLog).order_by(desc(WorkLog.created_at))
    if user.role == Role.worker:
        raise HTTPException(403, "Рабочему доступен только список назначенных задач; API задач подключается отдельно")
    if user.role == Role.foreman:
        allowed = select(UserProject.project_id).where(UserProject.user_id == user.id)
        stmt = stmt.where(WorkLog.project_id.in_(allowed), WorkLog.user_id == user.id)
    if project_id: stmt = stmt.where(WorkLog.project_id == project_id)
    if module_id: stmt = stmt.where(WorkLog.module_id == module_id)
    if foreman_id: stmt = stmt.where(WorkLog.user_id == foreman_id)
    if status: stmt = stmt.where(WorkLog.status == status)
    if date_from: stmt = stmt.where(WorkLog.created_at >= datetime.combine(date_from, time.min, tzinfo=timezone.utc))
    if date_to: stmt = stmt.where(WorkLog.created_at < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc))
    if q:
        stmt = stmt.join(Project).join(User).outerjoin(Module).where(
            WorkLog.work_name.ilike(f"%{q}%") | Project.name.ilike(f"%{q}%") |
            User.full_name.ilike(f"%{q}%") | Module.name.ilike(f"%{q}%"))
    rows = (await db.execute(stmt.limit(500))).scalars().unique().all()
    result = []
    for w in rows:
        await db.refresh(w, ["project", "module", "user", "photos"])
        result.append(work_dict(w))
    return result

class WorkPatch(BaseModel):
    work_name: str | None = None
    quantity: float | None = None
    unit: str | None = None
    status: WorkStatus | None = None
    comment: str | None = None

@app.patch("/api/works/{work_id}")
async def update_work(work_id: int, patch: WorkPatch, db: AsyncSession = Depends(get_db), user: User = Depends(foreman_or_manager)):
    w = await get_work(db, work_id)
    if not w: raise HTTPException(404, "Работа не найдена")
    if user.role != Role.manager and w.user_id != user.id: raise HTTPException(403, "Можно менять только свои записи")
    before = {"work_name": w.work_name, "quantity": w.quantity, "unit": w.unit, "status": w.status.value, "comment": w.comment}
    changes = patch.model_dump(exclude_unset=True)
    if changes.get("unit") and changes["unit"] not in ALLOWED_UNITS: raise HTTPException(400, "Недопустимая единица")
    for key, value in changes.items():
        if key == "status" and value: value = WorkStatus(value)
        setattr(w, key, value)
    after = {"work_name": w.work_name, "quantity": w.quantity, "unit": w.unit, "status": w.status.value, "comment": w.comment}
    db.add(AuditLog(work_id=w.id, actor_id=user.id, action="updated", before_data=before, after_data=after))
    await db.commit()
    w = await get_work(db, work_id)
    await publish({"type": "work.updated", "work": work_dict(w)})
    return work_dict(w)

@app.get("/api/works/{work_id}/history")
async def work_history(work_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    w = await get_work(db, work_id)
    if not w: raise HTTPException(404, "Работа не найдена")
    if user.role == Role.foreman and w.user_id != user.id: raise HTTPException(403, "Нет доступа")
    logs = (await db.execute(select(AuditLog).where(AuditLog.work_id == work_id).order_by(AuditLog.created_at))).scalars().all()
    return [{"actor_id": x.actor_id, "action": x.action, "before": x.before_data, "after": x.after_data, "created_at": x.created_at.isoformat()} for x in logs]

@app.post("/api/works/{work_id}/photos")
async def upload_photos(work_id: int, files: list[UploadFile] = File(...), db: AsyncSession = Depends(get_db), user: User = Depends(foreman_or_manager)):
    w = await get_work(db, work_id)
    if not w: raise HTTPException(404, "Работа не найдена")
    if user.role != Role.manager and w.user_id != user.id: raise HTTPException(403, "Нет доступа")
    created = []
    for f in files:
        if not f.content_type or not f.content_type.startswith("image/"): raise HTTPException(400, "Разрешены только изображения")
        content = await f.read()
        if len(content) > settings.max_upload_mb * 1024 * 1024: raise HTTPException(413, "Файл слишком большой")
        ext = Path(f.filename or "photo.jpg").suffix[:10] or ".jpg"
        key = f"{uuid.uuid4().hex}{ext}"
        (UPLOAD_DIR / key).write_bytes(content)
        photo = Photo(work_id=work_id, object_key=key, original_name=f.filename or key, content_type=f.content_type)
        db.add(photo); created.append(photo)
    await db.commit()
    await publish({"type": "work.photos_added", "work_id": work_id, "count": len(created)})
    return [{"id": p.id, "url": f"/api/photos/{p.id}"} for p in created]

@app.get("/api/photos/{photo_id}")
async def get_photo(photo_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(current_user)):
    p = await db.get(Photo, photo_id)
    if not p: raise HTTPException(404, "Фото не найдено")
    w = await get_work(db, p.work_id)
    if user.role == Role.foreman and w.user_id != user.id: raise HTTPException(403, "Нет доступа")
    path = UPLOAD_DIR / p.object_key
    if not path.exists(): raise HTTPException(404, "Файл отсутствует")
    return FileResponse(path, media_type=p.content_type, filename=p.original_name)

@app.get("/api/summary")
async def summary(db: AsyncSession = Depends(get_db), user: User = Depends(manager_only)):
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    start = datetime.combine(today, time.min, tzinfo=ZoneInfo(settings.timezone)).astimezone(timezone.utc)
    total = await db.scalar(select(func.count(WorkLog.id)).where(WorkLog.created_at >= start))
    active = await db.scalar(select(func.count(WorkLog.id)).where(WorkLog.status == WorkStatus.in_progress))
    users = (await db.execute(select(User).where(User.role == Role.foreman, User.active == True))).scalars().all()
    now = datetime.now(timezone.utc)
    stale = [{"name": u.full_name, "last_login": u.last_login_at.isoformat() if u.last_login_at else None} for u in users if not u.last_login_at or now - u.last_login_at > timedelta(hours=4)]
    return {"works_today": total or 0, "in_progress": active or 0, "foremen": len(users), "stale_foremen": stale}

@app.get("/api/users")
async def list_users(db: AsyncSession = Depends(get_db), user: User = Depends(manager_only)):
    users = (await db.execute(select(User).order_by(User.full_name))).scalars().all()
    return [{"id": u.id, "username": u.username, "name": u.full_name, "role": u.role.value, "last_login": u.last_login_at.isoformat() if u.last_login_at else None} for u in users]

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Production deployment should validate a short-lived JWT passed as a query parameter.
    token = ws.query_params.get("token")
    from jose import jwt, JWTError
    try:
        payload = jwt.decode(token or "", settings.jwt_secret, algorithms=["HS256"])
        async with SessionLocal() as db:
            user = await db.get(User, int(payload["sub"]))
            if not user or user.role != Role.manager: raise ValueError()
    except Exception:
        await ws.close(code=1008); return
    await hub.connect(ws)
    try:
        while True: await ws.receive_text()
    except WebSocketDisconnect: hub.disconnect(ws)

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    # Telegram sends this header when setWebhook is configured with secret_token.
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not settings.telegram_webhook_secret or supplied != settings.telegram_webhook_secret:
        raise HTTPException(401, "Invalid Telegram webhook secret")

    update = await request.json()
    message = update.get("message") or update.get("edited_message")
    callback = update.get("callback_query")
    from_user = (message or {}).get("from") or (callback or {}).get("from") or {}
    telegram_id = str(from_user.get("id") or "")
    if not telegram_id:
        return {"ok": True}

    chat_id = str(((message or {}).get("chat") or {}).get("id") or telegram_id)
    text = ((message or {}).get("text") or "").strip()
    payload = ((callback or {}).get("data") or "").strip()
    if callback:
        try:
            await answer_callback(callback.get("id"), "Принято")
        except Exception:
            pass

    user = (await db.execute(
        select(User).where(User.telegram_user_id == telegram_id, User.active == True)
    )).scalar_one_or_none()

    # Help an administrator bind a Telegram account to a staff record.
    if text in ("/id", "/start") and not user:
        await send_text(chat_id, f"Ваш Telegram ID: {telegram_id}\\nПередайте этот ID руководителю для привязки к учётной записи.")
        return {"ok": True}
    if not user:
        await send_text(chat_id, f"Доступ не настроен. Ваш Telegram ID: {telegram_id}\\nПопросите руководителя добавить вас в СтройКонтроль.")
        return {"ok": True}

    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    if text in ("/start", "/menu") or payload == "menu":
        await main_menu(user)
        return {"ok": True}
    if text == "/id":
        await send_text(chat_id, f"Ваш Telegram ID: {telegram_id}")
        return {"ok": True}

    if payload == "work:new" and user.role in (Role.foreman, Role.manager):
        q = select(Project).where(Project.active == True)
        if user.role != Role.manager:
            allowed_ids = select(UserProject.project_id).where(UserProject.user_id == user.id)
            q = q.where(Project.id.in_(allowed_ids))
        projects = (await db.execute(q.order_by(Project.name))).scalars().all()
        rows = keyboard([[(p.name[:60], f"work:project:{p.id}")] for p in projects])
        await send_text(chat_id, "Выберите объект:", rows or keyboard([[("Меню", "menu")]]))
        return {"ok": True}

    if payload.startswith("work:project:"):
        project_id = int(payload.rsplit(":", 1)[1])
        if user.role != Role.manager:
            allowed = (await db.execute(select(UserProject).where(
                UserProject.user_id == user.id, UserProject.project_id == project_id
            ))).scalar_one_or_none()
            if not allowed:
                await send_text(chat_id, "Нет доступа к этому объекту.")
                return {"ok": True}
        await redis.setex(f"flow:{telegram_id}", 1800, json.dumps({
            "step": "module", "project_id": project_id
        }))
        await send_text(chat_id, "Введите модуль (например, 25.2). Для пропуска отправьте —")
        return {"ok": True}

    if payload == "work:cancel":
        await redis.delete(f"flow:{telegram_id}")
        await send_text(chat_id, "Создание записи отменено.")
        await main_menu(user)
        return {"ok": True}

    if payload == "work:confirm":
        state_raw = await redis.get(f"flow:{telegram_id}")
        if not state_raw:
            await send_text(chat_id, "Черновик истёк. Начните заново: /start")
            return {"ok": True}
        state = json.loads(state_raw)
        module = None
        if state.get("module"):
            module = (await db.execute(select(Module).where(
                Module.project_id == state["project_id"], Module.name == state["module"]
            ))).scalar_one_or_none()
            if not module:
                module = Module(project_id=state["project_id"], name=state["module"])
                db.add(module)
                await db.flush()
        w = WorkLog(
            project_id=state["project_id"], module_id=module.id if module else None,
            user_id=user.id, work_name=state["work_name"],
            quantity=state["quantity"], unit=state["unit"],
            status=WorkStatus.completed, comment=state.get("comment")
        )
        db.add(w)
        await db.flush()
        db.add(AuditLog(work_id=w.id, actor_id=user.id, action="created",
            after_data={"work_name": w.work_name, "quantity": w.quantity,
                        "unit": w.unit, "status": w.status.value}))
        await db.commit()
        w = await get_work(db, w.id)
        await publish({"type": "work.created", "work": work_dict(w)})
        await notify_managers(db, w, w.project, w.module, user)
        await redis.delete(f"flow:{telegram_id}")
        await send_text(chat_id, "✅ Работа сохранена и отправлена руководителю.")
        await main_menu(user)
        return {"ok": True}

    state_raw = await redis.get(f"flow:{telegram_id}")
    if state_raw and text:
        state = json.loads(state_raw)
        step = state["step"]
        if step == "module":
            state["module"] = None if text == "—" else text
            state["step"] = "work"
            prompt = "Введите выполненную работу:"
        elif step == "work":
            state["work_name"] = text[:500]
            state["step"] = "quantity"
            prompt = "Введите объём числом (если объём не нужен — 0):"
        elif step == "quantity":
            try:
                state["quantity"] = float(text.replace(",", "."))
                if state["quantity"] < 0:
                    raise ValueError()
            except ValueError:
                await send_text(chat_id, "Введите неотрицательное число, например 13.")
                return {"ok": True}
            state["step"] = "unit"
            prompt = "Единица: м / шт / м² / комплект / кг / л"
        elif step == "unit":
            if text not in ALLOWED_UNITS:
                await send_text(chat_id, "Выберите единицу: м, шт, м², комплект, кг, л")
                return {"ok": True}
            state["unit"] = text
            state["step"] = "comment"
            prompt = "Комментарий (или —):"
        elif step == "comment":
            state["comment"] = None if text == "—" else text[:4000]
            state["step"] = "confirm"
            project = await db.get(Project, state["project_id"])
            summary = (
                f"Проверьте запись:\\n\\n"
                f"Объект: {project.name if project else '—'}\\n"
                f"Модуль: {state.get('module') or '—'}\\n"
                f"Работа: {state['work_name']}\\n"
                f"Объём: {state['quantity']:g} {state['unit']}\\n"
                f"Комментарий: {state.get('comment') or '—'}"
            )
            await redis.setex(f"flow:{telegram_id}", 1800, json.dumps(state, ensure_ascii=False))
            await send_text(chat_id, summary, keyboard([[("Отправить", "work:confirm"), ("Отмена", "work:cancel")]]))
            return {"ok": True}
        await redis.setex(f"flow:{telegram_id}", 1800, json.dumps(state, ensure_ascii=False))
        await send_text(chat_id, prompt)
        return {"ok": True}

    if payload == "manager:summary" and user.role == Role.manager:
        total = await db.scalar(select(func.count(WorkLog.id)).where(
            WorkLog.created_at >= datetime.now(ZoneInfo(settings.timezone)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        ))
        active = await db.scalar(select(func.count(WorkLog.id)).where(WorkLog.status == WorkStatus.in_progress))
        await send_text(chat_id, f"Сводка стройки\\nРабот сегодня: {total or 0}\\nСейчас в работе: {active or 0}")
        return {"ok": True}

    await send_text(chat_id, "Используйте меню: /start")
    return {"ok": True}
