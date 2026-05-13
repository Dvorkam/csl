import uvicorn
from fastapi import FastAPI

from control_station_lite.server.api import (
    admin,
    audit,
    auth,
    builtin,
    health,
    jobs,
    machines,
    scripts,
)

app = FastAPI(title="control-station-lite")

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(machines.router)
app.include_router(scripts.router)
app.include_router(jobs.router)
app.include_router(builtin.router)
app.include_router(audit.router)
app.include_router(admin.router)


def main() -> None:
    uvicorn.run("control_station_lite.server.main:app", host="127.0.0.1", port=8000, reload=False)
