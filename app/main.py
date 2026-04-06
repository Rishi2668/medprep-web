import os
from datetime import datetime, timezone
from datetime import date, timedelta

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from .database import Base, engine, get_db
from .models import ExamMark, IncorrectRevision, MarrowSyncState, PracticeSession, User, WeeklyGoal
from .schemas import (
    AuthResponse,
    ExamMarkCreate,
    ExamMarkOut,
    GoogleAuthCreate,
    IncorrectRevisionCreate,
    IncorrectRevisionOut,
    LoginCreate,
    MarrowIngestCreate,
    PracticeCreate,
    PracticeOut,
    SignupCreate,
    WeeklyGoalCreate,
    WeeklyGoalOut,
)

load_dotenv()
Base.metadata.create_all(bind=engine)

app = FastAPI(title="NEET PG Daily Question Tracker API")
INGEST_API_KEY = os.getenv("INGEST_API_KEY", "")
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-this-for-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "10080"))
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def ensure_user_columns():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE practice_sessions ADD COLUMN IF NOT EXISTS user_id INTEGER"))
        conn.execute(text("ALTER TABLE exam_marks ADD COLUMN IF NOT EXISTS user_id INTEGER"))
        conn.execute(text("ALTER TABLE incorrect_revisions ADD COLUMN IF NOT EXISTS user_id INTEGER"))
        conn.execute(text("ALTER TABLE weekly_goals ADD COLUMN IF NOT EXISTS user_id INTEGER"))
        conn.execute(text("ALTER TABLE marrow_sync_state ADD COLUMN IF NOT EXISTS user_id INTEGER"))

origins = [
    "http://localhost:5173",
    "https://medprep-web-frontend.vercel.app",
]
ensure_user_columns()

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def calculate_streaks(active_dates: list[date], reference_end_date: date | None = None) -> tuple[int, int]:
    if not active_dates:
        return 0, 0

    sorted_dates = sorted(set(active_dates))
    max_streak = 1
    current_run = 1

    for idx in range(1, len(sorted_dates)):
        delta = (sorted_dates[idx] - sorted_dates[idx - 1]).days
        if delta == 1:
            current_run += 1
        else:
            current_run = 1
        max_streak = max(max_streak, current_run)

    today = reference_end_date or date.today()
    last_date = sorted_dates[-1]
    if (today - last_date).days > 1:
        return 0, max_streak

    current_streak = 1
    for idx in range(len(sorted_dates) - 1, 0, -1):
        if (sorted_dates[idx] - sorted_dates[idx - 1]).days == 1:
            current_streak += 1
        else:
            break

    return current_streak, max_streak


def filter_practice_query(query, start_date: date | None, end_date: date | None):
    if start_date:
        query = query.filter(PracticeSession.date >= start_date)
    if end_date:
        query = query.filter(PracticeSession.date <= end_date)
    return query


def week_bounds(any_date: date):
    start = any_date - timedelta(days=any_date.weekday())
    end = start + timedelta(days=6)
    return start, end


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    return pwd_context.verify(password, password_hash)


def create_access_token(user_id: int, email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "email": email, "exp": expire}
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def parse_google_token(credential: str) -> dict:
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID is not configured.")
    try:
        return id_token.verify_oauth2_token(credential, google_requests.Request(), GOOGLE_CLIENT_ID)
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid Google credential: {exc}") from exc


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
    )
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id = int(payload.get("sub"))
    except (JWTError, ValueError, TypeError):
        raise credentials_exception

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise credentials_exception
    return user


@app.post("/practice", response_model=PracticeOut)
def add_practice(
    payload: PracticeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    accuracy = (payload.correct_questions / payload.attempted_questions) * 100

    session = PracticeSession(
        user_id=current_user.id,
        date=payload.date,
        attempted_questions=payload.attempted_questions,
        correct_questions=payload.correct_questions,
        accuracy=round(accuracy, 2),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@app.post("/auth/signup", response_model=AuthResponse)
def auth_signup(payload: SignupCreate, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == payload.email.lower().strip()).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered.")

    user = User(
        name=payload.name.strip(),
        email=payload.email.lower().strip(),
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(user.id, user.email)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "name": user.name, "email": user.email},
    }


@app.post("/auth/login", response_model=AuthResponse)
def auth_login(payload: LoginCreate, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email.lower().strip()).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    token = create_access_token(user.id, user.email)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "name": user.name, "email": user.email},
    }


