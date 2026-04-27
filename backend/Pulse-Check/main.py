import asyncio
from fastapi import FastAPI, Depends, HTTPException, status
from sqlalchemy import create_engine, Column, String, Integer, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from pydantic import BaseModel, EmailStr
from datetime import datetime, timezone


# 1. DATABASE SETUP (SQLAlchemy + SQLite)
SQLALCHEMY_DATABASE_URL = "sqlite:///./watchdog.db"

# connect_args is needed only for SQLite to allow multiple threads
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# 2. DATABASE MODEL (SQLite Table)
class Monitor(Base):
    __tablename__ = "monitors"
    
    id = Column(String, primary_key=True, index=True)
    timeout = Column(Integer, nullable=False)
    alert_email = Column(String, nullable=False)
    status = Column(String, default="active") # Status: 'active', 'down', or 'paused'
    last_heartbeat = Column(DateTime, default=lambda: datetime.now(timezone.utc))

# automatically create the database tables when the app starts
Base.metadata.create_all(bind=engine)

# 3. PYDANTIC SCHEMAS (Input Validation)
class MonitorCreate(BaseModel):
    id: str
    timeout: int
    alert_email: EmailStr

# 4. FASTAPI APP & DEPENDENCIES
app = FastAPI(title="CritMon Watchdog API")

# dependency to open and close database sessions for every request
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 5. ENDPOINTS
# User Story 1: Registering a Monitor
@app.post("/monitors", status_code=status.HTTP_201_CREATED)
def create_monitor(monitor: MonitorCreate, db: Session = Depends(get_db)):
    
    # check if the device already exists to prevent duplicates
    existing_monitor = db.query(Monitor).filter(Monitor.id == monitor.id).first()
    if existing_monitor:
        raise HTTPException(status_code=400, detail="Monitor ID already registered.")
    
    # create the new monitor object using the current UTC time as the first heartbeat
    new_monitor = Monitor(
        id=monitor.id,
        timeout=monitor.timeout,
        alert_email=monitor.alert_email,
        status="active",
        last_heartbeat=datetime.now(timezone.utc)
    )
    
    # save it to the SQLite database
    db.add(new_monitor)
    db.commit()
    db.refresh(new_monitor)
    
    return {"message": "Monitor created successfully", "monitor_id": new_monitor.id}

# User Story 2: The Heartbeat (Reset)
@app.post("/monitors/{id}/heartbeat", status_code=status.HTTP_200_OK)
def heartbeat(id: str, db: Session = Depends(get_db)):
    monitor = db.query(Monitor).filter(Monitor.id == id).first()
    
    # if the ID does not exist, return 404 Not Found
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
        
    # restart the countdown by updating the timestamp, and un-pause if it was paused
    monitor.last_heartbeat = datetime.now(timezone.utc)
    monitor.status = "active"
    
    db.commit()
    return {"message": f"Heartbeat received for {id}. Timer reset."}

# Bonus User Story: The "Snooze" Button
@app.post("/monitors/{id}/pause", status_code=status.HTTP_200_OK)
def pause_monitor(id: str, db: Session = Depends(get_db)):
    monitor = db.query(Monitor).filter(Monitor.id == id).first()
    
    if not monitor:
        raise HTTPException(status_code=404, detail="Monitor not found")
        
    # stop the timer completely
    monitor.status = "paused"
    
    db.commit()
    return {"message": f"Monitor {id} is now paused. No alerts will fire."}

# User Story 3: The Alert (Failure State + Background Watchdog Task)
async def watchdog_task():
    """Runs continuously in the background to check for expired timers."""
    while True:
        await asyncio.sleep(1) # check every 1 second
        
        # open a new database session specifically for this background watchdog task
        db = SessionLocal()
        try:
            # only check monitors that are currently "active"
            active_monitors = db.query(Monitor).filter(Monitor.status == "active").all()
            now = datetime.now(timezone.utc)
            
            for monitor in active_monitors:
                # ensure the database timestamp is timezone-aware
                last_beat = monitor.last_heartbeat
                if last_beat.tzinfo is None:
                    last_beat = last_beat.replace(tzinfo=timezone.utc)
                
                # calculate how many seconds have passed since the last heartbeat
                time_elapsed = (now - last_beat).total_seconds()
                
                # if no heartbeat is received before the timer runs out
                if time_elapsed > monitor.timeout:
                    # change status to 'down'
                    monitor.status = "down"
                    db.commit()
                    
                    # log the JSON payload
                    alert_payload = {
                        "ALERT": f"Device {monitor.id} is down!",
                        "time": now.isoformat(),
                        "email_sent_to": monitor.alert_email
                    }
                    print(alert_payload) 
        finally:
            db.close()

# start the background watchdog task automatically when the FastAPI server starts
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(watchdog_task())