@app.post("/auth/google", response_model=AuthResponse)
def auth_google(payload: GoogleAuthCreate, db: Session = Depends(get_db)):
    token_data = parse_google_token(payload.credential)
    email = str(token_data.get("email", "")).lower().strip()
    name = str(token_data.get("name", "Google User")).strip()
    google_id = str(token_data.get("sub", "")).strip()
    if not email or not google_id:
        raise HTTPException(status_code=400, detail="Google token missing email/sub.")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(name=name, email=email, google_id=google_id)
        db.add(user)
        db.commit()
        db.refresh(user)
    elif not user.google_id:
        user.google_id = google_id
        db.commit()
        db.refresh(user)

    token = create_access_token(user.id, user.email)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "name": user.name, "email": user.email},
    }


@app.get("/auth/me")
def auth_me(current_user: User = Depends(get_current_user)):
    return {"id": current_user.id, "name": current_user.name, "email": current_user.email}


@app.post("/ingest/marrow")
def ingest_marrow_data(
    payload: MarrowIngestCreate,
    x_ingest_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if INGEST_API_KEY and x_ingest_key != INGEST_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid ingest API key.")

    accuracy = round((payload.correct_questions / payload.attempted_questions) * 100, 2)
    sync_state = db.query(MarrowSyncState).filter(MarrowSyncState.date == payload.date).first()

    if sync_state:
        existing_session = (
            db.query(PracticeSession).filter(PracticeSession.id == sync_state.practice_session_id).first()
        )
        if existing_session:
            existing_session.attempted_questions = payload.attempted_questions
            existing_session.correct_questions = payload.correct_questions
            existing_session.accuracy = accuracy
            db.commit()
            return {"status": "updated", "practice_id": existing_session.id, "date": payload.date.isoformat()}

    session = PracticeSession(
        date=payload.date,
        attempted_questions=payload.attempted_questions,
        correct_questions=payload.correct_questions,
        accuracy=accuracy,
    )
    db.add(session)
    db.flush()

    sync_state = MarrowSyncState(date=payload.date, practice_session_id=session.id)
    db.add(sync_state)
    db.commit()
    return {"status": "created", "practice_id": session.id, "date": payload.date.isoformat()}


@app.get("/stats")
def get_stats(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    base_query = filter_practice_query(
        db.query(PracticeSession).filter(PracticeSession.user_id == current_user.id), start_date, end_date
    )

    total_questions = base_query.with_entities(func.coalesce(func.sum(PracticeSession.attempted_questions), 0)).scalar()
    total_correct_questions = base_query.with_entities(func.coalesce(func.sum(PracticeSession.correct_questions), 0)).scalar()
    total_active_days = base_query.with_entities(func.count(func.distinct(PracticeSession.date))).scalar() or 0
    average_accuracy = base_query.with_entities(func.coalesce(func.avg(PracticeSession.accuracy), 0.0)).scalar() or 0.0

    dates = [row[0] for row in base_query.with_entities(PracticeSession.date).all()]
    current_streak, max_streak = calculate_streaks(dates, end_date)
    today = date.today()
    todays_questions = (
        db.query(func.coalesce(func.sum(PracticeSession.attempted_questions), 0))
        .filter(PracticeSession.date == today, PracticeSession.user_id == current_user.id)
        .scalar()
        or 0
    )

    return {
        "total_questions_solved": int(total_questions or 0),
        "total_correct_questions": int(total_correct_questions or 0),
        "total_active_days": int(total_active_days),
        "current_streak": int(current_streak),
        "max_streak": int(max_streak),
        "average_accuracy": round(float(average_accuracy), 2),
        "todays_questions": int(todays_questions),
    }


@app.get("/heatmap")
def get_heatmap(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    today = date.today()
    start_date = today - timedelta(days=364)

    rows = (
        db.query(
            PracticeSession.date.label("date"),
            func.sum(PracticeSession.attempted_questions).label("count"),
        )
        .filter(PracticeSession.date >= start_date, PracticeSession.user_id == current_user.id)
        .group_by(PracticeSession.date)
        .all()
    )
    value_map = {row.date: int(row.count) for row in rows}

    activity = []
    for day_index in range(365):
        d = start_date + timedelta(days=day_index)
        activity.append({"date": d.isoformat(), "count": value_map.get(d, 0)})

    return {"activity": activity}


@app.get("/accuracy-trend")
def get_accuracy_trend(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(
            PracticeSession.date.label("date"),
            (
                (func.sum(PracticeSession.correct_questions) * 100.0)
                / func.nullif(func.sum(PracticeSession.attempted_questions), 0)
            ).label("accuracy"),
        ).filter(PracticeSession.user_id == current_user.id)
    query = filter_practice_query(query, start_date, end_date)
    rows = query.group_by(PracticeSession.date).order_by(PracticeSession.date.asc()).all()

    trend = [{"date": row.date.isoformat(), "accuracy": round(float(row.accuracy or 0), 2)} for row in rows]
    return {"trend": trend}


@app.get("/question-periods")
def get_question_periods(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    today = date.today()
    start_week = today - timedelta(days=6)
    start_month = today - timedelta(days=29)

    today_count = (
        db.query(func.coalesce(func.sum(PracticeSession.attempted_questions), 0))
        .filter(PracticeSession.date == today, PracticeSession.user_id == current_user.id)
        .scalar()
        or 0
    )
    week_count = (
        db.query(func.coalesce(func.sum(PracticeSession.attempted_questions), 0))
        .filter(
            PracticeSession.date >= start_week,
            PracticeSession.date <= today,
            PracticeSession.user_id == current_user.id,
        )
        .scalar()
        or 0
    )
    month_count = (
        db.query(func.coalesce(func.sum(PracticeSession.attempted_questions), 0))
        .filter(
            PracticeSession.date >= start_month,
            PracticeSession.date <= today,
            PracticeSession.user_id == current_user.id,
        )
        .scalar()
        or 0
    )
    return {
        "periods": [
            {"period": "Today", "questions": int(today_count)},
            {"period": "Past 1 Week", "questions": int(week_count)},
            {"period": "Past 1 Month", "questions": int(month_count)},
        ]
    }


@app.get("/question-trend")
def get_question_trend(
    period: str = Query(default="month"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    today = date.today()
    days = 29
    if period == "today":
        days = 0
    elif period == "week":
        days = 6
    start_date = today - timedelta(days=days)

    rows = (
        db.query(
            PracticeSession.date.label("date"),
            func.sum(PracticeSession.attempted_questions).label("questions"),
        )
        .filter(
            PracticeSession.date >= start_date,
            PracticeSession.date <= today,
            PracticeSession.user_id == current_user.id,
        )
        .group_by(PracticeSession.date)
        .order_by(PracticeSession.date.asc())
        .all()
    )
    value_map = {row.date: int(row.questions or 0) for row in rows}
    series = []
    for day_offset in range(days + 1):
        current_date = start_date + timedelta(days=day_offset)
        series.append({"date": current_date.isoformat(), "questions": value_map.get(current_date, 0)})
    return {"trend": series}


@app.get("/progress-trend")
def get_progress_trend(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(
        PracticeSession.date.label("date"),
        func.sum(PracticeSession.attempted_questions).label("questions"),
        (
            (func.sum(PracticeSession.correct_questions) * 100.0)
            / func.nullif(func.sum(PracticeSession.attempted_questions), 0)
        ).label("accuracy"),
    ).filter(PracticeSession.user_id == current_user.id)
    query = filter_practice_query(query, start_date, end_date)
    rows = query.group_by(PracticeSession.date).order_by(PracticeSession.date.asc()).all()

    cumulative = 0
    trend = []
    for row in rows:
        daily_questions = int(row.questions or 0)
        cumulative += daily_questions
        trend.append(
            {
                "date": row.date.isoformat(),
                "daily_questions": daily_questions,
                "cumulative_questions": cumulative,
                "accuracy": round(float(row.accuracy or 0), 2),
            }
        )
    return {"trend": trend}


@app.post("/exam-marks", response_model=ExamMarkOut)
def add_exam_mark(
    payload: ExamMarkCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    percentage = (payload.marks_obtained / payload.total_marks) * 100
    mark = ExamMark(
        user_id=current_user.id,
        date=payload.date,
        marks_obtained=round(payload.marks_obtained, 2),
        total_marks=round(payload.total_marks, 2),
        percentage=round(percentage, 2),
    )
    db.add(mark)
    db.commit()
    db.refresh(mark)
    return mark


@app.get("/exam-marks")
def get_exam_marks(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(ExamMark).filter(ExamMark.user_id == current_user.id).order_by(ExamMark.date.asc())
    if start_date:
        query = query.filter(ExamMark.date >= start_date)
    if end_date:
        query = query.filter(ExamMark.date <= end_date)
    marks = query.all()
    return {
        "marks": [
            {
                "date": row.date.isoformat(),
                "marks_obtained": row.marks_obtained,
                "total_marks": row.total_marks,
                "percentage": row.percentage,
            }
            for row in marks
        ]
    }


@app.post("/incorrect-revisions", response_model=IncorrectRevisionOut)
def add_incorrect_revision(
    payload: IncorrectRevisionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    revision = IncorrectRevision(user_id=current_user.id, date=payload.date, revised_count=payload.revised_count)
    db.add(revision)
    db.commit()
    db.refresh(revision)
    return revision


@app.get("/incorrect-revisions")
def get_incorrect_revisions(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(IncorrectRevision).filter(IncorrectRevision.user_id == current_user.id).order_by(IncorrectRevision.date.asc())
    if start_date:
        query = query.filter(IncorrectRevision.date >= start_date)
    if end_date:
        query = query.filter(IncorrectRevision.date <= end_date)
    rows = query.all()
    return {
        "revisions": [{"date": row.date.isoformat(), "revised_count": row.revised_count} for row in rows]
    }


@app.post("/weekly-goals", response_model=WeeklyGoalOut)
def create_weekly_goal(
    payload: WeeklyGoalCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    goal = WeeklyGoal(
        user_id=current_user.id,
        week_start_date=payload.week_start_date,
        questions_target=payload.questions_target,
        mock_score_target=round(payload.mock_score_target, 2),
    )
    db.add(goal)
    db.commit()
    db.refresh(goal)
    return goal


@app.get("/weekly-goals/current")
def get_current_weekly_goal(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    today = date.today()
    week_start, week_end = week_bounds(today)
    goal = (
        db.query(WeeklyGoal)
        .filter(
            WeeklyGoal.week_start_date >= week_start,
            WeeklyGoal.week_start_date <= week_end,
            WeeklyGoal.user_id == current_user.id,
        )
        .order_by(WeeklyGoal.created_at.desc())
        .first()
    )
    if not goal:
        return {"goal": None}

    questions_done = (
        db.query(func.coalesce(func.sum(PracticeSession.attempted_questions), 0))
        .filter(
            PracticeSession.date >= week_start,
            PracticeSession.date <= week_end,
            PracticeSession.user_id == current_user.id,
        )
        .scalar()
        or 0
    )
    mock_avg = (
        db.query(func.coalesce(func.avg(ExamMark.percentage), 0.0))
        .filter(
            ExamMark.date >= week_start,
            ExamMark.date <= week_end,
            ExamMark.user_id == current_user.id,
        )
        .scalar()
        or 0.0
    )

    q_progress = (questions_done / goal.questions_target) * 100 if goal.questions_target else 0
    m_progress = (mock_avg / goal.mock_score_target) * 100 if goal.mock_score_target else 0

    alerts = []
    if q_progress < 60:
        alerts.append("Questions target is behind pace this week.")
    if m_progress < 80:
        alerts.append("Mock score trend is below your weekly target.")

    return {
        "goal": {
            "week_start_date": goal.week_start_date.isoformat(),
            "week_end_date": week_end.isoformat(),
            "questions_target": goal.questions_target,
            "mock_score_target": goal.mock_score_target,
            "questions_done": int(questions_done),
            "mock_average": round(float(mock_avg), 2),
            "questions_progress": round(float(q_progress), 2),
            "mock_progress": round(float(m_progress), 2),
            "alerts": alerts,
        }
    }


@app.get("/health")
def health():
    return {"ok": True}

